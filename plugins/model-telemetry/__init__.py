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
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    pending_inputs: list[int] = field(default_factory=list)


_usage_by_session: dict[str, TurnUsage] = {}
_interview_session_keys: set[str] = set()
_interview_session_ids: set[str] = set()
_lock = threading.Lock()
_INTERVIEW_START = re.compile(
    r"(开始|继续|进入|恢复).{0,4}(模拟)?面试|"
    r"(模拟)?面试.{0,4}(开始|继续)|"
    r"整理.{0,8}面试"
)
_INTERVIEW_END = re.compile(
    r"(结束|停止|退出|暂停).{0,4}(模拟)?面试|"
    r"(模拟)?面试.{0,4}(结束|停止)"
)
_INTERVIEW_MODEL = "openai/gpt-5.4-mini"
_QUESTION_MARKER = re.compile(
    r"(?im)(?:^|\n)\s*(?:#{1,6}\s*)?"
    r"(?:第\s*\d+\s*题\b|"
    r"(?:题目|问题)\s*(?:#?\s*\d+)?\s*[：:]|"
    r"面试题\s*(?:#?\s*\d+)?\s*[：:]|"
    r"#\s*\d+\b)"
)


def reset_state() -> None:
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
    raw = usage if isinstance(usage, dict) else {}
    real_input = _integer(raw.get("input_tokens") or raw.get("prompt_tokens"))
    real_output = _integer(raw.get("output_tokens") or raw.get("completion_tokens"))

    with _lock:
        turn = _state(session_id)
        turn.provider = provider or turn.provider
        turn.model = response_model or model or turn.model
        estimated_input = turn.pending_inputs.pop(0) if turn.pending_inputs else 0
        turn.input_tokens += real_input or estimated_input
        turn.output_tokens += real_output or math.ceil(
            _integer(assistant_content_chars) / 4
        )


def transform_output(
    response_text: str,
    session_id: str = "",
    model: str = "",
    **_: Any,
) -> str:
    with _lock:
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


def _openrouter_api_key() -> str:
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
    try:
        with socket.create_connection(("127.0.0.1", 17897), timeout=0.3):
            return True
    except OSError:
        return False


def _remember_interview_session(
    session_key: str, source: Any, session_store: Any = None
) -> None:
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
        gateway._session_model_overrides.pop(session_key, None)
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
    gateway._session_model_overrides[session_key] = {
        "model": _INTERVIEW_MODEL,
        "provider": "openrouter",
        "api_key": api_key,
        "base_url": "https://openrouter.ai/api/v1",
        "api_mode": "chat_completions",
    }
    return None


def cap_interview_gpt_output(
    request: dict[str, Any],
    provider: str = "",
    model: str = "",
    session_id: str = "",
    **_: Any,
) -> dict[str, Any]:
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
    with _lock:
        is_interview = session_id in _interview_session_ids
    if not is_interview:
        return request

    message_key = "messages" if isinstance(request.get("messages"), list) else "input"
    messages = request.get(message_key)
    if not isinstance(messages, list):
        return request

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


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", route_interview_session)
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("transform_llm_output", transform_output)
    ctx.register_middleware("llm_request", cap_interview_gpt_output)
