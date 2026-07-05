# Hermes 插件、Hook 与扩展点源码详解

> 适用版本：Hermes Agent `0.18.0 (2026.7.1)`
>
> 源码依据：`hermes_cli/plugins.py`、`hermes_cli/middleware.py`、
> `hermes_cli/hooks.py`，以及本项目的 `plugins/model-telemetry/`。

## 1. 先建立整体认识

Hermes 的扩展机制不是只有“插件”一个概念。它至少包含以下几层：

| 扩展点 | 解决的问题 | 能否改变行为 | 典型用途 |
|---|---|---:|---|
| Plugin | 承载和注册扩展能力 | 取决于注册内容 | 打包 Hook、Middleware、Tool、Skill |
| Hook | 观察生命周期事件 | 部分 Hook 可以 | 统计、审计、会话事件、输出加工 |
| Middleware | 改写请求或包装执行 | 可以 | 裁剪上下文、修改工具参数、权限控制 |
| Tool | 给模型增加可调用能力 | 可以执行外部动作 | 文件、终端、搜索、业务 API |
| Skill | 给模型提供按需加载的操作说明 | 间接影响模型决策 | 面试流程、邮件工作流、领域知识 |
| Memory | 跨会话保存用户或事实信息 | 影响后续上下文 | 长期偏好、稳定背景 |
| Gateway Platform | 接入消息平台 | 决定消息收发 | 微信、Telegram、Discord、Slack |
| Cron/Kanban | 在对话外触发任务 | 可以执行任务 | 每日计划、晚间整理、后台工作 |

最关键的边界：

```text
Plugin 是容器
Hook 用于观察生命周期
Middleware 用于改变运行行为
Tool 用于执行动作
Skill 用于指导模型如何做事
Memory 用于跨会话保存信息
```

如果只是希望模型“更懂流程”，优先写 Skill；如果必须保证请求被裁剪、参数被修改或某个写操作绝对不能发生，应使用 Middleware、Tool 权限或操作系统权限。

## 2. 插件目录结构

目录型插件至少需要两个文件：

```text
model-telemetry/
├── plugin.yaml
└── __init__.py
```

`plugin.yaml` 描述插件：

```yaml
name: model-telemetry
version: 1.3.0
description: "Report model usage and improve interview sessions."
author: codex
hooks:
  - pre_gateway_dispatch
  - pre_api_request
  - post_api_request
  - transform_llm_output
```

`__init__.py` 必须提供 `register(ctx)`：

```python
def register(ctx) -> None:
    ctx.register_hook("pre_api_request", on_pre_api_request)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_middleware("llm_request", shape_llm_request)
```

Hermes 加载模块后不会扫描任意函数，而是明确寻找并调用 `register(ctx)`。因此阅读一个插件时，应先从文件末尾的 `register()` 开始，再顺着回调函数向上看。

## 3. Hermes 从哪里发现插件

`PluginManager.discover_and_load()` 会扫描四类来源：

1. Hermes 自带插件：安装包的 `plugins/`。
2. 用户插件：`~/.hermes/plugins/`。
3. 项目插件：当前目录的 `.hermes/plugins/`。
4. Python 包插件：入口点组 `hermes_agent.plugins`。

项目插件默认关闭，需要：

```bash
export HERMES_ENABLE_PROJECT_PLUGINS=1
```

同名插件冲突时，源码采用“后发现者覆盖前发现者”的 winner 逻辑。当前顺序是 bundled → user → project → Python entry point，因此后面的来源可以覆盖前面的同名 key。

用户自定义的 standalone 插件默认不会自动启用，需要写入配置：

```yaml
plugins:
  enabled:
    - model-telemetry
```

显式禁用优先级最高：

```yaml
plugins:
  disabled:
    - model-telemetry
```

安全排障模式会跳过全部插件发现：

```bash
HERMES_SAFE_MODE=1 hermes chat
```

插件完全没有加载时，先检查 safe mode、插件目录、`plugin.yaml`、`__init__.py` 和 enabled 配置。

## 4. 插件类型

`plugin.yaml` 的 `kind` 支持：

| kind | 含义 |
|---|---|
| `standalone` | 普通插件，注册自己的 Hook、Tool、Middleware |
| `backend` | 某项核心工具的可替换后端，例如 image generation provider |
| `exclusive` | 同一类别只激活一个实现，例如 Memory provider |
| `platform` | Gateway 消息平台适配器 |
| `model-provider` | 模型供应商接入 |

