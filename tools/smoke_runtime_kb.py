"""新库轻量实测：首次文字资料复核与写作；总计最多2个真实HTTP请求。"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import compose, compliance, fact_check, strategy
from agent.catalogkb import CatalogStore, DEFAULT_RUNTIME_KB
from agent.envfile import load_dotenv
from agent.model_routing import make_llm
from agent.models import Brief
from agent.prompts import Prompts


def main():
    if "--run" not in sys.argv:
        print("--run 才执行。最多2次：文字资料复核1、写作1。图片不调用视觉模型；复用缓存检查不另发请求。")
        return 0
    load_dotenv()
    report_path = ROOT / ".local/knowledge-check.json"
    if report_path.exists() and "--resume" not in sys.argv:
        print("已有测试记录，停止重复调用：" + str(report_path))
        return 1
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {"max_requests": 2, "requests_started": 0, "completed": False}
    if report["requests_started"] >= 2:
        print("本轮已到2次上限，不再调用。")
        return 1
    if report.get("error"):
        report.setdefault("prior_errors", []).append(report.pop("error"))
    def save():
        report_path.parent.mkdir(exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    router = make_llm()
    prompts = Prompts.load()
    store = CatalogStore(DEFAULT_RUNTIME_KB)
    brief = Brief(product="HUAWEI MateBook 14", audience="大学新生", scene="开学季", platform="朋友圈",
                  goal="种草", stage="了解产品", main_problem="外观和日常学习", extra="只介绍有依据的特点，语气自然")
    os.environ.update(KB_REFERENCE_LIMIT="0", KB_IMAGE_LIMIT="1")
    save()
    try:
        ev = store.collect(brief, lambda title, detail: print(title + "：" + detail, flush=True))
        report["first_use"] = {"facts": len(ev.facts), "images": len(ev.images), "stats": ev.review_stats,
                               "image_review_disabled": True, "excluded": ev.dropped}
        before = report["requests_started"]
        again = store.collect(brief)
        # 完全信任知识库:知识库阶段不发起任何模型请求。
        report["no_model_requests"] = before == report["requests_started"]
        if not report["no_model_requests"] or len(again.facts) != len(ev.facts):
            raise RuntimeError("知识库阶段发起了模型请求或结果不一致")
        if "--skip-compose" in sys.argv:
            report["scope"] = "文字资料复核、登记图片读取与缓存；写作仅离线验证，未调用"
            report["completed"] = bool(ev.facts)
        else:
            plan = strategy.match(brief)
            draft, refusals = compose.generate(router.for_stage("compose"), prompts, brief, ev, plan)
            report["draft"] = json.loads(compose.draft_to_json(draft))
            report["refusals"] = refusals
            report["rule_issues"] = [i.__dict__ for i in fact_check.rule_check(draft, ev, brief, plan) + compliance.rule_check(draft, ev)]
            report["completed"] = bool(draft.body) and not any(i["severity"] == "error" for i in report["rule_issues"])
    except Exception as exc:
        report["error"] = str(exc)
    report["requests"] = report.get("requests", []) + [row for client in router.clients.values() for row in client.requests]
    save()
    print(json.dumps({k: v for k, v in report.items() if k in ("completed", "requests_started", "error", "cache_reused_without_requests", "rule_issues")}, ensure_ascii=False, indent=2))
    print("报告：" + str(report_path))
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
