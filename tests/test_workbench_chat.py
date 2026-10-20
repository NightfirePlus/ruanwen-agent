import unittest
from unittest.mock import patch

from helpers import FULL_BRIEF, FakeLLM, FakeStore, make_prompts
from agent.pipeline import Pipeline


class WorkbenchChatTest(unittest.TestCase):
    def test_progress_survives_inside_final_chat_and_snapshots_are_independent(self):
        import app

        pipe = Pipeline(FakeLLM(FULL_BRIEF), FakeStore(), make_prompts(), review_mode="relaxed")
        with patch.object(app, "get_pipeline", return_value=pipe), patch.object(pipe, "save"):
            updates = list(app.user_send_stream("go", [], None))
        self.assertTrue(all(len(update) == 7 for update in updates))
        self.assertIn("正在开始处理", updates[0][1][-1]["content"])
        self.assertTrue(any("正在匹配知识库" in update[1][-1]["content"] for update in updates[1:-1]))
        final = updates[-1]
        self.assertEqual(final[2]["last"], "review")
        self.assertIn("<details>", final[1][-1]["content"])
        self.assertIn("2 条可用事实", final[1][-1]["content"])
        self.assertIn(final[2]["session"].draft.title, final[1][-1]["content"])

    def test_free_text_and_selected_settings_use_one_request(self):
        import app

        request = app.compose_request("标题短一些", "MateBook 14", "大学新生", "开学季", "小红书", "种草")
        self.assertTrue(request.startswith("标题短一些"))
        for field in ("产品：MateBook 14", "受众：大学新生", "场景：开学季", "平台：小红书", "目标：种草"):
            self.assertIn(field, request)
        self.assertEqual(app.compose_request("只改标题", None, None, None, "", None), "只改标题")

    def test_settings_can_start_a_request_without_entering_text(self):
        import app

        request = app.compose_request("", "MateBook 14", None, None, "朋友圈", None)
        self.assertIn("请按以下设置", request)
        self.assertIn("平台：朋友圈", request)

    def test_unchanged_settings_do_not_override_followup_instructions(self):
        import app

        settings = ["MateBook 14", "大学新生", "开学季", "朋友圈", "种草"]
        state = {"session": None, "last": "review", "composer_settings": settings}
        result = ("", [], state, "", "", "", "")
        with patch.object(app, "user_send_stream", return_value=iter([result])) as send:
            updates = list(app.composer_send("换成公众号", *settings, [], state))
        self.assertEqual(send.call_args.args[0], "换成公众号")
        self.assertEqual(updates[-1][2]["composer_settings"], settings)

    def test_error_is_presented_in_same_assistant_message(self):
        import app
        from agent.llm import LLMError

        with patch.object(app, "get_pipeline", side_effect=LLMError("测试连接错误")):
            updates = list(app.user_send_stream("go", [], None))
        self.assertEqual(len(updates[-1][1]), 2)
        self.assertIn("测试连接错误", updates[-1][1][-1]["content"])
        self.assertEqual(updates[-1][2]["last"], "error")
