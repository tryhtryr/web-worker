# 原理详解

这份文档记录了实现过程中**踩过的坑和验证过的结论**，都是实打实调出来的。

---

## 一、整体架构

```
┌─────────────┐  HTTP(OpenAI)  ┌──────────────┐   CDP/WS   ┌──────────────┐
│ DSH / SDK   │ ─────────────► │  bridge.py   │ ─────────► │ Edge 浏览器   │
│             │ ◄───────────── │              │ ◄───────── │ chat.deepseek │
└─────────────┘   JSON / SSE   └──────────────┘  注入 JS   └──────────────┘
```

桥接做三件事：
1. 启动/连接一个带调试端口的浏览器
2. 把 OpenAI 消息转成一条文本发进网页输入框
3. 从**网络响应**里取回原始文本，转回 OpenAI 格式

---

## 二、CDP 层（cdp.py）

### 坑 1：同步读取会丢失事件

最初 `call()` 是"发请求 → 循环 recv 直到拿到匹配的 id"，**期间收到的所有事件消息都被丢掉了**。

后来要监听网络事件，必须改成**后台读线程**：

```python
def _read_loop(self):
    while self._alive:
        raw = self.ws.recv()
        data = json.loads(raw)
        if data.get("id") in self._pending:      # 响应 → 唤醒调用方
            slot = self._pending.pop(data["id"])
            slot["result"] = data
            slot["done"].set()
        else:                                     # 事件 → 进缓冲队列
            self.events.append(data)
```

这样 `call()` 只负责等待，事件进 `events` 队列供 `wait_event()` 扫描。

### 坑 2：页面导航后会失效

CDP 会话绑定的是**具体页面**。页面被导航/重载后，旧的 WebSocket 可能还在，但拿不到新页面的数据。

**对策**：桥接层加了失败重连 —— 请求异常时重置 `_cdp`，重新 `wait_page()` 再试一次。

---

## 三、页面交互（deepseek_web.py）

### 坑 3：React 控件的赋值

直接 `el.value = text` 不会触发 React 的 onChange。必须用**原生 setter**：

```js
const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
setter.call(el, text);
el.dispatchEvent(new Event('input', {bubbles: true}));
```

### 坑 4：点错了发送按钮 ⭐

最初用"任何带 svg 图标的按钮"定位发送键：

```js
btns.find(b => b.querySelector('svg'))     // ❌ 匹配到第一个按钮
```

实测页面有 5 个按钮，第一个在**顶部工具栏**（x=63,y=15），真正的发送键在**输入框右侧**（x=738,y=456）。

结果就是：消息填进去了，但点的是"新对话"之类的按钮 → 页面重置 → 消息消失。

**正确做法**：按 class 精确匹配

```js
btns.find(b => /ds-button--primary/.test(b.className) &&
               /circle/.test(b.className))
```

DeepSeek 的发送键特征是 `ds-button--primary` + `ds-button--circle`（圆形主按钮）。

### 坑 5：大文本提交时机

填完内容后 React 需要时间更新状态，发送按钮才会从 disabled 变可用。
`sleep(0.35)` 不够，改成 **0.6 秒 + 提交后校验**：

```python
for attempt in range(3):
    cdp.eval(_js_submit())
    for _ in range(12):                  # 最多等 3 秒
        time.sleep(0.25)
        if cdp.eval(_js_len()) == 0:     # 输入框被清空 = 发出去了
            return ok
    # 没发出去 → 补按 Enter
```

### 坑 6：新对话后页面没加载完

`Page.navigate` 之后 `sleep(3)` 不够，输入框还没渲染出来，`send_message` 直接失败。

**对策**：轮询等待输入框出现（最多 25 秒）。

---

## 四、抓回复：DOM vs 网络 ⭐⭐

### 最初的做法：轮询 DOM

```js
document.querySelectorAll('.ds-markdown')  // 取最后一个的 innerText
```

**致命缺陷**：网页把内容渲染成富文本后再取 `innerText`，过程中：

| 现象 | 例子 |
|---|---|
| `(...)` 被当 LaTeX 公式 | `[math]::Round` → `[𝑚𝑎𝑡ℎ]::𝑅𝑜𝑢𝑛𝑑` |
| 空格丢失 | `Select-Object -First 200` → `Select-Object-First200` |
| 内容重复 | `$z=[...]::OpenRead($p)` 出现两次 |
| 换行错乱 | 每个字符一行 |

**这会导致生成的 PowerShell/代码完全无法执行。**

### 正确的做法：抓网络响应

DeepSeek 网页请求的接口是：

```
POST https://chat.deepseek.com/api/v0/chat/completion
```

用 CDP 的 `Network` 域捕获它：

```python
cdp.call("Network.enable")
cdp.clear_events()
# ...发送消息...
ev = cdp.take_event("Network.requestWillBeSent",
                    lambda p: "/api/v0/chat/completion" in p["request"]["url"])
rid = ev["requestId"]
cdp.wait_event("Network.loadingFinished", lambda p: p["requestId"] == rid)
body = cdp.call("Network.getResponseBody", {"requestId": rid})["body"]
```

