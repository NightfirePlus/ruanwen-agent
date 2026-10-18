# 阶段7:判断用户修改意图

<!--
作为唯一的用户消息发给模型。
变量:{{需求}} {{稿件摘要}} {{用户修改意见}}
输出契约(JSON):{"action":"confirm|rewrite|change_scene|change_platform|change_product|unclear","value":"换成什么(场景/平台/型号),其余为空","note":"对文案的修改要求"}
action 含义:confirm 确认定稿 / rewrite 只是文案不满意或临时要求 / change_scene 换场景或人群 / change_platform 换平台 / change_product 换产品 / unclear 看不出想改什么。
具体怎么重跑由程序按 action 决定,这里只判断意图。
-->

你的任务是判断用户对生成稿件的修改意图，帮助程序决定如何处理修改请求。

## 原始需求

{{需求}}

## 当前稿件摘要

{{稿件摘要}}

## 用户修改意见

{{用户修改意见}}

---

## 判断规则

根据用户的修改意见，判断属于以下哪种意图：

### 1. confirm（确认定稿）
- 用户明确表示满意、确认、可以了
- **触发词**："可以""满意""就这样""确认""没问题""OK"
- **程序行为**：标记为最终稿，导出
- **示例**："可以了，就用这个" → confirm

### 2. rewrite（文案不满意或临时要求）
- 只是对文案表达不满意，不涉及场景、平台、产品变更
- 临时增加或删除某个卖点
- 调整语气、篇幅、强调重点
- **触发词**："改成...""不要...""突出...""太长了""换个说法"
- **程序行为**：复用资料和策略，只重新生成文案
- **示例**：
  - "标题改短一点" → rewrite，note="缩短标题"
  - "突出多屏协同功能" → rewrite，note="强调多屏协同"
  - "不要提续航" → rewrite，note="删除续航相关内容"

### 3. change_scene（换场景或人群）
- 换目标人群或使用场景
- **触发词**："换成...场景""改成...人群""面向...写"
- **程序行为**：复用资料和图片，重新匹配场景策略，重新生成
- **示例**：
  - "改成职场办公场景" → change_scene，value="职场办公"
  - "面向创作者写" → change_scene，value="内容创作"

### 4. change_platform（换平台）
- 换发布平台
- **触发词**："换成...平台""改成...发布""换个渠道"
- **程序行为**：复用资料和图片，重新匹配平台策略，重新生成
- **示例**：
  - "改成公众号版本" → change_platform，value="公众号"
  - "换成朋友圈" → change_platform，value="朋友圈"

### 5. change_product（换产品）
- 换型号或产品
- **触发词**："换成...型号""改成...产品""换个产品"
- **程序行为**：全部重来，重新检索资料
- **示例**：
  - "换成MateBook D 14" → change_product，value="MateBook D 14"
  - "改成GT系列" → change_product，value="MateBook GT"

### 6. unclear（看不出想改什么）
- 意图不明确，需要追问
- **示例**：
  - "不太好" → unclear
  - "再改改" → unclear

---

## 判断优先级

当意见包含多种意图时，按以下优先级判断：

1. **confirm** > 其他（明确说满意就是确认）
2. **change_product** > 其他（换产品需要全部重做）
3. **change_platform** / **change_scene**（平台和场景同级，根据用户重点判断）
4. **rewrite**（其他情况）
5. **unclear**（实在看不出来）

---

## 输出格式

输出JSON，包含3个字段：

```json
{
  "action": "rewrite",
  "value": "",
  "note": "缩短标题，突出轻薄特性"
}
```

**字段说明**：
- `action`: 意图类型（confirm / rewrite / change_scene / change_platform / change_product / unclear）
- `value`: 
  - change_scene时填新场景（如"职场办公"）
  - change_platform时填新平台（如"公众号"）
  - change_product时填新型号（如"MateBook D 14"）
  - 其他action时留空`""`
- `note`: 
  - rewrite时填具体的文案修改要求
  - unclear时填不确定的原因
  - 其他action时可以留空或简要说明

---

## 示例

**示例1**：
用户："标题太长了，改短一点"
```json
{
  "action": "rewrite",
  "value": "",
  "note": "缩短标题"
}
```

**示例2**：
用户："换成公众号版本"
```json
{
  "action": "change_platform",
  "value": "公众号",
  "note": ""
}
```

**示例3**：
用户："可以了"
```json
{
  "action": "confirm",
  "value": "",
  "note": ""
}
```

**示例4**：
用户："不太满意"
```json
{
  "action": "unclear",
  "value": "",
  "note": "用户未说明具体不满意的地方，需要追问"
}
```

现在开始判断，输出纯JSON，不要包含markdown代码块标记：

需求:{{需求}}
稿件摘要:
{{稿件摘要}}
用户修改意见:{{用户修改意见}}
