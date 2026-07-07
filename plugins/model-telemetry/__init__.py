"""Hermes 模型信息、Token 统计和面试会话增强插件。

阅读入口在文件末尾的 register()。Hermes 会通过那里注册的 Hook，在请求前后
调用本文件中的函数。代码按三个功能分区：

1. 统计每轮请求的模型和 Token；
2. 识别面试会话，并在条件满足时切换模型；
3. 面试时只把当前题及其追问发送给模型。
"""

from __future__ import annotations

import math
import os
import re
import socket
import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnUsage:
    """一次 Hermes 回复可能包含多次模型调用，这里累计整轮用量。"""

    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    pending_inputs: list[int] = field(default_factory=list)


# session_id -> 本轮累计用量。回复发出后会删除，避免统计到下一轮。
_usage_by_session: dict[str, TurnUsage] = {}

# Gateway 使用 session_key 设置模型覆盖，LLM 中间件使用 session_id 裁剪上下文，
# 因此需要同时记住两种标识。
_interview_session_keys: set[str] = set()
_interview_session_ids: set[str] = set()

# Hermes 可能并发处理多个微信会话，共享集合和字典必须加锁。
_lock = threading.Lock()

# 用户说“开始模拟面试”“继续面试”等话时，进入面试状态。
_INTERVIEW_START = re.compile(
    r"(开始|继续|进入|恢复).{0,4}(模拟)?面试|"
    r"(模拟)?面试.{0,4}(开始|继续)|"
    r"整理.{0,8}面试"
)
_INTERVIEW_END = re.compile(
    r"(结束|停止|退出|暂停).{0,4}(模拟)?面试|"
    r"(模拟)?面试.{0,4}(结束|停止)"
)

# 只有显式开启路由且代理、凭据都可用时，才会切换到这个模型。
_INTERVIEW_MODEL = "openai/gpt-5.4-mini"

# 用于从完整聊天记录中定位“最新一道题”的开头。
_QUESTION_MARKER = re.compile(
    r"(?im)(?:^|\n)\s*(?:#{1,6}\s*)?"
    r"(?:第\s*\d+\s*题\b|"
    r"(?:题目|问题)\s*(?:#?\s*\d+)?\s*[：:]|"
    r"面试题\s*(?:#?\s*\d+)?\s*[：:]|"
    r"#\s*\d+\b)"
)


# ---------------------------------------------------------------------------
# 一、模型与 Token 统计
# ---------------------------------------------------------------------------

def reset_state() -> None:
    """清空插件内存状态，主要供自动化测试和插件重载使用。"""

    with _lock:
        _usage_by_session.clear()
        _interview_session_keys.clear()
        _interview_session_ids.clear()


def _state(session_id: str) -> TurnUsage:
    return _usage_by_session.setdefault(session_id or "unknown", TurnUsage())


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def on_pre_api_request(
    session_id: str = "",
    provider: str = "",
    model: str = "",
    approx_input_tokens: int = 0,
    **_: Any,
) -> None:
    """请求发出前保存估算输入量，供不返回 usage 的模型兜底。"""

    with _lock:
        usage = _state(session_id)
        usage.provider = provider or usage.provider
        usage.model = model or usage.model
        usage.pending_inputs.append(_integer(approx_input_tokens))


def on_post_api_request(
    session_id: str = "",
    provider: str = "",
    model: str = "",
    response_model: str = "",
    usage: Any = None,
    assistant_content_chars: int = 0,
    **_: Any,
) -> None:
    """模型返回后优先累计真实 usage，没有真实值时才使用估算值。"""

    raw = usage if isinstance(usage, dict) else {}
    real_input = _integer(raw.get("input_tokens") or raw.get("prompt_tokens"))
    real_output = _integer(raw.get("output_tokens") or raw.get("completion_tokens"))

    with _lock:
        turn = _state(session_id)
        turn.provider = provider or turn.provider
        turn.model = response_model or model or turn.model
        # pre/post Hook 可能在一轮内调用多次，按进入顺序消费估算值。
        estimated_input = turn.pending_inputs.pop(0) if turn.pending_inputs else 0
        turn.input_tokens += real_input or estimated_input
        # 某些供应商不返回输出 Token，中文/英文混合文本粗略按 4 字符一个 Token。
        turn.output_tokens += real_output or math.ceil(
            _integer(assistant_content_chars) / 4
        )


