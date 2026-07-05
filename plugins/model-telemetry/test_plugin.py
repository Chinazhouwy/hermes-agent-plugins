from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace


PLUGIN = Path(__file__).with_name("__init__.py")


def load_plugin():
    spec = importlib.util.spec_from_file_location("model_telemetry", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.reset_state()
    return module


class ModelTelemetryTest(unittest.TestCase):
    def test_sums_real_usage_across_api_calls(self):
        plugin = load_plugin()
        plugin.on_post_api_request(
            session_id="s1",
            provider="openrouter",
            model="openai/gpt-5.5",
            usage={"input_tokens": 100, "output_tokens": 20},
        )
        plugin.on_post_api_request(
            session_id="s1",
            provider="openrouter",
            model="openai/gpt-5.5",
            usage={"prompt_tokens": 50, "completion_tokens": 10},
        )

        result = plugin.transform_output(
            response_text="回答", session_id="s1", model="openai/gpt-5.5"
        )

        self.assertIn("模型：openrouter / openai/gpt-5.5", result)
        self.assertIn("Token：输入约 150，输出约 30，合计约 180", result)

    def test_uses_estimate_when_provider_omits_usage(self):
        plugin = load_plugin()
        plugin.on_pre_api_request(
            session_id="s1",
            provider="custom:minio",
            model="mimo-v2.5",
            approx_input_tokens=240,
        )
        plugin.on_post_api_request(
            session_id="s1",
            provider="custom:minio",
            model="mimo-v2.5",
            usage={},
            assistant_content_chars=80,
        )

        result = plugin.transform_output(
            response_text="回答", session_id="s1", model="mimo-v2.5"
        )

        self.assertIn("Token：输入约 240，输出约 20，合计约 260", result)

    def test_sessions_do_not_share_usage(self):
        plugin = load_plugin()
        plugin.on_post_api_request(
            session_id="s1",
            provider="openrouter",
            model="openai/gpt-5.5",
            usage={"input_tokens": 100, "output_tokens": 20},
        )
        plugin.on_post_api_request(
            session_id="s2",
            provider="custom:minio",
            model="mimo-v2.5",
            usage={"input_tokens": 30, "output_tokens": 5},
        )

        first = plugin.transform_output("一", "s1", "openai/gpt-5.5")
        second = plugin.transform_output("二", "s2", "mimo-v2.5")

        self.assertIn("合计约 120", first)
        self.assertIn("合计约 35", second)

    def test_transform_consumes_turn_usage_once(self):
        plugin = load_plugin()
        plugin.on_post_api_request(
            session_id="s1",
            provider="openrouter",
            model="openai/gpt-5.5",
            usage={"input_tokens": 10, "output_tokens": 2},
        )
        plugin.transform_output("第一次", "s1", "openai/gpt-5.5")

        second = plugin.transform_output("第二次", "s1", "openai/gpt-5.5")

        self.assertIn("Token：本轮未返回用量", second)
        self.assertEqual(1, second.count("模型："))

    def test_interview_start_sets_session_gpt_override(self):
        plugin = load_plugin()
        gateway = FakeGateway()
        event = fake_event("开始模拟面试，先问我一道 Java 题")
        with (
            mock.patch.dict(
                os.environ,
                {
                    "HERMES_INTERVIEW_GPT_ROUTING": "1",
                },
            ),
            mock.patch.object(plugin, "_proxy_available", return_value=True),
            mock.patch.object(
                plugin, "_openrouter_api_key", return_value="secret-key"
            ),
        ):
            plugin.route_interview_session(event=event, gateway=gateway)

        override = gateway._session_model_overrides["weixin:chat-1"]
        self.assertEqual("openrouter", override["provider"])
        self.assertEqual("openai/gpt-5.4-mini", override["model"])
        self.assertEqual("secret-key", override["api_key"])

    def test_interview_followup_keeps_existing_override(self):
        plugin = load_plugin()
        gateway = FakeGateway()
        gateway._session_model_overrides["weixin:chat-1"] = {"model": "openai/gpt-5.4-mini"}

        plugin.route_interview_session(
            event=fake_event("为什么这里是 Next-Key Lock？"), gateway=gateway
        )

        self.assertEqual(
            "openai/gpt-5.4-mini",
            gateway._session_model_overrides["weixin:chat-1"]["model"],
        )

    def test_interview_end_restores_default_model(self):
        plugin = load_plugin()
        gateway = FakeGateway()
        gateway._session_model_overrides["weixin:chat-1"] = {"model": "openai/gpt-5.4-mini"}

        plugin.route_interview_session(
            event=fake_event("结束模拟面试"), gateway=gateway
        )

        self.assertNotIn("weixin:chat-1", gateway._session_model_overrides)

    def test_interview_routing_is_disabled_until_gpt_is_reachable(self):
        plugin = load_plugin()
        gateway = FakeGateway()
        with mock.patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "secret-key",
                "HERMES_INTERVIEW_GPT_ROUTING": "0",
            },
        ):
            plugin.route_interview_session(
                event=fake_event("开始模拟面试"), gateway=gateway
            )

        self.assertEqual({}, gateway._session_model_overrides)

    def test_interview_stays_on_default_when_proxy_is_down(self):
        plugin = load_plugin()
        gateway = FakeGateway()
        with (
            mock.patch.dict(
                os.environ,
                {
                    "OPENROUTER_API_KEY": "secret-key",
                    "HERMES_INTERVIEW_GPT_ROUTING": "1",
                },
            ),
            mock.patch.object(plugin, "_proxy_available", return_value=False),
        ):
            plugin.route_interview_session(
                event=fake_event("开始模拟面试"), gateway=gateway
            )

        self.assertEqual({}, gateway._session_model_overrides)

    def test_gpt_request_is_capped_for_openrouter_balance(self):
        plugin = load_plugin()
        request = {"messages": [], "max_completion_tokens": 65536}

        result = plugin.cap_interview_gpt_output(
            request=request,
            provider="openrouter",
            model="openai/gpt-5.4-mini",
        )

        self.assertEqual(1024, result["request"]["max_completion_tokens"])

    def test_mimo_request_is_not_changed(self):
        plugin = load_plugin()
        request = {"messages": [], "max_tokens": 65536}

        result = plugin.cap_interview_gpt_output(
            request=request,
            provider="custom:minio",
            model="mimo-v2.5",
        )

        self.assertIs(request, result["request"])
        self.assertEqual(65536, result["request"]["max_tokens"])

    def test_interview_request_keeps_only_current_question_history(self):
        plugin = load_plugin()
        gateway = FakeGateway()
        store = FakeSessionStore()
        plugin.route_interview_session(
            event=fake_event("开始模拟面试"),
            gateway=gateway,
            session_store=store,
        )
        request = {
            "messages": [
                {"role": "system", "content": "面试规则"},
                {"role": "assistant", "content": "第 1 题：什么是 JVM？"},
                {"role": "user", "content": "Java 虚拟机"},
                {"role": "assistant", "content": "第 2 题：解释 G1。"},
                {"role": "user", "content": "它是垃圾收集器"},
            ],
            "max_tokens": 65536,
        }

        result = plugin.cap_interview_gpt_output(
            request=request,
            provider="custom:minio",
            model="mimo-v2.5",
            session_id="session-1",
        )

        self.assertEqual(
            [
                {"role": "system", "content": "面试规则"},
                {"role": "assistant", "content": "第 2 题：解释 G1。"},
                {"role": "user", "content": "它是垃圾收集器"},
            ],
            result["request"]["messages"],
        )

    def test_interview_followups_stay_in_current_question(self):
        plugin = load_plugin()
        plugin._interview_session_ids.add("session-1")
        messages = [
            {"role": "system", "content": "面试规则"},
            {"role": "assistant", "content": "题目 #8：解释 MVCC。"},
            {"role": "user", "content": "通过版本链"},
            {"role": "assistant", "content": "那 ReadView 有什么作用？"},
            {"role": "user", "content": "判断可见性"},
        ]

        result = plugin.cap_interview_gpt_output(
            request={"messages": messages},
            provider="custom:minio",
            model="mimo-v2.5",
            session_id="session-1",
        )

        self.assertEqual(messages, result["request"]["messages"])

    def test_regular_chat_is_never_trimmed(self):
        plugin = load_plugin()
        messages = [
            {"role": "system", "content": "规则"},
            {"role": "assistant", "content": "第 1 题：随便聊聊？"},
            {"role": "user", "content": "好"},
        ]

        result = plugin.cap_interview_gpt_output(
            request={"messages": messages},
            provider="custom:minio",
            model="mimo-v2.5",
            session_id="regular-session",
        )

        self.assertIs(messages, result["request"]["messages"])

    def test_ending_interview_disables_context_trimming(self):
        plugin = load_plugin()
        gateway = FakeGateway()
        store = FakeSessionStore()
        plugin.route_interview_session(
            event=fake_event("开始模拟面试"),
            gateway=gateway,
            session_store=store,
        )
        plugin.route_interview_session(
            event=fake_event("结束模拟面试"),
            gateway=gateway,
            session_store=store,
        )

        self.assertNotIn("session-1", plugin._interview_session_ids)


class FakeGateway:
    def __init__(self):
        self._session_model_overrides = {}

    def _normalized_override_source(self, source):
        return source

    def _session_key_for_source(self, source):
        return f"{source.platform}:{source.chat_id}"


class FakeSessionStore:
    def get_or_create_session(self, source):
        return SimpleNamespace(session_id="session-1")


def fake_event(text: str):
    source = SimpleNamespace(platform="weixin", chat_id="chat-1")
    return SimpleNamespace(text=text, source=source)


if __name__ == "__main__":
    unittest.main()
