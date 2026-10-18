"""KnowledgeStore 的测试:用模拟的 ima,结构与真实知识库一致(文件夹 media_type=99,
型号目录含 产品事实.md / 卖点与限制.md / 图片索引.csv / images/)。不联网,不消耗任何额度。"""
import tempfile
import unittest
from pathlib import Path

from helpers import FakeLLM  # noqa: F401  (确保 sys.path 已设置)

from agent import evidence  # noqa: E402
from agent.ima import ImaError, ImaQuotaError  # noqa: E402

CSV = (
    "文件名,产品型号,来源页面,原始图片URL,官网图片说明,宽度,高度,建议用途\n"
    "hwpc_matebook-14-2024_主视觉_01.jpg,HUAWEI MateBook 14 酷睿 Ultra,https://x/p,https://x/a.jpg,高质感主视觉,100,100,产品介绍配图\n"
    "hwpc_matebook-14-2024_键盘_04.png,HUAWEI MateBook 14 酷睿 Ultra,https://x/p,https://x/b.png,舒适键盘细节,100,100,产品介绍配图\n"
    "hwpc_other_01.jpg,HUAWEI MateBook D 14,https://x/p,https://x/c.jpg,别的型号,100,100,产品介绍配图\n"
)


def folder(mid, title):
    return {"media_id": mid, "title": title, "media_type": 99}


def md(mid, title):
    return {"media_id": mid, "title": title, "media_type": 7}


def img(mid, title):
    return {"media_id": mid, "title": title, "media_type": 9}


TREE = {
    None: [folder("root", "测试库")],
    "root": [folder("p1", "P1-产品参数库"), folder("p4", "P4-价格政策库（机密）"), md("readme", "README.md")],
    "p1": [folder("s2", "02-MateBook-Windows"), folder("s6", "06-型号检索卡")],
    "s2": [folder("m14", "MateBook 14 酷睿 Ultra"), folder("md14", "MateBook D 14")],
    "s6": [folder("card", "HUAWEI MateBook 14")],            # 检索卡:没有产品事实.md,不算型号
    "card": [md("card-idx", "00-总索引.md")],
    "m14": [md("m14-fact", "产品事实.md"), md("m14-sell", "卖点与限制.md"), md("m14-idx", "图片索引.csv"), folder("m14-img", "images")],
    "md14": [md("md14-fact", "产品事实.md")],
    "m14-img": [
        img("i1", "hwpc_matebook-14-2024_主视觉_01.jpg"),
        img("i2", "hwpc_matebook-14-2024_键盘_04.png"),
        img("i3", "hwpc_other_01.jpg"),                      # 索引里属于别的型号
        img("i4", "hwpc_unlisted_09.jpg"),                   # 索引里没登记
    ],
}
CONTENT = {"m14-fact": "重量 1.31 kg", "m14-sell": "适合移动办公", "m14-idx": CSV, "md14-fact": "D 14 事实"}


class FakeIma:
    def __init__(self, tree=None, content=None, quota_after=None):
        self.tree, self.content = tree or TREE, content or CONTENT
        self.list_calls = 0
        self.fetch_calls = 0
        self.quota_after = quota_after       # 第 N 次浏览之后抛额度错误

    def credential_status(self):
        return {"client_id": "tes***id", "api_key": "tes***=="}

    def search_knowledge_base(self, query, limit=20):
        return [{"kb_id": "KB", "kb_name": "测试库"}]

    def list_dir(self, kb_id, folder_id=None):
        self.list_calls += 1
        if self.quota_after is not None and self.list_calls > self.quota_after:
            raise ImaQuotaError("额度已用完", code=220021)
        return list(self.tree.get(folder_id, []))

    def fetch(self, media_id, max_bytes=0):
        self.fetch_calls += 1
        return self.content[media_id].encode("utf-8") if media_id in self.content else None


def store(ima=None, cache=None):
    s = evidence.KnowledgeStore(ima or FakeIma(), "测试库", cache_dir=cache)
    s.connect()
    return s


class DiscoveryTest(unittest.TestCase):
    def test_models_found_by_fact_file_not_by_name(self):
        names = set(store().models())
        self.assertEqual(names, {"MateBook 14 酷睿 Ultra", "MateBook D 14"})
        self.assertNotIn("HUAWEI MateBook 14", names)       # 检索卡没有产品事实.md,不是可推广型号

    def test_other_libraries_not_scanned(self):
        ima = FakeIma()
        # 价格库、README 等不在 P1 之下,扫描不应进入 p4
        visited = []
        orig = ima.list_dir
        ima.list_dir = lambda kb, fid=None: (visited.append(fid), orig(kb, fid))[1]
        s = evidence.KnowledgeStore(ima, "测试库", cache_dir=None)
        s.connect(); s.models()
        self.assertNotIn("p4", visited)

    def test_docs_are_the_two_fact_files(self):
        docs = store().docs("MateBook 14 酷睿 Ultra")
        self.assertEqual([d["title"] for d in docs], ["产品事实.md", "卖点与限制.md"])
        self.assertTrue(docs[0]["path"].endswith("MateBook 14 酷睿 Ultra/产品事实.md"))
        self.assertTrue(docs[0]["path"].startswith("P1-产品参数库/"))

    def test_model_without_selling_file_still_works(self):
        self.assertEqual([d["title"] for d in store().docs("MateBook D 14")], ["产品事实.md"])

    def test_similar_models(self):
        self.assertIn("MateBook D 14", evidence.similar("MateBook D 16", list(store().models())))