def transform_output(
    response_text: str,
    session_id: str = "",
    model: str = "",
    **_: Any,
) -> str:
    """在用户最终看到的回复末尾追加实际模型和本轮 Token 用量。"""

    with _lock:
        # pop 表示一轮回复到这里已经结束，下一轮重新统计。
        turn = _usage_by_session.pop(session_id or "unknown", None)

    provider = turn.provider if turn else ""
    effective_model = (turn.model if turn else "") or model or "unknown"
    label = f"{provider} / {effective_model}" if provider else effective_model
    if turn and (turn.input_tokens or turn.output_tokens):
        total = turn.input_tokens + turn.output_tokens
        token_text = (
            f"输入约 {turn.input_tokens}，输出约 {turn.output_tokens}，合计约 {total}"
        )
    else:
        token_text = "本轮未返回用量"

    return (
        response_text.rstrip()
        + "\n\n---\n"
        + f"模型：{label}｜Token：{token_text}"
    )


# ---------------------------------------------------------------------------
# 二、面试会话识别与可选的 GPT 路由
# ---------------------------------------------------------------------------

def _openrouter_api_key() -> str:
    """先从 Hermes 凭据池取 Key，再回退到环境变量。"""

    try:
        from hermes_cli.auth import read_credential_pool

        for credentials in read_credential_pool("openrouter"):
            if not isinstance(credentials, dict):
                continue
            key = credentials.get("api_key") or credentials.get("access_token")
            if isinstance(key, str) and key.strip():
                return key.strip()
    except Exception:
        pass
    return os.getenv("OPENROUTER_API_KEY", "").strip()


def _proxy_available() -> bool:
    """快速检查远程机本地代理端口是否可连接。"""

    try:
        with socket.create_connection(("127.0.0.1", 17897), timeout=0.3):
            return True
    except OSError:
        return False


def _clear_session_model_override(gateway: Any, session_key: str) -> None:
    """清除会话级模型覆盖，优先使用公开 API，兜底兼容旧 Hermes 私有字段。"""

    clear = getattr(gateway, "clear_session_model_override", None)
    if callable(clear):
        clear(session_key)
        return
    lock = getattr(gateway, "_session_model_overrides_lock", None)
    overrides = getattr(gateway, "_session_model_overrides", None)
    if not isinstance(overrides, dict):
        return
    if lock is not None and hasattr(lock, "__enter__"):
        with lock:
            overrides.pop(session_key, None)
        return
    overrides.pop(session_key, None)


def _set_session_model_override(
    gateway: Any, session_key: str, override: dict[str, Any]
) -> None:
    """设置会话级模型覆盖，优先使用公开 API，兜底兼容旧 Hermes 私有字段。"""

    setter = getattr(gateway, "set_session_model_override", None)
    if callable(setter):
        setter(session_key, override)
        return
    lock = getattr(gateway, "_session_model_overrides_lock", None)
    overrides = getattr(gateway, "_session_model_overrides", None)
    if not isinstance(overrides, dict):
        return
    if lock is not None and hasattr(lock, "__enter__"):
        with lock:
            overrides[session_key] = override
        return
    overrides[session_key] = override


def _remember_interview_session(
    session_key: str, source: Any, session_store: Any = None
) -> None:
    """记录面试会话的两种 ID，供模型路由和上下文裁剪分别使用。"""

    session_id = ""
    if session_store is not None:
        try:
            entry = session_store.get_or_create_session(source)
            session_id = str(getattr(entry, "session_id", "") or "")
        except Exception:
            pass
    with _lock:
        _interview_session_keys.add(session_key)
        if session_id:
            _interview_session_ids.add(session_id)


