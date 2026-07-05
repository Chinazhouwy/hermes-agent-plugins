# Hermes Agent Plugins

个人维护的 Hermes Agent 插件集合。

## 代码在哪里

仓库目前只有一个插件，核心代码在：

```text
plugins/model-telemetry/
├── __init__.py       # 插件核心代码，主要看这个文件
├── plugin.yaml       # 插件元数据，以及告诉 Hermes 要监听哪些 Hook
└── test_plugin.py    # 自动化测试，可当作使用示例看

scripts/install.sh    # 将插件复制到 Hermes 配置目录
skills/interview-practice/SKILL.md
                      # 精简、可版本管理的面试练习 Skill
```

推荐阅读顺序：

1. 先看本 README，了解插件做什么。
2. 看 `plugin.yaml`，了解 Hermes 会在哪些时机调用插件。
3. 看 `__init__.py` 最下面的 `register()`，它是插件入口。
4. 根据 `register()` 注册的函数，分别阅读 Token 统计、面试路由和上下文裁剪。
5. 看 `test_plugin.py`，了解每项功能输入什么、预期输出什么。

## model-telemetry

当前版本：`1.3.0`

功能：

- 在每次回复末尾显示实际模型和本轮 Token 用量。
- 支持按会话切换模拟面试模型。
- 模拟面试时按题目隔离上下文：保留当前题的回答和追问，不向模型重复发送前面题目的对话。
- 结束模拟面试后恢复普通会话策略。

上下文隔离只影响发送给模型的请求，不删除 Hermes 保存的原始聊天、面试进度或整理记录。

## 执行流程

每轮消息大致经过下面几个阶段：

```text
微信消息进入 Hermes
  │
  ├─ pre_gateway_dispatch
  │    识别是否开始/结束面试，并按需设置会话模型
  │
  ├─ llm_request middleware
  │    面试会话只保留当前题上下文；GPT 输出上限设为 1024 Token
  │
  ├─ pre_api_request / post_api_request
  │    记录实际或估算的输入、输出 Token
  │
  └─ transform_llm_output
       在最终回复末尾追加模型和 Token 信息
```

插件的三个功能彼此独立：

- **Token 统计**：所有会话都会使用。
- **面试模型路由**：只有明确开启环境变量、代理可用且存在 API Key 时才启用。
- **按题隔离上下文**：被识别为面试会话后启用，和当前使用 MiMo 还是 GPT 无关。

## 测试

```bash
python3 plugins/model-telemetry/test_plugin.py
```

## 安装

默认安装到当前 Hermes 配置目录：

```bash
./scripts/install.sh
```

安装到指定 Profile：

```bash
HERMES_HOME="$HOME/.hermes/profiles/weixin2" ./scripts/install.sh
```

然后在对应的 `config.yaml` 中启用：

```yaml
plugins:
  enabled:
    - model-telemetry
```

重启 Hermes Gateway 后生效。

## 可选配置

允许面试会话切换到 GPT：

```bash
export HERMES_INTERVIEW_GPT_ROUTING=1
export OPENROUTER_API_KEY="你的 OpenRouter API Key"
```

当前代码还会检查本机 `127.0.0.1:17897` 代理端口。任一条件不满足时，插件不会切换模型，Hermes 继续使用默认模型。

## 安全说明

仓库不包含 API Key、代理节点、聊天记录、Cron 数据或服务器配置。模型路由所需凭据应通过 Hermes 凭据池或环境变量提供。