它们并不全部走相同加载路径：

- `exclusive` 交给对应类别自己的 provider 发现机制。
- `model-provider` 交给模型 provider 机制延迟加载。
- Hermes 自带的 `backend` 自动加载。
- Hermes 自带的 `platform` 注册延迟加载器，用到该平台时才导入 SDK。
- 普通用户插件必须显式加入 `plugins.enabled`。

因此“插件列表里看得到”不一定等于“模块已经导入并执行 register”。

## 5. PluginContext 可以注册什么

Hermes 将 `PluginContext` 传给 `register(ctx)`。主要能力包括：

```python
ctx.register_hook(...)
ctx.register_middleware(...)
ctx.register_tool(...)
ctx.register_skill(...)
ctx.register_command(...)
ctx.register_cli_command(...)
ctx.register_auxiliary_task(...)
```

还可以通过 `ctx.llm` 使用 Hermes 托管的模型调用能力。它复用当前 Hermes 的模型和认证，插件不必自行携带另一套 SDK 与 Key；模型覆盖能力默认 fail-closed，需要配置授权。

`ctx.profile_name` 可读取当前 Profile，例如 `default` 或 `weixin2`。插件要区分不同 Profile 的配置或状态目录时，应以该值为依据，不能把路径写死。

### 插件提供的只读 Skill

`ctx.register_skill(name, path, description)` 可以注册插件附带的只读 Skill。它使用限定名：

```text
<plugin-name>:<skill-name>
```

这种 Skill 不会直接进入 `~/.hermes/skills/` 的扁平目录，也不会自动出现在系统提示词的普通 Skill 索引里。它适合让插件携带自己的操作说明，同时避免 Hermes 的 Skill 管理流程直接修改源文件。

这也是未来保护 `interview-practice` 的一个方向：由插件注册只读 Skill，而不是让正式 Skill 长期暴露在可写目录。

## 6. Hook 的执行模型

插件注册 Hook：

```python
ctx.register_hook("post_api_request", callback)
```

核心代码触发 Hook：

```python
invoke_hook("post_api_request", **payload)
```

`PluginManager.invoke_hook()` 会：

1. 找到同名 Hook 的全部 callback。
2. 按注册顺序逐个调用。
3. 给 payload 增加 `telemetry_schema_version`。
4. 收集所有非 `None` 返回值。
5. 单独捕获每个 callback 的异常。

某个插件 Hook 抛异常时，Hermes 记录 warning，继续执行其他插件和主流程。这提供了故障隔离，但也意味着插件失败可能是“静默降级”，不能只观察用户界面判断是否成功。

## 7. Hermes 0.18.0 的 Hook

### 7.1 工具调用

| Hook | 时机 | 常用数据 | 用途 |
|---|---|---|---|
| `pre_tool_call` | 工具执行前 | tool_name、args、session_id | 审计、阻止危险工具 |
| `post_tool_call` | 工具执行后 | result、duration_ms | 统计耗时、记录结果 |
| `transform_tool_result` | 工具结果进入模型前 | 工具结果 | 清洗或压缩工具结果 |
| `transform_terminal_output` | 终端输出返回前 | 终端输出 | 脱敏、截断、格式化 |

需要硬性阻止某个工具时，优先使用 `pre_tool_call` 或工具 Middleware，而不是只在 Skill 中写“禁止”。

### 7.2 LLM 生命周期

| Hook | 时机 | 用途 |
|---|---|---|
| `pre_llm_call` | 一轮模型推理前 | 注入临时召回上下文 |
| `post_llm_call` | 一轮模型推理后 | 记录轮次完成 |
| `pre_api_request` | 每次模型 API 请求发出前 | 统计请求模型、输入量 |
| `post_api_request` | 每次模型 API 成功后 | 统计真实 usage、模型和耗时 |
| `api_request_error` | 模型 API 失败时 | 错误监控、降级统计 |
| `transform_llm_output` | 最终回复返回用户前 | 添加模型信息、格式化输出 |

“一轮 LLM 调用”和“一次 API 请求”不是同一个概念。一轮 Agent 可能因为工具循环调用模型多次，因此：

- `pre_llm_call/post_llm_call` 更接近 Agent 轮次。
- `pre_api_request/post_api_request` 更接近供应商 API 调用。

我们的 Token 插件按 session 累加多次 `post_api_request`，最后在 `transform_llm_output` 一次性显示整轮用量。

