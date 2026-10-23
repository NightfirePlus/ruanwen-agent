import tempfile
import unittest
from pathlib import Path

from agent.models import Brief, ContentBlock, ContentPlan, Draft, Paragraph, Review, Session
from agent.workspace import WorkspaceStore


def confirmed_session():
    return Session(
        brief=Brief(
            product="MateBook 14", audience="大学新生", scene="开学季",
            platform="小红书", goal="种草",
        ),
        draft=Draft(title="开学新装备", body=[Paragraph("正文")]),
        review=Review([]),
    )


class WorkspaceStoreTest(unittest.TestCase):
    def test_only_confirmed_content_is_saved_and_duplicate_confirm_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            store = WorkspaceStore(Path(folder) / "workspace.sqlite3")
            with self.assertRaises(ValueError):
                store.save_confirmed_work(Session())
            session = confirmed_session()
            first = store.save_confirmed_work(session)
            second = store.save_confirmed_work(session)
            self.assertEqual(first, second)
            self.assertEqual(len(store.list_works()), 1)
            self.assertEqual(store.get_work(first)["title"], "开学新装备")

    def test_performance_history_edit_and_user_preferences_persist(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "workspace.sqlite3"
            store = WorkspaceStore(path)
            work_id = store.save_confirmed_work(confirmed_session())
            store.add_performance(work_id, 1200, 32, 5, 8, 3, 1, "发布24小时")
            record = store.performance_for(work_id)[0]
            self.assertEqual(record["impressions"], 1200)
            self.assertEqual(record["orders"], 1)

            updated = store.update_work(
                work_id, title="修改后的标题", product="MateBook 14",
                audience="职场新人", scene="通勤", platform="朋友圈",
                goal="到店", article_markdown="# 修改后的正文",
            )
            self.assertEqual(updated["title"], "修改后的标题")
            self.assertEqual(updated["article_markdown"], "# 修改后的正文")

            store.save_user_preferences(
                display_name="小林", preferred_tone="专业真诚",
                preferred_platforms="朋友圈", audience_focus="职场新人",
                content_preferences="突出到店体验", avoid_topics="未经确认的价格",
                notes="少用感叹号",
            )
            reopened = WorkspaceStore(path)
            preferences = reopened.get_user_preferences()
            self.assertEqual(preferences["display_name"], "小林")
            self.assertEqual(preferences["preferred_tone"], "专业真诚")
            self.assertEqual(preferences["avoid_topics"], "未经确认的价格")

    def test_better_performance_receives_higher_reference_weight(self):
        with tempfile.TemporaryDirectory() as folder:
            store = WorkspaceStore(Path(folder) / "workspace.sqlite3")
            high = confirmed_session()
            high.draft.title = "高表现作品"
            low = confirmed_session()
            low.draft.title = "低表现作品"
            high_id = store.save_confirmed_work(high)
            low_id = store.save_confirmed_work(low)
            store.add_performance(high_id, impressions=1000, likes=100, shares=20, inquiries=15, store_visits=8, orders=5)
            store.add_performance(low_id, impressions=1000, likes=3, shares=0, inquiries=0, store_visits=0, orders=0)

            refs = store.performance_guidance(high.brief)["references"]
            self.assertEqual([item["work_id"] for item in refs[:2]], [high_id, low_id])
            self.assertGreater(refs[0]["reference_weight"], refs[1]["reference_weight"])
            self.assertIn("同平台", refs[0]["match"])

    def test_history_reference_switch_stops_using_archived_works(self):
        with tempfile.TemporaryDirectory() as folder:
            store = WorkspaceStore(Path(folder) / "workspace.sqlite3")
            work = confirmed_session()
            work_id = store.save_confirmed_work(work)
            store.add_performance(work_id, impressions=1000, likes=100)
            self.assertTrue(store.performance_guidance(work.brief)["references"])

            store.save_user_preferences(history_reference=False)
            disabled = store.performance_guidance(work.brief)
            self.assertEqual(disabled["references"], [])
            self.assertEqual(disabled["mode"], "disabled")

            # 只保存其它偏好时，开关不能被意外重置
            store.save_user_preferences(display_name="导购小李")
            self.assertFalse(store.get_user_preferences()["history_reference"])

            store.save_user_preferences(history_reference=True)
            self.assertTrue(store.performance_guidance(work.brief)["references"])

    def test_works_without_real_performance_data_do_not_participate(self):
        with tempfile.TemporaryDirectory() as folder:
            store = WorkspaceStore(Path(folder) / "workspace.sqlite3")
            measured = confirmed_session()
            measured.draft.title = "有宣传数据"
            empty_record = confirmed_session()
            empty_record.draft.title = "只有空数据记录"
            no_record = confirmed_session()
            no_record.draft.title = "没有宣传记录"
            measured_id = store.save_confirmed_work(measured)
            empty_id = store.save_confirmed_work(empty_record)
            no_record_id = store.save_confirmed_work(no_record)
            store.add_performance(measured_id, impressions=100)
            store.add_performance(empty_id, notes="尚未投放")

            references = store.performance_guidance(measured.brief)["references"]
            self.assertEqual([item["work_id"] for item in references], [measured_id])
            self.assertNotIn(empty_id, [item["work_id"] for item in references])
            self.assertNotIn(no_record_id, [item["work_id"] for item in references])

    def test_delete_work_also_deletes_its_performance_records(self):
        with tempfile.TemporaryDirectory() as folder:
            store = WorkspaceStore(Path(folder) / "workspace.sqlite3")
            work_id = store.save_confirmed_work(confirmed_session())
            store.add_performance(work_id, impressions=100, likes=8)

            self.assertEqual(store.delete_work(work_id), work_id)
            self.assertIsNone(store.get_work(work_id))
            self.assertEqual(store.performance_for(work_id), [])
            with self.assertRaises(ValueError):
                store.delete_work(work_id)

    def test_creation_checkpoint_roundtrip_and_resume_normalization(self):
        with tempfile.TemporaryDirectory() as folder:
            store = WorkspaceStore(Path(folder) / "workspace.sqlite3")
            session = confirmed_session()
            session.content_plan = ContentPlan([
                ContentBlock("B1", "body", "卖点", status="reviewing", version=2,
                             draft=Draft(body=[Paragraph("处理中")]))
            ])
            store.save_checkpoint(
                "checkpoint-1", session, "running",
                chat_history=[{"role": "user", "content": "继续"}],
                state={"last": "running", "session": session},
            )
            restored = store.latest_checkpoint()
            self.assertEqual(restored["checkpoint_id"], "checkpoint-1")
            self.assertEqual(restored["session"].draft.title, "开学新装备")
            self.assertEqual(restored["chat_history"][0]["content"], "继续")
            restored["session"].prepare_resume()
            self.assertEqual(restored["session"].content_plan.blocks[0].status, "pending_generate")

    def test_workspace_online_backup_is_consistent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = WorkspaceStore(root / "workspace.sqlite3")
            store.save_confirmed_work(confirmed_session())
            backup = store.backup_to(root / "backups" / "workspace.sqlite3")
            copy = WorkspaceStore(backup)
            self.assertTrue(copy.quick_check())
            self.assertEqual(len(copy.list_works()), 1)


if __name__ == "__main__":
    unittest.main()
