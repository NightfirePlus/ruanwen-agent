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
import os
import threading
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
            _pipe = Pipeline(LLMClient(), make_store(), prompts)
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
        return f"⚠️ **已停止输出**\n\n{out.reason}"

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
    try:
        pipe = get_pipeline()
        s = state["session"] or Session()

        # 与 main.py 一致:上一步在 review 状态时,用户输入按“修改意见”处理
        if state["last"] == "review":
            out = pipe.feedback(s, message)
        else:
            if state["last"] in ("done", "blocked"):
                # 已定稿/已停止后继续输入,视为开始新任务
                s = Session()
            out = pipe.talk(s, message)

        answer = render(out)
        state = {"session": s, "last": out.status}

        # 运行记录留存
        if out.status in ("review", "done"):
            try:
                pipe.save(s)
            except OSError:
                pass

    except (LLMError, PromptError, LocalKBError) as e:
        answer = f"❌ {e}"
    except Exception as e:  # 兜底,避免界面崩溃
        answer = f"❌ 系统错误:{type(e).__name__}: {e}"

    history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": answer},
    ]
    return "", history, state


def reset_chat(state: dict | None):
    return [], {"session": None, "last": ""}, ""


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

HERO = """
<div id="hero">
  <h1>华为 PC 营销软文智能体</h1>
  <p>事实来自本地知识库 · 场景与平台策略驱动 · 事实核验 + 合规检查,一条闭环</p>
  <div>
    <span class="chip">开学季</span><span class="chip">职场办公</span><span class="chip">内容创作</span>
    <span class="chip">家庭换新</span><span class="chip">企业采购</span><span class="chip">门店到访</span>
    <span class="chip">新品首发</span><span class="chip">节日营销</span>
  </div>
  <div style="margin-top:10px">
    <span class="chip platform">公众号</span><span class="chip platform">小红书</span>
    <span class="chip platform">朋友圈</span><span class="chip platform">门店海报</span>
  </div>
</div>
"""

CUSTOM_CSS = """
.gradio-container { max-width: 1240px !important; margin: 0 auto !important; }

#hero {
  background: linear-gradient(120deg, #1c1c21 0%, #3a0d10 55%, #8a0d15 100%);
  border-radius: 18px; padding: 30px 34px 26px;
  color: #fff; box-shadow: 0 10px 30px rgba(122, 11, 18, .28);
}
#hero h1 { color: #fff !important; font-size: 1.7rem; margin: 0 0 10px 0; letter-spacing: .5px; }
#hero p { color: rgba(255,255,255,.85); margin: 0 0 12px 0; font-size: .95rem; }
#hero .chip {
  display: inline-block; padding: 4px 12px; margin: 5px 8px 0 0;
  border: 1px solid rgba(255,255,255,.28); border-radius: 999px;
  font-size: .78rem; color: rgba(255,255,255,.92);
}
#hero .chip.platform { border-color: rgba(255,170,170,.5); background: rgba(199,0,11,.25); }

#status-pill { background: #f5f6f8; border-radius: 10px; padding: 6px 16px; margin: 10px 0 2px; }
#status-pill p { margin: 0; font-size: .85rem; color: #555; }

#chat-box { border: 1px solid #e9e9ef; border-radius: 14px; }
#msg-input textarea { border-radius: 12px; }

#form-card {
  background: #fff; border: 1px solid #ececf1; border-radius: 16px;
  padding: 18px 18px 10px; box-shadow: 0 2px 14px rgba(0,0,0,.05);
}

.quick-row button { font-size: .8rem !important; }
footer { display: none !important; }
"""


def _ui_kwargs() -> dict:
    kw: dict = {}
    if "theme" in _BLOCKS_PARAMS:
        kw["theme"] = gr.themes.Soft(primary_hue="red")
    if "css" in _BLOCKS_PARAMS:
        kw["css"] = CUSTOM_CSS
    return kw


def _launch_kwargs() -> dict:
    kw: dict = {}
    if "theme" not in _BLOCKS_PARAMS:
        kw["theme"] = gr.themes.Soft(primary_hue="red")
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
            with gr.Column(scale=5):
                chatbot = gr.Chatbot(label="对话", height=520, elem_id="chat-box")

                with gr.Row():
                    msg = gr.Textbox(
                        placeholder="例如:给大学新生写一篇 MateBook 14 的小红书",
                        show_label=False,
                        lines=2,
                        scale=5,
                        elem_id="msg-input",
                    )
                    send = gr.Button("发送", variant="primary", scale=0, min_width=92)

                with gr.Row(elem_classes="quick-row"):
                    q1 = gr.Button("开学季 · 小红书", size="sm")
                    q2 = gr.Button("职场出差 · 公众号", size="sm")
                    q3 = gr.Button("创作人群 · 朋友圈", size="sm")
                    reset = gr.Button("重新开始", size="sm", variant="secondary")

            # 右:表单
            with gr.Column(scale=3):
                with gr.Column(elem_id="form-card"):
                    gr.Markdown("### 表单快捷生成")
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
                    form_btn = gr.Button("生成软文", variant="primary")

                gr.Markdown(
                    "**使用提示**\n"
                    "- 生成后可说「标题短一点」「换成公众号」「改推 MateBook D 14」\n"
                    "- 满意后回复「确认」定稿\n"
                    "- 无知识库依据的内容会被拦截或自动删除"
                )

        msg.submit(user_send, [msg, chatbot, session_state], [msg, chatbot, session_state])
        send.click(user_send, [msg, chatbot, session_state], [msg, chatbot, session_state])
        reset.click(reset_chat, [session_state], [chatbot, session_state, msg])
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