`pre_llm_call` 返回的上下文只注入当前 user message，不写入 system prompt，也不持久化到 session DB。这样可以保持 system prompt 前缀稳定，提高 Prompt Cache 命中。

### 7.3 会话生命周期

| Hook | 含义 |
|---|---|
| `on_session_start` | 新会话开始 |
| `on_session_end` | 会话结束 |
| `on_session_finalize` | 会话完成最终落盘 |
| `on_session_reset` | 用户重置会话 |

如果插件使用进程内集合记录“当前正在面试”，必须处理 reset/end，否则容易残留状态。反过来，如果状态只存在内存，Gateway 重启后会丢失，需要从结构化文件或 session metadata 恢复。

### 7.4 Gateway

`pre_gateway_dispatch` 在消息进入 Gateway 后、认证和 Agent dispatch 前触发。主要参数：

```text
event
gateway
session_store
```

支持返回：

```python
{"action": "skip", "reason": "..."}     # 丢弃消息
{"action": "rewrite", "text": "..."}   # 改写消息文本
{"action": "allow"}                     # 正常继续
None                                    # 正常继续
```

我们的插件在这里识别“开始面试”“结束面试”，并设置会话级模型覆盖。

注意：这是 Hermes 内部 API 较多的区域。直接访问：

```python
gateway._session_model_overrides
gateway._session_key_for_source(...)
```

属于依赖私有属性，版本升级时风险较高，应通过集成测试及时发现变化。

### 7.5 输出、验证与子 Agent

| Hook | 用途 |
|---|---|
| `transform_llm_output` | 改写最终用户回复 |
| `pre_verify` | 编码任务停止前要求继续验证 |
| `subagent_start` | 子 Agent 启动 |
| `subagent_stop` | 子 Agent 完成或失败 |

`pre_verify` 可以返回继续指令，阻止 Agent 过早结束，但 Hermes 会通过 `agent.max_verify_nudges` 限制次数，防止无限循环。

### 7.6 审批与 Kanban

审批 Hook：

```text
pre_approval_request
post_approval_response
```

它们是 observer，返回值不会直接批准或拒绝操作。真正要阻止危险工具，应在工具到达审批流程前使用 `pre_tool_call`。

Kanban Hook：

```text
kanban_task_claimed
kanban_task_completed
kanban_task_blocked
```

Kanban worker 可能运行在独立进程中。进程内全局变量不会自动跨 dispatcher 和 worker 共享；需要全局一致状态时，应使用数据库或文件锁，而不是 Python 全局字典。

## 8. Hook 与 Middleware 的本质区别

Hermes 源码直接说明：

```text
Observer hooks report what happened.
Middleware can change what happens.
```

Hook 更像事件监听；Middleware 是执行管道的一部分。

Hermes 0.18.0 支持四类 Middleware：

```text
tool_request
tool_execution
llm_request
llm_execution
```

### 8.1 Request Middleware

LLM request middleware：

```python
def shape_request(request, **context):
    changed = dict(request)
    changed["max_tokens"] = 1024
    return {"request": changed}
```

工具参数 middleware：

```python
def shape_tool(tool_name, args, **context):
    changed = dict(args)
    return {"args": changed}
```

多个 request middleware 按顺序串行执行，前一个返回的新 request/args 会成为后一个的输入。

Hermes 会保存：

- `original_request` / `original_args`
- 当前有效 payload
- middleware trace
- 是否发生改变

### 8.2 Execution Middleware

Execution middleware 接收 `next_call`，形成洋葱式调用链：

```python
def around_tool(next_call, tool_name, args, **context):
    check_permission(tool_name, args)
    result = next_call(args)
    audit(result)
    return result
```

它适合：

- 执行前权限判断
- 超时、重试、熔断
- 结果脱敏
- 统一审计

它比普通 Hook 更强，也更危险。写错 `next_call`、重复调用或吞掉异常，都可能改变核心行为。

## 9. 用 Middleware 保护正式记忆

只在 Skill 中写“不要修改自己”属于软约束。真正保护文件，可以在 `tool_request` middleware 中检查工具名和目标路径：

