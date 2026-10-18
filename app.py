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
from uuid import uuid4
from pathlib import Path

import gradio as gr

from agent import export
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
        return f"✅ 服务就绪 · 本地知识库「{pipe.store.root.name}」· 大模型接口已配置"
    except LocalKBError as e:
        return f"⚠️ 服务未就绪 · {e}。请在环境变量或 .env 中设置 KB_LOCAL_DIR"
    except (LLMError, PromptError) as e:
        return f"⚠️ 服务未就绪 · {e}"


# ---- 渲染 Outcome ----
def render(out: Outcome) -> str:
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
        md = export.to_markdown(out.session)
        if out.status == "done":
            return "✅ **已确认定稿**\n\n" + md
        tail = "\n---\n满意请回复「确认」;想改请直接说(例如:换成朋友圈、标题短一点、改推 MateBook X Pro)。"
        if out.refusals:
            tail += "\n\n已拒绝写入:\n" + "\n".join(f"- {r}" for r in out.refusals)
        return md + tail

    return "处理完成"


# ---- 对话 ----
def user_send(message: str, history: list, state: dict | None):
    message = (message or "").strip()
    if not message:
        return "", history, state

    state = state or {"session": None, "last": ""}
    s = None
    pipe = None
    try:
        pipe = get_pipeline()
        s = state["session"] or Session()

        # 已出稿、已确认和修改意图追问后，都继续围绕当前稿件对话。
        if (state["last"] in ("review", "done", "need_more_info")
                and s.draft is not None and s.review is not None and s.review.passed):
            out = pipe.feedback(s, message)
        else:
            out = pipe.talk(s, message)

        answer = render(out)
        state = {"session": s, "last": out.status}

    except (LLMError, PromptError, LocalKBError) as e:
        answer = f"❌ {e}"
        state = {"session": s, "last": "error"}
    except Exception as e:  # 兜底,避免界面崩溃
        answer = f"❌ 系统错误:{type(e).__name__}: {e}"
        state = {"session": s, "last": "error"}
    finally:
        if pipe is not None and s is not None:
            try:
                pipe._trace(s, "页面结果", status=state["last"])
                pipe.save(s, f"web-{uuid4().hex}")
            except OSError:
                logging.getLogger(__name__).warning("运行记录保存失败")

    history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": answer},
    ]
    return "", history, state


def reset_chat(state: dict | None):
    return [], {"session": None, "last": ""}, ""


def prepare_downloads(state: dict | None):
    """每次生成独立文件，避免多用户串稿；失败和重置时清空下载。"""
    s = (state or {}).get("session")
    if ((state or {}).get("last") not in ("review", "done")
            or not s or not s.draft or not s.review or not s.review.passed):
        return gr.update(value=None, visible=False), gr.update(value=None, visible=False)
    folder = Path(tempfile.mkdtemp(prefix="ruanwen-export-"))
    txt, md = folder / "营销文案.txt", folder / "完整报告.md"
    txt.write_text(export.to_text(s), encoding="utf-8-sig")
    md.write_text(export.to_markdown(s), encoding="utf-8")
    return gr.update(value=str(txt), visible=True), gr.update(value=str(md), visible=True)


# ---- 表单快捷生成 ----
def from_form(product, audience, scene, platform, goal, extra, history, state):
    product = (product or "").strip()
    if not product:
        history = (history or []) + [
            {"role": "assistant", "content": "请先填写产品型号,例如 MateBook X Pro。"}
        ]
        return history, state
    parts = [f"请为{audience},围绕{scene}场景,写一篇发布到{platform}的{product}营销软文,目标:{goal}。"]
    if extra:
        parts.append(f"补充要求:{extra}")
    return user_send("".join(parts), history or [], state)[1:]


# ---- 外观 ----
# Gradio 6 把 theme/css 从 Blocks 构造器移到了 launch();按签名自动适配,4/5/6 都能跑
_BLOCKS_PARAMS = set(inspect.signature(gr.Blocks.__init__).parameters)
_CHATBOT_PARAMS = set(inspect.signature(gr.Chatbot.__init__).parameters)

