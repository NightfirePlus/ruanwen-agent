# 魔搭部署

[项目进度](ROADMAP.md) · [运行约定](docs/knowledge-runtime.md)

上传代码、prompts、config及完整的 `knowledge_base/current`。当前库已随仓库发布，运行时审核数据库不提交，由各环境持久保存。

环境变量：

```dotenv
LLM_PROFILE=modelscope-v1
MODELSCOPE_API_TOKEN=在平台私密设置中填写访问令牌
KB_LOCAL_DIR=/实际存储位置/knowledge_base/current
PORT=7860
```

也可将魔搭令牌填在 `LLM_API_KEY`。分工模式使用魔搭接口地址和 `config/model_routing.json`，无需再配置旧的模型端点。不要提交令牌或将其放入截图。

安装依赖并启动：

```bash
pip install -r requirements.txt
python app.py
```

服务端口与 `PORT` 保持一致。本地不设置PORT时，会自动选择空闲端口，访问启动日志给出的地址。

当前知识库包含首次使用复核与缓存；`00-规范与索引/usage-review.sqlite3` 所在目录需要可写且持久化，避免重建环境后丢失审核标记。新库强制严格核验，不需要设置宽松模式。

模型或提示词调整后重启应用。知识库不分目录保存版本，直接更新当前目录及对应来源、哈希；审核记录会据此失效或复用。
