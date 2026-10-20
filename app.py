"""Gradio Web 界面(魔搭创空间部署入口)。

  python app.py            # 本地启动,访问 http://localhost:7860

界面与命令行 main.py 使用同一套 Pipeline 与同一个数据源(本地知识库目录):
  - 知识库:KB_LOCAL_DIR 指定的目录,默认项目内 华为PC全板块深度知识库_v7
  - 需求未完整 → pipe.talk 追问
  - 稿件已生成(处于 review)→ pipe.feedback 判断修改意图
  - 确认后 → done
"""
from __future__ import annotations

import inspect
import logging
import os
import tempfile
import threading
from html import escape
from queue import Empty, Queue
from uuid import uuid4
from functools import lru_cache
from urllib.parse import quote
from pathlib import Path

import gradio as gr

from agent import artifacts, export
from agent.envfile import load_dotenv
from agent.llm import LLMClient, LLMError
from agent.localkb import LocalKBError, LocalStore
from agent.models import Session
from agent.pipeline import Outcome, Pipeline
from agent.prompts import PromptError, Prompts

load_dotenv()

DEFAULT_LOCAL_KB = Path(__file__).resolve().parent / "华为PC全板块深度知识库_v7"

# ---- Pipeline 全局单例(多用户共享,提示词与知识库索引只加载一次) ----
_pipe: Pipeline | None = None
_pipe_lock = threading.Lock()


def make_store() -> LocalStore:
    """只读本地知识库,与 main.py 一致;不初始化、不访问 ima 接口。"""
    configured = os.environ.get("KB_LOCAL_DIR", "").strip()
    local = Path(configured).expanduser() if configured else DEFAULT_LOCAL_KB
    store = LocalStore(local)
    if not store.connect()["connected"]:
        raise LocalKBError(f"本地知识库目录无效(找不到 P1-产品参数库):{local}")
    return store


def get_pipeline() -> Pipeline:
    global _pipe
    if _pipe is not None:
        return _pipe
    with _pipe_lock:
        if _pipe is None:
            custom = os.environ.get("PROMPTS_DIR")
            prompts = Prompts.load(Path(custom) if custom else None, strict=False)
            mode = os.environ.get("REVIEW_MODE", "relaxed").strip().lower()
            _pipe = Pipeline(LLMClient(), make_store(), prompts, review_mode=mode)
    return _pipe


def pipeline_status() -> str:
    """首页状态条:知识库与大模型是否就绪。"""
    try:
        pipe = get_pipeline()
        return "● 服务就绪 · 本地知识库"
    except LocalKBError as e:
        return f"⚠️ 服务未就绪 · {e}。请在环境变量或 .env 中设置 KB_LOCAL_DIR"
    except (LLMError, PromptError) as e:
        return f"⚠️ 服务未就绪 · {e}"


@lru_cache(maxsize=1)
def allowed_image_files() -> tuple[str, ...]:
    """只开放图片索引确认归属的实际产品图，不开放整个知识库。"""
    try:
        store = make_store()
        files = []
        for model in store.models():
            for im in store.images(model):
                if not im.model:
                    continue
                path = Path(im.media_id).resolve()
                if path.is_relative_to(store.root) and path.is_file():
                    files.append(str(path))
        return tuple(sorted(set(files)))
    except (LocalKBError, OSError):
        return ()


def web_image_url(im) -> str:
    path = str(Path(im.media_id).resolve())
    if path not in allowed_image_files():
        return ""
    prefix = getattr(gr.routes, "API_PREFIX", "")
    return f"{prefix}/file=" + quote(path.replace("\\", "/"), safe="/:")


