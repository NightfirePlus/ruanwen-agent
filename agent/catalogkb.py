"""新知识库读取与首次使用复核；审核结果写入配套SQLite索引，原始资料只读。"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import sqlite3
import time
from dataclasses import asdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from .evidence import similar
from .knowledge import KnowledgeError
from .localkb import LocalKBError
from .models import Evidence, Fact, ImageRef
from .norm import norm_model

INDEX = "00-规范与索引"
RULE_VERSION = "usage-review-1"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def packed(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def original_excerpt(quote, source):
    """仅容忍空白排版差异，返回原文的连续片段；绝不接受改写。"""
    if not isinstance(quote, str) or not quote.strip():
        return None
    if quote in source:
        return quote
    positions = [i for i, ch in enumerate(source) if not ch.isspace()]
    flattened = "".join(source[i] for i in positions)
    normalized = "".join(quote.split())
    start = flattened.find(normalized)
    if start < 0:
        return None
    return source[positions[start]:positions[start + len(normalized) - 1] + 1]


class ReviewCache:
    """先持久化占位再请求，跨线程/进程不重复审核；失败不能变成通过。"""
    def __init__(self, path: Path):
        self.path = path
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS reviews (key TEXT PRIMARY KEY, status TEXT NOT NULL, result TEXT, at REAL NOT NULL)")

    @contextmanager
    def db(self):
        try:
            db = sqlite3.connect(self.path, timeout=10)
        except sqlite3.Error as exc:
            raise KnowledgeError("审核索引不可写，请检查知识库目录权限") from exc
        try:
            with db:
                yield db
        except sqlite3.Error as exc:
            raise KnowledgeError("审核索引忙或不可写；未放行、未重复调用模型") from exc
        finally:
            db.close()

    def obtain(self, key: str, compute, reserve=None):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT status,result FROM reviews WHERE key=?", (key,)).fetchone()
            if old:
                if old[0] in ("in_progress", "failed"):
                    raise KnowledgeError("资料审核尚未完成或曾失败，请检查审核索引；未重复调用或放行")
                return json.loads(old[1]), True
            if reserve:
                reserve()
            db.execute("INSERT INTO reviews VALUES (?,?,?,?)", (key, "in_progress", None, time.time()))
        try:
            result = compute()
        except Exception:
            with self.db() as db:
                db.execute("UPDATE reviews SET status='failed',at=? WHERE key=?", (time.time(), key))
            raise
        with self.db() as db:
            db.execute("UPDATE reviews SET status=?,result=?,at=? WHERE key=?",
                       (result["status"], json.dumps(result, ensure_ascii=False), time.time(), key))
        return result, False

    def contains(self, key):
        with self.db() as db:
            return db.execute("SELECT 1 FROM reviews WHERE key=?", (key,)).fetchone() is not None

    def annotate(self, key, metadata):
        """补齐旧审核记录的追溯字段，保留原状态及原审核时间。"""
        with self.db() as db:
            raw, at = db.execute("SELECT result,at FROM reviews WHERE key=?", (key,)).fetchone()
            data = json.loads(raw)
            data["_review"] = dict(metadata, checked_at_utc=datetime.fromtimestamp(at, timezone.utc).isoformat())
            db.execute("UPDATE reviews SET result=? WHERE key=?", (json.dumps(data, ensure_ascii=False), key))
        return data


class CatalogStore:
    requires_strict_review = True
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.kb_name = self.root.name
        self._catalog = None
        self._catalog_hash = None
        self._cache = None

    def _safe(self, relative):
        rel = Path(relative)
        path = (self.root / rel).resolve()
        if rel.is_absolute() or ".." in rel.parts or not path.is_relative_to(self.root):
            raise LocalKBError("知识库路径越界")
        return path

    def _load(self):
        try:
            raw = self._safe(f"{INDEX}/catalog.json").read_bytes()
            if self._catalog_hash != sha(raw):
                data = json.loads(raw)
                records = data["records"]
                ids = [r["id"] for r in records]
                names = [norm_model(m["model_key"]) for m in data["models"]]
                if len(ids) != len(set(ids)) or len(names) != len(set(names)):
                    raise ValueError("重复标识或型号")
                for r in records:
                    self._safe(r["path"])
                self._catalog, self._catalog_hash = data, sha(raw)
            return self._catalog
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LocalKBError(f"新知识库索引无效：{type(exc).__name__}") from exc

    def connect(self):
        return {"connected": bool(self._load()["models"]), "kb_name": self.kb_name}

    def models(self):
        return {m["model_key"]: {"path": m["official_product"]} for m in self._load()["models"]}

    def records(self, model=None):
        return [r for r in self._load()["records"] if model is None or r.get("model_key") == model]

    def policy(self):
        path = self._safe(f"{INDEX}/runtime-policy.json")
        if not path.exists():
            return {"contract_version": 1, "review_max_calls": 5, "reference_limit": 2, "image_limit": 2}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("runtime_enabled") is not True or data.get("contract_version") != 1:
                raise ValueError("运行约定未启用或版本不支持")
            return data
        except (ValueError, OSError) as exc:
            raise LocalKBError("知识库使用约定无效") from exc

    def docs(self, model):
        return [dict(media_id=r["path"], title=Path(r["path"]).name, path=r["path"])
                for r in self.records(model) if r["kind"] == "product_facts"]

    def read(self, media_id):
        return self._safe(media_id).read_text(encoding="utf-8-sig")

    def _raw(self, record):
        raw = self._safe(record["path"]).read_bytes()
        if sha(raw) != record["sha256"]:
            raise LocalKBError("资料内容已变化，须同步来源与索引后重新复核：" + record["path"])
        return raw

    def images(self, model, limit=30):
        refs = []
        for r in self.records(model):
            if r["kind"] != "registered_image":
                continue
            self._raw(r)
            row = r["source"]["index_row"]
            refs.append(ImageRef(r["id"], model, Path(r["path"]).name, str(self._safe(r["path"])), r["path"],
                                 row.get("官网图片说明", ""), row.get("建议用途", ""), row.get("原始图片URL", "")))
        return refs[:limit]

    def _key(self, record, prompt, model):
        # 文件、标注、归属、来源、依赖资料或审核规则/提示词/模型变化均触发重新复核。
        deps = {r["id"]: sha(packed(dict(record=r, actual=sha(self._raw(r))))) for r in self.records(record.get("model_key"))
                if r["kind"] == "product_facts"}
        return sha(packed(dict(record=record, actual=sha(self._raw(record)), dependencies=deps,
                               prompt=prompt, model=model, rules=RULE_VERSION)))

    @staticmethod
    def _terms(text):
        return set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2}", text.lower()))

    def references(self, brief, limit=2):
        query = " ".join((brief.audience, brief.scene, brief.platform, brief.main_problem, brief.extra))
        aliases = {"大学": "学生 学习", "新生": "学生 学习", "出差": "商务 差旅", "设计": "创作 内容创作者"}
        query += " " + " ".join(v for k, v in aliases.items() if k in query)
        terms = self._terms(query)
        ranked = []
        for r in self.records():
            if r["category"] in ("dynamic", "images", "products") and r["kind"] != "product_guidance":
                continue
            if r.get("model_key") and norm_model(r["model_key"]) != norm_model(brief.product):
                continue
            if r.get("platform") and r["platform"] != brief.platform:
                continue
            label = r["source"]["path"]
            score = len(terms & self._terms(label)) + (2 if r.get("platform") else 0)
            if score:
                ranked.append((score, r["id"], r))
        return [r for _, _, r in sorted(ranked, key=lambda v: (-v[0], v[1]))[:limit]]

    def collect(self, analysis, vision, prompts, brief, progress=None):
        from .evidence import ModelNotResolved, mentioned_models
        names = list(self.models())
        model = next((m for m in names if norm_model(m) == norm_model(brief.product)), None)
        if not model:
            raise ModelNotResolved(brief.product, similar(brief.product, names))
        ev = Evidence(candidates=similar(model, names), review_stats={"new": 0, "cached": 0, "excluded": 0})
        if self._cache is None:
            self._cache = ReviewCache(self._safe(f"{INDEX}/usage-review.sqlite3"))
        policy = self.policy()
        remaining = int(os.environ.get("KB_REVIEW_MAX_CALLS", policy["review_max_calls"]))

        def audited(record, stage, client, compute):
            key = self._key(record, prompts.text(stage), getattr(client, "model", "injected-test-model"))
            def reserve():
                nonlocal remaining
                if remaining <= 0:
                    raise KnowledgeError("本轮知识库复核次数已达上限，未审阅资料不放行")
                remaining -= 1
            def checked_compute():
                result = compute()
                result["_review"] = dict(record_id=record["id"], path=record["path"], file_sha256=record["sha256"],
                    checked_at_utc=datetime.now(timezone.utc).isoformat(), rules_version=RULE_VERSION,
                    model=getattr(client, "model", "injected-test-model"), prompt_sha256=sha(prompts.text(stage).encode()),
                    scope="内部一致性与使用边界复核，不代表重新核验官网或人工批准")
                return result
            result, cached = self._cache.obtain(key, checked_compute, reserve)
            if "_review" not in result:
                result = self._cache.annotate(key, dict(record_id=record["id"], path=record["path"],
                    file_sha256=record["sha256"], rules_version=RULE_VERSION,
                    model=getattr(client, "model", "injected-test-model"), prompt_sha256=sha(prompts.text(stage).encode()),
                    scope="内部一致性与使用边界复核，不代表重新核验官网或人工批准"))
            ev.review_stats["cached" if cached else "new"] += 1
            if progress:
                progress("知识库复核", f"{'复用审核记录' if cached else '首次使用复核完成'}：{Path(record['path']).name}")
            return result

        def review_text(record):
            text = self._raw(record).decode("utf-8-sig")
            from .kb_migration import suspicious_dimensions
            problems = suspicious_dimensions(text)
            excluded_values = [i["detail"] for i in problems]
            safe_text = text
            if problems:
                safe_text = re.sub(r"(?ms)^## [^\n]*尺寸[^\n]*\n.*?(?=^## |\Z)", "", text)
            info = {k: record.get(k) for k in ("kind", "model_key", "official_product", "system_scope_raw", "sku_scope", "source")}
            info["local_exclusions"] = excluded_values
            def compute():
                data = analysis.json(prompts.render("material_review", {"资料信息": json.dumps(info, ensure_ascii=False), "资料全文": safe_text}))
                if not isinstance(data, dict) or data.get("status") not in ("passed", "uncertain", "rejected"):
                    raise KnowledgeError("资料复核返回结构不完整，未放行")
                for field in ("facts", "guidance", "excluded"):
                    if not isinstance(data.get(field), list):
                        raise KnowledgeError("资料复核缺少必要数组，未放行")
                verified = []
                for item in data["facts"]:
                    text = original_excerpt(item.get("text") if isinstance(item, dict) else None, safe_text)
                    if not text or mentioned_models(text, names) - {model} or suspicious_dimensions(text):
                        data["excluded"].append({"quote": str(item), "reason": "模型摘录未通过连续原文、型号或字段校验，已丢弃"})
                        continue
                    verified.append({"text": text})
                data["facts"] = verified
                data["guidance"] = [text for g in data["guidance"] if (text := original_excerpt(g, safe_text))]
                if record["kind"] != "product_facts" and data["facts"]:
                    raise KnowledgeError("表达参考不可升级为产品事实")
                if record["kind"] == "product_facts" and data["status"] == "passed" and not data["facts"]:
                    raise KnowledgeError("没有可用产品事实，不能标记通过")
                if record["kind"] != "product_facts" and data["status"] == "passed" and not data["guidance"]:
                    data["status"] = "uncertain"
                data["local_exclusions"] = excluded_values
                return data
            return text, audited(record, "material_review", analysis, compute)

        core = [r for r in self.records(model) if r["kind"] == "product_facts"]
        for record in core:
            text, result = review_text(record)
            if result["status"] != "passed":
                raise KnowledgeError("产品资料复核未通过：" + str(result.get("reason", "待核实")))
            ev.sources.append(record["path"])
            ev.originals.append(dict(path=record["path"], text=text, exclusions=result.get("local_exclusions", []) + result["excluded"]))
            for f in result["facts"]:
                ev.facts.append(Fact(f"F{len(ev.facts)+1}", model, f["text"], Path(record["path"]).name, record["path"], record["path"]))
            ev.dropped.extend(result.get("local_exclusions", []))
        if not ev.facts:
            raise KnowledgeError("新库没有复核通过的产品事实")
        from .evidence import detect_conflicts
        ev.conflicts = detect_conflicts(ev.facts)
        reference_limit = max(0, min(4, int(os.environ.get("KB_REFERENCE_LIMIT", policy["reference_limit"]))))
        image_limit = max(0, min(4, int(os.environ.get("KB_IMAGE_LIMIT", policy["image_limit"]))))
        for record in self.references(brief, limit=reference_limit):
            _, result = review_text(record)
            if result["status"] == "passed":
                ev.context.append(dict(path=record["path"], kind=record["kind"], quotes=result["guidance"], authority="表达与场景参考，不是产品事实"))
            else:
                ev.review_stats["excluded"] += 1

        # 只审核本轮有望使用的两张候选，避免第一次生成时扫描整个图片库。
        terms = self._terms(" ".join((brief.main_problem, brief.scene, brief.extra)))
        images = self.images(model, limit=10000)
        images.sort(key=lambda i: -len(terms & self._terms(i.name + i.caption + i.usage)))
        new_images = 0
        for image in images:
            if len(ev.images) >= image_limit:
                break
            record = next(r for r in self.records(model) if r["id"] == image.id)
            image_key = self._key(record, prompts.text("image_review"), getattr(vision, "model", "injected-test-model"))
            if not self._cache.contains(image_key) and (new_images >= image_limit or remaining <= 0):
                ev.dropped.append(f"本轮未审核图片：{image.name}，不加入生成候选")
                continue
            def compute_image():
                raw = self._raw(record)
                with Image.open(io.BytesIO(raw)) as pic:
                    pic.load()
                    file_checks = dict(width=pic.width, height=pic.height)
                    if "A" in pic.getbands():
                        file_checks["fully_transparent_fraction"] = pic.getchannel("A").histogram()[0] / (pic.width * pic.height)
                        file_checks["center_pixel"] = pic.getpixel((pic.width // 2, pic.height // 2))
                    pic.thumbnail((1280, 1280))
                    # 保留透明度供模型判断图层，提供紧凑预览，不修改原文件。
                    preview = io.BytesIO()
                    pic.convert("RGBA").save(preview, format="PNG")
                metadata = {"record": record, "caption": image.caption, "usage": image.usage, "file_checks": file_checks}
                prompt = prompts.render("image_review", {"图片信息": json.dumps(metadata, ensure_ascii=False),
                    "产品依据": json.dumps([asdict(f) for f in ev.facts], ensure_ascii=False)})
                uri = "data:image/png;base64," + base64.b64encode(preview.getvalue()).decode("ascii")
                data = vision.json([{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": uri}}])
                allowed = ("product", "detail", "scene", "infographic", "composition_required", "unknown")
                if (not isinstance(data, dict) or data.get("status") not in ("passed", "uncertain", "rejected")
                        or data.get("asset_type") not in allowed or not isinstance(data.get("description"), str)
                        or not data["description"].strip() or not isinstance(data.get("topics"), list)
                        or not isinstance(data.get("restrictions"), list) or not isinstance(data.get("visible_text"), str)):
                    raise KnowledgeError("图片审阅结构不完整，未放行")
                if any(not isinstance(value, str) for value in data["topics"] + data["restrictions"]):
                    raise KnowledgeError("图片审阅的主题或限制格式错误，未放行")
                if data["asset_type"] in ("composition_required", "unknown") and data["status"] == "passed":
                    data["status"] = "uncertain"
                return data
            before = ev.review_stats["new"]
            result = audited(record, "image_review", vision, compute_image)
            new_images += ev.review_stats["new"] - before
            with Image.open(self._safe(record["path"])) as pic:
                result["file_checks"] = dict(width=pic.width, height=pic.height)
                if "A" in pic.getbands():
                    result["file_checks"]["fully_transparent_fraction"] = pic.getchannel("A").histogram()[0] / (pic.width * pic.height)
                    if result["file_checks"]["fully_transparent_fraction"] > 0:
                        result["restrictions"] = list(dict.fromkeys(result["restrictions"] + [
                            "文件含透明区域，实际背景由发布容器决定；不要将模型背景颜色描述当成产品事实"]))
            if result["status"] == "passed":
                image.review = result
                ev.images.append(image)
            else:
                ev.review_stats["excluded"] += 1
                ev.dropped.append(f"图片未采用：{image.name}；{result.get('reason', '待核实')}")
        ev.review_stats["catalog_signature"] = self._catalog_hash
        ev.review_stats["source_hashes"] = {r["path"]: sha(self._raw(r)) for r in self.records()
            if r["path"] in ev.sources or any(c["path"] == r["path"] for c in ev.context) or any(i.id == r["id"] for i in ev.images)}
        return ev

    def still_valid(self, evidence):
        self._load()
        if evidence.review_stats.get("catalog_signature") != self._catalog_hash:
            return False
        return all(self._safe(path).is_file() and sha(self._safe(path).read_bytes()) == value
                   for path, value in evidence.review_stats.get("source_hashes", {}).items())


DEFAULT_RUNTIME_KB = Path(__file__).resolve().parents[1] / "knowledge_base/current"


def make_store():
    from .localkb import LocalStore
    root = Path(os.environ.get("KB_LOCAL_DIR") or DEFAULT_RUNTIME_KB).expanduser()
    store = CatalogStore(root) if (root / INDEX / "catalog.json").is_file() else LocalStore(root)
    if not store.connect()["connected"]:
        raise LocalKBError(f"本地知识库不可用：{root}")
    return store
