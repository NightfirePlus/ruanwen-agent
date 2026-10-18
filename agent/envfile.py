""".env 加载器(标准库实现)。

规则:
- 读取项目根目录的 .env,格式为 KEY=VALUE;支持 # 注释、空行、export 前缀、成对的单/双引号。
- 系统环境变量优先:已经存在且非空的变量不会被 .env 覆盖。所以两种方式都能用,临时覆盖也方便。
- 返回实际从 .env 载入的变量名(不含值),便于日志里提示而不泄露内容。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def load_dotenv(path: Path | None = None) -> list[str]:
    """把 .env 载入 os.environ。文件不存在时什么也不做。返回实际载入的变量名。"""
    p = path or ROOT / ".env"
    if not p.is_file():
        return []
    loaded = []
    for key, value in parse(p.read_text(encoding="utf-8-sig")).items():
        if os.environ.get(key):          # 系统环境变量优先
            continue
        if value == "":                  # 留空的项不写入,避免把“未配置”误当成空值
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded
