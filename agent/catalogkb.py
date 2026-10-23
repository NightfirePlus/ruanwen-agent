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

# 表达参考按类型分名额：平台语气要 1 份，成稿写法/任务卡 1 份，人群场景或角色沟通卡 1 份。
# 名额互不挤占；库里某类没有匹配资料时该名额留空，不拿别的类型凑数。
REFERENCE_BUCKETS = ("platform", "copy", "audience")
DEFAULT_REFERENCE_SLOTS = {"platform": 1, "copy": 1, "audience": 1}
# 表达参考不经过模型复核,直接取正文;截断只为控制提示词长度。三份参考合计控制在
# 八千字以内,因此每个写作块(通常被调用两次以上)不至于因为输入过长而明显变慢。
REFERENCE_CHARS = 2500
# 完全信任模式下按原文逐行取用产品事实,上限只用于控制提示词长度;规格字段本身很短,
# 给太少会让可写的卖点不够用。
PRODUCT_FACT_LINES = 80


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


# 受众说法归一到固定角色词表；优先按角色取该型号的沟通卡，而不是靠路径字面重合。
ROLE_ALIASES = (
    ("企业采购", ("采购", "政企", "招标", "集团", "单位", "企业IT", "IT管理", "信息化")),
    ("管理者", ("管理者", "高管", "老板", "负责人", "决策人", "主管")),
    ("教师", ("教师", "老师", "教授", "讲师", "教研", "科研", "教学", "备课", "授课")),
    ("学生", ("学生", "新生", "大学生", "同学", "考研", "校园", "学习", "开学")),
    ("商务差旅", ("商务", "差旅", "出差", "移动办公", "移动商务")),
    ("内容创作者", ("创作", "设计", "视频", "剪辑", "摄影", "自媒体", "博主")),
    ("旧机换新", ("换新", "以旧换新", "旧机", "换代")),
    ("门店顾客", ("门店", "到店", "顾客", "导购", "体验店")),
)
# 标题或正文里出现这些词才算是可直接模仿的成稿写法。
COPY_HINTS = ("成稿", "任务卡", "话术", "范文", "样例", "示例")
# 带这些词的资料是任务说明/写作规则,归平台写法名额;否则就是人群场景资料。
GUIDE_HINTS = ("模板", "指南", "流程", "正例", "反例", "规则", "框架", "清单")


def norm_role(*texts: str) -> str:
    """从受众/场景描述里认出标准角色，认不出时返回空串。

    按传入顺序逐段判断:受众是写得最准的一段,先认出来就直接用,“开学季”这类场景词
    只在前面的字段都认不出时才参与,免得场景词盖过受众。
    """
    for text in texts:
        if not text:
            continue
        for role, words in ROLE_ALIASES:
            if any(word in text for word in words):
                return role
    return ""


def reference_excerpt(text: str, limit: int = REFERENCE_CHARS) -> str:
    """表达参考只需要正文,不需要迁移与核验声明;超长时按段落截断,不切在半句上。"""
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">")).strip()
    if len(body) <= limit:
        return body
    cut = body[:limit]
    stop = max(cut.rfind("\n"), cut.rfind("。"))
    return cut[:stop + 1] if stop > limit // 2 else cut


def suspicious_dimensions(text: str) -> list[dict]:
    """标记明显异常的笔记本尺寸，不猜测字段顺序。"""
    issues = []
    for name, low, high in (("厚度", 1, 50), ("长度", 100, 600)):
        match = re.search(rf"(?m)^{name}\s*\n\s*(\d+(?:\.\d+)?)\s*mm", text)
        if match and not low <= float(match[1]) <= high:
            issues.append({"code": "suspected_dimension_order", "detail": f"{name} {match[1]} mm 超出笔记本常见范围，仅提示复核，不认定正确值"})
    return issues


