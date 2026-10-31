# 实现要点

桥接与技能的关键实现约束。每条都是踩过坑之后确定的，改动前请先看清楚原因。

---

## 一、整体架构

```
┌─────────────┐  HTTP(OpenAI)  ┌──────────────┐   CDP/WS   ┌──────────────┐
│ DSH / SDK   │ ─────────────► │  bridge.py   │ ─────────► │ Edge 浏览器   │
│             │ ◄───────────── │              │ ◄───────── │ chat.deepseek │
└─────────────┘   JSON / SSE   └──────────────┘  注入 JS   └──────────────┘
```

桥接做三件事：启动/连接浏览器 → 把 OpenAI 消息转成文本发进输入框 → 从网络响应取回原文转回 OpenAI 格式。

---

## 二、CDP 层约束（cdp.py）

### 必须用后台读线程

CDP 的 WebSocket 会同时推送"响应"和"事件"。同步读取时事件会被直接丢掉。
`CDP` 用一个后台线程分发：

- 有 `id` 且匹配 pending → 唤醒调用方
- 其余 → 进 `events` 队列，供 `wait_event()` 扫描

### 会话会失效

CDP 绑定具体页面，页面导航或关闭后旧连接不可用。
`bridge._complete()` 在异常时重置 `_cdp` 并重连一次。

---

## 三、页面注入约束（deepseek_web.py）

### 赋值必须用原生 setter

直接 `el.value = text` 不会触发 React 的 onChange：

```js
const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
setter.call(el, text);
el.dispatchEvent(new Event('input', {bubbles: true}));
```

### 发送按钮必须按 class 定位

页面上有多个带图标的按钮，按"有没有 svg"匹配会点中顶部工具栏。

```js
btns.find(b => /ds-button--primary/.test(b.className) &&
               /circle/.test(b.className))
```

### 提交后必须校验

React 更新状态需要时间。填完等 **0.6 秒**再点，点完检查**输入框是否被清空**，
没清空就重试（最多 3 次，补发 Enter）。

### 新对话后必须等输入框

`Page.navigate` 后页面需要时间渲染。轮询 `probe()` 直到出现输入框（最多 25 秒）再发送。

---

## 四、必须抓网络响应，不能抓 DOM

网页把内容渲染成富文本后再取 `innerText` 会破坏代码：

| 现象 | 例子 |
|---|---|
| 括号被当 LaTeX | `[math]::Round` → 数学斜体字符 |
| 空格丢失 | `Select-Object -First 200` → `Select-Object-First200` |
| 内容重复、换行错乱 | 文件被写出两遍 |

**正确做法**：用 CDP 的 `Network` 域抓 `/api/v0/chat/completion` 的原始响应。

```python
cdp.call("Network.enable"); cdp.clear_events()
# 发送...
ev = cdp.take_event("Network.requestWillBeSent",
                    lambda p: "/api/v0/chat/completion" in p["request"]["url"])
rid = ev["requestId"]
cdp.wait_event("Network.loadingFinished", lambda p: p["requestId"] == rid)
body = cdp.call("Network.getResponseBody", {"requestId": rid})["body"]
```

### SSE 格式

```
data: {"v":{"response":{...,"fragments":[{"id":2,"type":"THINK","content":"..."}]}}}
data: {"p":"response/fragments/-1/content","o":"APPEND","v":"回答"}
data: {"v":"用户"}                                        ← 无路径 = 续写最后一个片段
data: {"p":"response/fragments","o":"APPEND","v":[{"id":3,"type":"RESPONSE","content":"9"}]}
data: {"p":"response/status","o":"SET","v":"FINISHED"}
```

### 只取 RESPONSE 片段

开启深度思考时 `fragments` 有两种 type：`THINK`（思考过程）和 `RESPONSE`（最终答案）。
解析时**只拼接 RESPONSE**，否则拿到的是思考过程而不是答案。

---

## 五、输出上限与截断

### 实测上限 ≈ 54,000 字符（约 1,500 行代码）

| 测试 | 结果 |
|---|---|
| 20,000 行数字 | 截断于 10,814 行 / 53,777 字符 |
| 700 个函数（31,248 字符） | 完整返回 |

### 截断信号

SSE 的 `response/status` 正常是 `FINISHED`，截断时不是。
`extract_sse_meta()` 解析出 `incomplete`，桥接映射为标准的 **`finish_reason: "length"`**。

### 模型会自我压缩

要求大量重复内容时，模型会抽象成循环（实测要 2000 个函数，只给了 14 行）。
需要逐个展开时，提示词里要明确禁止循环 / `globals()` / 工厂函数。

---

## 六、工具调用转换

### OpenAI tools → 提示词

网页版没有原生 function calling，把工具定义渲染成文字：

```
- pwsh(command, description, timeoutMs) // 执行 PowerShell 命令
```

### 回复 → tool_calls（DSML）

模型用 DeepSeek 原生标记：

```
<||DSML||calls>
<||DSML||invoke name="pwsh">
<||DSML||parameter name="command" string="true">...</||DSML||parameter>
</||DSML||invoke>
</||DSML||calls>
```

**两个必须注意的点**：

1. 竖线是**全角** `｜`（U+FF5C），正则要同时接受 `|` 和 `｜`
2. 闭合标签是 `</||DSML||calls>`，斜杠在 `<` 后面

```python
_VB = r"[|\uff5c\u2502]"
_OPEN  = r"<" + _VB + r"{2,}\s*DSML\s*" + _VB + r"{2,}\s*"
_CLOSE = r"</" + _VB + r"{2,}\s*DSML\s*" + _VB + r"{2,}\s*"
```

---

## 七、会话管理

网页版自己维护上下文，不能每次都发完整历史。用指纹判断：

```python
def session_signature(messages):
    return uuid5(system_text + first_user_text)   # 各取前 800 字符
```

- 指纹变 → 导航到 `chat.deepseek.com` 开新对话，发**完整历史**
- 指纹不变 → 只发**最后一条**（通常是 tool 结果）

---

## 八、技能层：让 token 真正省下来

### 核心约束：规格和产物都不能进入调用方上下文

| 做法 | 后果 |
|---|---|
| 规格写进提示词 | 规格常驻上下文，之后每步都要重读（实测反而贵 24%） |
| 结果读进上下文 | 省下的输出 token 全还回去 |

**正确做法**：`--spec` 从磁盘读，`--out-dir` 写回磁盘，`--pytest` 只回一行摘要。

### 阈值

**产出量 ≥ 说明量的 5 倍** 才值得派活，且必须有自动验收手段。
500~700 行以下的小项目不划算。

---

## 九、一句话总结

> 网页版能扛生成，扛不住 Agent 协议。
> 把它当"写代码的工人"调用，而不是当模型替换。
