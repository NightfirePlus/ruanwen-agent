"""逐字核对生态能力卡的官网引文，并可标记为已完成官网来源核验。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.catalogkb import DEFAULT_RUNTIME_KB, INDEX, sha
from agent.knowledge_verification import fetch_official_text

URL = re.compile(r"https://consumer\.huawei\.com/[^\s）)]+/")
ROW = re.compile(r'^\|\s*["“](.+?)["”]\s*\|\s*(.*?)\s*\|\s*$')


def normalized(value: str) -> str:
    value = re.sub(r"\s+", "", value)
    return value.translate(str.maketrans("", "", "\\\"'“”‘’"))


def card_quotes(path: Path) -> list[dict]:
    rows = []
    last_url = ""
    in_evidence = False
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if line.startswith("## 官方证据"):
            in_evidence = True
            continue
        if in_evidence and line.startswith("## "):
            break
        if not in_evidence:
            continue
        match = ROW.match(line)
        if not match:
            continue
        source_urls = URL.findall(match.group(2))
        if source_urls:
            last_url = source_urls[-1]
        if not last_url:
            rows.append({"line": number, "quote": match.group(1), "url": "", "passed": False})
        else:
            rows.append({"line": number, "quote": match.group(1), "url": last_url})
    return rows


def review(root: Path, workers: int = 4) -> dict:
    cards = sorted((root / "02-生态协同" / "能力卡").glob("ECO-CAP-*.md"))
    entries = {str(path.relative_to(root)).replace("\\", "/"): card_quotes(path) for path in cards}
    urls = sorted({row["url"] for rows in entries.values() for row in rows if row.get("url")})
    pages, failures = {}, {}
    with ThreadPoolExecutor(max_workers=max(1, min(8, workers))) as pool:
        jobs = {pool.submit(fetch_official_text, url, ["huawei.com"]): url for url in urls}
        for future in as_completed(jobs):
            url = jobs[future]
            try:
                final_url, text = future.result()
                pages[url] = {"final_url": final_url, "text": normalized(text)}
            except Exception as exc:
                failures[url] = f"{type(exc).__name__}: {exc}"
    checked = passed = 0
    card_results = []
    for path, rows in entries.items():
        for row in rows:
            checked += 1
            page = pages.get(row.get("url", ""))
            row["passed"] = bool(page and normalized(row["quote"]) in page["text"])
            row["final_url"] = page["final_url"] if page else ""
            passed += int(row["passed"])
        card_results.append({
            "path": path,
            "passed": bool(rows) and all(row["passed"] for row in rows),
            "quotes": rows,
        })
    return {
        "checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "cards": len(cards), "urls": len(urls), "quotes": checked,
        "passed_quotes": passed, "failed_urls": failures,
        "all_passed": len(cards) > 0 and passed == checked and not failures,
        "results": card_results,
    }


def mark_source_verified(root: Path, report: dict) -> None:
    if not report.get("all_passed"):
        raise SystemExit("存在未匹配引文或官网读取失败，不能标记通过")
    checked_at = report["checked_at"]
    date = checked_at[:10]
    result_paths = {row["path"] for row in report["results"] if row["passed"]}
    catalog_path = root / INDEX / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    for relative in sorted(result_paths):
        path = root / relative
        text = path.read_text(encoding="utf-8-sig")
        text = re.sub(
            r"^> 生态协同能力卡。状态：待人工事实复核（.*?）。$",
            f"> 生态协同能力卡。状态：官网引文逐字核验通过（{date}；不替代人工发布批准）。",
            text, count=1, flags=re.M,
        )
        text = re.sub(
            r"^- \*\*复核状态\*\*：pending_review（待人工复核）$",
            "- **复核状态**：source_verified（官网引文逐字核验通过；待人工发布批准）",
            text, count=1, flags=re.M,
        )
        marker = f"- **官网自动核验时间**：{checked_at}"
        if marker not in text:
            text = text.replace(
                "- **复核状态**：source_verified（官网引文逐字核验通过；待人工发布批准）",
                "- **复核状态**：source_verified（官网引文逐字核验通过；待人工发布批准）\n" + marker,
                1,
            )
        path.write_bytes(text.encode("utf-8"))
        record = next(row for row in catalog["records"] if row["path"] == relative)
        record["sha256"] = sha(path.read_bytes())
        record["review_status"] = "source_verified"
        record["publishable"] = False
        record["live_verified_at"] = checked_at
        if "lifecycle" in record:
            record["lifecycle"]["last_verified_at"] = checked_at
            record["lifecycle"]["expires_at"] = (
                datetime.fromisoformat(checked_at) + timedelta(days=45)
            ).isoformat(timespec="seconds")
    catalog["metadata_updated_at"] = checked_at
    catalog_path.write_bytes((json.dumps(catalog, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb", type=Path, default=DEFAULT_RUNTIME_KB)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--report", type=Path, default=ROOT / ".local" / "capability-card-review.json")
    parser.add_argument("--mark-source-verified", action="store_true")
    args = parser.parse_args()
    report = review(args.kb, args.workers)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.mark_source_verified:
        mark_source_verified(args.kb, report)
    print(json.dumps({
        key: report[key] for key in
        ("checked_at", "cards", "urls", "quotes", "passed_quotes", "all_passed", "failed_urls")
    }, ensure_ascii=False, indent=2))
    print("报告：" + str(args.report))
    raise SystemExit(0 if report["all_passed"] else 1)


if __name__ == "__main__":
    main()
