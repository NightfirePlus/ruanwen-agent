"""各阶段共用的小工具,避免模块之间互相导入。"""
from __future__ import annotations

import re

from .models import Evidence, ImageRef, Issue


def norm_model(s: str) -> str:
    """型号归一化:小写,去品牌前缀(HUAWEI/华为)、空格、连字符、下划线。
    知识库里同一型号常见带或不带 “HUAWEI ” 的两种写法。"""
    return re.sub(r"[\s\-_·]|huawei|华为", "", (s or "").lower())


def mentioned_models(text: str, names: list[str]) -> set[str]:
    """文本里提到了哪些型号。按名称从长到短匹配,并在匹配后抹掉,
    这样 "MateBook 14s" 不会被误判成提到了 "MateBook 14"(后者是前者的前缀)。"""
    rest = norm_model(text)
    found: set[str] = set()
    for name in sorted(names, key=lambda n: len(norm_model(n)), reverse=True):
        key = norm_model(name)
        if key and key in rest:
            found.add(name)
            rest = rest.replace(key, "\0")
    return found


def normalize_where(where: str) -> str:
    """把 LLM 写的位置统一成程序认得的写法:body[0].text -> 正文第1段,title -> 标题 等。"""
    w = (where or "").strip()
    image = re.match(r"图注\s*(\S+)", w)
    if image:
        return f"图注 {image[1]}"
    m = re.search(r"body\[(\d+)\]", w)
    if m:
        return f"正文第{int(m.group(1)) + 1}段"
    m = re.search(r"(?:正文)?\s*第\s*(\d+)\s*段", w)
    if m:
        return f"正文第{int(m.group(1))}段"
    names = {"title": "标题", "intro": "导语", "cta": "行动引导", "tags": "标签", "images": "配图方案"}
    for k, v in names.items():
        if w.lower().startswith(k):
            return v
    return w


# “规格名 + 版本号”:HDMI 2.1、USB 3.2、Wi-Fi 6、蓝牙 5.1、PCIe 4.0 等。
# 这类写法没有单位,数值规则抓不到,但最容易被模型凭印象写错(例如资料只写 HDMI × 1,稿子却写 HDMI 2.1)。
SPEC_VERSION = re.compile(
    r"(?:HDMI|USB|Type-?C|Wi-?Fi|WLAN|蓝牙|Bluetooth|PCIe|NVMe|DDR|LPDDR|Thunderbolt|雷电|DisplayPort|DP)"
    r"(?:\s*-?\s*[A-C](?![A-Za-z]))?"          # 允许 USB-A / USB-C 这类接口形状夹在名称和版本之间
    r"[ 	]*-?[ 	]*(?:Gen[ 	]?)?\d+(?:\.\d+)?",   # 版本号必须和名称在同一行,不跨行
    re.I,
)


def spec_versions(text: str) -> set[str]:
    """文本里出现的“规格名+版本号”,统一成可比较的写法:小写、去空格与连字符,
    并去掉夹在名称和版本之间的接口形状字母(USB-A 3.2 与 USB3.2 视为同一个版本)。"""
    out = set()
    for m in SPEC_VERSION.findall(text or ""):
        key = re.sub(r"[\s\-]", "", m).lower()
        out.add(re.sub(r"^(usb|type)([abc])(?=\d)", lambda m: m.group(1), key))
    return out


def llm_issues(data: dict, stage: str) -> list[Issue]:
    """把 LLM 返回的 issues 转成 Issue。

    LLM 的结论一律按 error 处理,不采信它自己标的 warn:模型可能把真实的编造或夸大标轻,从而被放行。
    位置按统一写法归一化;认不出的位置原样保留,由后续“无法定位则不自动删除、整体阻断”兜底。
    """
    out = []
    for it in data.get("issues", []):
        if isinstance(it, dict) and it.get("detail"):
            out.append(
                Issue(
                    stage, "llm", "error",
                    normalize_where(str(it.get("where", ""))),
                    str(it["detail"]),
                    str(it.get("suggestion", "")),
                )
            )
    return out


def usable_images(ev: Evidence, model: str) -> list[ImageRef]:
    """只有确认属于该型号的图片才可用;所属型号未确认的不进入生成。"""
    return [i for i in ev.images if i.model and norm_model(i.model) == norm_model(model)]