def route_interview_session(
    event: Any = None,
    gateway: Any = None,
    session_store: Any = None,
    **_: Any,
):
    """根据用户消息进入或退出面试状态，并按条件覆盖当前会话模型。

    这个函数失败时始终返回 None，让 Hermes 继续使用默认模型。这样即使代理、
    OpenRouter 或插件自身出现问题，也不会阻断普通面试对话。
    """

    if event is None or gateway is None:
        return None
    text = str(getattr(event, "text", "") or "").strip()
    source = getattr(event, "source", None)
    if not text or source is None:
        return None

    try:
        normalized = gateway._normalized_override_source(source)
        session_key = gateway._session_key_for_source(normalized)
    except Exception:
        return None
    if not session_key:
        return None

    if _INTERVIEW_END.search(text):
        # 删除会话级模型覆盖后，Hermes 会自然恢复配置里的默认模型。
        _clear_session_model_override(gateway, session_key)
        with _lock:
            _interview_session_keys.discard(session_key)
            if session_store is not None:
                try:
                    entry = session_store.get_or_create_session(source)
                    _interview_session_ids.discard(
                        str(getattr(entry, "session_id", "") or "")
                    )
                except Exception:
                    pass
        return None

    is_start = bool(_INTERVIEW_START.search(text))
    with _lock:
        is_active = session_key in _interview_session_keys
    if is_start or is_active:
        _remember_interview_session(session_key, source, session_store)

    if not is_start:
        return None

    # GPT 路由默认关闭。三个条件必须同时满足：开关、代理、API Key。
    if os.getenv("HERMES_INTERVIEW_GPT_ROUTING", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return None
    if not _proxy_available():
        return None

    api_key = _openrouter_api_key()
    if not api_key:
        return None
    _set_session_model_override(
        gateway,
        session_key,
        {
            "model": _INTERVIEW_MODEL,
            "provider": "openrouter",
            "api_key": api_key,
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
        },
    )
    return None


# ---------------------------------------------------------------------------
# 三、面试请求整形：按题隔离上下文，并限制 GPT 输出
# ---------------------------------------------------------------------------

def cap_interview_gpt_output(
    request: dict[str, Any],
    provider: str = "",
    model: str = "",
    session_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    """在模型请求发出前裁剪面试历史，并限制 GPT 单次输出成本。"""

    shaped = _trim_to_current_interview_question(request, session_id)
    if model.startswith("openai/gpt-"):
        capped = dict(shaped)
        token_key = (
            "max_completion_tokens"
            if "max_completion_tokens" in capped
            else "max_tokens"
        )
        capped[token_key] = min(_integer(capped.get(token_key)) or 1024, 1024)
        return {"request": capped}
    return {"request": shaped}


def _message_text(message: Any) -> str:
    """兼容字符串和 OpenAI content parts 两种消息格式。"""

    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "input_text"}
        )
    return ""


def _trim_to_current_interview_question(
    request: dict[str, Any], session_id: str
) -> dict[str, Any]:
    """只保留系统规则、最新题目，以及这道题之后的回答和追问。

    原始聊天记录不会被修改或删除；这里只复制并整形即将发送给模型的 request。
    """

    with _lock:
        is_interview = session_id in _interview_session_ids
    if not is_interview:
        return request

    message_key = "messages" if isinstance(request.get("messages"), list) else "input"
    messages = request.get(message_key)
    if not isinstance(messages, list):
        return request

    # 不断覆盖索引，循环结束后得到最后一个题目标记，也就是当前题。
    question_index = -1
    for index, message in enumerate(messages):
        if (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and _QUESTION_MARKER.search(_message_text(message))
        ):
            question_index = index
    if question_index < 0:
        return request

    # 早期历史中只保留 system/developer 规则，之前题目的对话全部省略。
    prefix = [
        message
        for message in messages[:question_index]
        if isinstance(message, dict)
        and message.get("role") in {"system", "developer"}
    ]
    trimmed = prefix + messages[question_index:]
    if len(trimmed) == len(messages):
        return request

    shaped = dict(request)
    shaped[message_key] = trimmed
    return shaped


# ---------------------------------------------------------------------------
# 四、插件入口
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Hermes 加载插件时调用：把上面的功能挂到对应生命周期。"""

    ctx.register_hook("pre_gateway_dispatch", route_interview_session)
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("transform_llm_output", transform_output)
    ctx.register_middleware("llm_request", cap_interview_gpt_output)