# ---- 渲染 Outcome 与四块工作区 ----
def render_chat(out: Outcome) -> str:
    if out.status == "need_more_info":
        lines = ["**还需要确认以下信息:**", ""]
        lines += [f"- {q}" for q in out.questions]
        return "\n".join(lines)

    if out.status == "need_choice":
        lines = ["**知识库里没有找到这个型号,请确认:**", ""]
        lines += [f"- {q}" for q in out.questions]
        if out.candidates:
            lines += ["", f"候选型号:{'、'.join(out.candidates)}"]
        return "\n".join(lines)

    if out.status == "blocked":
        details = ""
        if out.session.review:
            details = "\n\n需要处理的问题：\n" + "\n".join(
                f"- {i.where}：{i.detail}" for i in out.session.review.errors
            )
        return f"⚠️ **已停止输出**\n\n{out.reason}{details}"

    if out.status in ("review", "done"):
        if out.status == "done":
            return "✅ **已确认定稿**\n\n可切换「图文预览」查看，或从右侧下载。"
        tail = "图文已生成。可切换上方「图文预览」查看排版，或直接继续告诉我需要怎样修改。"
        if out.refusals:
            tail += "\n\n**未写入的要求：**\n" + "\n".join(f"- {r}" for r in out.refusals)
        return tail

    return "处理完成"


def render_scene_card(s: Session | None) -> str:
    if not s or not any((s.brief.product, s.brief.audience, s.brief.scene, s.brief.platform)):
        return "_生成后显示产品、受众、场景与发布目标。_"
    b = s.brief
    angle = s.strategy.scene.get("angle", "") if s.strategy else "等待策略匹配"
    return (
        f"**产品**　{b.product or '待确认'}\n\n"
        f"**受众**　{b.audience or '待确认'}\n\n"
        f"**场景／平台**　{b.scene or '待确认'} ／ {b.platform or '待确认'}\n\n"
        f"**内容目标**　{b.goal or '待确认'}\n\n"
        f"**内容切入点**　{angle}"
    )


def render_evidence_card(s: Session | None) -> str:
    if not s or not s.evidence:
        return "_完成产品确认后，这里会列出本次实际采用的资料。_"
    used = {fact_id for p in (s.draft.body if s.draft else []) for fact_id in p.fact_ids}
    facts = [f for f in s.evidence.facts if not used or f.id in used]
    lines = [f"**{len(facts)} 条事实 · {len([i for i in s.evidence.images if i.model])} 张可用图片**", ""]
    for fact in facts[:8]:
        lines.append(f"- **{fact.id}** {fact.text}\n  <small>{fact.source_path}</small>")
    if len(facts) > 8:
        lines.append(f"- 另有 {len(facts) - 8} 条事实已匹配")
    return "\n".join(lines)


def render_review_card(s: Session | None) -> str:
    if not s or not s.review:
        return "_稿件生成后，这里会展示事实、图片与合规检查结果。_"
    review = s.review
    errors = review.errors
    warns = [i for i in review.issues if i.severity == "warn"]
    status = "✅ **阻断项已通过**" if not errors else f"⛔ **仍有 {len(errors)} 项阻断问题**"
    lines = [status, "", f"事实与合规提示：**{len(warns)}** 项"]
    for issue in review.issues[:10]:
        icon = "⛔" if issue.severity == "error" else "💡"
        lines.append(f"- {icon} **{issue.where}**：{issue.detail}")
    if review.removed:
        lines.append(f"\n已自动移除 {len(review.removed)} 处无依据或不合规内容。")
    return "\n".join(lines)


def render_article_card(s: Session | None) -> str:
    if not s or not s.draft:
        return "### 图文预览\n\n生成后可在这里查看完整排版。先在底部输入需求，也可以点开输入框内的「创作设置」。"
    return export.to_article_markdown(s, image_url=web_image_url)


def render_progress(events: list[tuple[str, str]], running: bool = True) -> str:
    if not events:
        return "正在开始处理……" if running else ""
    records = "".join(
        f"<li><strong>{escape(title)}</strong><br>{escape(detail)}</li>"
        for title, detail in events
    )
    log = (
        f"<details><summary>处理过程 · {len(events)} 条更新</summary>"
        f"<ol>{records}</ol></details>"
    )
    if running:
        title, detail = events[-1]
        return f"**{title}**\n\n{detail}\n\n{log}"
    return log


