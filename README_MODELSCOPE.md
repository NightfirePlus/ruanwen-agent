# 部署说明（v2.0）

[核心架构规范](docs/core-architecture.md) · [项目进度](ROADMAP.md) · [运行约定](docs/knowledge-runtime.md)

上传代码、prompts、config 及完整的 `knowledge_base/current`。当前库已随仓库发布，运行时不提交任何数据库，由各环境持久保存。

## 模型接口

只使用 OpenAI 兼容接口，Key 从环境读取，代码里不写任何默认地址或令牌。

```dotenv
LLM_API_KEY=你的模型服务 Key
LLM_BASE_URL=https://你的接口地址/v1
LLM_PROFILE=llm-multi-v1
KB_LOCAL_DIR=/实际存储位置/knowledge_base/current
PORT=7860
```

`LLM_PROFILE` 留空时，只要 `LLM_API_KEY` 与 `LLM_BASE_URL` 都设置了，就自动使用 `llm-multi-v1` 多模型分工（配置见 `config/llm_model_routing.json`）；否则退回 `single`，由 `LLM_MODEL` 指定的单个模型承担所有阶段。不要提交令牌或将其放入截图。

## 安装与启动

```bash
pip install -r requirements.txt
python app.py
```

正式部署建议改用 `pip install -r requirements.lock`，并把 `data/` 与
`knowledge_base/current/00-规范与索引/` 整个目录持久化，其中包含核验历史与来源登记。网页会保存创作
检查点，服务重启后可用“恢复中断”继续。平台健康探针可执行
`python tools/healthcheck.py --quiet`；数据库备份执行 `python tools/backup_runtime.py`。

服务端口与 `PORT` 保持一致。本地不设置 `PORT` 时，会自动选择空闲端口，访问启动日志给出的地址。

## 运行时行为

完全信任知识库：产品事实按原文取用，营销表达与人群场景参考直接进入写作上下文，知识库阶段不调用任何模型，也不抓取官网，匹配耗时在 1 秒以内。保留的确定性校验是 catalog 与文件哈希一致、型号归属、异常尺寸字段剔除和产品事实冲突检测。图片不调用视觉模型，只按登记的精确型号归属使用，默认每轮提供最多 8 张候选。

写作链路使用生成/重写模型与审核模型之间的内存分块队列，并在会话、运行记录和工作台检查点中保存内容计划、块状态与版本。队列只在单次请求进程内运行；进程中断后不会在后台自行发起模型请求，但用户可在页面点击“恢复中断”，从最近安全阶段继续。

历史作品默认参与创作参考，用户可在账户面板「历史作品」里关闭该开关。

## 知识修订

过时或冲突内容只会生成候选修订，不会静默覆盖正式资料。管理员使用 `python tools/knowledge_verify.py --candidates` 查看候选，并通过 `--apply 候选ID --approved-by 批准人` 显式回写；型号、价格权益、地区渠道、法律冲突等敏感变更还必须额外传入 `--allow-sensitive`。

模型或提示词调整后重启应用。知识库不分目录保存版本，直接更新当前目录及对应来源、哈希。