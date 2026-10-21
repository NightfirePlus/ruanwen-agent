"""命令行入口:多轮对话。

  python main.py                      # 交互式,从自然语言需求开始
  python main.py "给大学新生写一篇 MateBook 14 的小红书"   # 首轮需求直接给出

提示词(prompts/*.md)未写完时,默认拒绝启动并列出缺哪些;加 --allow-unfinished-prompts 可先跑通链路,
调用到未就绪环节时再报错。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from agent import export
from agent.envfile import load_dotenv
from agent.llm import LLMError
from agent.model_routing import make_llm
from agent.knowledge import LocalKBError
from agent.catalogkb import DEFAULT_RUNTIME_KB, make_store as open_store
from agent.models import Session
from agent.pipeline import Outcome, Pipeline
from agent.prompts import PromptError, Prompts


DEFAULT_LOCAL_KB = DEFAULT_RUNTIME_KB


def make_store():
    """打开唯一受支持的当前知识库。"""
    store = open_store()
    print(f"知识库:本地目录 {store.root}", file=sys.stderr)
    return store


def build(strict: bool) -> Pipeline:
    # PROMPTS_DIR 可指向另一套提示词目录(例如本地联调用的临时版本);不设置则用仓库的 prompts/
    custom = os.environ.get("PROMPTS_DIR")
    prompts = Prompts.load(Path(custom) if custom else None, strict=strict)
    if prompts.problems:
        print(prompts.report(), file=sys.stderr)
    return Pipeline(make_llm(), make_store(), prompts)


def show(pipe: Pipeline, out: Outcome, save: bool) -> None:
    if out.status in ("need_more_info", "need_choice"):
        print("还需要确认:")
        for q in out.questions:
            print(f"  - {q}")
        return
    if out.status == "blocked":
        print(f"已停止输出:{out.reason}")
    if out.session.draft:
        print(export.to_markdown(out.session))
    for r in out.refusals:
        print(f"已拒绝写入:{r}")
    if save and out.session.draft:
        print(f"\n运行记录已保存:{pipe.save(out.session)}.*")


def chat(pipe: Pipeline, first: str, save: bool) -> None:
    s = Session()
    text, out = first, None
    while True:
        text = text or input("你:").strip()
        if text.lower() in ("q", "quit", "exit"):
            return
        if not text:
            continue
        out = pipe.feedback(s, text) if out and out.status in ("review",) else pipe.talk(s, text)
        show(pipe, out, save)
        if out.status == "review":
            print("\n满意请回复“确认”;想改请直接说(例如:换成朋友圈、文案太啰嗦、改推 MateBook X Pro)。")
        elif out.status == "done":
            print("已确认定稿。")
            return
        elif out.status == "blocked":
            return
        text = ""


def main() -> int:
    ap = argparse.ArgumentParser(description="华为PC营销软文智能体")
    ap.add_argument("request", nargs="?", default="", help="首轮需求(可省略,进入后再说)")
    ap.add_argument("--no-save", action="store_true", help="不保存运行记录")
    ap.add_argument("--allow-unfinished-prompts", action="store_true", help="提示词未写完也允许启动")
    args = ap.parse_args()
    load_dotenv()      # 系统环境变量优先;.env 只补充没设置的
    try:
        pipe = build(strict=not args.allow_unfinished_prompts)
        chat(pipe, args.request, not args.no_save)
    except (LLMError, PromptError, LocalKBError) as e:
        print(f"运行失败:{e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