def _panel_values(s: Session | None):
    return render_scene_card(s), render_evidence_card(s), render_article_card(s), render_review_card(s)


# ---- 对话：后台执行流水线，前台按真实回调逐步更新 ----
def user_send_stream(message: str, history: list, state: dict | None):
    message = (message or "").strip()
    if not message:
        yield "", history, state, *_panel_values((state or {}).get("session"))
        return

    state = state or {"session": None, "last": ""}
    s = state.get("session") or Session()
    display = (history or []) + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": "正在开始处理……"},
    ]
    event_queue: Queue = Queue()
    result: dict = {}

    def progress(title: str, detail: str) -> None:
        event_queue.put(("progress", title, detail, _panel_values(s)))

    def worker() -> None:
        pipe = None
        try:
            pipe = get_pipeline()
            if (state.get("last") in ("review", "done", "need_more_info")
                    and s.draft is not None and s.review is not None and s.review.passed):
                outcome = pipe.feedback(s, message, progress=progress)
            else:
                outcome = pipe.talk(s, message, progress=progress)
            result.update(out=outcome, state={"session": s, "last": outcome.status})
        except (LLMError, PromptError, LocalKBError) as exc:
            result.update(error=f"❌ {exc}", state={"session": s, "last": "error"})
        except Exception as exc:  # 兜底，避免界面崩溃
            logging.getLogger(__name__).exception("页面生成失败")
            result.update(error=f"❌ 系统错误：{type(exc).__name__}: {exc}", state={"session": s, "last": "error"})
        finally:
            if pipe is not None:
                try:
                    pipe._trace(s, "页面结果", status=result["state"]["last"])
                    pipe.save(s, f"web-{uuid4().hex}")
                except Exception:
                    logging.getLogger(__name__).warning("运行记录保存失败")
            event_queue.put(("done",))

    initial_panels = _panel_values(s)
    threading.Thread(target=worker, daemon=True).start()
    events: list[tuple[str, str]] = []
    yield "", [dict(item) for item in display], gr.skip(), *initial_panels
    while True:
        try:
            event = event_queue.get(timeout=0.25)
        except Empty:
            continue
        if event[0] == "done":
            break
        events.append((event[1], event[2]))
        display[-1] = {"role": "assistant", "content": render_progress(events, True)}
        yield "", [dict(item) for item in display], gr.skip(), *event[3]

    final_state = result["state"]
    if "out" in result:
        answer = render_chat(result["out"])
    else:
        answer = result["error"]
    if "out" in result and result["out"].status in ("review", "done"):
        answer += "\n\n" + render_article_card(s)
    log = render_progress(events, False)
    display[-1] = {"role": "assistant", "content": f"{log}\n\n{answer}".strip()}
    yield "", [dict(item) for item in display], final_state, *_panel_values(s)


def user_send(message: str, history: list, state: dict | None):
    """保留原有三返回值接口，网页使用 user_send_stream 获得实时阶段更新。"""
    final = None
    for values in user_send_stream(message, history, state):
        final = values
    return final[:3]


def reset_chat(state: dict | None):
    return (
        [], {"session": None, "last": ""}, "",
        render_scene_card(None), render_evidence_card(None), render_article_card(None),
        render_review_card(None),
    )


def prepare_downloads(state: dict | None):
    """每次生成独立文件，避免多用户串稿；失败和重置时清空下载。"""
    s = (state or {}).get("session")
    if ((state or {}).get("last") not in ("review", "done")
            or not s or not s.draft or not s.review or not s.review.passed):
        return tuple(gr.update(value=None, visible=True, interactive=False) for _ in range(6))
    folder = Path(tempfile.mkdtemp(prefix="ruanwen-export-"))
    publish_md = folder / "发布成品.md"
    publish_docx = folder / "发布成品.docx"
    publish_pdf = folder / "发布成品.pdf"
    report_md = folder / "完整报告.md"
    report_docx = folder / "完整报告.docx"
    report_pdf = folder / "完整报告.pdf"
    publish_md.write_text(export.to_article_markdown(s), encoding="utf-8")
    report_md.write_text(export.to_markdown(s), encoding="utf-8")
    artifacts.create_docx(s, publish_docx)
    artifacts.create_pdf(s, publish_pdf)
    artifacts.create_docx(s, report_docx, full_report=True)
    artifacts.create_pdf(s, report_pdf, full_report=True)
    return tuple(
        gr.update(value=str(path), visible=True, interactive=True)
        for path in (publish_md, publish_docx, publish_pdf, report_md, report_docx, report_pdf)
    )


