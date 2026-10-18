# 阶段4:图文生成

<!--
作为用户消息发给模型,系统消息是 writer_system.md。
变量:{{需求}} {{策略}}(场景与平台策略 JSON) {{事实点清单}}(形如 [F1] (型号) 原文) {{可用图片}}(形如 [I1] 型号 名称 来源)
输出契约(JSON,字段名是程序读取稿件的依据,不要改):
  title / intro / body:[{text, fact_ids:["F1"]}] / cta / tags:[] /
  images:[{image_id, after_paragraph, caption, reason}] / refusals:["无法满足的用户要求及原因"]
body[].fact_ids 用于核验数值与型号;images[].image_id 必须来自可用图片。
-->

【待补充】任务说明由负责同学填写,写完后删除本行。

需求:{{需求}}
策略:{{策略}}
事实点清单:
{{事实点清单}}
可用图片:
{{可用图片}}