HERO = """
<div id="hero">
  <div class="hero-copy">
    <div class="eyebrow"><span></span> 本地知识库驱动</div>
    <h1>华为 PC 营销软文智能体</h1>
    <p>从真实产品资料出发，自动匹配场景与平台，生成可核验、可修改、可直接使用的营销内容。</p>
    <div class="hero-tags">
      <span class="chip">公众号</span><span class="chip">小红书</span>
      <span class="chip">朋友圈</span><span class="chip">门店海报</span>
    </div>
  </div>
  <div class="hero-features">
    <div><strong>01</strong><span>事实有据<br><small>本地资料溯源</small></span></div>
    <div><strong>02</strong><span>自动适配<br><small>8 场景 · 4 平台</small></span></div>
    <div><strong>03</strong><span>双重检查<br><small>事实 + 合规</small></span></div>
  </div>
</div>
"""

CUSTOM_CSS = """
:root {
  --app-bg: #f4f7fb;
  --surface: #ffffff;
  --surface-soft: #f8faff;
  --ink: #182230;
  --muted: #667085;
  --line: #dfe7f2;
  --brand: #d9162f;
  --brand-dark: #b90f26;
  --blue: #3478f6;
}

html, body { background: var(--app-bg) !important; }
.gradio-container {
  max-width: 1280px !important; margin: 0 auto !important; padding: 24px 24px 36px !important;
  background: transparent !important; color: var(--ink) !important;
}
.gradio-container .prose, .gradio-container p, .gradio-container label { color: var(--ink); }

#hero {
  position: relative; overflow: hidden; display: flex; align-items: center; justify-content: space-between;
  gap: 32px; min-height: 210px; padding: 34px 40px;
  background:
    radial-gradient(circle at 92% 6%, rgba(231, 52, 74, .12), transparent 30%),
    radial-gradient(circle at 62% 100%, rgba(52, 120, 246, .11), transparent 32%),
    linear-gradient(135deg, #ffffff 0%, #f7faff 58%, #fff7f8 100%);
  border: 1px solid #e4eaf3; border-radius: 24px;
  box-shadow: 0 18px 50px rgba(36, 59, 92, .08);
}
#hero::after {
  content: ""; position: absolute; width: 170px; height: 170px; right: -54px; top: -72px;
  border: 30px solid rgba(217, 22, 47, .055); border-radius: 50%; pointer-events: none;
}
#hero .hero-copy { position: relative; z-index: 1; max-width: 650px; }
#hero .eyebrow {
  display: inline-flex; align-items: center; gap: 8px; margin-bottom: 13px;
  color: #55657a; font-size: .78rem; font-weight: 650; letter-spacing: .08em;
}
#hero .eyebrow span {
  width: 8px; height: 8px; border-radius: 50%; background: #22b573;
  box-shadow: 0 0 0 5px rgba(34, 181, 115, .12);
}
#hero h1 { color: var(--ink) !important; font-size: 2rem; line-height: 1.2; margin: 0 0 12px; letter-spacing: -.02em; }
#hero p { color: var(--muted) !important; margin: 0 0 19px; max-width: 620px; font-size: .96rem; line-height: 1.75; }
#hero .hero-tags { display: flex; flex-wrap: wrap; gap: 8px; }
#hero .chip {
  display: inline-block; padding: 5px 12px; border: 1px solid #dce5f1; border-radius: 999px;
  background: rgba(255,255,255,.78); color: #506176; font-size: .76rem; font-weight: 550;
}
#hero .hero-features {
  position: relative; z-index: 1; flex: 0 0 350px; display: grid; gap: 10px;
}
#hero .hero-features > div {
  display: flex; align-items: center; gap: 14px; padding: 12px 15px;
  background: rgba(255,255,255,.8); border: 1px solid rgba(218, 226, 238, .9); border-radius: 14px;
  box-shadow: 0 5px 18px rgba(46, 70, 104, .045);
}
#hero .hero-features strong { color: var(--brand); font-size: .74rem; letter-spacing: .06em; }
#hero .hero-features span { color: #344054; font-size: .83rem; font-weight: 650; line-height: 1.35; }
#hero .hero-features small { color: #8491a3; font-size: .72rem; font-weight: 450; }

#status-pill {
  background: #eefbf5; border: 1px solid #caefdd; border-radius: 12px;
  padding: 8px 14px; margin: 12px 2px 5px;
}
#status-pill p { margin: 0; font-size: .82rem; color: #267151 !important; }

#chat-card, #form-card, #tips-card {
  background: var(--surface); border: 1px solid var(--line); border-radius: 20px;
  box-shadow: 0 10px 30px rgba(36, 59, 92, .06);
}
#chat-card { padding: 20px; }
#chat-card .section-kicker, #form-card .section-kicker { color: var(--brand); font-size: .73rem; font-weight: 700; letter-spacing: .08em; }
#chat-card h3, #form-card h3 { color: var(--ink); margin-top: 2px; margin-bottom: 2px; }
#chat-card .section-desc, #form-card .section-desc { color: var(--muted); font-size: .82rem; margin-bottom: 10px; }

#chat-box { border: 1px solid #e5ebf3; border-radius: 16px; background: var(--surface-soft); overflow: hidden; }
#chat-box .message-wrap .message { border-radius: 15px !important; }
#msg-input textarea { border-radius: 13px !important; background: #fff !important; }
#msg-input { border-color: #dbe4ef !important; border-radius: 13px !important; }

#form-card {
  padding: 20px 20px 14px;
}
#form-card input, #form-card textarea { background: #fbfcff !important; }
#form-card .wrap, #form-card .secondary-wrap { border-radius: 11px !important; }

#tips-card { margin-top: 12px; padding: 14px 18px; background: #f9fbff; box-shadow: none; }
#tips-card p, #tips-card li { color: #66758a !important; font-size: .8rem; }

.primary {
  background: linear-gradient(135deg, var(--brand), #ef4055) !important;
  border-color: var(--brand) !important; color: #fff !important;
  box-shadow: 0 6px 16px rgba(217, 22, 47, .18) !important;
}
.primary:hover { background: linear-gradient(135deg, var(--brand-dark), var(--brand)) !important; }
.quick-row button {
  font-size: .78rem !important; border-color: #dfe6f0 !important; background: #fff !important;
  color: #526174 !important; border-radius: 10px !important;
}
.quick-row button:hover { border-color: #aebed3 !important; background: #f7faff !important; }

@media (max-width: 900px) {
  .gradio-container { padding: 14px !important; }
  #hero { padding: 26px 24px; min-height: auto; }
  #hero .hero-features { display: none; }
  #hero h1 { font-size: 1.55rem; }
}

footer { display: none !important; }
"""


