# 阶段7：判断修改意图

<!--
作为唯一的用户消息发送。
变量：{{需求}} {{稿件摘要}} {{用户修改意见}}
输出契约：{"action":"confirm|rewrite|change_scene|change_platform|change_product|unclear","value":"","note":""}
-->

请判断用户对当前稿件的主要修改意图。

## 原始需求

{{需求}}

## 当前稿件摘要

{{稿件摘要}}

## 用户修改意见

{{用户修改意见}}

动作规则：

- `confirm`：明确确认当前稿件定稿。
- `rewrite`：只调整表达、篇幅、语气、标题或卖点取舍，不更换产品、平台或场景。
- `change_scene`：更换目标人群或使用场景；`value` 填新的标准场景名。
- `change_platform`：更换发布平台；`value` 填新平台名。
- `change_product`：更换产品或型号；`value` 填新型号。
- `unclear`：无法判断要改什么。

换产品优先于换平台或场景；换平台或场景优先于普通改写。只有用户在本轮明确确认时才选择 `confirm`，不要因为话语中出现“可以”等局部肯定词就忽略后面的修改要求。

`note` 在 `rewrite` 时写可直接执行的修改要求，在 `unclear` 时写不确定原因，其他动作可留空。非换场景、平台或产品的动作将 `value` 留空。

返回且只返回一个合法 JSON 对象，不要使用 Markdown 代码围栏或附加说明：

{
  "action": "rewrite",
  "value": "",
  "note": "缩短标题并保持事实不变"
}