class ImageTest(unittest.TestCase):
    def setUp(self):
        self.imgs = {i.name: i for i in store().images("MateBook 14 酷睿 Ultra")}

    def test_indexed_images_belong_to_model(self):
        self.assertEqual(self.imgs["hwpc_matebook-14-2024_主视觉_01.jpg"].model, "MateBook 14 酷睿 Ultra")

    def test_index_mismatch_unconfirmed_even_in_folder(self):
        # 图片虽然在该型号目录里,但索引登记的是 D 14:不能认
        im = self.imgs["hwpc_other_01.jpg"]
        self.assertEqual(im.model, "")
        self.assertIn("MateBook D 14", im.note)

    def test_unlisted_image_unconfirmed(self):
        im = self.imgs["hwpc_unlisted_09.jpg"]
        self.assertEqual(im.model, "")
        self.assertIn("未登记", im.note)

    def test_registration_info_carried(self):
        im = self.imgs["hwpc_matebook-14-2024_键盘_04.png"]
        self.assertEqual((im.caption, im.origin_url), ("舒适键盘细节", "https://x/b.png"))
        self.assertTrue(im.source_path.endswith("images/hwpc_matebook-14-2024_键盘_04.png"))

    def test_brand_prefix_ignored_in_matching(self):
        self.assertTrue(evidence.KnowledgeStore._same_product("HUAWEI MateBook 14 酷睿 Ultra", "MateBook 14 酷睿 Ultra"))
        self.assertFalse(evidence.KnowledgeStore._same_product("HUAWEI MateBook D 14", "MateBook 14 酷睿 Ultra"))


class CacheTest(unittest.TestCase):
    def test_second_run_makes_no_browse_calls(self):
        with tempfile.TemporaryDirectory() as d:
            first = FakeIma()
            store(first, Path(d)).models()
            self.assertGreater(first.list_calls, 0)
            second = FakeIma()
            s = store(second, Path(d))
            self.assertEqual(len(s.models()), 2)
            self.assertEqual(second.list_calls, 0)          # 命中缓存,没碰浏览额度
            self.assertTrue(s.cache_hits["index"])

    def test_doc_text_cached(self):
        with tempfile.TemporaryDirectory() as d:
            ima = FakeIma()
            s = store(ima, Path(d))
            self.assertEqual(s.read("m14-fact"), "重量 1.31 kg")
            self.assertEqual(store(ima, Path(d)).read("m14-fact"), "重量 1.31 kg")
            self.assertEqual(ima.fetch_calls, 1)

    def test_refresh_forces_rescan(self):
        with tempfile.TemporaryDirectory() as d:
            ima = FakeIma()
            s = store(ima, Path(d)); s.models()
            calls = ima.list_calls
            s.refresh(); s.models()
            self.assertGreater(ima.list_calls, calls)

    def test_corrupt_or_other_kb_cache_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "kb_index.json").write_text('{"version": 1, "kb_name": "别的库", "models": {"X": {}}}', encoding="utf-8")
            self.assertEqual(set(store(FakeIma(), Path(d)).models()), {"MateBook 14 酷睿 Ultra", "MateBook D 14"})

    def test_no_cache_when_disabled(self):
        ima = FakeIma()
        store(ima, None).models()
        n = ima.list_calls
        store(ima, None).models()
        self.assertGreater(ima.list_calls, n)

    def test_partial_scan_not_cached_on_quota_error(self):
        with tempfile.TemporaryDirectory() as d:
            s = store(FakeIma(quota_after=2), Path(d))
            with self.assertRaises(ImaQuotaError):
                s.models()
            self.assertFalse((Path(d) / "kb_index.json").exists())   # 不把残缺的目录当成完整缓存


class QuotaTest(unittest.TestCase):
    def test_quota_error_is_an_ima_error(self):
        self.assertTrue(issubclass(ImaQuotaError, ImaError))

    def test_quota_without_cache_surfaces_clearly(self):
        s = store(FakeIma(quota_after=0), None)
        with self.assertRaises(ImaQuotaError):
            s.models()

    def test_quota_with_cache_still_works(self):
        with tempfile.TemporaryDirectory() as d:
            store(FakeIma(), Path(d)).models()
            self.assertEqual(len(store(FakeIma(quota_after=0), Path(d)).models()), 2)


if __name__ == "__main__":
    unittest.main()