class CatalogStore:
    # 知识库默认高信任：内容审核只阻断确定错误，其余问题以警告呈现，不阻断创作。
    requires_strict_review = False
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.kb_name = self.root.name
        self._catalog = None
        self._catalog_hash = None

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
            return {"contract_version": 1, "review_max_calls": 5, "reference_limit": 2,
                    "image_limit": 8, "reference_slots": dict(DEFAULT_REFERENCE_SLOTS)}
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

    @staticmethod
    def _terms(text):
        return set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2}", text.lower()))

    def reference_slots(self, policy):
        """表达参考的分类名额;知识库没配置时用默认值,旧库仍可读旧的 reference_limit。"""
        slots = dict(DEFAULT_REFERENCE_SLOTS)
        configured = policy.get("reference_slots")
        if isinstance(configured, dict):
            for name in REFERENCE_BUCKETS:
                if name in configured:
                    slots[name] = max(0, int(configured[name]))
        elif "reference_limit" in policy:
            # 旧约定只有一个总名额,按平台优先分配给新结构。
            remaining = max(0, int(policy["reference_limit"]))
            for name in REFERENCE_BUCKETS:
                slots[name], remaining = min(slots[name], remaining), max(0, remaining - slots[name])
        return slots

    def _bucket(self, record, brief, comparison, commercial):
        """把候选资料归入 platform / copy / audience 三类之一;不属于任何一类返回空串。"""
        path = record["source"]["path"]
        if record.get("model_key") and norm_model(record["model_key"]) != norm_model(brief.product):
            return ""
        if record.get("platform") and record["platform"] != brief.platform:
            return ""
        title = Path(path).stem
        if record["kind"] == "platform_guide":
            return "platform"
        if record["kind"] == "role_guidance":
            return "audience"
        if record["category"] == "competitor":
            return "audience" if comparison else ""
        if record["category"] == "dynamic":
            return "audience" if (record["kind"] == "business_rule" and commercial) else ""
        if record["category"] == "products":
            return "copy" if record["kind"] == "product_guidance" else ""
        # 其余都是营销表达与人群场景的参考材料:标题里带成稿/任务卡/话术的算可直接模仿的写法,
        # 带模板/指南/流程/清单的算写作规则,剩下的才是人群场景资料。
        if record["category"] not in ("marketing", "scenarios"):
            return ""
        if any(word in title for word in COPY_HINTS):
            return "copy"
        return "platform" if any(word in title for word in GUIDE_HINTS) else "audience"

    def references(self, brief, slots=None):
        """按类型分名额取表达参考;某类没有资料时该名额留空,不用其他类型补位。"""
        slots = dict(slots or DEFAULT_REFERENCE_SLOTS)
        query = " ".join((brief.audience, brief.scene, brief.platform, brief.main_problem, brief.extra))
        terms = self._terms(query)
        role = norm_role(brief.audience, brief.scene, brief.main_problem, brief.extra)
        comparison = any(word in query for word in ("竞品", "对比", "比较", "横评", "相比", "替代", "苹果", "MacBook",
                                                     "联想", "ThinkPad", "ThinkBook", "戴尔", "Dell", "XPS", "惠普", "HP",
                                                     "华硕", "小米", "REDMI"))
        commercial = brief.goal == "促销告知" or any(word in query for word in
            ("价格", "报价", "优惠", "补贴", "库存", "赠品", "返利", "交付", "分期", "以旧换新"))
        ranked: dict[str, list[tuple]] = {name: [] for name in REFERENCE_BUCKETS}
        model_names = {norm_model(name) for name in self.models()}
        for r in self.records():
            bucket = self._bucket(r, brief, comparison, commercial)
            if not bucket or not slots.get(bucket):
                continue
            title = Path(r["source"]["path"]).stem
            # 打分看标题,但也参考文件所在的路由词(型号、行业、平台目录),只看标题会漏掉
            # “客户画像”“问题清单”这类通用文件名。
            score = len(terms & self._terms(r["source"]["path"]))
            if bucket == "audience" and role:
                # 沟通卡标题里带角色名(学生用户沟通卡);没有角色卡时别让不相干的角色卡占位。
                score += 100 if role in title else -100
            elif bucket == "copy" and r["kind"] == "reference_material" and brief.platform in title:
                score += 50           # 给了成稿写法就别再塞一份卖点清单
            score += 20 if r.get("platform") == brief.platform else 0
            own = norm_model(r.get("model_key") or "")
            if own and own == norm_model(brief.product):
                score += 15           # 本型号资料优先,但不至于压过真正对得上受众的沟通卡
            # 成稿/案例通常绑定其它型号;引用了库内其他型号的明显扣分,免得写成别家产品的稿子。
            names_in_path = {n for n in model_names if n and n in norm_model(r["source"]["path"])}
            if names_in_path - {norm_model(brief.product)}:
                score -= 40
            if not score:
                continue
            ranked[bucket].append((score, r["id"], r))
        picked = []
        for name in REFERENCE_BUCKETS:
            picked += [(name, r) for _, _, r in sorted(ranked[name], key=lambda v: (-v[0], v[1]))[:slots[name]]]
        return picked

    def collect(self, brief, progress=None):
        """完全信任知识库：只做确定性校验，不调用任何模型。

        保留的校验：型号归属、catalog 与文件哈希一致性、明显异常尺寸字段剔除、事实冲突检测。
        """
        from .evidence import ModelNotResolved, mentioned_models
        names = list(self.models())
        model = next((m for m in names if norm_model(m) == norm_model(brief.product)), None)
        if not model:
            raise ModelNotResolved(brief.product, similar(brief.product, names))
        ev = Evidence(candidates=similar(model, names), review_stats={"excluded": 0})
        policy = self.policy()

        def local_text(record):
            """读正文并剔除明显异常字段，不调用模型。"""
            text = self._raw(record).decode("utf-8-sig")
            problems = suspicious_dimensions(text)
            if problems:
                text = re.sub(r"(?ms)^## [^\n]*尺寸[^\n]*\n.*?(?=^## |\Z)", "", text)
            return text, [i["detail"] for i in problems]

        def extract_products(text):
            """把每个字段连同它所在的小节一起摘成原文。

            规格文件一行一个字段、字段名本身没有信息量（只写“厚度:14.5 mm”读者不知道是谁的），
            所以按“## 小节 + 字段”组成一条原文；来源、链接、页眉声明等非规格内容跳过。
            """
            out, heading, last = [], "", ""
            usable = re.compile(r"^[^\s>#].{1,200}$")
            for line in text.splitlines():
                raw = line.strip()
                if raw.startswith("## "):
                    heading = raw[3:].strip()
                    last = ""
                    continue
                if raw.startswith((">", "#", "*", "|")) or not raw:
                    last = ""
                    continue
                if not usable.match(raw) or "http" in raw or raw.endswith("："):
                    last = ""
                    continue
                value = re.sub(r"\s+", " ", f"{heading} {raw}".strip() if heading and heading not in raw else raw)
                if value == last or value in out:
                    continue
                if mentioned_models(value, names) - {model} or suspicious_dimensions(value):
                    continue
                out.append(value)
                last = value
            return out[:PRODUCT_FACT_LINES]

        core = [r for r in self.records(model) if r["kind"] == "product_facts"]
        for record in core:
            text, problems = local_text(record)
            ev.sources.append(record["path"])
            ev.originals.append(dict(path=record["path"], text=text, exclusions=problems))
            for fact_text in extract_products(text):
                ev.facts.append(Fact(f"F{len(ev.facts)+1}", model, fact_text,
                                     Path(record["path"]).name, record["path"], record["path"]))
            ev.dropped.extend(problems)
        if not ev.facts:
            raise KnowledgeError("知识库没有可用的产品事实")
        from .evidence import detect_conflicts
        ev.conflicts = detect_conflicts(ev.facts)
        slots = self.reference_slots(policy)
        if "KB_REFERENCE_LIMIT" in os.environ:
            # 旧的环境变量按总名额收紧:从人群场景名额开始扣,平台与成稿写法尽量保留。
            budget = max(0, int(os.environ["KB_REFERENCE_LIMIT"]))
            for name in ("audience", "copy", "platform"):
                slots[name] = min(slots[name], budget)
                budget -= slots[name]
        image_limit = max(0, min(12, int(os.environ.get("KB_IMAGE_LIMIT", policy["image_limit"]))))
        for bucket, record in self.references(brief, slots=slots):
            # 表达参考只影响语气与结构，不提供产品事实，直接取正文。
            text, problems = local_text(record)
            ev.context.append(dict(path=record["path"], kind=record["kind"], bucket=bucket,
                                   quotes=[reference_excerpt(text)],
                                   authority="表达与场景参考，不是产品事实"))
            ev.dropped.extend(problems)

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
