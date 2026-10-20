import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from agent.kb_migration import (
    INDEX, MigrationError, build_snapshot, digest, select_sections, suspicious_dimensions, validate_snapshot,
)
from agent.localkb import LocalKBError, LocalStore


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "original"
        self.output = Path(self.temp.name) / "candidate"
        self.model = "MateBook 14"
        self.base = "P1-产品参数库/电脑/MateBook 14"
        self.put(self.base + "/产品事实.md", (
            "# HUAWEI MateBook 14 - 产品事实\n\n来源：https://consumer.huawei.com/cn/test/\n"
            "## 操作系统\nHarmonyOS 6\n## 尺寸与重量\n长度\n\n13.5 mm\n厚度\n\n222 mm\n"
            "*实际重量依配置而异。\n"
        ))
        self.put(self.base + "/卖点与限制.md", "# 使用边界\n仅适合已确认兼容条件的任务。\n")
        csv_text = (
            "文件名,产品型号,官网图片说明,宽度,高度\n"
            "good.png,HUAWEI MateBook 14,主图,20,10\n"
            "wrong.png,HUAWEI MateBook 16,错型号,20,10\n"
        )
        self.put(self.base + "/图片索引.csv", csv_text)
        for name in ("good.png", "wrong.png", "unlisted.png"):
            path = self.source / self.base / "images" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (20, 10), "red").save(path)
        self.put("P7/分享.md", "# 分享\n## 范围\n仅限原文设备。\nhttps://consumer.huawei.com/cn/support/x/\n## 不提取\n冗余内容\n")
        self.put("P8/画像.md", "# 学生\n## 问题\n先确认专业软件。\n")
        self.put("P6/方法.md", "# 写作\n## 方法\n一个卖点解决一个问题。\n")
        self.put("P4-价格政策库（机密）/内部.md", "绝不能迁移")
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=["记录ID", "规范产品名称", "核验时间", "页面金额（元）"])
        writer.writeheader()
        writer.writerow({"记录ID": "A", "规范产品名称": "HUAWEI MateBook 14", "核验时间": "2020-01-01", "页面金额（元）": "999"})
        writer.writerow({"记录ID": "B", "规范产品名称": "HUAWEI MateBook 16", "核验时间": "2020-01-01", "页面金额（元）": "888"})
        self.put("P1-产品参数库/prices.csv", stream.getvalue())
        self.policy = {
            "version": 1, "product_documents": ["产品事实.md", "卖点与限制.md"],
            "max_images_per_product": 6, "official_domains": ["huawei.com", "vmall.com"],
            "governance_sources": [], "price_snapshot": "P1-产品参数库/prices.csv",
            "documents": [
                {"glob": "P7/*.md", "category": "ecosystem", "kind": "support_reference", "sections": ["范围"]},
                {"glob": "P8/*.md", "category": "scenarios", "kind": "audience_profile", "sections": ["问题"]},
                {"glob": "P6/*.md", "category": "marketing", "kind": "writing_method", "sections": ["方法"]},
            ],
        }

    def put(self, relative, text):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def build(self):
        return build_snapshot(self.source, self.output, self.policy)

    def catalog(self):
        return json.loads((self.output / INDEX / "catalog.json").read_text(encoding="utf-8"))

    def test_all_categories_provenance_and_runtime_gate(self):
        before = {p.relative_to(self.source).as_posix(): digest(p.read_bytes()) for p in self.source.rglob("*") if p.is_file()}
        report = self.build()
        self.assertTrue(report["structural_checks_passed"])
        self.assertEqual(set(report["by_category"]), {"products", "ecosystem", "scenarios", "marketing", "images", "dynamic"})
        self.assertFalse(self.catalog()["ready_for_runtime"])
        self.assertTrue(all(not r["publishable"] for r in self.catalog()["records"]))
        self.assertFalse((self.output / "P1-产品参数库").exists())
        after = {p.relative_to(self.source).as_posix(): digest(p.read_bytes()) for p in self.source.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(any("P4-" in r["source"]["path"] for r in self.catalog()["records"]))

    def test_wrong_or_unregistered_images_are_not_selected(self):
        self.build()
        images = [r for r in self.catalog()["records"] if r["category"] == "images"]
        self.assertEqual([Path(r["path"]).name for r in images], ["good.png"])

    def test_price_keeps_original_row_and_historical_status(self):
        self.build()
        prices = [r for r in self.catalog()["records"] if r["category"] == "dynamic"]
        self.assertEqual(len(prices), 1)
        self.assertEqual(prices[0]["review_status"], "historical_only")
        self.assertEqual(prices[0]["observed_at_in_source"], "2020-01-01")
        row = json.loads((self.output / prices[0]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(row["页面金额（元）"], "999")

    def test_suspicious_dimensions_are_flagged_but_preserved(self):
        self.build()
        facts = next(r for r in self.catalog()["records"] if r["kind"] == "product_facts")
        self.assertTrue(any(i["code"] == "suspected_dimension_order" for i in facts["issues"]))
        self.assertIn("222 mm", (self.output / facts["path"]).read_text(encoding="utf-8"))

    def test_output_tamper_is_detected(self):
        self.build()
        record = self.catalog()["records"][0]
        (self.output / record["path"]).write_text("被修改的数据", encoding="utf-8")
        result = validate_snapshot(self.source, self.output)
        self.assertFalse(result["structural_checks_passed"])
        self.assertTrue(any("输出" in e for e in result["errors"]))

    def test_source_tamper_is_detected(self):
        self.build()
        self.put(self.base + "/产品事实.md", "伪造的事实")
        self.assertFalse(validate_snapshot(self.source, self.output)["structural_checks_passed"])

    def test_metadata_cannot_silently_grant_publication(self):
        self.build()
        catalog = self.catalog()
        catalog["records"][0]["publishable"] = True
        (self.output / INDEX / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
        result = validate_snapshot(self.source, self.output)
        self.assertFalse(result["structural_checks_passed"])
        self.assertTrue(any("未经独立审核" in e for e in result["errors"]))

    def test_overwrite_and_source_target_are_rejected(self):
        self.build()
        with self.assertRaises(MigrationError):
            self.build()
        with self.assertRaises(MigrationError):
            build_snapshot(self.source, self.source / "new", self.policy)

    def test_missing_selected_heading_does_not_write_partial_snapshot(self):
        self.policy["documents"][0]["sections"] = ["不存在的标题"]
        with self.assertRaises(MigrationError):
            self.build()
        self.assertFalse(self.output.exists())

    def test_section_ancestors_and_limitations_are_preserved(self):
        source = "# 原题\n## 范围\n设备限制\n## 说明\n### 第2步 原始证据\n必须满足条件\n### 第3步\n不要提取\n"
        text, spans = select_sections(source, ["范围"], ["第2步"])
        self.assertIn("## 说明", text)
        self.assertIn("必须满足条件", text)
        self.assertNotIn("不要提取", text)
        self.assertEqual(spans[0]["start"], 1)

    def test_duplicate_local_model_names_do_not_return_partial_cache(self):
        self.put("P1-产品参数库/另一个系统/MateBook 14/产品事实.md", "# 不同的产品")
        store = LocalStore(self.source)
        for _ in range(2):
            with self.assertRaises(LocalKBError):
                store.models()

    def test_local_connection_has_no_remote_credential_fields(self):
        self.assertEqual(set(LocalStore(self.source).connect()), {"connected", "kb_name"})
