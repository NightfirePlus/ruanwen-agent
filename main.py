"""命令行入口。

示例:
  python main.py --product "MateBook 14" --audience "大学新生" --scene 开学季 --platform 小红书
  python main.py --interactive
"""
from __future__ import annotations

import argparse
import sys

from agent import export
from agent.models import Task
from agent.pipeline import Pipeline
from agent.prompts import PromptError, Prompts
from agent.retrieve import Retriever
from ima import ImaClient, ImaError
from llm.client import LLMClient, LLMError


def build_pipeline() -> Pipeline:
    # 先加载提示词:缺失或未填写时在这里一次性报出,不会等到调用大模型才失败
    prompts = Prompts.load()
    return Pipeline(LLMClient(), Retriever(ImaClient()), prompts)


def show(result, pipe: Pipeline, save: bool) -> None:
    if result.status == "need_more_info":
        print("信息还不够,请补充:")
        for q in result.questions:
            print(f"  - {q}")
        return
    if result.status == "blocked":
        print(f"已停止输出:{result.reason}")
    if result.draft:
        print(export.to_markdown(result))
    if save:
        print(f"\n运行记录已保存:{pipe.save_sample(result)}.*")


def ask(prompt: str, default: str = "") -> str:
    v = input(f"{prompt}{f'[{default}]' if default else ''}:").strip()
    return v or default


def interactive(pipe: Pipeline, save: bool) -> None:
    print("华为PC营销软文智能体(输入 q 退出)")
    while True:
        product = ask("产品型号")
        if product.lower() == "q":
            return
        task = Task(product, ask("目标人群"), ask("业务场景"), ask("发布平台"), ask("临时要求(可空)"))
        show(pipe.run(task), pipe, save)
        print()


def main() -> int:
    ap = argparse.ArgumentParser(description="华为PC营销软文智能体")
    ap.add_argument("--product", default="")
    ap.add_argument("--audience", default="")
    ap.add_argument("--scene", default="")
    ap.add_argument("--platform", default="")
    ap.add_argument("--extra", default="", help="临时要求")
    ap.add_argument("--interactive", action="store_true")
    ap.add_argument("--no-save", action="store_true", help="不保存运行记录")
    args = ap.parse_args()
    try:
        pipe = build_pipeline()
    except (ImaError, LLMError, PromptError) as e:
        print(f"初始化失败:{e}", file=sys.stderr)
        return 2
    try:
        if args.interactive:
            interactive(pipe, not args.no_save)
        else:
            task = Task(args.product, args.audience, args.scene, args.platform, args.extra)
            show(pipe.run(task), pipe, not args.no_save)
    except (ImaError, LLMError) as e:
        print(f"运行失败:{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
