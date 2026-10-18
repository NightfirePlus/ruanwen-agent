# 华为PC营销软文智能体

面向华为PC门店与内容运营的自研 Agent。产品事实与图片只来自 ima 共享知识库;Agent 负责理解需求、匹配资料、套用场景与平台策略、生成图文、核验事实、检查合规,并在确认环节按用户意见局部重做。

## 流程

```
1 多轮追问 ─▶ 结构化需求 ─▶ 2 知识库匹配资料 ─▶ 3 策略表匹配 ─▶ 4 图文生成
                                                                    │
        ┌──────────── 有问题:改写,最多 2 轮 ◀── 5 事实核验 + 6 合规检查 ◀┘
        ▼
   7 确认环节 ── 确认 ─▶ 定稿
        ├─ 文案不满意 / 临时要求 ─▶ 回到 4(资料、策略复用)
        ├─ 换场景 / 换平台 ───────▶ 回到 3(资料复用,换策略表)
        └─ 换产品 ────────────────▶ 回到 1(全部重来)
```

| 阶段 | 代码 | 方式 | 要点 |
|---|---|---|---|
| 1 结构化需求 | `agent/intake.py` | LLM 抽取与判断,规则判定是否完整 | 场景/人群/平台/目标/购买阶段/核心问题;缺项或型号太笼统就追问,是否完整不信任 LLM 自述 |
| 2 资料匹配 | `agent/evidence.py` | 规则 + LLM | 型号必须精确匹配,不猜;摘录必须逐字出现在原文,否则丢弃;提到相近型号的丢弃;资料冲突则停止;每条事实带来源路径 |
| 3 策略匹配 | `agent/strategy.py` | 查 `config/*.json` | 场景表给切入点、卖点顺序、禁区;平台表给标题/篇幅/结构/语气/图片数/行动引导 |
| 4 图文生成 | `agent/compose.py` | LLM | 卖点必须标注事实编号;图片只能选确认属于该型号的 |
| 5 事实核验 | `agent/fact_check.py` | 规则 + LLM | 数值、型号、图片归属、数量由规则核对;LLM 只能新增问题,不能撤销规则发现的问题 |
| 6 合规检查 | `agent/compliance.py` | 规则 + LLM | 词表与正则 + LLM 找隐性问题;无依据的价格权益一律拦截 |
| 7 确认与修改 | `agent/revise.py` | LLM 判断意图,规则决定重跑起点 | 意图不明确就追问,不乱改 |

调度在 `agent/pipeline.py`,返回状态:`need_more_info` / `need_choice` / `blocked` / `review` / `done`。

## 安装与运行

需要 Python 3.10+,无第三方依赖。凭证只通过环境变量提供,不要写入代码或提交:

```powershell
# ima(在 https://ima.qq.com/agent-interface 获取,须与加入知识库的账号一致)
$env:IMA_OPENAPI_CLIENTID="<Client ID>"
$env:IMA_OPENAPI_APIKEY="<API Key>"
# 大模型(阿里云百炼 OpenAI 兼容接口)
$env:LLM_API_KEY="<百炼 API Key>"
$env:LLM_MODEL="qwen-plus"        # 可选,也可用 deepseek 系列
```

```bash
python main.py                                  # 多轮对话
python main.py "给大学新生写一篇 MateBook 14 的小红书"
python main.py --allow-unfinished-prompts       # 提示词没写完时先跑通其余部分
python run_tests.py                              # 运行全部测试
```

每次运行默认把完整记录(含每个阶段的输入输出)保存到 `samples/`,用 `--no-save` 关闭。

## 提示词

代码里不写任何提示词文字,全部在 `prompts/`,一个环节一个文件,每个文件开头的注释写明可用变量与输出契约。

| 文件 | 阶段 | 使用 `writer_system.md` |
|---|---|---|
| `writer_system.md` | 软文写作的系统提示词(手册要求的 8 个模块) | — |
| `intake.md` | 1 结构化需求 | 否 |
| `evidence.md` | 2 摘录事实点 | 否 |
| `compose.md` | 4 图文生成 | 是 |
| `fact_review.md` | 5 事实核验(LLM 部分) | 否 |
| `compliance_review.md` | 6 合规检查(LLM 部分) | 否 |
| `revise_intent.md` | 7 判断修改意图 | 否 |
| `rewrite.md` | 按问题改写 | 是 |

- 模板用 `{{变量名}}` 引用程序提供的内容;`<!-- ... -->` 注释发给模型前剥离。
- 输出 JSON 的字段名是程序读取结果的依据,改文字可以,改字段名要同步改代码。
- 骨架里带“待补充”的文件视为未完成。默认启动时一次性列出全部未完成项并拒绝启动;`--allow-unfinished-prompts` 下,调用到未就绪环节才报错,事实核验和合规检查的 LLM 部分会明确提示“已跳过”,规则部分照常工作。
- 平台与场景的具体规则在 `config/*.json`,每次生成自动注入,提示词里不需要重复写。

## 目录

```
agent/    各阶段与调度器
config/   scenes.json 场景表 / platforms.json 平台表 / compliance_rules.json 合规词表
prompts/  全部提示词
tests/    单元测试(不联网、不使用真实凭证)
samples/  运行记录
main.py   命令行入口
```

## 已知限制

- 知识库目录结构尚未用真实数据联调。型号列表取自“产品参数”目录下的子文件夹、图片归属取自所在目录,这些是按手册描述写的,联调后可能需要调整 `agent/evidence.py` 的 `KnowledgeStore`。
- 提示词内容待补充,在此之前无法真实生成。
- 暂无网页界面,当前为命令行版本。
