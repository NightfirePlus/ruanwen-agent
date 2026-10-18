"""运行三组真实接口案例，生成可提交的样例和来源汇总。"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import json
import time
import sys
from urllib.parse import quote

from app import make_store
from agent import export
from agent.llm import LLMClient
from agent.models import Session
from agent.pipeline import Pipeline
from agent.prompts import Prompts

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "samples"
CASES = [
    ("01-school-xiaohongshu", "开学季", "小红书", "HUAWEI MateBook 14", "大学新生", "种草",
     "正文300—600个汉字，分短段，5—8个话题标签，配3—6张库内图片。围绕携带、课堂续航需求、资料流转展开。"),
    ("02-office-wechat", "职场办公", "公众号", "HUAWEI MateBook Pro", "经常参加会议和出差的职场人士", "咨询引导",
     "正文必须800—1500个汉字，建议1000字，分3—5个小节，配2—4张库内图片。围绕会议准备、差旅携带、文档处理展开。不要堆参数，多写选购时可核实的问题。"),
    ("03-store-moments", "门店到访", "朋友圈", "MateBook D 14", "门店附近有办公购机需求的顾客", "转化到店",
     "全文80—150字，只讲一个核心卖点，正文仅1段，配1—3张库内图片，最后邀请私信确认配置和到店体验安排。"),
]


class RecordedLLM:
    def __init__(self, session):
        self.client = LLMClient()
        self.session = session

    def json(self, user, system=None, temperature=0.2):
        start = time.monotonic()
        result = self.client.json(user, system=system, temperature=temperature)
        self.session.trace.append({
            "stage": "模型调用", "model": self.client.model,
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "input": {"system": system, "user": user, "temperature": temperature},
            "output": result,
        })
        return result


def run_case(case):
    name, scene, platform, product, audience, goal, extra = case
    s = Session()
    pipe = Pipeline(RecordedLLM(s), make_store(), Prompts.load(), review_mode="relaxed")
    request = (f"请为{audience}，围绕{scene}场景，写一篇发布到{platform}的{product}营销软文，目标：{goal}。"
               f"{extra}只使用知识库明确记载的功能名和参数，保留适用条件。不要声称全天续航、秒同步、无感、零延迟、"
               "更护眼等未经证实的效果，不作跨型号比较，不虚构个人使用经历、优惠或门店库存。"
               "选购建议可以写成读者到店可自行核实的问题，不把主观感受写成产品保证。")
    start = time.monotonic()
    print(f"START {name}", flush=True)
    try:
        out = pipe.talk(s, request)
        if out.status in ("need_choice", "need_more_info"):
            out = pipe.talk(s, product)
        for attempt in range(2):
            if out.status not in ("review", "done"):
                break
            body_len = sum(len(p.text) for p in s.draft.body)
            full_len = len(''.join(export.to_text(s).split()))
            correction = ""
            if platform == "朋友圈" and not 80 <= full_len <= 150:
                correction = "请压缩全文到100字左右。标题、导语、正文、行动引导加起来总共不得超过150字。导语可为空，仅保留一个卖点和到店引导，不要标签。"
            if platform == "公众号" and not 800 <= body_len <= 1500:
                correction = "请将body正文扩写到1000个汉字左右，至少800字、不超过1500字，标题导语和结尾不计入正文。保留事实条件，多写选购核实问题，不新增未经证实的功能。"
            if not correction:
                break
            out = pipe.feedback(s, correction)
        elapsed = round(time.monotonic() - start, 1)
        pipe._trace(s, "案例运行结果", status=out.status, reason=out.reason,
                    review_mode=pipe.review_mode, elapsed_seconds=elapsed)
        base = pipe.save(s, name)
        if out.status not in ("review", "done"):
            print(f"FAILED {name}: {out.status} {out.reason} {out.questions}", flush=True)
            return {"name": name, "status": out.status, "reason": out.reason}

        article = export.to_text(s)
        base.with_suffix(".txt").write_text(article, encoding="utf-8-sig")
        extra_lines = ["", "# 五、资料与图片来源", "", "资料来自本地知识库，以下链接指向实际使用的文件：", ""]
        for source in s.evidence.sources:
            link = quote("../" + pipe.store.root.name + "/" + source, safe="/")
            extra_lines.append(f"- [{source}]({link})")
        refs = {im.id: im for im in s.evidence.images}
        for placement in s.draft.images:
            im = refs[placement.image_id]
            link = quote("../" + pipe.store.root.name + "/" + im.source_path, safe="/")
            extra_lines += ["", f"## 配图 {im.id}：{im.name}", f"- 所属型号：{im.model}",
                            f"- 库内路径：`{im.source_path}`", f"- 配图说明：{placement.caption}"]
            if im.origin_url:
                extra_lines.append(f"- 原始来源：[{im.name}]({im.origin_url})")
            else:
                extra_lines.append("- 原始来源：图片索引未记录 URL，以库内文件为准。")
            extra_lines += ["", f"![{im.name}]({link})"]
        base.with_suffix(".md").write_text(export.to_markdown(s) + "\n".join(extra_lines) + "\n", encoding="utf-8")
        row = {"name": name, "scene": scene, "platform": platform, "product": s.brief.product,
               "status": out.status, "elapsed_seconds": elapsed,
               "body_characters": sum(len(p.text) for p in s.draft.body),
               "article_characters": len(''.join(article.split())), "images": len(s.draft.images),
               "warnings": len(s.review.issues), "errors": len(s.review.errors)}
        print(json.dumps(row, ensure_ascii=False), flush=True)
        return row
    except Exception as exc:
        pipe._trace(s, "案例失败", error_type=type(exc).__name__)
        pipe.save(s, name)
        print(f"FAILED {name}: {type(exc).__name__}", flush=True)
        raise


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    selected = [c for c in CASES if len(sys.argv) == 1 or c[0] in sys.argv[1:]]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = [future.result() for future in as_completed([pool.submit(run_case, c) for c in selected])]
    summary_path = OUT / "results-summary.json"
    if summary_path.exists():
        old = json.loads(summary_path.read_text(encoding="utf-8"))
        results += [row for row in old if row["name"] not in {r["name"] for r in results}]
    results.sort(key=lambda r: r["name"])
    (OUT / "results-summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    if any(row["status"] not in ("review", "done") for row in results):
        raise SystemExit("存在未完成案例，请查看运行记录")
