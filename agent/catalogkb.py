"""新知识库读取与文字资料首次使用复核；图片只使用已登记的型号归属。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .evidence import similar
from .knowledge import KnowledgeError, LocalKBError
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


def suspicious_dimensions(text: str) -> list[dict]:
    """标记明显异常的笔记本尺寸，不猜测字段顺序。"""
    issues = []
    for name, low, high in (("厚度", 1, 50), ("长度", 100, 600)):
        match = re.search(rf"(?m)^{name}\s*\n\s*(\d+(?:\.\d+)?)\s*mm", text)
        if match and not low <= float(match[1]) <= high:
            issues.append({"code": "suspected_dimension_order", "detail": f"{name} {match[1]} mm 超出笔记本常见范围，仅提示复核，不认定正确值"})
    return issues


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
                                 row.get("官网图片说明") or r.get("caption", ""),
                                 row.get("建议用途") or r.get("usage", ""), row.get("原始图片URL", "")))
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
        comparison = any(word in query for word in ("竞品", "对比", "比较", "横评", "相比", "替代", "苹果", "MacBook",
                                                     "联想", "ThinkPad", "ThinkBook", "戴尔", "Dell", "XPS", "惠普", "HP",
                                                     "华硕", "小米", "REDMI"))
        commercial = brief.goal == "促销告知" or any(word in query for word in
            ("价格", "报价", "优惠", "补贴", "库存", "赠品", "返利", "交付", "分期", "以旧换新"))
        ranked = []
        for r in self.records():
            if r["category"] == "competitor" and not comparison:
                continue
            if r["category"] == "dynamic" and not (r["kind"] == "business_rule" and commercial):
                continue
            if r["category"] == "images":
                continue
            if r["category"] == "products" and r["kind"] != "product_guidance":
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

    def collect(self, analysis, prompts, brief, progress=None):
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

        # 图片不再调用视觉模型审核。这里只使用目录中已登记、已绑定当前精确型号且哈希一致的图片，
        # 并按本轮场景关键词排序；图片画面本身不作为产品事实来源。
        terms = self._terms(" ".join((brief.main_problem, brief.scene, brief.extra)))
        images = self.images(model, limit=10000)
        images.sort(key=lambda i: -len(terms & self._terms(i.name + i.caption + i.usage)))
        ev.images.extend(images[:image_limit])
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
    root = Path(os.environ.get("KB_LOCAL_DIR") or DEFAULT_RUNTIME_KB).expanduser()
    if not (root / INDEX / "catalog.json").is_file():
        raise LocalKBError(f"当前知识库缺少 catalog.json：{root}")
    store = CatalogStore(root)
    if not store.connect()["connected"]:
        raise LocalKBError(f"本地知识库不可用：{root}")
    return store