# ---- 输入框内的创作设置与文字通过同一个发送入口提交 ----
def compose_request(message, product, audience, scene, platform, goal):
    fields = [("产品", product), ("受众", audience), ("场景", scene), ("平台", platform), ("目标", goal)]
    selected = [f"{label}：{value.strip()}" for label, value in fields if value and value.strip()]
    text = (message or "").strip()
    if not selected:
        return text
    return (text or "请按以下设置创作营销图文。") + "\n\n本次创作设置：" + "；".join(selected)


def composer_send(message, product, audience, scene, platform, goal, history, state):
    settings = [product, audience, scene, platform, goal]
    previous = (state or {}).get("composer_settings")
    # 已发送的设置随会话保留，后续只附加改动项，避免旧的平台选择覆盖“换成公众号”等新指令。
    changed = settings if previous is None else [
        value if value != old else None for value, old in zip(settings, previous)
    ]
    request = compose_request(message, *changed)
    for values in user_send_stream(request, history, state):
        new_state = values[2]
        if isinstance(new_state, dict) and "session" in new_state:
            new_state = {**new_state, "composer_settings": settings}
        yield values[:2] + (new_state,) + values[3:]


def toggle_settings(opened):
    return not opened, gr.update(visible=not opened)


# ---- 外观 ----
# Gradio 6 把 theme/css 从 Blocks 构造器移到了 launch();按签名自动适配,4/5/6 都能跑
_BLOCKS_PARAMS = set(inspect.signature(gr.Blocks.__init__).parameters)
_CHATBOT_PARAMS = set(inspect.signature(gr.Chatbot.__init__).parameters)

HERO = """
<header id="hero">
  <div class="app-mark" aria-hidden="true">✦</div>
  <div class="hero-copy">
    <h1>华为 PC 营销工作台</h1>
    <p>从产品资料到可发布图文</p>
  </div>
  <span class="header-note">场景理解 · 事实溯源 · 图文创作</span>
</header>
"""

