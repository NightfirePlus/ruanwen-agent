"""新库、文字资料首次使用缓存、登记图片和失败处理的离线测试。"""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from agent.catalogkb import CatalogStore, INDEX, sha, original_excerpt
from agent.knowledge import KnowledgeError
from agent.models import Brief
from agent.prompts import Prompts


class Analysis:
    model = "fake-analysis"
    def __init__(self):
        self.calls = 0
        self.bad = False
        self.status = "passed"
    def json(self, user, **kwargs):
        self.calls += 1
        excerpt = "屏幕14.2英寸。" if "屏幕14.2英寸" in user else "内存16GB。"
        facts = [{"text": "编造的事实" if self.bad else excerpt}] if '"kind": "product_facts"' in user else []
        return dict(status=self.status, facts=facts, guidance=[] if facts else ["学生更关心学习。"], excluded=[], reason="测试")


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

    def collect(self):
        return self.store.collect(self.brief)

    def test_registered_images_are_available_without_visual_model_call(self):
        ev = self.collect()
        self.assertEqual(len(ev.images), 2)
        self.assertTrue(all(not image.review for image in ev.images))
        self.assertEqual(self.vision.calls, 0)

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

    def test_cached_session_checks_file_and_catalog_versions(self):
        ev = self.collect()
        self.assertTrue(self.store.still_valid(ev))
        self.records[-1]["source"]["index_row"]["建议用途"] = "仅作为细节图"
        self.write_catalog()
        self.assertFalse(self.store.still_valid(ev))

    def test_whitespace_only_differences_are_recovered_as_exact_original(self):
        self.assertEqual(original_excerpt("长度 300 mm", "长度\n300 mm\n厚度"), "长度\n300 mm")
        self.assertIsNone(original_excerpt("长度301mm", "长度\n300 mm"))

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
        class Sequence(Fixed):
            def __init__(self, results):
                super().__init__(None)
                self.results = results
            def json(self, user, **kwargs):
                self.inputs.append(user)
                return copy.deepcopy(self.results[min(len(self.inputs) - 1, len(self.results) - 1)])
        draft = dict(title="开学了解新装备", intro="从学习场景了解新装备。", body=[dict(text="内存16GB。", fact_ids=["F1"])],
                     cta="欢迎到店了解。", tags=[], images=[dict(image_id="I0", after_paragraph=1, caption="产品外观")], refusals=[])
        empty = dict(title="", intro="", body=[], cta="", tags=[], images=[], refusals=[])
        opening = {**empty, "title": draft["title"], "intro": draft["intro"]}
        body = {**empty, "body": draft["body"], "images": draft["images"]}
        closing = {**empty, "cta": draft["cta"], "tags": draft["tags"]}
        plan = {"blocks": [
            {"id": "B1", "kind": "opening", "objective": "开篇", "fact_ids": [], "depends_on": []},
            {"id": "B2", "kind": "body", "objective": "卖点", "fact_ids": ["F1"], "depends_on": []},
            {"id": "B3", "kind": "closing", "objective": "收束", "fact_ids": [], "depends_on": ["B1", "B2"]},
        ]}
        clients = {s: Fixed({"issues": []}) for s in STAGES}
        clients.update(intake=Fixed(FULL_BRIEF), evidence=self.analysis,
                       compose=Fixed(draft), rewrite=Fixed(draft), content_plan=Fixed(plan),
                       chunk_generate=Sequence([opening, body, closing]), chunk_rewrite=Fixed(body),
                       coherence=Fixed(draft), revise_intent=Fixed({"action": "confirm"}))
        router = ModelRouter(clients, {s: s for s in STAGES})
        pipe = Pipeline(router, self.store, self.prompts)
        # 完全信任模式(默认):知识库不调用复核模型,产品事实按原文进入写作上下文。
        out = pipe.talk(Session(), "给新生写软文")
        self.assertEqual(out.status, "review", out.reason)
        self.assertTrue(any("产品外观" in value for value in clients["chunk_generate"].inputs))
        self.assertTrue(any("未执行视觉模型审图" in value for value in clients["chunk_generate"].inputs))
        self.assertIn("内存16GB", clients["fact_review"].inputs[0])
        self.assertEqual(pipe.feedback(out.session, "确认").status, "done")
        pipe.talk(Session(), "再写一篇")
        self.assertEqual((self.analysis.calls, self.vision.calls), (0, 0))

    def test_new_store_defaults_to_trusted_relaxed_review(self):
        from agent.pipeline import Pipeline
        pipe = Pipeline(self.analysis, self.store, self.prompts, review_mode="relaxed")
        self.assertEqual(pipe.review_mode, "relaxed")

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
        slots = {"platform": 1, "copy": 1, "audience": 5}
        self.assertNotIn("C1", {r["id"] for _, r in self.store.references(self.brief, slots)})
        self.assertNotIn("B1", {r["id"] for _, r in self.store.references(self.brief, slots)})
        comparing = copy.deepcopy(self.brief)
        comparing.main_problem = "与竞品对比"
        self.assertIn("C1", {r["id"] for _, r in self.store.references(comparing, slots)})
        commercial = copy.deepcopy(self.brief)
        commercial.main_problem = "价格优惠"
        self.assertIn("B1", {r["id"] for _, r in self.store.references(commercial, slots)})


class ReferenceSlotTest(unittest.TestCase):
    """表达参考按类型分名额:名额互不挤占,缺一类时留空,不用别的类型补位。"""

    def test_role_aliases_normalized(self):
        from agent.catalogkb import norm_role
        self.assertEqual(norm_role("大学新生", "开学季"), "学生")
        self.assertEqual(norm_role("老师", "教学办公"), "教师")
        self.assertEqual(norm_role("商务差旅用户", ""), "商务差旅")
        self.assertEqual(norm_role("", ""), "")

    def test_empty_slot_is_left_empty_instead_of_backfilled(self):
        store = CatalogStore.__new__(CatalogStore)
        store.records = lambda model=None: [
            {"id": "P", "kind": "platform_guide", "category": "marketing", "platform": "公众号",
             "model_key": None, "source": {"path": "04-营销表达/微信公众号图文模板.md"}},
            {"id": "S", "kind": "reference_material", "category": "scenarios", "platform": None,
             "model_key": None, "source": {"path": "03-人群场景/08-高校学生/客户画像.md"}},
        ]
        store.models = lambda: {}
        brief = Brief(product="MateBook 14", audience="高校学生", scene="学习", platform="公众号", goal="种草")
        picked = store.references(brief, {"platform": 1, "copy": 1, "audience": 1})
        self.assertEqual([(bucket, r["id"]) for bucket, r in picked], [("platform", "P"), ("audience", "S")])

    def test_slots_configured_in_policy_override_defaults(self):
        store = CatalogStore.__new__(CatalogStore)
        slots = store.reference_slots({"reference_slots": {"platform": 2, "copy": 0, "audience": 3}})
        self.assertEqual(slots, {"platform": 2, "copy": 0, "audience": 3})

    def test_legacy_total_limit_still_read(self):
        store = CatalogStore.__new__(CatalogStore)
        self.assertEqual(store.reference_slots({"reference_limit": 1}),
                         {"platform": 1, "copy": 0, "audience": 0})


