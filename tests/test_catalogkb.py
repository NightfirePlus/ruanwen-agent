"""新库、文字资料首次使用缓存、登记图片和失败处理的离线测试。"""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from agent.catalogkb import CatalogStore, ReviewCache, INDEX, sha, original_excerpt
from agent.knowledge import KnowledgeError
from agent.models import Brief
from agent.prompts import Prompts


class Analysis:
    model = "fake-analysis"
    def __init__(self):
        self.calls = 0
        self.bad = False
    def json(self, user, **kwargs):
        self.calls += 1
        facts = [{"text": "编造的事实" if self.bad else "内存16GB。"}] if '"kind": "product_facts"' in user else []
        return dict(status="passed", facts=facts, guidance=[] if facts else ["学生更关心学习。"], excluded=[], reason="测试")


class Vision:
    model = "fake-vision"
    def __init__(self, asset="product"):
        self.calls = 0
        self.asset = asset
        self.inputs = []
    def json(self, user, **kwargs):
        self.calls += 1
        self.inputs.append(user)
        return dict(status="passed", asset_type=self.asset, description="完整产品展示", visible_text="", topics=["外观"], restrictions=[], reason="测试")


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / INDEX).mkdir()
        self.records = []
        def record(id, path, text, kind, category, **extra):
            p = self.root / path
            p.write_text(text, encoding="utf-8")
            digest = sha(p.read_bytes())
            row = dict(id=id, path=path, model_key="MateBook 14", official_product="MateBook 14",
                       kind=kind, category=category, sha256=digest,
                       source=dict(path=path, sha256=digest, locator_type="local"), **extra)
            self.records.append(row)
        record("D1", "产品事实.md", "# MateBook 14\n内存16GB。", "product_facts", "products")
        record("D2", "学生沟通卡.md", "学生更关心学习。", "role_guidance", "marketing")
        for n in range(2):
            name = f"image{n}.png"
            Image.new("RGBA", (12, 12), (20 + n, 30, 40, 255)).save(self.root / name)
            digest = sha((self.root / name).read_bytes())
            self.records.append(dict(id=f"I{n}", path=name, model_key="MateBook 14", official_product="MateBook 14",
                category="images", kind="registered_image", sha256=digest,
                source=dict(path=name, sha256=digest, locator_type="local",
                            index_row={"官网图片说明": "产品外观", "建议用途": "产品展示"})))
        self.write_catalog()
        self.store = CatalogStore(self.root)
        self.analysis, self.vision = Analysis(), Vision()
        self.brief = Brief(product="MateBook 14", audience="大学新生", scene="开学季", platform="朋友圈", goal="种草")
        self.prompts = Prompts.load()

    def tearDown(self):
        self.temp.cleanup()

    def write_catalog(self):
        (self.root / INDEX / "catalog.json").write_text(json.dumps(dict(version=1,
            models=[dict(model_key="MateBook 14", official_product="MateBook 14")], records=self.records), ensure_ascii=False), encoding="utf-8")

    def collect(self, prompts=None):
        return self.store.collect(self.analysis, prompts or self.prompts, self.brief)

    def test_first_use_reviews_then_persistent_cache_reuses_without_calls(self):
        before = {r["path"]: (self.root / r["path"]).read_bytes() for r in self.records}
        first = self.collect()
        self.assertEqual((self.analysis.calls, self.vision.calls), (2, 0))
        self.assertEqual(first.review_stats["new"], 2)
        self.assertEqual(len(first.facts), 1)
        self.assertEqual(len(first.context), 1)
        self.assertEqual(len(first.images), 2)
        self.store = CatalogStore(self.root)  # 模拟应用重启
        second = self.collect()
        self.assertEqual((self.analysis.calls, self.vision.calls), (2, 0))
        self.assertEqual(second.review_stats["cached"], 2)
        self.assertEqual(before, {p: (self.root / p).read_bytes() for p in before})

    def test_registered_images_are_available_without_visual_model_call(self):
        ev = self.collect()
        self.assertEqual(len(ev.images), 2)
        self.assertTrue(all(not image.review for image in ev.images))
        self.assertEqual(self.vision.calls, 0)

    def test_changed_caption_updates_registered_image_without_model_call(self):
        self.collect()
        self.records[-1]["source"]["index_row"]["官网图片说明"] = "改为接口细节说明"
        self.write_catalog()
        ev = self.collect()
        self.assertEqual((self.analysis.calls, self.vision.calls), (2, 0))
        self.assertEqual(ev.review_stats["new"], 0)
        self.assertEqual(ev.images[-1].caption, "改为接口细节说明")

    def test_catalog_caption_fills_missing_legacy_index_caption(self):
        self.records[-1]["source"]["index_row"]["官网图片说明"] = ""
        self.records[-1]["source"]["index_row"]["建议用途"] = ""
        self.records[-1]["caption"] = "人工复核后的产品外观"
        self.records[-1]["usage"] = "产品介绍"
        self.write_catalog()
        images = self.store.images("MateBook 14")
        self.assertEqual(images[-1].caption, "人工复核后的产品外观")
        self.assertEqual(images[-1].usage, "产品介绍")

    def test_changed_file_requires_updated_source_metadata_then_rechecks(self):
        self.collect()
        path = self.root / self.records[-1]["path"]
        Image.new("RGB", (14, 14), (10, 10, 10)).save(path)
        with self.assertRaises(KnowledgeError):
            self.collect()
        self.records[-1]["sha256"] = sha(path.read_bytes())
        self.write_catalog()
        ev = self.collect()
        self.assertEqual(len(ev.images), 2)
        self.assertEqual(self.vision.calls, 0)

    def test_changed_prompt_or_model_invalidates_corresponding_cache(self):
        self.collect()
        texts = dict(self.prompts._t)
        texts["material_review"] += "\n增加新的审阅规则。"
        self.collect(Prompts(texts))
        self.assertEqual(self.vision.calls, 0)
        self.assertEqual(self.analysis.calls, 4)

    def test_cached_session_checks_file_and_catalog_versions(self):
        ev = self.collect()
        self.assertTrue(self.store.still_valid(ev))
        self.records[-1]["source"]["index_row"]["建议用途"] = "仅作为细节图"
        self.write_catalog()
        self.assertFalse(self.store.still_valid(ev))

    def test_whitespace_only_differences_are_recovered_as_exact_original(self):
        self.assertEqual(original_excerpt("长度 300 mm", "长度\n300 mm\n厚度"), "长度\n300 mm")
        self.assertIsNone(original_excerpt("长度301mm", "长度\n300 mm"))

    def test_suspicious_dimension_section_is_withheld_from_the_auditor(self):
        record = self.records[0]
        text = "# MateBook 14\n内存16GB。\n## 机身尺寸\n长度\n15.9 mm\n厚度\n227.79 mm\n## 屏幕\n屏幕信息。"
        (self.root / record["path"]).write_text(text, encoding="utf-8")
        record["sha256"] = sha((self.root / record["path"]).read_bytes())
        self.write_catalog()
        ev = self.collect()
        self.assertTrue(ev.dropped)
        self.assertEqual([f.text for f in ev.facts], ["内存16GB。"])
        self.assertIn("15.9", ev.originals[0]["text"])
        self.assertTrue(ev.originals[0]["exclusions"])

    def test_pipeline_runs_all_roles_with_new_catalog_and_reuses_reviews(self):
        from agent.model_routing import ModelRouter, STAGES
        from agent.pipeline import Pipeline
        from agent.models import Session
        from helpers import FULL_BRIEF
        class Fixed:
            model = "offline-stage"
            def __init__(self, result):
                self.result, self.inputs = result, []
            def json(self, user, **kwargs):
                self.inputs.append(user)
                return copy.deepcopy(self.result)
        draft = dict(title="开学了解新装备", intro="", body=[dict(text="内存16GB。", fact_ids=["F1"])],
                     cta="欢迎到店了解。", tags=[], images=[dict(image_id="I0", after_paragraph=1, caption="产品外观")], refusals=[])
        clients = {s: Fixed({"issues": []}) for s in STAGES}
        clients.update(intake=Fixed(FULL_BRIEF), evidence=self.analysis,
                       compose=Fixed(draft), rewrite=Fixed(draft), revise_intent=Fixed({"action": "confirm"}))
        router = ModelRouter(clients, {s: s for s in STAGES})
        pipe = Pipeline(router, self.store, self.prompts)
        out = pipe.talk(Session(), "给新生写软文")
        self.assertEqual(out.status, "review", out.reason)
        self.assertIn("产品外观", clients["compose"].inputs[0])
        self.assertIn("未执行视觉模型审图", clients["compose"].inputs[0])
        self.assertIn("内存16GB", clients["fact_review"].inputs[0])
        self.assertEqual(pipe.feedback(out.session, "确认").status, "done")
        pipe.talk(Session(), "再写一篇")
        self.assertEqual((self.analysis.calls, self.vision.calls), (2, 0))

    def test_new_store_cannot_downgrade_necessary_reviews(self):
        from agent.pipeline import Pipeline
        pipe = Pipeline(self.analysis, self.store, self.prompts, review_mode="relaxed")
        self.assertEqual(pipe.review_mode, "strict")

    def test_standalone_validation_needs_no_original_library(self):
        from tools.validate_knowledge_base import inspect_library
        result = inspect_library(self.root)
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["records"], 4)

    def test_standalone_validation_detects_missing_payload(self):
        from tools.validate_knowledge_base import inspect_library
        (self.root / self.records[-1]["path"]).unlink()
        self.assertFalse(inspect_library(self.root)["passed"])

    def test_standalone_validation_detects_links_outside_current_library(self):
        from tools.validate_knowledge_base import inspect_library
        path = self.root / self.records[0]["path"]
        path.write_text("[外部旧库](../removed-library/facts.md)", encoding="utf-8")
        self.records[0]["sha256"] = sha(path.read_bytes())
        self.write_catalog()
        result = inspect_library(self.root)
        self.assertFalse(result["passed"])
        self.assertTrue(any("链接" in error for error in result["errors"]))

    def test_standalone_validation_accepts_explicit_historical_source(self):
        from tools.validate_knowledge_base import inspect_library
        self.records[0]["source"] = dict(path="P1-产品参数库/已删除.md", sha256="0" * 64,
                                         locator_type="historical")
        self.write_catalog()
        result = inspect_library(self.root)
        self.assertTrue(result["passed"], result["errors"])

    def test_standalone_validation_rejects_unmarked_missing_source(self):
        from tools.validate_knowledge_base import inspect_library
        self.records[0]["source"] = dict(path="P1-产品参数库/已删除.md", sha256="0" * 64)
        self.write_catalog()
        result = inspect_library(self.root)
        self.assertFalse(result["passed"])
        self.assertTrue(any("historical" in error for error in result["errors"]))

    def test_visual_model_result_does_not_control_registered_images(self):
        self.vision.asset = "composition_required"
        ev = self.collect()
        self.assertEqual(len(ev.images), 2)
        self.assertEqual(ev.review_stats["excluded"], 0)
        self.collect()
        self.assertEqual(self.vision.calls, 0)

    def test_hallucinated_quote_never_becomes_passed_or_retried_automatically(self):
        self.analysis.bad = True
        with self.assertRaises(KnowledgeError):
            self.collect()
        with self.assertRaises(KnowledgeError):
            self.collect()
        self.assertEqual(self.analysis.calls, 1)
        db = ReviewCache(self.root / INDEX / "usage-review.sqlite3")
        with db.db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM reviews").fetchall(), [("failed",)])

    def test_quota_exhaustion_does_not_cache_an_unattempted_item_as_failure(self):
        with patch.dict("os.environ", {"KB_REVIEW_MAX_CALLS": "1"}):
            with self.assertRaises(KnowledgeError):
                self.collect()
        self.assertEqual(self.analysis.calls, 1)
        self.collect()
        self.assertEqual(self.analysis.calls, 2)

    def test_discovery_and_historical_records_are_not_product_facts(self):
        for kind, cat in (("product_discovery", "products"), ("historical_price_snapshot", "dynamic")):
            row = copy.deepcopy(self.records[1])
            row.update(id=kind, kind=kind, category=cat, model_key=None)
            self.records.append(row)
        self.write_catalog()
        ev = self.collect()
        self.assertEqual(len(ev.facts), 1)
        self.assertEqual(len(ev.context), 1)
        self.assertEqual(len(ev.images), 2)

    def test_competitor_and_business_rules_require_matching_intent(self):
        competitor = copy.deepcopy(self.records[1])
        competitor.update(id="C1", path="竞品对比.md", kind="competitor_method", category="competitor", model_key=None)
        (self.root / competitor["path"]).write_text("竞品对比应统一口径。", encoding="utf-8")
        competitor["sha256"] = sha((self.root / competitor["path"]).read_bytes())
        business = copy.deepcopy(self.records[1])
        business.update(id="B1", path="价格规则.md", kind="business_rule", category="dynamic", model_key=None)
        (self.root / business["path"]).write_text("价格需记录核验时间。", encoding="utf-8")
        business["sha256"] = sha((self.root / business["path"]).read_bytes())
        self.records.extend((competitor, business))
        self.write_catalog()
        self.assertNotIn("C1", {r["id"] for r in self.store.references(self.brief, 10)})
        self.assertNotIn("B1", {r["id"] for r in self.store.references(self.brief, 10)})
        comparing = copy.deepcopy(self.brief)
        comparing.main_problem = "与竞品对比"
        self.assertIn("C1", {r["id"] for r in self.store.references(comparing, 10)})
        commercial = copy.deepcopy(self.brief)
        commercial.main_problem = "价格优惠"
        self.assertIn("B1", {r["id"] for r in self.store.references(commercial, 10)})


class ConcurrentCacheTest(unittest.TestCase):
    def test_concurrent_same_material_only_one_computation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = ReviewCache(Path(tmp) / "cache.db")
            started, finish = threading.Event(), threading.Event()
            calls = []
            def compute():
                calls.append(1)
                started.set()
                finish.wait(3)
                return {"status": "passed"}
            worker = threading.Thread(target=lambda: cache.obtain("key", compute))
            worker.start()
            self.assertTrue(started.wait(3))
            try:
                with self.assertRaises(KnowledgeError):
                    cache.obtain("key", compute)
            finally:
                finish.set()
                worker.join(3)
            self.assertEqual(calls, [1])
            self.assertEqual(cache.obtain("key", compute), ({"status": "passed"}, True))