def _ui_kwargs() -> dict:
    kw: dict = {}
    if "theme" in _BLOCKS_PARAMS:
        kw["theme"] = gr.themes.Soft(primary_hue="red", neutral_hue="slate")
    if "css" in _BLOCKS_PARAMS:
        kw["css"] = CUSTOM_CSS
    return kw


def _launch_kwargs() -> dict:
    kw: dict = {}
    if "theme" not in _BLOCKS_PARAMS:
        kw["theme"] = gr.themes.Soft(primary_hue="red", neutral_hue="slate")
    if "css" not in _BLOCKS_PARAMS:
        kw["css"] = CUSTOM_CSS
    return kw


# ---- 界面 ----
def build_ui() -> gr.Blocks:
    with gr.Blocks(title="华为PC营销软文智能体", **_ui_kwargs()) as demo:
        gr.HTML(HERO)
        gr.Markdown(pipeline_status(), elem_id="status-pill")

        session_state = gr.State({"session": None, "last": ""})

        with gr.Row(equal_height=False):
            # 左:对话
            with gr.Column(scale=5, elem_id="chat-card"):
                gr.HTML(
                    '<div class="section-kicker">AI 对话创作</div>'
                    '<h3>描述你的内容需求</h3>'
                    '<div class="section-desc">可以直接说产品、受众、场景和平台，我会在信息不足时继续追问。</div>'
                )
                chatbot_kw = {
                    "label": "对话",
                    "show_label": False,
                    "height": 480,
                    "elem_id": "chat-box",
                }
                if "type" in _CHATBOT_PARAMS:  # Gradio 4/5 需要显式启用 messages；6.x 已移除此参数
                    chatbot_kw["type"] = "messages"
                chatbot = gr.Chatbot(**chatbot_kw)

                with gr.Row():
                    msg = gr.Textbox(
                        placeholder="例如：给大学新生写一篇 MateBook 14 的小红书种草文案……",
                        show_label=False,
                        lines=2,
                        scale=5,
                        elem_id="msg-input",
                    )
                    send = gr.Button("发送 →", variant="primary", scale=0, min_width=96)

                with gr.Row(elem_classes="quick-row"):
                    q1 = gr.Button("开学季 · 小红书", size="sm")
                    q2 = gr.Button("职场出差 · 公众号", size="sm")
                    q3 = gr.Button("创作人群 · 朋友圈", size="sm")
                    reset = gr.Button("重新开始", size="sm", variant="secondary")

                with gr.Row():
                    txt_download = gr.DownloadButton("下载文案 TXT", visible=False)
                    md_download = gr.DownloadButton("下载完整报告 Markdown", visible=False)

            # 右:表单
            with gr.Column(scale=3):
                with gr.Column(elem_id="form-card"):
                    gr.HTML(
                        '<div class="section-kicker">快捷模式</div>'
                        '<h3>结构化生成</h3>'
                        '<div class="section-desc">填写核心信息，一键拼成完整需求并开始创作。</div>'
                    )
                    product = gr.Textbox(label="产品型号 *", placeholder="MateBook X Pro")
                    audience = gr.Dropdown(
                        label="目标人群",
                        choices=["大学新生", "职场人士", "设计师/创作者", "家庭用户", "企业采购", "其他"],
                        value="大学新生",
                    )
                    scene = gr.Dropdown(
                        label="使用场景",
                        choices=["开学季", "职场办公", "内容创作", "家庭换新", "企业采购", "门店到访", "新品首发", "节日营销"],
                        value="开学季",
                    )
                    platform = gr.Dropdown(
                        label="发布平台 *",
                        choices=["小红书", "公众号", "朋友圈", "门店海报"],
                        value="小红书",
                    )
                    goal = gr.Dropdown(
                        label="内容目标",
                        choices=["种草", "转化到店", "咨询引导", "促销告知"],
                        value="种草",
                    )
                    extra = gr.Textbox(label="补充要求", placeholder="如:突出多屏协同;500字以内", lines=2)
                    form_btn = gr.Button("开始生成 →", variant="primary")

                with gr.Column(elem_id="tips-card"):
                    gr.Markdown(
                        "**使用提示**\n"
                        "- 生成后可继续说「标题短一点」或「换成公众号」\n"
                        "- 满意后回复「确认」即可定稿\n"
                        "- 生成后可下载文案 TXT 和完整报告 Markdown\n"
                        "- 明确规则问题会修正；模型核验提示请在发布前确认"
                    )

        msg.submit(user_send, [msg, chatbot, session_state], [msg, chatbot, session_state])
        send.click(user_send, [msg, chatbot, session_state], [msg, chatbot, session_state])
        reset.click(reset_chat, [session_state], [chatbot, session_state, msg])
        session_state.change(prepare_downloads, [session_state], [txt_download, md_download])
        form_btn.click(
            from_form,
            [product, audience, scene, platform, goal, extra, chatbot, session_state],
            [chatbot, session_state],
        )

        # 快捷场景:填入输入框,由用户确认后发送
        q1.click(lambda: "给大学新生写一篇 MateBook 14 的小红书", outputs=msg)
        q2.click(lambda: "给经常出差的职场人士写一篇 MateBook X Pro 的公众号文章", outputs=msg)
        q3.click(lambda: "给视频创作者写一篇朋友圈文案,MateBook 16", outputs=msg)

    return demo


demo = build_ui()

if __name__ == "__main__":
    demo.queue().launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("PORT", "7860")),
        show_error=True,
        **_launch_kwargs(),
    )