CUSTOM_CSS = """
:root {
  --app-bg: #f5f6f8; --surface: #fff; --ink: #202734; --muted: #7a8494;
  --line: #e7e9ee; --brand: #ce2841;
}
html, body { background: var(--app-bg) !important; }
.gradio-container {
  width: 100% !important; max-width: 1740px !important; margin: 0 auto !important;
  padding: 14px 22px 18px !important; box-sizing: border-box;
  background: transparent !important; color: var(--ink) !important;
}
.gradio-container .main { width: 100% !important; max-width: none !important; padding: 0 !important; }
.gradio-container main > .column { gap: 12px; }
#app-header { align-items: center; gap: 20px; margin: 0; }
#hero { display: flex; align-items: center; gap: 12px; min-height: 58px; }
#hero .app-mark {
  width: 40px; height: 40px; display: grid; place-items: center; border-radius: 12px;
  color: #fff; background: linear-gradient(140deg, #db3b52, #b71d36); font-size: 25px;
}
#hero h1 { font-size: 18px; line-height: 1.4; margin: 0; color: var(--ink); font-weight: 700; }
#hero p { color: var(--muted); font-size: 11px; margin: 3px 0 0; }
#hero .header-note { color: #929aa6; font-size: 11px; margin-left: 24px; }
#status-pill { text-align: right; }
#status-pill p { color: #64746e; font-size: 11px; margin: 0; }
#workspace {
  align-items: stretch; gap: 16px; height: calc(100dvh - 112px); min-height: 510px;
}
#left-rail, #right-rail { flex: 0 0 220px !important; min-width: 0 !important; gap: 14px; overflow-y: auto; }
#center-stage {
  flex: 1 1 0% !important; min-width: 0 !important; gap: 0; overflow: visible;
  background: #fff; border: 1px solid var(--line); border-radius: 20px;
  box-shadow: 0 6px 28px rgba(26, 36, 53, .035);
}
.insight-card, #download-card {
  flex: 0 0 auto !important;
  background: #fff; border: 1px solid var(--line); border-radius: 16px;
  padding: 16px; gap: 10px; box-shadow: none;
}
.card-heading { display: flex; align-items: center; gap: 8px; }
.card-heading span { font-size: 10px; color: #ab7d86; }
.card-heading h3 { font-size: 13px; margin: 0; color: #3c4553; font-weight: 650; }
.insight-card .prose { font-size: 12px; line-height: 1.7; color: #627083; overflow-wrap: anywhere; }
.insight-card .prose p { margin: 6px 0; }
.insight-card .prose strong { color: #424c5c; font-weight: 600; }
#evidence-body, #review-body { max-height: 43vh; overflow-y: auto; }
#center-topbar { align-items: center; margin: 0; padding: 8px 16px 0; gap: 12px; flex-shrink: 0; }
#center-title { font-size: 13px; font-weight: 650; color: #374151; }
#reset-button { min-height: 28px; font-size: 11px; border: 0; background: transparent; box-shadow: none; color: #7a8494; white-space: nowrap; }
#work-tabs { display: flex; flex-direction: column; flex: 1; min-height: 0; gap: 0; }
#work-tabs > .tab-wrapper, #work-tabs > .tab-nav { padding: 0 18px; border-bottom: 1px solid #f0f1f4; flex-shrink: 0; }
#work-tabs [role="tab"] { font-size: 12px; padding: 11px 14px; border-radius: 0; }
#work-tabs [role="tab"].selected { color: var(--brand); border-bottom-color: var(--brand); }
#chat-pane, #preview-pane { position: relative; padding: 0; border: 0; flex: 1; min-height: 0; overflow: hidden; }
#chat-pane > div, #preview-pane > div { position: absolute; inset: 0; height: 100%; min-height: 0; }
#chat-box {
  height: 100% !important; min-height: 0; border: 0; border-radius: 0; background: #fff;
}
#chat-box .message { font-size: 13px; line-height: 1.8; }
#chat-box .message details { font-size: 11px; color: #7a8494; margin-bottom: 12px; }
#chat-box .message summary { cursor: pointer; color: #7a8494; }
#chat-box .message details ol { margin: 10px 0; padding-left: 20px; }
#chat-box .message details li { margin-bottom: 9px; }
#chat-box img { display: block; max-width: 100%; max-height: 350px; object-fit: contain; border-radius: 12px; margin: 14px auto; }
#chat-box .empty-chat { color: #8993a2; text-align: center; }
#chat-box .empty-chat h2 { font-size: 21px; color: #414c5d; margin-bottom: 10px; }
#chat-box .empty-chat p { font-size: 12px; }
#result-preview { height: 100%; overflow: auto; padding: 24px 32px; box-sizing: border-box; }
#result-preview .prose { max-width: 760px; margin: auto; font-size: 14px; line-height: 1.9; }
#result-preview img { display: block; max-width: 100%; max-height: 430px; object-fit: contain; margin: 20px auto; border-radius: 12px; }
#composer {
  position: relative; flex: 0 0 auto !important; min-width: 0 !important;
  margin: 10px 16px 14px; width: auto;
  padding: 10px 12px 8px; gap: 4px; background: #fff; border: 1px solid #e0e4eb;
  border-radius: 16px; box-shadow: 0 3px 13px rgba(31, 44, 64, .035); overflow: visible;
}
#composer:focus-within { border-color: #d4a2ab; box-shadow: 0 0 0 2px rgba(206, 40, 65, .035); }
#msg-input { border: 0 !important; box-shadow: none !important; background: transparent !important; }
#msg-input textarea {
  min-height: 48px; font-size: 13px; padding: 6px 4px !important; border: 0 !important;
  background: transparent !important; box-shadow: none !important;
}
#composer-toolbar { align-items: center; flex-wrap: nowrap; gap: 8px; min-height: 34px; }
#settings-toggle {
  font-size: 11px; min-height: 30px; border: 0; background: #f4f5f7; border-radius: 9px;
  color: #5d6878; box-shadow: none; padding: 5px 10px;
}
#platform-select { min-width: 120px !important; max-width: 150px; font-size: 11px; }
#platform-select .wrap { border: 0; box-shadow: none; background: transparent; min-height: 30px; }
#send-button { min-height: 31px; border-radius: 9px; font-size: 12px; padding: 4px 15px; margin-left: auto; }
#composer-hint { font-size: 10px; color: #a1a8b3; padding: 0 3px; margin: 0; }
#composer-hint p { margin: 0; font-size: 10px; color: #a1a8b3; }
#composer-settings {
  position: absolute; bottom: calc(100% + 10px); left: 0; width: min(480px, 100%);
  max-height: min(460px, calc(100dvh - 180px)); overflow-y: auto; z-index: 60;
  padding: 16px; border: 1px solid #e3e6ec; border-radius: 16px; background: #fff;
  box-shadow: 0 14px 55px rgba(24, 37, 57, .16); gap: 12px; box-sizing: border-box;
}
#composer-settings .prose p { margin: 0; font-size: 12px; color: #8490a0; }
#composer-settings label { font-size: 11px; }
#composer-settings .block-title { background: transparent !important; color: #657184 !important; padding: 0 !important; }
#composer-settings .wrap { border-radius: 9px; }
#settings-done { min-height: 32px; font-size: 12px; border-radius: 9px; }
#download-card { gap: 8px; }
#download-card .download-label { color: #8993a2; font-size: 11px; margin: 4px 0 0; }
#download-card .download-row { gap: 5px; flex-wrap: nowrap; }
#download-card .download-row button, #download-card .download-row a {
  font-size: 10px; min-height: 30px; padding: 4px 6px; border-radius: 7px; box-shadow: none;
}
#download-note { color: #a0a7b2; font-size: 10px; }
.primary { background: var(--brand) !important; border-color: var(--brand) !important; color: #fff !important; box-shadow: none !important; }
.primary:hover { background: #b82339 !important; }
footer { display: none !important; }
@media (min-width: 1600px) { #left-rail, #right-rail { flex-basis: 240px !important; } }
@media (max-width: 1150px) {
  .gradio-container { padding: 10px 12px 14px !important; }
  #left-rail, #right-rail { flex-basis: 185px !important; }
  #workspace { gap: 10px; }
  .insight-card, #download-card { padding: 12px; }
  #hero .header-note { display: none; }
}
@media (max-width: 900px) {
  #workspace { flex-wrap: wrap; height: auto; min-height: 0; }
  #center-stage { order: 0; flex: 1 0 100% !important; height: calc(100dvh - 105px); min-height: 450px; }
  #left-rail, #right-rail { order: 1; flex: 1 1 44% !important; overflow: visible; }
  #status-pill { display: none; }
  #result-preview { padding: 18px; }
}
"""

