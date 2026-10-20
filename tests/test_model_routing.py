"""分工和请求预算测试；全部模拟响应，不消耗魔粒。"""
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from agent.llm import LLMClient, LLMError, RequestBudget
from agent.model_routing import ModelRouter, STAGES, make_llm, modelscope_router
from agent.models import Session
from agent.pipeline import Pipeline
from helpers import FULL_BRIEF, FakeLLM, FakeStore, good_draft, make_prompts


def response(content='{"ok":true}', finish="stop"):
    return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}, "finish_reason": finish}],
                                  "usage": {"total_tokens": 12}}).encode())


class BudgetTest(unittest.TestCase):
    def test_budget_shared_across_clients_and_counts_http_failure(self):
        budget = RequestBudget(1)
        a = LLMClient(api_key="fake", budget=budget, max_retries=0)
        b = LLMClient(api_key="fake", budget=budget, max_retries=0)
        error = urllib.error.HTTPError("https://example.test", 401, "denied", {}, None)
        with patch("agent.llm.urllib.request.urlopen", side_effect=error) as send:
            with self.assertRaisesRegex(LLMError, "401"):
                a.json("test")
            with self.assertRaisesRegex(LLMError, "请求上限"):
                b.json("test")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(budget.used, 1)
        self.assertFalse(a.requests[0]["success"])

    def test_budget_applies_to_retry_and_json_repair(self):
        budget = RequestBudget(1)
        client = LLMClient(api_key="fake", budget=budget)
        with patch("agent.llm.urllib.request.urlopen", return_value=response("invalid")) as send:
            with self.assertRaisesRegex(LLMError, "请求上限"):
                client.json("test")
        self.assertEqual(send.call_count, 1)
        client = LLMClient(api_key="fake", budget=RequestBudget(1))
        error = urllib.error.HTTPError("https://example.test", 429, "limited", {}, None)
        with patch("agent.llm.urllib.request.urlopen", side_effect=error) as send, patch("agent.llm.time.sleep"):
            with self.assertRaisesRegex(LLMError, "请求上限"):
                client.json("test")
        self.assertEqual(send.call_count, 1)

    def test_profile_does_not_retry_or_repair_json(self):
        with patch.dict(os.environ, {"MODELSCOPE_API_TOKEN": "fake"}):
            client = modelscope_router().for_stage("intake")
        with patch("agent.llm.urllib.request.urlopen", return_value=response("invalid")) as send:
            with self.assertRaisesRegex(LLMError, "合法 JSON"):
                client.json("test")
        self.assertEqual(send.call_count, 1)

    def test_token_cap_thinking_switch_usage_and_truncation(self):
        client = LLMClient(api_key="private-test-key", max_tokens=128, enable_thinking=False, max_retries=0)
        with patch("agent.llm.urllib.request.urlopen", return_value=response()) as send:
            self.assertEqual(client.json("test"), {"ok": True})
        body = json.loads(send.call_args.args[0].data)
        self.assertEqual(body["max_tokens"], 128)
        self.assertIs(body["enable_thinking"], False)
        self.assertEqual(client.requests[0]["usage"]["total_tokens"], 12)
        self.assertNotIn("private-test-key", str(client.requests))
        with patch("agent.llm.urllib.request.urlopen", return_value=response('{"ok":true}', "length")) as send:
            with self.assertRaisesRegex(LLMError, "截断"):
                client.json("test")
        self.assertEqual(send.call_count, 1)

    def test_empty_or_missing_content_does_not_pass(self):
        for content in (None, "", []):
            client = LLMClient(api_key="fake", max_retries=0)
            with patch("agent.llm.urllib.request.urlopen", return_value=response(content)):
                with self.assertRaises(LLMError):
                    client.json("test")


class RoutingTest(unittest.TestCase):
    def test_deployment_defaults_to_routing_without_local_env(self):
        with patch.dict(os.environ, {"MODELSCOPE_API_TOKEN": "fake"}, clear=True):
            self.assertIsInstance(make_llm(), ModelRouter)

    def test_smoke_plan_renders_all_prompts_without_network(self):
        from tools.smoke_models import cases
        with patch("agent.llm.urllib.request.urlopen", side_effect=AssertionError("禁止联网")):
            tasks = cases()
        self.assertEqual([t[0] for t in tasks], ["intake", "evidence", "compose", "fact_review"])
        self.assertTrue(all("{{" not in user for _, user, _, _ in tasks))
        check = tasks[0][3]
        self.assertTrue(check({**FULL_BRIEF, "product": "华为 MateBook 14"}))
        self.assertFalse(check({**FULL_BRIEF, "product": "MateBook 14s"}))

    def test_default_stays_single_and_explicit_profile_uses_own_endpoint(self):
        with patch.dict(os.environ, {"LLM_API_KEY": "fake", "LLM_PROFILE": "single"}):
            self.assertIsInstance(make_llm(), LLMClient)
        with patch.dict(os.environ, {"LLM_API_KEY": "fake", "LLM_BASE_URL": "https://example.test/v1",
                                     "LLM_PROFILE": "modelscope-v1"}):
            router = make_llm()
            self.assertEqual(set(router.stages), STAGES)
            self.assertTrue(all(c._base_url == "https://api-inference.modelscope.cn/v1"
                                for c in router.clients.values()))
            self.assertIs(router.for_stage("compose"), router.for_stage("rewrite"))
            self.assertIsNot(router.for_stage("compose"), router.for_stage("fact_review"))

    def test_incomplete_routes_fail_closed(self):
        with self.assertRaises(LLMError):
            ModelRouter({"one": FakeLLM()}, {"compose": "one"})

    def test_real_pipeline_dispatches_rewrites_and_feedback(self):
        understand = FakeLLM(FULL_BRIEF, intent={"action": "confirm"})
        analyze = FakeLLM()
        bad = good_draft()
        bad["body"][0]["text"] = "续航长达20小时"
        writer = FakeLLM(drafts=[bad, good_draft()])
        stages = {s: ("understand" if s in ("intake", "revise_intent") else
                      "write" if s in ("compose", "rewrite") else "analyze") for s in STAGES}
        router = ModelRouter({"understand": understand, "write": writer, "analyze": analyze}, stages)
        pipe = Pipeline(router, FakeStore(), make_prompts())
        session = Session()
        out = pipe.talk(session, "go")
        self.assertEqual(out.status, "review", out.reason)
        self.assertEqual(understand.calls, ["intake"])
        self.assertEqual(writer.calls, ["compose", "rewrite"])
        self.assertEqual(analyze.calls, ["evidence", "fact_review", "compliance_review", "fact_review", "compliance_review"])
        self.assertEqual(pipe.feedback(session, "确认").status, "done")
        self.assertEqual(understand.calls, ["intake", "revise_intent"])


if __name__ == "__main__":
    unittest.main()
