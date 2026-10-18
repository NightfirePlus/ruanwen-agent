# 阶段7:判断用户修改意图

<!--
作为唯一的用户消息发给模型。
变量:{{需求}} {{稿件摘要}} {{用户修改意见}}
输出契约(JSON):{"action":"confirm|rewrite|change_scene|change_platform|change_product|unclear","value":"换成什么(场景/平台/型号),其余为空","note":"对文案的修改要求"}
action 含义:confirm 确认定稿 / rewrite 只是文案不满意或临时要求 / change_scene 换场景或人群 / change_platform 换平台 / change_product 换产品 / unclear 看不出想改什么。
具体怎么重跑由程序按 action 决定,这里只判断意图。
-->

【待补充】任务说明由负责同学填写,写完后删除本行。

需求:{{需求}}
稿件摘要:
{{稿件摘要}}
用户修改意见:{{用户修改意见}}