```python
from pathlib import Path

PROTECTED = {
    Path("/root/.hermes/skills/research/interview-practice/SKILL.md"),
}


def protect_paths(tool_name, args, **_):
    if tool_name not in {"write_file", "edit_file", "terminal", "skill_manage"}:
        return {"args": args}

    # 实际实现必须针对每个工具解析结构化参数。
    # terminal 不能简单做字符串 contains，需要解析允许的命令模型。
    target = extract_target_path(tool_name, args)
    if target in PROTECTED:
        raise PermissionError("正式 Skill 只读，请写入候选区")
    return {"args": args}
```

需要注意：

1. Middleware 抛异常会被插件管理器捕获并记录 warning。是否能阻止执行，要看调用方如何处理 middleware 结果和异常，因此必须做集成测试。
2. 只检查文件名可能被 `..`、符号链接、相对路径绕过，必须 `resolve()` 后比较。
3. `terminal` 可以通过 shell、Python、重定向等多种方式写文件，不能靠几个正则完全封死。
4. Hermes 当前以 root 运行，普通 `chmod 444` 不是可靠安全边界。
5. 真正的强边界应结合只读挂载、独立低权限用户或 `chattr +i`。

比较稳妥的三层设计：

```text
Skill 指令：告诉模型正式文件不可修改
Middleware：识别并拒绝常见写路径，返回友好原因
操作系统：提供不可绕过的最终保护
```

## 10. 我们的 model-telemetry 执行链

入口位于：

```text
plugins/model-telemetry/__init__.py -> register(ctx)
```

注册关系：

```python
ctx.register_hook("pre_gateway_dispatch", route_interview_session)
ctx.register_hook("pre_api_request", on_pre_api_request)
ctx.register_hook("post_api_request", on_post_api_request)
ctx.register_hook("transform_llm_output", transform_output)
ctx.register_middleware("llm_request", cap_interview_gpt_output)
```

完整数据流：

```text
微信消息
  │
  ▼
pre_gateway_dispatch
  ├─ 识别开始/结束面试
  └─ 条件满足时设置 session model override
  │
  ▼
Agent 组织模型请求
  │
  ▼
llm_request middleware
  ├─ 普通会话：原样返回
  ├─ 面试会话：只保留当前题及追问
  └─ GPT：max tokens 限制为 1024
  │
  ▼
pre_api_request
  └─ 保存 provider、model、估算输入 Token
  │
  ▼
模型供应商 API
  │
  ▼
post_api_request
  ├─ 优先读取真实 usage
  └─ 没有 usage 时按字符数估算
  │
  ▼
transform_llm_output
  └─ 在最终回复末尾追加模型和 Token
```

### 为什么使用锁

Gateway 可能同时处理多个会话，插件使用全局字典保存 session usage 和面试状态。所有读写通过 `threading.Lock`，避免并发回调互相覆盖。

### 为什么 Token 状态最后要 pop

`transform_llm_output` 表示本轮回复即将发送。此时：

```python
turn = _usage_by_session.pop(session_id, None)
```

既取出统计，也删除本轮状态，防止下一轮累计到上一轮。

### 当前设计的限制

1. 面试状态主要保存在进程内，Gateway 重启后需要重新触发。
2. 问题识别依赖文本正则，题目格式变化可能漏判。
3. 模型路由访问 Gateway 私有字段，升级 Hermes 时需回归测试。
4. 输出 Token 的字符估算不是精确 tokenizer。
5. 上下文裁剪不会减少系统提示词、Skill 或 Memory 自身的体积。

这解释了为什么之前 66 KB 的面试 Skill 会让新会话也达到约 10 万 Prompt Token：插件只裁剪 conversation messages，无法替代 Skill 治理。

## 11. Skill、Memory 与插件应该如何分工

### 应放入 Skill

- 稳定工作流
- 固定输入输出格式
- 数据源优先级
- 常见但可复用的决策规则

### 应放入 Memory

- 用户长期稳定偏好
- 多个领域都会使用的背景
- 跨会话仍然成立的事实

### 应放入业务仓库

- 题号、得分、完成日期
- 每日任务和顺延
- 技术答案、追问和纠错
- 可审计的结构化状态

### 应写成插件或 Middleware

- 模型与 Token 统计
- 请求裁剪
- 强制权限边界
- 工具参数校验
- 无法依靠模型自觉保证的规则

判断口诀：

```text
希望模型知道：Skill/Memory
希望事实可审计：业务仓库
必须每次执行：Plugin/Middleware
必须绝对禁止：操作系统权限
```

## 12. 调试插件

开启详细插件日志：

```bash
HERMES_PLUGINS_DEBUG=1 hermes plugins list
```

