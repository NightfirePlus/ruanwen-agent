import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from helpers import FULL_BRIEF, FakeLLM, FakeStore, make_prompts
from agent.models import Brief, Draft, Evidence, Session
from agent.pipeline import Pipeline
from agent.workspace import WorkspaceStore


class WorkbenchChatTest(unittest.TestCase):
    def test_progress_survives_inside_final_chat_and_snapshots_are_independent(self):
        import app

        pipe = Pipeline(FakeLLM(FULL_BRIEF), FakeStore(), make_prompts(), review_mode="relaxed")
        with patch.object(app, "get_pipeline", return_value=pipe), patch.object(pipe, "save"):
            updates = list(app.user_send_stream("go", [], None))
        self.assertTrue(all(len(update) == 8 for update in updates))
        self.assertIn("正在开始处理", updates[0][1][-1]["content"])
        # 计时刷新只更新顶部计时条，聊天消息走 gr.skip()，因此只检查真正携带消息的更新。
        self.assertTrue(any(
            "正在匹配知识库" in update[1][-1]["content"]
            for update in updates[1:-1]
            if isinstance(update[1], list)
        ))
        final = updates[-1]
        self.assertEqual(final[2]["last"], "review")
        self.assertIn("<details>", final[1][-1]["content"])
        self.assertIn("总用时", final[1][-1]["content"])
        self.assertIn("progress-duration", final[1][-1]["content"])
        self.assertIn("2 条可用事实", final[1][-1]["content"])
        self.assertIn(final[2]["session"].draft.title, final[1][-1]["content"])

    def test_progress_summary_and_expanded_rows_show_elapsed_time(self):
        import app

        events = [
            {"title": "命令一", "detail": "读取资料", "started": 0.5, "duration": 1.25},
            {"title": "命令二", "detail": "模型核验", "started": 1.75, "duration": None},
        ]
        running = app.render_progress(events, True, 4.0)
        self.assertNotIn("当前用时", running)
        self.assertIn("命令一", running)
        self.assertIn("用时 1.2 秒", running)
        self.assertIn("用时 2.2 秒", running)
        complete = app.render_progress(events, False, 5.0)
        self.assertIn("总用时 5.0 秒", complete)
        self.assertIn("当前用时 4.0 秒", app._elapsed_html(4.0, True))
        self.assertIn("总用时 5.0 秒", app._elapsed_html(5.0, False))

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
        result = ("", [], state, "", "", "", "", "")
        with patch.object(app, "user_send_stream", return_value=iter([result])) as send:
            updates = list(app.composer_send("换成公众号", *settings, [], state))
        self.assertEqual(send.call_args.args[0], "换成公众号")
        self.assertEqual(updates[-1][2]["composer_settings"], settings)

    def test_product_dropdown_change_uses_deterministic_change_product_intent(self):
        import app

        previous = ["MateBook 14", "大学新生", "开学季", "朋友圈", "种草"]
        state = {
            "session": Session(draft=Draft(title="旧稿")),
            "last": "review",
            "composer_settings": previous,
        }
        result = ("", [], state, "", "", "", "", "")
        with patch.object(app, "user_send_stream", return_value=iter([result])) as send:
            list(app.composer_send("", "MateBook X Pro", *previous[1:], [], state))
        self.assertEqual(send.call_args.kwargs["forced_intent"]["action"], "change_product")
        self.assertEqual(send.call_args.kwargs["forced_intent"]["value"], "MateBook X Pro")

    def test_error_is_presented_in_same_assistant_message(self):
        import app
        from agent.llm import LLMError

        with patch.object(app, "get_pipeline", side_effect=LLMError("测试连接错误")):
            updates = list(app.user_send_stream("go", [], None))
        self.assertEqual(len(updates[-1][1]), 2)
        self.assertIn("测试连接错误", updates[-1][1][-1]["content"])
        self.assertEqual(updates[-1][2]["last"], "error")

    def test_interrupted_checkpoint_can_be_restored_without_replaying_finished_work(self):
        import app

        with tempfile.TemporaryDirectory() as folder:
            workspace = WorkspaceStore(Path(folder) / "workspace.sqlite3")
            session = Session(brief=Brief(product="MateBook 14"))
            workspace.save_checkpoint(
                "resume-1", session, "error",
                chat_history=[{"role": "user", "content": "继续之前的创作"}],
                state={"last": "error"}, error="进程中断",
            )
            pipe = Pipeline(FakeLLM(FULL_BRIEF), FakeStore(), make_prompts(), review_mode="relaxed")
            with patch.object(app, "get_workspace", return_value=workspace), \
                    patch.object(app, "get_pipeline", return_value=pipe), \
                    patch.object(pipe, "save"):
                updates = list(app.resume_latest_stream([], None))
            self.assertEqual(updates[-1][2]["checkpoint_id"], "resume-1")
            self.assertEqual(updates[-1][2]["last"], "need_more_info")
            self.assertIn("检查点已恢复", updates[-1][1][-1]["content"])

    def test_account_panel_owns_scrolling_instead_of_nested_tabs(self):
        import app

        self.assertIn("overflow-y: auto !important", app.CUSTOM_CSS)
        self.assertIn("#account-history-content, #preferences-content", app.CUSTOM_CSS)
        self.assertIn("height: auto !important", app.CUSTOM_CSS)

    def test_history_work_can_be_deleted_from_account_panel(self):
        import app

        with tempfile.TemporaryDirectory() as folder:
            workspace = WorkspaceStore(Path(folder) / "workspace.sqlite3")
            session = Session(
                brief=Brief(
                    product="MateBook 14", audience="大学新生", scene="开学季",
                    platform="小红书", goal="种草",
                ),
                draft=Draft(title="待删除作品"),
            )
            from agent.models import Review
            session.review = Review([])
            work_id = workspace.save_confirmed_work(session)
            workspace.add_performance(work_id, impressions=100)

            with patch.object(app, "get_workspace", return_value=workspace):
                result = app.delete_history_work(str(work_id), {"work_id": work_id})

            self.assertIn("已删除", result[0])
            self.assertIsNone(workspace.get_work(work_id))
            self.assertEqual(workspace.performance_for(work_id), [])
            self.assertNotIn("work_id", result[-1])
