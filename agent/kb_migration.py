"""从原库构建独立的精简候选快照；不接入生成流水线，不调用网络或大模型。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .evidence import parse_image_index, same_product
from .localkb import LocalStore
from .knowledge import KnowledgeError


CATEGORIES = {
    "products": "01-产品事实", "ecosystem": "02-生态协同", "scenarios": "03-人群场景",
    "marketing": "04-营销表达", "images": "05-图片素材", "dynamic": "06-动态信息",
}
INDEX = "00-规范与索引"
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
URL = re.compile(r"https?://[^\s<>`\"）)]+")
LINK = re.compile(r"(!?\[[^\]\n]*\])\((<[^>\n]+>|[^)\n]+)\)")


class MigrationError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    """拒绝绝对路径、目录穿越和指向库外的链接。"""
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise MigrationError(f"不安全的相对路径：{relative}")
    path = (root / rel).resolve()
    if not path.is_relative_to(root.resolve()):
        raise MigrationError(f"路径越出知识库：{relative}")
    return path


def _read(root: Path, relative: str) -> tuple[bytes, str]:
    raw = safe_path(root, relative).read_bytes()
    return raw, raw.decode("utf-8-sig")


def _json(data) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _heading_name(title: str) -> str:
    return re.sub(r"^[一二三四五六七八九十百\d]+[、.．]\s*", "", title).strip()


def select_sections(text: str, sections=(), prefixes=()) -> tuple[str, list[dict]]:
    """按明确标题保留完整章节及祖先标题，返回一基、闭区间的原始行号。"""
    lines = text.splitlines(keepends=True)
    if not sections and not prefixes:
        return text, [{"start": 1, "end": len(lines)}]
    headings = [(i, len(m[1]), m[2]) for i, line in enumerate(lines) if (m := HEADING.match(line))]
    intervals = []
    matched = set()
    for n, (start, level, title) in enumerate(headings):
        name = _heading_name(title)
        keys = [key for key in sections if name == key]
        keys += [key for key in prefixes if name.startswith(key)]
        if not keys:
            continue
        matched.update(keys)
        end = next((i for i, depth, _ in headings[n + 1:] if depth <= level), len(lines))
        intervals.append((start, end))
        # 单独摘录第三级标题时保留它的原父标题，避免移入错误章节。
        depth = level
        for parent, parent_depth, _ in reversed(headings[:n]):
            if parent_depth < depth:
                intervals.append((parent, parent + 1))
                depth = parent_depth
    missing = set(sections) | set(prefixes)
    missing -= matched
    if missing:
        raise MigrationError(f"原文缺少指定章节：{'、'.join(sorted(missing))}")
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    spans = [{"start": start + 1, "end": end} for start, end in merged]
    return "\n".join("".join(lines[a:b]) for a, b in merged), spans


def suspicious_dimensions(text: str) -> list[dict]:
    """只标记明显需要复核的笔记本尺寸，不猜测或自动修正字段顺序。"""
    issues = []
    for name, low, high in (("厚度", 1, 50), ("长度", 100, 600)):
        match = re.search(rf"(?m)^{name}\s*\n\s*(\d+(?:\.\d+)?)\s*mm", text)
        if match and not low <= float(match[1]) <= high:
            issues.append({"code": "suspected_dimension_order", "detail": f"{name} {match[1]} mm 超出笔记本常见范围，仅提示复核，不认定正确值"})
    return issues


def _official(url: str, domains: list[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def _rebase_links(text: str, source_root: Path, relative: str, target: Path) -> tuple[str, list[dict]]:
    issues = []

    def replace(match):
        raw = match[2].strip()
        link = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
        parts = urlsplit(link)
        if parts.scheme or parts.netloc or link.startswith("#"):
            return match[0]
        dest = (source_root / relative).parent / unquote(parts.path)
        resolved = dest.resolve()
        if not resolved.is_relative_to(source_root) or not resolved.exists():
            issues.append({"code": "unresolved_local_link", "detail": f"原文链接需复核：{link}"})
            return match[1] + f"（原库引用：`{link}`）"
        rebased = Path(os.path.relpath(resolved, target.parent)).as_posix()
        if parts.fragment:
            rebased += "#" + parts.fragment
        return f"{match[1]}(<{rebased}>)"

    return LINK.sub(replace, text), issues


def inventory(source: Path) -> list[dict]:
    records = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise MigrationError(f"原库含符号链接，需先人工确认：{path.relative_to(source)}")
        if path.is_file():
            raw = path.read_bytes()
            records.append({"path": path.relative_to(source).as_posix(), "sha256": digest(raw), "bytes": len(raw)})
    return records


def _excluded_reason(path: str) -> str:
    if path.startswith(("P3-", "P4-", "P5-", "90-", "99-")):
        return "本轮不纳入竞品、机密、培训、待审核或归档资料"
    if "/07-扩展产品目录/" in path:
        return "扩展产品仅作为未来补库线索，尚未建立逐型号事实审核"
    if path.startswith("P6-"):
        return "保留方法与边界；本轮不复制成稿、第三方案例和重复选题"
    return "未命中本版明确选择规则；原库文件保留不动"


def build_snapshot(source: Path | str, output: Path | str, policy: dict) -> dict:
    source, output = Path(source).resolve(), Path(output).resolve()
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise MigrationError("目标必须在原库以外，且不能是原库的祖先目录")
    if output.exists():
        raise MigrationError("目标已存在，拒绝覆盖；请使用新的版本目录，或使用 --validate 只读检查")
    store = LocalStore(source)
    if not store.connect()["connected"]:
        raise MigrationError("原库不是有效的本地 P1 产品库")
    before = inventory(source)
    source_map = {r["path"]: r for r in before}
    records, payloads, selected_sources = [], {}, set()
    models = store.models()
    if not models:
        raise MigrationError("原库没有可提取的产品事实")
    official_names = {name: store.official(name) for name in models}
    policies = []

    def add_bytes(path: str, data: bytes):
        if path in payloads:
            raise MigrationError(f"输出路径重复：{path}")
        safe_path(output, path)
        payloads[path] = data

    def add_document(relative, category, kind, sections=(), prefixes=(), model=None, platform=None):
        if relative.split("/")[0].startswith(("P3-", "P4-", "P5-", "90-", "99-")):
            raise MigrationError(f"禁止从此板块提取：{relative}")
        raw, text = _read(source, relative)
        selected, spans = select_sections(text, sections, prefixes)
        parent = Path(relative).parent.name
        filename = Path(relative).name
        if category == "products" and kind in ("product_facts", "product_guidance"):
            path = f"{CATEGORIES[category]}/{model}/{filename}"
        elif kind == "role_guidance":
            path = f"{CATEGORIES[category]}/按型号/{model}/{filename}"
        elif category == "scenarios":
            path = f"{CATEGORIES[category]}/{parent}/{filename}"
        elif kind in ("reference_material", "product_discovery", "support_reference_full"):
            path = f"{CATEGORIES[category]}/补充资料/{digest(relative.encode('utf-8'))[:10]}-{filename}"
        else:
            path = f"{CATEGORIES[category]}/{filename}"
        rebased, link_issues = _rebase_links(selected, source, relative, output / path)
        notice = "> 迁移候选，尚未完成人工事实复核。原文中的核验声明仅代表原库记录。\n\n"
        add_bytes(path, (notice + rebased).encode("utf-8"))
        urls = sorted(set(url.rstrip("，。；;") for url in URL.findall(text)))
        issues = list(link_issues)
        if kind == "product_facts":
            issues += suspicious_dimensions(text)
        if kind in ("product_facts", "support_reference", "support_reference_full"):
            if not any(_official(url, policy["official_domains"]) for url in urls):
                issues.append({"code": "missing_official_source", "detail": "未发现白名单中的官方来源 URL"})
        if kind == "platform_guide":
            issues.append({"code": "platform_rules_require_alignment", "detail": "原模板的篇幅和配图数量不替代 config/platforms.json；只提取写法，不导入数值规则"})
        system = re.search(r"(?ms)^## 操作系统\s*\n(.*?)(?=^## |\Z)", text)
        record = {
            "id": "DOC-" + digest(relative.encode("utf-8"))[:16],
            "category": category, "kind": kind, "path": path,
            "model_key": model, "official_product": official_names.get(model),
            "platform": platform, "system_scope_raw": system[1].strip() if system else None,
            "sku_scope": None, "review_status": "pending_review", "publishable": False,
            "live_verified_at": None, "issues": issues,
            "source": {"path": relative, "sha256": digest(raw), "line_spans": spans,
                       "urls": urls, "dates_in_source": sorted(set(re.findall(r"20\d{2}-\d{2}-\d{2}", text)))},
            "selection": {"sections": list(sections), "prefixes": list(prefixes), "sha256": digest(selected.encode("utf-8"))},
            "sha256": digest(payloads[path]),
        }
        records.append(record)
        selected_sources.add(relative)

    for model, info in models.items():
        for filename in policy["product_documents"]:
            relative = f"{info['path']}/{filename}"
            if relative not in source_map:
                raise MigrationError(f"核心型号缺少必须保留的文档：{relative}")
            kind = "product_facts" if filename == "产品事实.md" else "product_guidance"
            add_document(relative, "products", kind, model=model)

    for spec in policy["documents"]:
        matches = sorted(source.glob(spec["glob"]))
        if spec.get("parent_prefixes"):
            matches = [p for p in matches if any(p.parent.name.startswith(prefix) for prefix in spec["parent_prefixes"])]
        if not matches:
            raise MigrationError(f"选择规则没有命中资料：{spec['glob']}")
        for path in matches:
            model = path.parent.name if spec.get("model_from_parent") else None
            if model and model not in models:
                raise MigrationError(f"角色资料无法对应本版型号：{path}")
            add_document(
                path.relative_to(source).as_posix(), spec["category"], spec["kind"],
                spec.get("sections", []), spec.get("prefixes", []), model, spec.get("platform"),
            )

    from PIL import Image
    for model, info in models.items():
        index_path = f"{info['path']}/图片索引.csv"
        raw_index, index_text = _read(source, index_path)
        rows = parse_image_index(index_text)
        valid = [image for image in store.images(model, limit=10_000) if image.model == model]
        for image in valid[:policy["max_images_per_product"]]:
            raw = safe_path(source, image.source_path).read_bytes()
            with Image.open(io.BytesIO(raw)) as picture:
                width, height = picture.size
                picture.verify()
            path = f"{CATEGORIES['images']}/{model}/{image.name}"
            add_bytes(path, raw)
            row = rows[image.name]
            issues = []
            for key, value in (("宽度", width), ("高度", height)):
                if row.get(key) and int(row[key]) != value:
                    issues.append({"code": "image_size_mismatch", "detail": f"索引{key}与实际图片不一致"})
            records.append({
                "id": "IMG-" + digest(image.source_path.encode("utf-8"))[:16],
                "category": "images", "kind": "registered_image", "path": path,
                "model_key": model, "official_product": official_names[model],
                "review_status": "pending_review", "publishable": False, "live_verified_at": None,
                "width": width, "height": height, "issues": issues, "sha256": digest(raw),
                "source": {"path": image.source_path, "sha256": digest(raw), "index_path": index_path,
                           "index_sha256": digest(raw_index), "index_row": row},
            })
            selected_sources.update((image.source_path, index_path))

    # 额外商城/公共素材仅按原索引精确型号收录，泛称与未登记图不猜测归属。
    for pattern in policy.get("additional_image_indices", []):
        for index_file in sorted(source.glob(pattern)):
            index_path = index_file.relative_to(source).as_posix()
            raw_index, index_text = _read(source, index_path)
            rows = list(csv.DictReader(io.StringIO(index_text.lstrip(chr(0xFEFF)))))
            if any(not row.get("文件名") for row in rows) or len({row["文件名"] for row in rows}) != len(rows):
                raise MigrationError("补充图片索引缺少文件名或重复登记")
            for row in rows:
                filename = row["文件名"]
                model = next((m for m in models if same_product(row.get("产品型号", ""), m, official_names[m])), None)
                original_path = (index_file.parent / filename).relative_to(source).as_posix()
                if original_path in selected_sources:
                    continue
                raw = safe_path(source, original_path).read_bytes()
                with Image.open(io.BytesIO(raw)) as picture:
                    width, height = picture.size
                    picture.verify()
                path = f"{CATEGORIES['images']}/{model or '归属待确认'}/{digest(original_path.encode('utf-8'))[:8]}-{Path(filename).name}"
                add_bytes(path, raw)
                records.append({"id": "IMG-" + digest(original_path.encode('utf-8'))[:16],
                    "category": "images", "kind": "registered_image" if model else "unassigned_image", "path": path, "model_key": model,
                    "official_product": official_names.get(model), "review_status": "pending_review", "publishable": False,
                    "live_verified_at": None, "width": width, "height": height,
                    "issues": [] if model else [{"code": "unknown_image_model", "detail": "原索引无精确型号，保留追溯，不加入自动配图"}], "sha256": digest(raw),
                    "source": {"path": original_path, "sha256": digest(raw), "index_path": index_path,
                               "index_sha256": digest(raw_index), "index_row": row}})
                selected_sources.update((original_path, index_path))

    price_source = policy.get("price_snapshot")
    if price_source:
        raw, text = _read(source, price_source)
        for row_number, row in enumerate(csv.DictReader(io.StringIO(text)), 1):
            name = row.get("规范产品名称", "")
            model = next((m for m in models if same_product(name, m, official_names[m])), None)
            if model is None:
                continue
            record_id = "PRICE-" + digest((price_source + str(row_number)).encode("utf-8"))[:16]
            path = f"{CATEGORIES['dynamic']}/{record_id}.json"
            data = _json(row)
            add_bytes(path, data)
            records.append({
                "id": record_id, "category": "dynamic", "kind": "historical_price_snapshot",
                "path": path, "model_key": model, "official_product": official_names[model],
                "review_status": "historical_only", "publishable": False, "live_verified_at": None,
                "observed_at_in_source": row.get("核验时间") or None, "expires_at": None,
                "issues": [{"code": "requires_live_recheck", "detail": "历史价格或状态不得作为当前承诺；需重新核对 SKU、地区、渠道、资格和有效期"}],
                "sha256": digest(data),
                "source": {"path": price_source, "sha256": digest(raw), "csv_record": row_number},
            })
        selected_sources.add(price_source)

    fact_records = {r["model_key"]: r for r in records if r["kind"] == "product_facts"}
    for record in records:
        fact = fact_records.get(record.get("model_key"))
        if fact and record["kind"] != "product_facts":
            record["depends_on"] = [fact["id"]]
            if fact["issues"]:
                record["issues"].append({"code": "product_source_requires_review", "detail": "对应产品事实存在待复核问题，不得通过衍生话术绕过"})

    for path in policy.get("governance_sources", []):
        raw, _ = _read(source, path)
        policies.append({"path": path, "sha256": digest(raw)})
    for item in before:
        item["selected"] = item["path"] in selected_sources
        item["reason"] = "保留全部或指定章节/记录/登记图片" if item["selected"] else _excluded_reason(item["path"])
    # 构建期间原库发生变化时不写出一个来源不一致的版本。
    after = inventory(source)
    if [{k: r[k] for k in ("path", "sha256", "bytes")} for r in before] != after:
        raise MigrationError("构建期间原库发生变化，请确认资料稳定后重试")

    created_at = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    catalog = {
        "version": 1, "created_at": created_at, "state": "candidate",
        "ready_for_runtime": False, "source_library": source.name,
        "runtime_default_changed": False, "official_source_check": "URL domain only, not live content verification",
        "models": [{"model_key": m, "official_product": official_names[m]} for m in models],
        "governance_sources": policies, "records": records,
    }
    inv_data, policy_data, catalog_data = _json(before), _json(policy), _json(catalog)
    add_bytes(f"{INDEX}/source-inventory.json", inv_data)
    add_bytes(f"{INDEX}/selection-policy.json", policy_data)
    add_bytes(f"{INDEX}/catalog.json", catalog_data)
    pending = [{"id": r["id"], "path": r["path"], "issues": r["issues"]} for r in records if r["issues"]]
    add_bytes(f"{INDEX}/待复核清单.json", _json(pending))
    build_info = {
        "catalog_sha256": digest(catalog_data), "inventory_sha256": digest(inv_data),
        "policy_sha256": digest(policy_data), "content_file_count": len(records),
    }
    add_bytes(f"{INDEX}/build.json", _json(build_info))
    summary = {
        "source_file_count": len(before), "selected_source_files": len(selected_sources),
        "records": len(records), "model_count": len(models),
        "by_category": dict(Counter(r["category"] for r in records)),
        "records_with_review_issues": len(pending), "ready_for_runtime": False,
    }
    review_lines = ["# 待复核问题", "", "全部资料仍需人工复核，以下列出自动发现的重点问题。", ""]
    for item in pending:
        review_lines.append(f"- `{item['path']}`：" + "；".join(i["detail"] for i in item["issues"]))
    add_bytes(f"{INDEX}/待复核清单.md", "\n".join(review_lines).encode("utf-8"))
    readme = [
        "# 华为营销精简本地知识库 第一版候选", "",
        "此库从现有原库提取，不是重新核验后的事实发布版。当前 Agent 继续使用原库。", "",
        "分类与边界见项目 `docs/knowledge-runtime.md`。来源、行号、哈希和审核状态见 `00-规范与索引/catalog.json`。", "",
        "产品事实保留完整原文；营销与画像只帮助理解和表达；生态支持资料不证明所有设备均兼容；价格全部是历史快照。", "",
    ]
    for key, count in summary["by_category"].items():
        readme.append(f"- {CATEGORIES[key]}：{count} 条")
    readme.extend(["", f"原库 {len(before)} 个文件，本版 {len(records)} 条资料；默认不接入生产。", ""])
    add_bytes("README.md", "\n".join(readme).encode("utf-8"))
    output.mkdir(parents=True)
    for relative, data in payloads.items():
        path = safe_path(output, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    result = validate_snapshot(source, output)
    report = {**summary, **result}
    (output / INDEX / "验收报告.json").write_bytes(_json(report))
    lines = ["# 第一版提取验收报告", "", "本报告只检查本地迁移完整性，不代表官网事实已复核。", ""]
    lines.extend(f"- {key}：{value}" for key, value in report.items() if key not in ("errors", "by_category"))
    lines.extend(["", "## 分类统计", ""])
    lines.extend(f"- {CATEGORIES[k]}：{v}" for k, v in summary["by_category"].items())
    lines.extend(["", "## 切换条件", "", "需完成人工事实复核、生态兼容核对、图片授权确认、动态信息时效检查和检索评测；当前不切换运行库。", ""])
    lines.extend(f"- {err}" for err in result["errors"])
    (output / INDEX / "验收报告.md").write_text("\n".join(lines), encoding="utf-8")
    if not result["structural_checks_passed"]:
        raise MigrationError(f"候选库生成，但校验失败：{result['errors'][:3]}")
    return report


def validate_snapshot(source: Path | str, output: Path | str) -> dict:
    """独立读取产物，重新比对原库、摘录、价格行、图片与输出哈希。"""
    source, output = Path(source).resolve(), Path(output).resolve()
    errors = []
    checked = 0
    try:
        build = json.loads((output / INDEX / "build.json").read_text(encoding="utf-8"))
        for name, key in (("catalog.json", "catalog_sha256"), ("source-inventory.json", "inventory_sha256"), ("selection-policy.json", "policy_sha256")):
            if digest((output / INDEX / name).read_bytes()) != build[key]:
                errors.append(f"元数据哈希不一致：{name}")
        catalog = json.loads((output / INDEX / "catalog.json").read_text(encoding="utf-8"))
        inv = json.loads((output / INDEX / "source-inventory.json").read_text(encoding="utf-8"))
        if catalog.get("ready_for_runtime") is not False or catalog.get("runtime_default_changed") is not False:
            errors.append("候选库不得自动标为运行就绪")
        current = {r["path"]: r for r in inventory(source)}
        if set(current) != {r["path"] for r in inv}:
            errors.append("原库文件集合已发生变化")
        for old in inv:
            if current.get(old["path"], {}).get("sha256") != old["sha256"]:
                errors.append(f"原库文件发生变化：{old['path']}")
        ids = [r["id"] for r in catalog["records"]]
        if len(ids) != len(set(ids)):
            errors.append("资料 ID 重复")
        for record in catalog["records"]:
            checked += 1
            try:
                path = safe_path(output, record["path"])
                payload = path.read_bytes()
                if digest(payload) != record["sha256"]:
                    errors.append(f"输出文件哈希不一致：{record['path']}")
                origin = record["source"]
                raw = safe_path(source, origin["path"]).read_bytes()
                if digest(raw) != origin["sha256"]:
                    errors.append(f"来源哈希不一致：{record['id']}")
                if record.get("publishable") is not False or record.get("live_verified_at") is not None:
                    errors.append(f"未经独立审核不能标记可发布：{record['id']}")
                if record["kind"] in ("registered_image", "unassigned_image"):
                    index_raw, index_text = _read(source, origin["index_path"])
                    row = next((r for r in csv.DictReader(io.StringIO(index_text.lstrip(chr(0xFEFF))))
                                if r.get("文件名") == Path(origin["path"]).name), None)
                    if digest(index_raw) != origin["index_sha256"] or row != origin["index_row"]:
                        errors.append(f"图片索引已变化：{record['id']}")
                    if not row or (record["kind"] == "registered_image" and not same_product(row.get("产品型号", ""), record["model_key"], record["official_product"])):
                        errors.append(f"图片型号不匹配：{record['id']}")
                    if raw != payload:
                        errors.append(f"图片不是原文件：{record['id']}")
                elif record["kind"] == "historical_price_snapshot":
                    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
                    if json.loads(payload) != rows[origin["csv_record"] - 1]:
                        errors.append(f"价格记录与原 CSV 不同：{record['id']}")
                    if record["review_status"] != "historical_only":
                        errors.append(f"价格记录必须标记为历史：{record['id']}")
                else:
                    text = raw.decode("utf-8-sig")
                    selected, spans = select_sections(text, **{k: record["selection"][k] for k in ("sections", "prefixes")})
                    if spans != origin["line_spans"] or digest(selected.encode("utf-8")) != record["selection"]["sha256"]:
                        errors.append(f"摘录位置或内容不一致：{record['id']}")
                    rebuilt, _ = _rebase_links(selected, source, origin["path"], path)
                    expected = "> 迁移候选，尚未完成人工事实复核。原文中的核验声明仅代表原库记录。\n\n" + rebuilt
                    if payload.decode("utf-8") != expected:
                        errors.append(f"输出不等于原文摘录：{record['id']}")
            except (OSError, KeyError, ValueError, IndexError, KnowledgeError) as exc:
                errors.append(f"{record.get('id')}：{exc}")
    except (OSError, KeyError, ValueError) as exc:
        errors.append(f"无法读取快照索引：{exc}")
    return {"structural_checks_passed": not errors, "checked_records": checked, "errors": errors, "fact_review_completed": False}
