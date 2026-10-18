"""测试用的假 LLM、假知识库,不联网、不使用真实凭证。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.evidence import KnowledgeStore  # noqa: E402
from agent.models import ImageRef  # noqa: E402
from agent.prompts import SPECS, Prompts  # noqa: E402

DOC = "MateBook 14 重量约1.36kg。内存16GB。屏幕14.2英寸。"
MODELS = {"MateBook 14": "F14", "MateBook 14s": "F14S", "MateBook X Pro": "FXP"}


def make_prompts(skip: tuple[str, ...] = ()) -> Prompts:
    """为每个环节生成一份变量齐全、内容为首行唯一标识的提示词。"""
    texts = {}
    for name, req in SPECS.items():
        if name in skip:
            continue
        texts[name] = f"【{name}】" + " ".join("{{%s}}" % v for v in req)
    return Prompts(texts, strict=False)


class FakeStore(KnowledgeStore):
    """不访问 ima,直接提供型号、资料和图片。"""

    def __init__(self, docs: dict[str, str] | None = None, images=None, models=None):
        self.kb_name, self.kb_id = "测试库", "KB"
        self._docs = docs if docs is not None else {"MateBook 14": DOC}
        self._images = images if images is not None else [
            ImageRef("I1", "MateBook 14", "mb14-主图.png", "P1", "P1-产品参数库/MateBook 14/images/mb14-主图.png", caption="主视觉"),
            ImageRef("I2", "MateBook X Pro", "xpro-主图.png", "P2", "P1-产品参数库/MateBook X Pro/images/xpro-主图.png"),
            ImageRef("I3", "", "未知.png", "P3", "P1-产品参数库/MateBook 14/images/未知.png", note="图片索引中未登记"),
        ]
        self._m = models if models is not None else dict(MODELS)

    def connect(self):
        return {"connected": True}

    def models(self):
        return {m: {"folder_id": f, "path": f"产品参数/{m}"} for m, f in self._m.items()}

    def docs(self, model_name):
        if model_name not in self._docs:
            return []
        return [{"media_id": f"M-{model_name}", "title": "产品事实.md", "path": f"P1-产品参数库/{model_name}/产品事实.md"}]

    def read(self, media_id):
        return self._docs.get(media_id.removeprefix("M-"), "")

    def images(self, model_name, limit=30):
        return list(self._images)


class FakeLLM:
    """按提示词首行的环节标识返回预设 JSON。"""

    def __init__(self, brief=None, facts=None, drafts=None, intent=None, fact_issues=None, comp_issues=None):
        self.brief = brief or {}
        self.facts = facts if facts is not None else [{"text": "重量约1.36kg"}, {"text": "内存16GB"}]
        self.drafts = list(drafts or [good_draft()])
        self.intent = intent or {"action": "confirm"}
        self.fact_issues = fact_issues or []
        self.comp_issues = comp_issues or []
        self.calls: list[str] = []
        self.systems: dict[str, object] = {}
        self.users: dict[str, str] = {}

    def json(self, user, system=None, temperature=0.2):
        stage = user.split("】")[0].lstrip("【")
        self.calls.append(stage)
        self.systems[stage] = system
        self.users[stage] = user
        if stage == "intake":
            return self.brief
        if stage == "evidence":
            return {"facts": self.facts, "conflicts": []}
        if stage in ("compose", "rewrite"):
            return self.drafts.pop(0) if len(self.drafts) > 1 else self.drafts[0]
        if stage == "fact_review":
            return {"issues": self.fact_issues}
        if stage == "compliance_review":
            return {"issues": self.comp_issues}
        if stage == "revise_intent":
            return self.intent
        raise AssertionError(f"未预期的环节 {stage}")


FULL_BRIEF = {
    "product": "MateBook 14", "audience": "大学新生", "scene": "开学季", "platform": "朋友圈",
    "goal": "种草", "stage": "对比选购", "main_problem": "携带", "llm_notes": "测试",
}


def good_draft():
    return {
        "title": "新生开学带它够轻", "intro": "每天背电脑去教室。",
        "body": [{"text": "重量约1.36kg,背着不累。", "fact_ids": ["F1"]}],
        "cta": "欢迎到店体验。", "tags": [],
        "images": [{"image_id": "I1", "after_paragraph": 0, "caption": "主图", "reason": "开篇"}],
        "refusals": [],
    }
