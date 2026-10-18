# 华为PC营销软文智能体

面向华为PC门店与内容运营的自研 Agent。产品事实与图片只来自 ima 共享知识库,Agent 负责场景理解、平台适配、图文编排、事实核验和合规检查。

## 链路

任务接收 → 场景解析 → 策略匹配 → ima 资料与图片检索 → 事实点提取 → 图文生成 → 事实与图片核验 → 合规检查 → 导出

| 环节 | 实现 | 说明 |
|---|---|---|
| 任务接收 | 规则 | 产品/人群/场景/平台缺项即追问,不调用大模型 |
| 场景解析 | 大模型 + `config/scenes.json` | 得出人群、购买阶段、问题、目标、切入点 |
| 策略匹配 | 配置表 | 场景与平台规则可配置,换场景/平台输出结构性不同 |
| 检索 | ima OpenAPI | 搜索为空自动回退目录浏览;记录每次调用 |
| 事实提取 | 大模型 + 代码校验 | 只许摘录原文,摘录不在原文中的条目被丢弃 |
| 图文生成 | 大模型 | 卖点必须标注事实编号,图片只能选清单内同型号的 |
| 核验 | 代码规则 | 数值、型号、图片归属、数量逐项比对;资料冲突则停止 |
| 合规 | 词表 + 正则 | 绝对化、虚假承诺、竞品、伪造体验、无依据价格 |

返回状态:`done` / `need_more_info`(追问)/ `blocked`(冲突或多轮修订后仍不合规,停止输出)。

## 安装与运行

需要 Python 3.10+,无第三方依赖。

凭证只通过环境变量提供,不要写入代码或提交:

```powershell
# ima 接口凭证(在 https://ima.qq.com/agent-interface 获取,须与加入知识库的账号一致)
$env:IMA_OPENAPI_CLIENTID="<Client ID>"
$env:IMA_OPENAPI_APIKEY="<API Key>"
# 大模型(阿里云百炼 OpenAI 兼容接口)
$env:LLM_API_KEY="<百炼 API Key>"
$env:LLM_MODEL="qwen-plus"        # 可选,也可用 deepseek 系列
```

```bash
python main.py --product "MateBook 14" --audience "大学新生" --scene 开学季 --platform 小红书
python main.py --interactive
python -m unittest discover -s tests
```

每次运行默认把完整记录(含每一步的输入输出)保存到 `samples/`,用 `--no-save` 关闭。

## 目录

```
agent/    各环节与调度器(pipeline.py)
config/   scenes.json 场景 / platforms.json 平台 / compliance_rules.json 合规词表
prompts/  system.md 系统提示词(角色、场景、平台、知识库、图片、生成、核验、合规)
ima/      ima OpenAPI 封装(凭证脱敏、调用留痕、自动重试)
llm/      OpenAI 兼容大模型封装
tests/    单元测试(不联网、不使用真实凭证)
samples/  运行结果留存
```

## 已知限制

- 知识库目录结构尚未用真实数据联调,检索与图片归属判定按手册描述实现,联调后可能需要调整 `agent/retrieve.py`。
- 暂无网页界面,当前为命令行版本。
