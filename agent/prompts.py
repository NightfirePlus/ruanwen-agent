"""提示词管理:所有环节的提示词都放在 prompts/*.md,代码里不写任何提示词文字。

约定:
- 每个环节一个文件,文件名见 SPECS。
- 模板用 {{变量名}} 引用程序提供的内容;每个文件必须包含它声明的全部变量,也不能出现未声明的变量。
- <!-- ... --> 是给编写者看的注释,发给模型前会被剥离。
- 只有标题没有正文,或文件缺失,都视为"未填写",启动时一次性报出全部问题。
"""
from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 环节 -> 该提示词必须包含的变量
SPECS: dict[str, tuple[str, ...]] = {
    "system": (),
    "scene": ("产品", "人群", "场景", "平台", "临时要求", "场景参考"),
    "facts": ("型号", "资料标题", "资料全文"),
    "compose": ("任务", "场景判断", "内容策略", "平台规则", "事实点清单", "可用图片"),
    "revise": ("当前稿件", "核验问题"),
}

PLACEHOLDER = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")
COMMENT = re.compile(r"<!--.*?-->", re.S)


class PromptError(Exception):
    """提示词缺失、为空或变量不匹配。"""


def clean(text: str) -> str:
    return COMMENT.sub("", text).strip()


def is_blank(text: str) -> bool:
    """去掉注释后只剩标题和空行,视为未填写。"""
    return all(not ln.strip() or ln.lstrip().startswith("#") for ln in text.splitlines())


class Prompts:
    def __init__(self, texts: dict[str, str]):
        problems: list[str] = []
        self._t: dict[str, str] = {}
        for name, required in SPECS.items():
            raw = texts.get(name)
            if raw is None:
                problems.append(f"prompts/{name}.md 不存在")
                continue
            text = clean(raw)
            if is_blank(text):
                problems.append(f"prompts/{name}.md 还没有填写内容")
                continue
            found = set(PLACEHOLDER.findall(text))
            missing = [v for v in required if v not in found]
            unknown = sorted(found - set(required))
            if missing:
                problems.append(f"prompts/{name}.md 缺少变量:" + "、".join("{{%s}}" % v for v in missing))
            if unknown:
                problems.append(f"prompts/{name}.md 含有未定义的变量:" + "、".join("{{%s}}" % v for v in unknown))
            self._t[name] = text
        if problems:
            raise PromptError("提示词未就绪:\n  - " + "\n  - ".join(problems))

    @classmethod
    def load(cls, directory: Path | None = None) -> "Prompts":
        base = directory or PROMPTS_DIR
        texts: dict[str, str] = {}
        for name in SPECS:
            path = base / f"{name}.md"
            if path.is_file():
                texts[name] = path.read_text(encoding="utf-8")
        return cls(texts)

    @property
    def system(self) -> str:
        return self._t["system"]

    def render(self, name: str, values: dict[str, str]) -> str:
        """一次性替换模板中的变量;值里即使出现 {{ }} 也不会被再次展开。"""
        required = SPECS[name]
        lacking = [v for v in required if v not in values]
        if lacking:
            raise PromptError(f"渲染 {name} 时缺少变量值:{'、'.join(lacking)}")
        return PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), self._t[name])