主要检查：

```bash
hermes plugins list
tail -f ~/.hermes/logs/agent.log
tail -f ~/.hermes/logs/gateway.log
tail -f ~/.hermes/logs/errors.log
```

Hook 脚本支持测试和诊断：

```bash
hermes hooks test pre_api_request
hermes hooks test pre_tool_call --for-tool terminal
hermes hooks doctor
```

修改插件后，长生命周期 Gateway 可能仍持有已经加载的 Python module。`discover_plugins(force=True)` 可以在进程内重新扫描，但实际部署中重启对应 Gateway 更直接。

排障顺序：

1. `plugin.yaml` 是否可解析。
2. `__init__.py` 是否存在。
3. 配置中是否 enabled。
4. 是否处于 safe mode。
5. `register()` 是否抛异常。
6. Hook 名和 Middleware kind 是否拼错。
7. 实际调用路径是否触发该扩展点。
8. 日志中是否出现插件 warning。

## 13. 测试策略

插件测试至少分三层：

### 单元测试

直接导入插件，调用 callback：

```bash
python3 plugins/model-telemetry/test_plugin.py
```

适合验证：

- Token 累加
- 会话隔离
- 正则识别
- request 裁剪
- 环境变量开关

### Contract 测试

根据 Hermes 当前版本验证：

- Hook 名仍存在于 `VALID_HOOKS`
- Middleware kind 仍存在于 `VALID_MIDDLEWARE`
- callback 返回结构仍符合调用方约定
- `plugin.yaml` 与 register 内容一致

### 集成测试

启动真实 Gateway，发送测试消息，验证：

- 插件成功加载
- 消息路由未被意外阻断
- 模型覆盖只影响目标会话
- 工具失败不会产生无限循环
- Gateway 重启后的状态符合预期

依赖 `_session_model_overrides` 这类私有字段的功能必须有集成测试。

## 14. 推荐源码阅读顺序

第一轮只理解骨架：

1. `hermes_cli/plugins.py` 顶部说明和 `VALID_HOOKS`
2. `PluginManifest`
3. `PluginContext`
4. `PluginManager.discover_and_load`
5. `_load_plugin`
6. `invoke_hook` / `invoke_middleware`

第二轮理解行为修改：

1. `hermes_cli/middleware.py`
2. `apply_llm_request_middleware`
3. `apply_tool_request_middleware`
4. `_run_execution_chain`
5. 搜索四类 middleware 的真实调用位置

第三轮理解面试插件：

1. 本项目 `register()`
2. `route_interview_session`
3. `_trim_to_current_interview_question`
4. `on_pre_api_request`
5. `on_post_api_request`
6. `transform_output`
7. 对照 `test_plugin.py`

第四轮再做源码改造：

1. Tool 写入保护
2. 只读插件 Skill
3. 候选 Memory
4. Profile 级隔离
5. Gateway 重启后的状态恢复

## 15. 研读 Action

每次只完成一个 Action，先读源码，再写最小实验。

### Action 1：画出插件加载链

找到：

```text
discover_plugins
PluginManager.discover_and_load
_discover_and_load_inner
_load_plugin
register(ctx)
```

回答：为什么插件目录存在但不一定已经启用？

### Action 2：新增一个只记录日志的 Hook

注册 `on_session_start`，只输出 session_id，不修改任何行为。观察日志并确认异常隔离。

### Action 3：对比 Hook 和 Middleware

分别用 `pre_api_request` 和 `llm_request` 读取模型请求。回答：为什么前者适合统计，后者适合裁剪？

### Action 4：实现 Tool 路径审计

先只记录 `tool_request` 中出现的目标路径，不阻止写入。收集真实参数结构后，再设计保护规则。

### Action 5：实现只读 Skill

使用 `ctx.register_skill()` 注册插件附带的面试 Skill，验证限定名、加载方式和普通 Skill 的差异。

### Action 6：设计候选记忆

把“正式长期记忆”和“待审核候选”分开，定义证据、状态和人工合并流程。

### Action 7：做版本升级 Contract 测试

读取 `VALID_HOOKS`、`VALID_MIDDLEWARE`，断言本插件注册的名称全部存在；再检查 Gateway 私有字段是否变化。

完成这些 Action 后，才适合进一步修改 Hermes 核心源码。优先使用公开扩展点；只有扩展点无法表达需求时，才维护 Hermes fork。
