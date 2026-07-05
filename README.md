# Hermes Agent Plugins

个人维护的 Hermes Agent 插件集合。

## model-telemetry

当前版本：`1.3.0`

功能：

- 在每次回复末尾显示实际模型和本轮 Token 用量。
- 支持按会话切换模拟面试模型。
- 模拟面试时按题目隔离上下文：保留当前题的回答和追问，不向模型重复发送前面题目的对话。
- 结束模拟面试后恢复普通会话策略。

上下文隔离只影响发送给模型的请求，不删除 Hermes 保存的原始聊天、面试进度或整理记录。

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

## 安全说明

仓库不包含 API Key、代理节点、聊天记录、Cron 数据或服务器配置。模型路由所需凭据应通过 Hermes 凭据池或环境变量提供。
