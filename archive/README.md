# 项目历史归档

这里仅保存对项目演进仍有参考价值、但不参与当前运行的资料。运行时不得从本目录检索事实、图片、价格或营销参考；当前有效资料只存在于 `knowledge_base/current/`。

## 性能基准

- `project-history/runtime-benchmark-2026-10-21.md`：串行架构时代的总耗时基准，v2.0 已不再沿用该架构。
- `project-history/runtime-benchmark-2026-10-22.md`：分块流水线相对串行架构的首次实测对比。v2.0 取消了知识库模型复核并改成默认派发，耗时结构已不同，结论只作历史参考。

## 运行样例

- `project-history/sample-runs-2026-10-21/`：串行架构阶段的生成样例、运行记录与截图。
- `project-history/sample-runs-2026-10-23/`：v2.0 前的分块流水线调试运行记录，保留用于对比输出变化。

新样例不再提交到仓库根目录，需要时由 `generate_samples.py` 在本地生成。

## 说明书历史版本

- `project-history/manual-before-revision-2026-10-23/`：复赛说明书改版前的原始 docx/PDF，以及当时的 4 张界面截图（首页、小红书成稿、朋友圈、门店海报）。仅作历史对比，现行版以仓库根目录的 `作品说明书-复赛版.docx` / `.pdf` 为准。
