# 魔搭部署

[核心架构规范](docs/core-architecture.md) · [项目进度](ROADMAP.md) · [运行约定](docs/knowledge-runtime.md)

上传代码、prompts、config及完整的 `knowledge_base/current`。当前库已随仓库发布，运行时审核数据库不提交，由各环境持久保存。

环境变量：

```dotenv
LLM_PROFILE=modelscope-v1
MODELSCOPE_API_TOKEN=在平台私密设置中填写访问令牌
KB_LOCAL_DIR=/实际存储位置/knowledge_base/current
PORT=7860
```

也可将魔搭令牌填在 `LLM_API_KEY`。分工模式使用魔搭接口地址和 `config/model_routing.json`，无需再配置旧的模型端点。不要提交令牌或将其放入截图。

本地或其他 OpenAI 兼容端点若设置 `LLM_PROFILE=llm-multi-v1`，则改用 `LLM_API_KEY`、`LLM_BASE_URL` 和 `config/llm_model_routing.json`；该配置与本页的魔搭部署配置相互独立。

安装依赖并启动：

```bash
pip install -r requirements.txt
python app.py
```

服务端口与 `PORT` 保持一致。本地不设置PORT时，会自动选择空闲端口，访问启动日志给出的地址。

当前部署代码包含文字资料首次使用内部复核与缓存；图片不调用视觉模型，只按登记的精确型号归属使用。`00-规范与索引/usage-review.sqlite3` 所在目录需要可写且持久化，避免重建环境后丢失文字审核标记。新库强制严格核验，不需要设置宽松模式。

当前写作链路已经使用生成/重写模型与审核模型之间的内存分块队列，并在会话和运行记录中保存内容计划、块状态与版本。队列只在单次请求进程内运行，进程中断后不会自动续跑。

后续还将增加按资料类型配置的有效期、官网文字核验、候选修订与受控知识更新。部署环境届时必须持久化知识核验状态，并允许专用核验角色访问已登记的官方来源。这些知识时效能力尚未进入当前代码，不能把现有内部复核解释为官网实时核验。

模型或提示词调整后重启应用。知识库不分目录保存版本，直接更新当前目录及对应来源、哈希；审核记录会据此失效或复用。