def _ui_kwargs() -> dict:
    kw: dict = {}
    if "theme" in _BLOCKS_PARAMS:
        kw["theme"] = gr.themes.Soft(primary_hue="red", neutral_hue="slate")
    if "css" in _BLOCKS_PARAMS:
        kw["css"] = CUSTOM_CSS
    return kw


def _launch_kwargs() -> dict:
    kw: dict = {"allowed_paths": list(allowed_image_files())}
    if "theme" not in _BLOCKS_PARAMS:
        kw["theme"] = gr.themes.Soft(primary_hue="red", neutral_hue="slate")
    if "css" not in _BLOCKS_PARAMS:
        kw["css"] = CUSTOM_CSS
    return kw


# ---- 界面 ----
def build_ui() -> gr.Blocks:
    with gr.Blocks(title="华为PC营销软文智能体", **_ui_kwargs()) as demo:
        with gr.Row(elem_id="app-header"):
            with gr.Column(scale=3, min_width=0):
                gr.HTML(HERO)
            with gr.Column(scale=1, min_width=0):
                gr.Markdown(pipeline_status(), elem_id="status-pill")

        session_state = gr.State({"session": None, "last": ""})
        settings_open = gr.State(False)
        try:
            products = sorted(make_store().models())
        except (LocalKBError, OSError):
            products = []

        with gr.Row(equal_height=False, elem_id="workspace"):
            with gr.Column(scale=2, min_width=0, elem_id="left-rail"):
                with gr.Column(elem_classes="insight-card"):
                    gr.HTML('<div class="card-heading"><span>01</span><h3>场景判断</h3></div>')
                    scene_card = gr.Markdown(render_scene_card(None))
                with gr.Column(elem_classes="insight-card"):
                    gr.HTML('<div class="card-heading"><span>02</span><h3>知识库依据</h3></div>')
                    evidence_card = gr.Markdown(render_evidence_card(None), elem_id="evidence-body")

            with gr.Column(scale=8, min_width=0, elem_id="center-stage"):
                with gr.Row(elem_id="center-topbar"):
                    gr.HTML('<span id="center-title">创作空间</span>')
                    reset = gr.Button("新建对话", scale=0, min_width=70, elem_id="reset-button")
                with gr.Tabs(selected="chat", elem_id="work-tabs") as work_tabs:
                    with gr.Tab("对话创作", id="chat", elem_id="chat-pane"):
                        chatbot_kw = {
                            "label": "对话", "show_label": False, "height": 400, "elem_id": "chat-box",
                            "placeholder": (
                                '<div class="empty-chat"><h2>今天想向谁介绍这款产品？</h2>'
                                '<p>直接描述需求，或在输入框内选择产品与发布平台。</p></div>'
                            ),
                        }
                        if "type" in _CHATBOT_PARAMS:
                            chatbot_kw["type"] = "messages"
                        chatbot = gr.Chatbot(**chatbot_kw)
                    with gr.Tab("图文预览", id="preview", elem_id="preview-pane"):
                        article_card = gr.Markdown(render_article_card(None), elem_id="result-preview")

                with gr.Column(elem_id="composer"):
                    with gr.Column(visible=False, elem_id="composer-settings") as settings_panel:
                        gr.Markdown("**创作设置**\n\n留空的项目会从对话中自动识别。")
                        product = gr.Dropdown(
                            label="产品型号", choices=products, allow_custom_value=True, value=None,
                            filterable=True, interactive=True,
                        )
                        with gr.Row():
                            audience = gr.Dropdown(
                                label="目标人群", choices=["大学新生", "职场人士", "设计师/创作者", "家庭用户", "企业采购"],
                                value=None, allow_custom_value=True, interactive=True, min_width=120,
                            )
                            scene = gr.Dropdown(
                                label="使用场景",
                                choices=["开学季", "职场办公", "内容创作", "家庭换新", "企业采购", "门店到访", "新品首发", "节日营销"],
                                value=None, allow_custom_value=True, interactive=True, min_width=120,
                            )
                        goal = gr.Dropdown(
                            label="内容目标", choices=["种草", "转化到店", "咨询引导", "促销告知"],
                            value=None, interactive=True,
                        )
                        settings_done = gr.Button("完成", elem_id="settings-done")
                    msg = gr.Textbox(
                        placeholder="描述你的需求，或继续修改这篇内容……", show_label=False,
                        container=False, lines=1, max_lines=4, elem_id="msg-input",
                    )
                    with gr.Row(elem_id="composer-toolbar"):
                        settings_toggle = gr.Button(
                            "创作设置 ⌃", scale=0, min_width=95, elem_id="settings-toggle",
                        )
                        platform = gr.Dropdown(
                            choices=[("自动识别平台", ""), ("小红书", "小红书"), ("公众号", "公众号"), ("朋友圈", "朋友圈"), ("门店海报", "门店海报")], value="",
                            label="发布平台", show_label=False, container=False, min_width=120,
                            scale=1, interactive=True, filterable=False, elem_id="platform-select",
                        )
                        send = gr.Button("发送 ↑", variant="primary", scale=0, min_width=72, elem_id="send-button")
                    gr.Markdown("Enter 发送 · Shift + Enter 换行", elem_id="composer-hint")

            with gr.Column(scale=2, min_width=0, elem_id="right-rail"):
                with gr.Column(elem_classes="insight-card"):
                    gr.HTML('<div class="card-heading"><span>04</span><h3>核验结果</h3></div>')
                    review_card = gr.Markdown(render_review_card(None), elem_id="review-body")
                with gr.Column(elem_id="download-card"):
                    gr.HTML('<div class="card-heading"><h3>下载与交付</h3></div>')
                    gr.HTML('<div class="download-label">发布成品</div>')
                    with gr.Row(elem_classes="download-row"):
                        publish_md = gr.DownloadButton("MD", interactive=False, min_width=0)
                        publish_docx = gr.DownloadButton("DOCX", interactive=False, min_width=0)
                        publish_pdf = gr.DownloadButton("PDF", interactive=False, min_width=0)
                    gr.HTML('<div class="download-label">完整报告</div>')
                    with gr.Row(elem_classes="download-row"):
                        report_md = gr.DownloadButton("MD", interactive=False, min_width=0)
                        report_docx = gr.DownloadButton("DOCX", interactive=False, min_width=0)
                        report_pdf = gr.DownloadButton("PDF", interactive=False, min_width=0)
                    gr.HTML('<div id="download-note">完成核验后可下载</div>')

        turn_inputs = [msg, product, audience, scene, platform, goal, chatbot, session_state]
        turn_outputs = [msg, chatbot, session_state, scene_card, evidence_card, article_card, review_card]
        # 共用并发队列，避免连续点击与回车同时修改同一会话。
        event_kwargs = {"concurrency_limit": 1, "concurrency_id": "creation"}
        for trigger in (msg.submit, send.click):
            trigger(
                lambda: (gr.update(selected="chat"), False, gr.update(visible=False)),
                outputs=[work_tabs, settings_open, settings_panel], queue=False,
            ).then(
                composer_send, turn_inputs, turn_outputs, **event_kwargs,
            )
        settings_toggle.click(
            toggle_settings, settings_open, [settings_open, settings_panel], queue=False,
        )
        settings_done.click(
            lambda: (False, gr.update(visible=False)), outputs=[settings_open, settings_panel], queue=False,
        )
        reset.click(
            reset_chat, [session_state],
            [chatbot, session_state, msg, scene_card, evidence_card, article_card, review_card],
            **event_kwargs,
        ).then(lambda: gr.update(selected="chat"), outputs=work_tabs, queue=False)
        download_outputs = [publish_md, publish_docx, publish_pdf, report_md, report_docx, report_pdf]
        session_state.change(prepare_downloads, [session_state], download_outputs)

    return demo

demo = build_ui()

if __name__ == "__main__":
    # 本地未固定 PORT 时由 Gradio 从 7860 起寻找空闲端口，避免旧实例占用导致启动失败。
    # 创空间等部署环境显式设置 PORT，仍严格使用指定端口。
    configured_port = os.environ.get("PORT", "").strip()
    demo.queue().launch(
        server_name="0.0.0.0",
        server_port=int(configured_port) if configured_port else None,
        show_error=True,
        **_launch_kwargs(),
    )