拿到的是**原始 SSE 文本**，没有任何渲染加工。

### SSE 格式（实测）

```
event: ready
data: {"request_message_id":3,"response_message_id":4,"model_type":"default"}

data: {"v":{"response":{...,"fragments":[
        {"id":2,"type":"THINK","content":"我们需要",...}]}}}

data: {"p":"response/fragments/-1/content","o":"APPEND","v":"回答"}
data: {"v":"用户"}                                    ← 无路径 = 续写最后一个片段
data: {"p":"response/fragments/-1/elapsed_secs","o":"SET","v":0.556}

data: {"p":"response/fragments","o":"APPEND","v":[
        {"id":3,"type":"RESPONSE","content":"9",...}]}  ← 答案在这里

data: {"p":"response/status","o":"SET","v":"FINISHED"}
```

### 坑 7：思考内容混进答案 ⭐

**开启深度思考时，`fragments` 里会有两种类型**：

| type | 含义 |
|---|---|
| `THINK` | 模型的思考过程 |
| `RESPONSE` | 最终答案 |

最初的解析器把所有片段拼在一起，结果拿到的是思考过程（"需要只回答数字。应输出 2..."）而不是答案。

**正确做法**：按片段维护列表，最后只取 `type == "RESPONSE"` 的。

```python
frags = []                          # [{"type":..., "content":...}]
# 初始快照 → 填充；p=="response/fragments" → 新增；
# p=="response/fragments/N/content" → 追加到第 N 个（-1 = 最后一个）
# 无 p 的 {"v": "..."} → 追加到最后一个
return "".join(f["content"] for f in frags if f["type"] == "RESPONSE")
```

---

## 五、工具调用转换

### OpenAI tools → 提示词

网页版没有原生 function calling，所以要把工具定义"翻译"成文字：

```
- pwsh(command, description, timeoutMs) // 执行 PowerShell 命令
- read(file_path, offset, limit)        // 读取文件
```

### 回复 → tool_calls

模型会用 **DSML**（DeepSeek 原生的工具调用标记）：

```
<||DSML||calls>
<||DSML||invoke name="pwsh">
<||DSML||parameter name="command" string="true">Get-Item ...</||DSML||parameter>
</||DSML||invoke>
</||DSML||calls>
```

### 坑 8：竖线是全角的 ⭐

模型输出的竖线是 **U+FF5C（全角 ｜）**，不是半角 `|`（U+007C）：

```
<\uff5c\uff5cDSML\uff5c\uff5c calls>
```

正则必须两种都接受：

```python
_VB = r"[|\uff5c\u2502]"      # 半角 / 全角 / 制表符
_OPEN  = r"<" + _VB + "{2,}\s*DSML\s*" + _VB + "{2,}\s*"
_CLOSE = r"</" + _VB + "{2,}\s*DSML\s*" + _VB + "{2,}\s*"
```

### 坑 9：闭合标签的斜杠位置

闭合标签是 `</||DSML||calls>`（斜杠在 `<` 后面），不是 `<||DSML||/calls>`。

```python
_CLOSE = r"</" + _VB + "{2,}..."      # ✅
# 不是 _OPEN + r"/calls>"             # ❌
```

---

## 六、会话管理

网页版自己维护上下文，所以不能每次都把完整历史发过去。

**用指纹判断**：

```python
def session_signature(messages):
    # system + 第一条 user 的前 800 字符
    return uuid5(system_text + first_user_text)
```

- 指纹变了 → 导航到 `chat.deepseek.com` 开新对话，发**完整历史**
- 指纹没变 → 只发**最后一条**（通常是 tool 结果）

---

## 七、技能层设计（让 token 真的省下来）

### 坑 10：v1 把规格写进提示词，反而更贵 ⭐⭐

v1 的做法是让 DSH 把规格内容**写进提示词文件**。

实测结果：

| 指标 | A 自己写 | B 派活 v1 | 变化 |
|---|---|---|---|
| 输出 token | 15,969 | 9,984 | −37.5% |
| 输入 token | 4,221 | 5,472 | **+29.6%** |
| 缓存读取 | 425,472 | 674,048 | **+58.4%** |
| 加权成本 | 1.00× | **1.24×** | **反而更贵** |

**原因**：写进提示词的 430 行规格会**永久留在上下文里**，之后每一步都要重读它。
省下的 6k 输出被 250k 缓存读取吃光了。

### v2 的修法

| v1 | v2 |
|---|---|
| 规格写进提示词 | **`--spec` 从磁盘读，不进上下文** |
| 产物要自己看 | **`--out-dir` 自动落盘** |
| 测试输出全读 | **`--pytest` 只回一行摘要** |
| 每轮 3~4 步 | **每轮 1 步** |

结果：**输出 token 2,610（降 84%），产出 1,413 行、117 个测试全通过。**

---

## 八、一句话总结

> 网页版**能扛生成**，但**扛不住 Agent 协议**。
> 所以别把 DSH 的模型换成它，而是**把它当成一个"写代码的工人"**来调用 ——
> API 负责想，网页版负责写，脚本负责搬运。
