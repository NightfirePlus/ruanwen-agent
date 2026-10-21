"""提示词管理:所有提示词放在 prompts/*.md,代码里不写任何提示词文字。

- 每个环节一个文件,名称与变量见 SPECS。
- 模板用 {{变量名}} 引用程序提供的内容;<!-- ... --> 是给编写者看的注释,发给模型前剥离。
- 只有标题没有正文,或文件缺失,都视为"未填写"。
- 加载分两级:Prompts.load() 严格,任何问题都报错;Prompts.load(strict=False) 把问题记在 .problems,
  调用到未就绪的环节时才报错,方便在提示词还没写完时先跑通链路的其他部分。
"""
from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 环节 -> 该提示词必须包含的变量。writer_system 只用于软文生成与修订。
SPECS: dict[str, tuple[str, ...]] = {
    "writer_system": (),
    "intake": ("已有信息", "对话记录", "场景选项", "平台选项"),
    "evidence": ("型号", "资料标题", "资料全文"),
    "compose": ("需求", "策略", "事实点清单", "可用图片", "表达参考"),
    "fact_review": ("稿件", "事实点清单", "原始资料", "图片登记"),
    "compliance_review": ("稿件", "事实点清单", "图片登记"),
    "revise_intent": ("需求", "稿件摘要", "用户修改意见"),
    "rewrite": ("稿件", "问题", "策略", "事实点清单", "可用图片", "表达参考"),
    "material_review": ("资料信息", "资料全文"),
}

PLACEHOLDER = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")
COMMENT = re.compile(r"<!--.*?-->", re.S)


class PromptError(Exception):
    """提示词缺失、为空或变量不匹配。"""


def clean(text: str) -> str:
    return COMMENT.sub("", text).strip()


TODO_MARK = "待补充"


def is_blank(text: str) -> bool:
    """去掉注释后只剩标题和空行,或含有"待补充"标记,都视为未填写。"""
    if TODO_MARK in text:
        return True
    return all(not ln.strip() or ln.lstrip().startswith("#") for ln in text.splitlines())


class Prompts:
    def __init__(self, texts: dict[str, str], strict: bool = True):
        self.problems: dict[str, list[str]] = {}
        self._t: dict[str, str] = {}
        for name, required in SPECS.items():
            raw = texts.get(name)
            if raw is None:
                self.problems.setdefault(name, []).append(f"prompts/{name}.md 不存在")
                continue
            text = clean(raw)
            if is_blank(text):
                self.problems.setdefault(name, []).append(f"prompts/{name}.md 还没有填写内容")
                continue
            found = set(PLACEHOLDER.findall(text))
            for v in required:
                if v not in found:
                    self.problems.setdefault(name, []).append(f"prompts/{name}.md 缺少变量 {{{{{v}}}}}")
            for v in sorted(found - set(required)):
                self.problems.setdefault(name, []).append(f"prompts/{name}.md 含有未定义的变量 {{{{{v}}}}}")
            self._t[name] = text
        if strict and self.problems:
            raise PromptError(self.report())

    def report(self) -> str:
        lines = [m for ms in self.problems.values() for m in ms]
        return "提示词未就绪:\n  - " + "\n  - ".join(lines)

    @classmethod
    def load(cls, directory: Path | None = None, strict: bool = True) -> "Prompts":
        base = directory or PROMPTS_DIR
        texts = {
            name: (base / f"{name}.md").read_text(encoding="utf-8")
            for name in SPECS
            if (base / f"{name}.md").is_file()
        }
        return cls(texts, strict=strict)

    def ready(self, name: str) -> bool:
        return name not in self.problems

    def text(self, name: str) -> str:
        """取原文(已去注释)。环节未就绪时报错,指出具体问题。"""
        if name in self.problems:
            raise PromptError("; ".join(self.problems[name]))
        return self._t[name]

    @property
    def writer_system(self) -> str:
        """软文写作的系统提示词,只用于图文生成与重写。"""
        return self.text("writer_system")

    def render(self, name: str, values: dict[str, str]) -> str:
        """一次性替换模板中的变量;值里即使出现 {{ }} 也不会被再次展开。"""
        text = self.text(name)
        lacking = [v for v in SPECS[name] if v not in values]
        if lacking:
            raise PromptError(f"渲染 {name} 时缺少变量值:{'、'.join(lacking)}")
        return PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), text)
