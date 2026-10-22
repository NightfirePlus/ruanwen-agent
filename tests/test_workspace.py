import tempfile
import unittest
from pathlib import Path

from agent.models import Brief, Draft, Paragraph, Review, Session
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


if __name__ == "__main__":
    unittest.main()
