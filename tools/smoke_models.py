"""当前模型配置的新分块链路抽查；默认只显示计划，--run 才调用。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import chunk_review, compose, intake, planning, strategy
from agent.envfile import load_dotenv
from agent.llm import LLMError, RequestBudget
from agent.model_routing import make_llm
from agent.models import Brief, ContentBlock, Draft, Evidence, Fact, Paragraph
from agent.norm import norm_model
from agent.prompts import Prompts

REPORT_DIR = ROOT / ".local"
# 故意构造的测试资料，不作为华为产品资料或生产事实。
DOC = "接口测试样例，不是实际产品参数。测试机型A采用16GB内存。测试机型A支持触控，仅触控版具备此功能。测试机型B采用32GB内存。"


def cases():
    prompts = Prompts.load()
    brief = Brief(product="测试机型A", audience="大学新生", scene="开学季", platform="朋友圈", goal="种草")
    ev = Evidence(facts=[Fact("F1", "测试机型A", "测试机型A采用16GB内存。", "构造样例"),
                         Fact("F2", "测试机型A", "测试机型A支持触控，仅触控版具备此功能。", "构造样例")])
    class Capture:
        def json(self, user, system=None, temperature=0.2):
            self.user, self.system = user, system
            return {}
    captured_intake, captured_plan, captured_chunk = Capture(), Capture(), Capture()
    intake.extract(captured_intake, prompts, Brief(), [{"role": "用户", "content":
        "开学季，给大学新生写一条华为 MateBook 14 的朋友圈，目标是种草，重点讲携带方便。"}])
    matched = strategy.match(brief)
    plan = planning.create(captured_plan, prompts, "产品：测试机型A；目标受众：大学新生；发布平台：朋友圈。", ev, matched)
    compose.generate_chunk(captured_chunk, prompts, "产品：测试机型A；目标受众：大学新生；发布平台：朋友圈。",
                           brief, ev, matched, plan, plan.blocks[0], [])
    def valid_intake(data):
        b = Brief(**{k: v for k, v in data.items() if k in Brief.__dataclass_fields__ and isinstance(v, str)})
        b = intake.normalize(b)
        return (norm_model(b.product) == norm_model("MateBook 14") and
                all(getattr(b, k) == v for k, v in {"audience": "大学新生",
                    "platform": "朋友圈", "scene": "开学季", "goal": "种草"}.items()))
    def valid_evidence(data):
        facts = data.get("facts", [])
        return (bool(facts) and all(isinstance(f, dict) and bool(f.get("text")) and f["text"] in DOC
                                   and "机型B" not in f["text"] for f in facts)
                and any("仅触控版" in f["text"] for f in facts)
                and data.get("conflicts") == [])
    def valid_plan(data):
        blocks = data.get("blocks", [])
        kinds = {item.get("kind") for item in blocks if isinstance(item, dict)}
        allowed = {fact.id for fact in ev.facts}
        return (3 <= len(blocks) <= 8 and {"opening", "body", "closing"} <= kinds
                and all(set(item.get("fact_ids", [])) <= allowed for item in blocks if isinstance(item, dict)))
    def valid_chunk(data):
        if not isinstance(data, dict):
            return False
        try:
            draft, _ = compose.parse_draft(data)
            # 生产链路会在审核前确定性裁掉模型越权返回的其他结构字段。
            fragment = compose.restrict_to_block(
                draft, ContentBlock("B1", "opening", "形成标题和导语")
            )
        except (TypeError, ValueError, KeyError):
            return False
        return bool(fragment.title and fragment.intro and not fragment.body
                    and not fragment.cta and not fragment.tags and not fragment.images)
    def valid_review(data):
        return bool(chunk_review.parse_llm_issues(data))
    return [
        ("intake", captured_intake.user, captured_intake.system, valid_intake),
        ("content_plan", captured_plan.user, captured_plan.system, valid_plan),
        ("chunk_generate", captured_chunk.user, captured_chunk.system, valid_chunk),
        ("chunk_review", prompts.render("chunk_review", {"规范需求": "产品：测试机型A；目标受众：大学新生；发布平台：朋友圈。",
            "块约束": '{"id":"B1","kind":"body","fact_ids":["F2"]}',
            "当前块稿件": compose.draft_to_json(
            Draft(title="所有配置均支持触控", body=[Paragraph("测试机型A全系标配触控。", ["F2"])])),
            "事实点清单": compose.facts_block(ev), "原始资料": "[]", "图片登记": "(无)"}), None, valid_review),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="实际执行，最多4次HTTP请求，失败立即停止")
    parser.add_argument("--resume", action="store_true", help="复核已保存响应，只续跑未调用阶段，总计仍不超过4次")
    parser.add_argument("--profile", choices=("single", "llm-multi-v1"),
                        help="临时覆盖 .env 中的 LLM_PROFILE")
    args = parser.parse_args()
    load_dotenv()
    if args.profile:
        os.environ["LLM_PROFILE"] = args.profile
    profile = os.environ.get("LLM_PROFILE", "").strip()
    if not profile:
        profile = ("llm-multi-v1" if os.environ.get("LLM_API_KEY", "").strip()
                   and os.environ.get("LLM_BASE_URL", "").strip() else "single")
    report_path = REPORT_DIR / f"model-check-{profile}.json"
    tasks = cases()  # 先完成本地提示词渲染，避免浪费调用
    if not args.run:
        print("计划：需求理解、文章分块规划、单块生成、单块事实与合规审核，各1次。")
        print("最多4次HTTP尝试；重试与JSON修复遵循当前profile，额外尝试同样占用预算。")
        print("达到长度限制、预算上限或任一抽查失败即停止。")
        print("这只是接口与基本契约抽查，不能代表产品事实准确率。--run 才消耗魔粒。")
        return 0
    REPORT_DIR.mkdir(exist_ok=True)
    if report_path.exists() and not args.resume:
        print("已有测试记录，停止重复调用：" + str(report_path))
        return 1
    report = (json.loads(report_path.read_text(encoding="utf-8")) if args.resume and report_path.exists() else
              {"profile": profile, "max_requests": 4, "requests_started": 0,
               "results": [], "completed": False})
    completed_count = len(report["results"])
    if (not 0 <= report["requests_started"] <= 4 or completed_count > len(tasks)
            or completed_count != report["requests_started"]):
        print("记录不完整或请求次数异常，停止调用。")
        return 1
    for old, (stage, _, _, check) in zip(report["results"], tasks):
        if old.get("stage") != stage or "output" not in old or not check(old["output"]):
            print("既有响应仍未通过本地检查，停止；不会重复调用该阶段。")
            return 1
        old["passed"] = True
    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    class RecordedBudget(RequestBudget):
        def consume(self):
            super().consume()
            report["requests_started"] = self.used
            save()  # 请求发出前落盘，进程退出后也不会自动重新跑一轮
    budget = RecordedBudget(4)
    budget.used = report["requests_started"]
    # make_llm 读取当前 profile；每个物理客户端在实际运行时仍共享这里的预算。
    router = make_llm()
    for client in getattr(router, "clients", {}).values():
        client._budget = budget
    if not getattr(router, "clients", None):
        router._budget = budget
    save()
    for stage, user, system, check in tasks[completed_count:]:
        client = router.for_stage(stage) if hasattr(router, "for_stage") else router
        result = {"stage": stage, "model": client.model, "passed": False}
        report["results"].append(result)
        try:
            data = client.json(user, system=system, temperature=0.7 if stage == "chunk_generate" else 0.2)
            result["output"] = data
            result["passed"] = isinstance(data, dict) and check(data)
        except (LLMError, ValueError, TypeError, KeyError) as exc:
            result["error"] = str(exc)
        result["request"] = client.requests[-1] if client.requests else {}
        save()
        print(f"{stage}：{'通过' if result['passed'] else '未通过，停止后续调用'}", flush=True)
        if not result["passed"]:
            print("报告：" + str(report_path))
            return 1
    report["completed"] = True
    save()
    print("请求总数：" + str(report["requests_started"]) + "；报告：" + str(report_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
