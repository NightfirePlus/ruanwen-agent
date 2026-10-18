"""LocalStore 测试:用临时目录搭一个结构与真实知识库一致的小库,不依赖真实数据。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import FakeLLM, make_prompts  # noqa: F401

from agent import evidence  # noqa: E402
from agent.localkb import LocalKBError, LocalStore  # noqa: E402

CSV = (
    "文件名,产品型号,来源页面,原始图片URL,官网图片说明,宽度,高度,建议用途\n"
    "a_01.jpg,HUAWEI MateBook D 16 2024,https://x/p,https://x/a.jpg,主视觉,1,1,配图\n"
    "b_02.jpg,HUAWEI MateBook D 16 SE 版 2024,https://x/p,https://x/b.jpg,SE主视觉,1,1,配图\n"
)


def build(root: Path):
    def put(rel, text="x", binary=False):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text if binary else text.encode("utf-8"))

    base = "P1-产品参数库/02-MateBook-Windows"
    put(f"{base}/MateBook D 16/产品事实.md", "# HUAWEI MateBook D 16 2024 - 产品事实\n重量 1.68 kg\n")
    put(f"{base}/MateBook D 16/卖点与限制.md", "适合大屏办公")
    put(f"{base}/MateBook D 16/图片索引.csv", CSV)
    for n in ("a_01.jpg", "b_02.jpg", "c_03.jpg"):
        put(f"{base}/MateBook D 16/images/{n}", b"\xff\xd8\xff\xe0", binary=True)
    put(f"{base}/MateBook D 16 SE 版/产品事实.md", "# HUAWEI MateBook D 16 SE 版 2024 - 产品事实\n重量 1.7 kg\n")
    put("P1-产品参数库/06-型号检索卡/HUAWEI MateBook 14/00-总索引.md", "检索卡,没有产品事实.md")
    put("P4-价格政策库（机密）/价格.md", "机密价格")
    put("README.md", "根说明")


class LocalStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "库"
        build(self.root)
        self.store = LocalStore(self.root)

    def test_connect_requires_p1(self):
        self.assertTrue(self.store.connect()["connected"])
        self.assertFalse(LocalStore(Path(self.tmp.name)).connect()["connected"])

    def test_models_by_fact_file_only(self):
        self.assertEqual(set(self.store.models()), {"MateBook D 16", "MateBook D 16 SE 版"})

    def test_other_libraries_never_read(self):
        paths = [str(i["folder_id"]) for i in self.store.models().values()]
        self.assertFalse(any("P4" in p for p in paths))

    def test_docs_and_read(self):
        docs = self.store.docs("MateBook D 16")
        self.assertEqual([d["title"] for d in docs], ["产品事实.md", "卖点与限制.md"])
        self.assertIn("1.68 kg", self.store.read(docs[0]["media_id"]))
        self.assertEqual([d["title"] for d in self.store.docs("MateBook D 16 SE 版")], ["产品事实.md"])

    def test_official_name_from_title(self):
        self.assertEqual(self.store.official("MateBook D 16"), "HUAWEI MateBook D 16 2024")
        self.assertEqual(self.store.official("MateBook D 16 SE 版"), "HUAWEI MateBook D 16 SE 版 2024")

    def test_image_ownership_follows_index_not_folder(self):
        imgs = {i.name: i for i in self.store.images("MateBook D 16")}
        self.assertEqual(imgs["a_01.jpg"].model, "MateBook D 16")
        self.assertEqual(imgs["b_02.jpg"].model, "")                 # 在 D 16 目录里,但索引登记的是 SE 版
        self.assertIn("SE", imgs["b_02.jpg"].note)
        self.assertEqual(imgs["c_03.jpg"].model, "")                 # 索引没登记
        self.assertIn("未登记", imgs["c_03.jpg"].note)

    def test_image_registration_carried(self):
        im = next(i for i in self.store.images("MateBook D 16") if i.name == "a_01.jpg")
        self.assertEqual((im.caption, im.origin_url), ("主视觉", "https://x/a.jpg"))
        self.assertTrue(im.source_path.endswith("MateBook D 16/images/a_01.jpg"))

    def test_model_without_images_dir(self):
        self.assertEqual(self.store.images("MateBook D 16 SE 版"), [])

    def test_read_refuses_path_outside_root(self):
        outside = Path(self.tmp.name) / "secret.txt"
        outside.write_text("机密", encoding="utf-8")
        with self.assertRaises(LocalKBError):
            self.store.read(str(outside))
        with self.assertRaises(LocalKBError):
            self.store.read(str(self.root / ".." / "secret.txt"))

    def test_read_missing_file_is_empty(self):
        self.assertEqual(self.store.read(str(self.root / "不存在.md")), "")

    def test_utf8_bom_tolerated(self):
        f = self.root / "bom.md"
        f.write_bytes(b"\xef\xbb\xbfhello")
        self.assertEqual(self.store.read(str(f)), "hello")


class SameProductTest(unittest.TestCase):
    def test_pairs_that_must_not_be_confused(self):
        sp = evidence.same_product
        self.assertFalse(sp("HUAWEI MateBook 14 酷睿 Ultra", "HUAWEI MateBook 14", "HUAWEI MateBook 14（鸿蒙版）"))
        self.assertFalse(sp("HUAWEI MateBook D 16 SE 版 2024", "MateBook D 16", "HUAWEI MateBook D 16 2024"))
        self.assertFalse(sp("HUAWEI MateBook Pro S", "HUAWEI MateBook Pro", "HUAWEI MateBook Pro"))

    def test_matching_pairs(self):
        sp = evidence.same_product
        self.assertTrue(sp("HUAWEI MateBook 14（鸿蒙版）", "HUAWEI MateBook 14", "HUAWEI MateBook 14（鸿蒙版）"))
        self.assertTrue(sp("HUAWEI MateBook D 14 2024", "MateBook D 14", "HUAWEI MateBook D 14 2024"))
        self.assertTrue(sp("(全角括号)", "x", "(全角括号)".replace("(", "（").replace(")", "）")))

    def test_empty_never_matches(self):
        self.assertFalse(evidence.same_product("", "MateBook D 14"))

    def test_official_name_parsing(self):
        on = evidence.official_name
        self.assertEqual(on("# HUAWEI MateBook D 14 2024 - 产品事实\n正文", "x"), "HUAWEI MateBook D 14 2024")
        self.assertEqual(on("没有标题", "后备名"), "后备名")
        self.assertEqual(on("", "后备名"), "后备名")


class BuildWithLocalStoreTest(unittest.TestCase):
    def test_build_works_end_to_end_on_local_store(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "库"
            build(root)
            store = LocalStore(root)
            llm = FakeLLM(facts=[{"text": "重量 1.68 kg"}, {"text": "编造的内容"}])
            ev = evidence.build(llm, make_prompts(), store, "MateBook D 16")
            self.assertEqual([f.text for f in ev.facts], ["重量 1.68 kg"])
            self.assertTrue(any("编造的内容" in x for x in ev.dropped))
            self.assertIn("MateBook D 16 SE 版", ev.candidates)       # 相近型号被识别
            self.assertTrue(ev.facts[0].source_path.endswith("MateBook D 16/产品事实.md"))
            self.assertEqual([i.name for i in ev.images if i.model], ["a_01.jpg"])

    def test_unknown_model_asks_instead_of_guessing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "库"
            build(root)
            with self.assertRaises(evidence.ModelNotResolved) as ctx:
                evidence.build(FakeLLM(), make_prompts(), LocalStore(root), "MateBook D 15")
            self.assertTrue(ctx.exception.candidates)


class MainStoreTest(unittest.TestCase):
    def test_main_uses_local_store_without_ima(self):
        import main

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "库"
            build(root)
            with patch.dict(os.environ, {"KB_LOCAL_DIR": str(root)}):
                store = main.make_store()
            self.assertIsInstance(store, LocalStore)
            self.assertEqual(store.root, root.resolve())


if __name__ == "__main__":
    unittest.main()
