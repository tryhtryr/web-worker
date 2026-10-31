"""
web-deepseek-bridge —— 把 DeepSeek 网页版伪装成 OpenAI 兼容 API

任何支持 custom baseURL 的客户端（DSH 的 pi-ai 适配器、EAgent-Lite、openai SDK）
都可以指向 http://127.0.0.1:8787/v1 使用，底层通过 CDP 驱动浏览器操作 chat.deepseek.com。

接口：
  GET  /v1/models
  POST /v1/chat/completions   （支持 stream=true SSE）
"""
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import llm as agent_mod
import cdp as cdp_mod
import deepseek_web as ds


HOST = os.environ.get("WEBDS_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEBDS_PORT", "8787"))
MODEL_ID = "deepseek-web"
DEBUG_PORT = int(os.environ.get("WEBDS_DEBUG_PORT", "9222"))

_lock = threading.Lock()
_browser = None
_cdp = None
_session_key = None      # 当前网页会话对应的"身份"，变了就开新对话


# ---------------------------------------------------------------- 浏览器

def ensure_cdp(force_new_chat=False):
    """确保连接到 DeepSeek 页面，必要时开新对话。"""
    global _browser, _cdp, _session_key

    if _browser is None:
        _browser = cdp_mod.Browser(port=DEBUG_PORT)
    if not _browser.is_alive():
        _browser.launch("https://chat.deepseek.com")
        if not _browser.wait_ready(30):
            raise RuntimeError("浏览器调试端口未就绪")
        _cdp = None

    if _cdp is None:
        page = _browser.wait_page("deepseek", 25)
        _cdp = cdp_mod.CDP(page["webSocketDebuggerUrl"])

    if force_new_chat:
        try:
            _cdp.call("Page.navigate", {"url": "https://chat.deepseek.com"})
        except Exception:
            pass
        # 等到输入框真正出现（最多 25 秒），否则发送必失败
        t0 = time.time()
        while time.time() - t0 < 25:
            try:
                info = ds.probe(_cdp)
                if (info.get("input") or {}).get("sel"):
                    break
            except Exception:
                pass
            time.sleep(0.6)
        _session_key = None
    return _cdp


# ---------------------------------------------------------------- 提示词转换

TOOL_PROTOCOL = """
【工具调用协议 —— 必须严格遵守】

你可以调用下面这些工具。需要调用时，在回复里输出一个 ```json 代码块：

```json
{"tool_calls":[{"name":"工具名","arguments":{"参数":"值"}}]}
```

可以一次调用多个。**不要**用自然语言描述你"准备执行"，直接输出 JSON。
当你不需要调用工具、只想回答用户时，正常用自然语言回复即可。

可用工具：
%s
"""


def render_tools(tools):
    """把 OpenAI tools 定义渲染成给模型看的说明。"""
    if not tools:
        return ""
    lines = []
    for t in tools:
        fn = t.get("function") or t
        name = fn.get("name", "?")
        desc = (fn.get("description") or "").strip().replace("\n", " ")[:200]
        params = fn.get("parameters") or {}
        props = params.get("properties") or {}
        argdesc = ", ".join(
            f"{k}: {((v.get('description') or v.get('type') or '')).strip()[:60]}"
            for k, v in props.items()) or "无参数"
        lines.append(f"- {name}({argdesc})  // {desc}")
    return TOOL_PROTOCOL % "\n".join(lines)


def flatten_messages(messages, tools):
    """把 OpenAI messages 压成一条发给网页版的文本。"""
    parts = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, list):
            content = "\n".join(
                c.get("text", "") for c in content if isinstance(c, dict))
        content = content or ""

        if role == "system":
            parts.append(f"<system>\n{content}\n</system>")
        elif role == "user":
            parts.append(f"<user>\n{content}\n</user>")
        elif role == "assistant":
            tcs = m.get("tool_calls") or []
            if tcs:
                rendered = json.dumps(
                    {"tool_calls": [{"name": t.get("function", {}).get("name"),
                                     "arguments": json.loads(t.get("function", {}).get("arguments") or "{}")}
                                    for t in tcs]}, ensure_ascii=False)
                parts.append(f"<assistant>\n{content}\n```json\n{rendered}\n```\n</assistant>")
            else:
                parts.append(f"<assistant>\n{content}\n</assistant>")
        elif role == "tool":
            parts.append(f"<tool_result name=\"{m.get('name') or m.get('tool_call_id')}\">\n{content}\n</tool_result>")

    body = "\n\n".join(parts)
    tp = render_tools(tools)
    return (tp + "\n\n" + body) if tp else body


def session_signature(messages):
    """用 system + 第一条 user 消息作为会话指纹。"""
    sys_txt, first_user = "", ""
    for m in messages:
        r = m.get("role")
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        if r == "system" and not sys_txt:
            sys_txt = (c or "")[:800]
        if r == "user" and not first_user:
            first_user = (c or "")[:800]
    h = uuid.uuid5(uuid.NAMESPACE_OID, sys_txt + "\x00" + first_user)
    return str(h)


# ---------------------------------------------------------------- 回复解析

# ---------------- DSML 解析（DeepSeek 原生工具调用标记） ----------------
# 开：<||DSML||calls>  <||DSML||invoke name="x">  <||DSML||parameter ...>
# 闭：</||DSML||calls> </||DSML||invoke>          </||DSML||parameter>
import re as _re2

_VB = r"[|\uff5c\u2502]"   # 半角 | / 全角 ｜ / 制表符 │
_OPEN = r"<" + _VB + r"{2,}\s*DSML\s*" + _VB + r"{2,}\s*"
_CLOSE = r"</" + _VB + r"{2,}\s*DSML\s*" + _VB + r"{2,}\s*"
RE_DSML_CALLS = _re2.compile(_OPEN + r"calls\s*>(.*?)" + _CLOSE + r"calls\s*>", _re2.S)
RE_DSML_INVOKE = _re2.compile(_OPEN + r"invoke\s+name\s*=\s*\"([^\"]*)\"\s*>(.*?)" + _CLOSE + r"invoke\s*>", _re2.S)
RE_DSML_PARAM = _re2.compile(_OPEN + r"parameter\s+name\s*=\s*\"([^\"]*)\"[^>]*>(.*?)" + _CLOSE + r"parameter\s*>", _re2.S)


def _unescape(s):
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'")
             .replace("&amp;", "&"))



# ---- 数学斜体还原：网页把 (...) 当 LaTeX 渲染，字母被替换成数学字符 ----
import unicodedata as _ud

_MATH = {}
_DIGITS = "ZERO ONE TWO THREE FOUR FIVE SIX SEVEN EIGHT NINE".split()
for _cp in range(0x1D400, 0x1D800):
    _ch = chr(_cp)
    _nm = _ud.name(_ch, "")
    _m = _re2.search(r"(CAPITAL|SMALL) ([A-Z])$", _nm)
    if _m:
        _MATH[_ch] = _m.group(2) if _m.group(1) == "CAPITAL" else _m.group(2).lower()
        continue
    _m2 = _re2.search(r"DIGIT (ZERO|ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE)$", _nm)
    if _m2:
        _MATH[_ch] = str(_DIGITS.index(_m2.group(1)))
_MATH.update({"\u2212": "-", "\u2223": "|", "\u2225": "|", "\u210e": "h", "\u210b": "H",
              "\u2113": "l", "\u212f": "e", "\u2217": "*", "\u2261": "=", "\u2a7d": "<",
              "\u2a7e": ">", "\u2208": "e", "\u2192": "->"})


def denormalize_math(s):
    """把被数学渲染拆碎的字母还原成 ASCII，并去掉夹在中间的换行。"""
    if not s:
        return s
    out = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        # 换行后面紧跟数学字符 -> 这段是被渲染拆碎的，吞掉换行
        if ch == "\n":
            j = i + 1
            while j < n and s[j] == "\n":
                j += 1
            if j < n and s[j] in _MATH:
                i = j
                continue
        if ch in _MATH:
            out.append(_MATH[ch])
            i += 1
            while i < n and (s[i] == "\n" or s[i] in _MATH):
                if s[i] == "\n":
                    i += 1
                else:
                    out.append(_MATH[s[i]])
                    i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_dsml(text):
    """提取 DSML 工具调用，返回 (纯文本, [{"name","arguments"}])。"""
    t = text or ""
    calls = []
    blocks = RE_DSML_CALLS.findall(t)
    if not blocks:
        blocks = [t]
    for blk in blocks:
        for m in RE_DSML_INVOKE.finditer(blk):
            name = m.group(1).strip()
            args = {}
            for pm in RE_DSML_PARAM.finditer(m.group(2)):
                args[pm.group(1).strip()] = denormalize_math(_unescape(pm.group(2)))
            if name:
                calls.append({"name": name, "arguments": args})

    prose = RE_DSML_INVOKE.sub(" ", t)      # 整个 invoke 块（含参数）都去掉
    prose = RE_DSML_CALLS.sub(" ", prose)
    prose = _re2.sub(_OPEN + r"calls[^>]*>", " ", prose)
    prose = _re2.sub(_CLOSE + r"calls\s*>", " ", prose)
    prose = " ".join(prose.split())
    return prose.strip(), calls


def parse_tool_calls(text):
    """先试 DSML（DSH 教给模型的格式），再试 JSON。"""
    dsml_prose, dsml_calls = parse_dsml(text)
    if dsml_calls:
        return dsml_prose, dsml_calls

    import re as _re
    t = text or ""
    spans = [(s, e, o) for s, e, o in agent_mod.scan_spans(t)
             if isinstance(o, dict) and ("tool" in o or "tool_calls" in o or "name" in o)]
    if not spans:
        return t.strip(), []

    parts, calls, last = [], [], 0
    for s, e, o in spans:
        parts.append(t[last:s])
        last = e
        if "tool_calls" in o and isinstance(o["tool_calls"], list):
            for x in o["tool_calls"]:
                if isinstance(x, dict) and x.get("name"):
                    calls.append({"name": x["name"], "arguments": x.get("arguments") or {}})
        elif "name" in o and "tool" not in o:
            calls.append({"name": o.get("name"), "arguments": o.get("arguments") or {}})
        else:
            args = {k: v for k, v in o.items() if k != "tool"}
            calls.append({"name": o["tool"], "arguments": args})
    parts.append(t[last:])

    prose = " ".join("".join(parts).split())
    prose = _re.sub(r"^(json|复制|下载|\s)+", "", prose)
    prose = _re.sub(r"(json|复制|下载)\s*$", "", prose).strip()
    # 清掉空代码块残留，如 "```json ```"
    prose = _re.sub(r"```[a-zA-Z]*\s*```", "", prose)
    prose = _re.sub(r"^[`\s:]+|[`\s:]+$", "", prose).strip()
    return prose, calls


def to_openai_tool_calls(tool_calls):
    out = []
    for i, tc in enumerate(tool_calls):
        out.append({
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "index": i,
            "function": {
                "name": tc.get("name"),
                "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False),
            },
        })
    return out


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[bridge] " + (fmt % args), flush=True)

    # ---------- 公共 ----------

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8", errors="replace"))

    # ---------- 路由 ----------

    def do_GET(self):
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            return self._json({
                "object": "list",
                "data": [{
                    "id": MODEL_ID, "object": "model", "created": int(time.time()),
                    "owned_by": "web-deepseek",
                }],
            })
        if self.path.rstrip("/") in ("", "/", "/health"):
            return self._json({"ok": True, "service": "web-deepseek-bridge"})
        self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            return self._json({"error": {"message": "not found"}}, 404)

        try:
            req = self._read_body()
        except Exception as e:
            return self._json({"error": {"message": f"bad json: {e}"}}, 400)

        messages = req.get("messages") or []
        tools = req.get("tools") or []
        stream = bool(req.get("stream"))

        with _lock:
            try:
                reply = self._complete(messages, tools)
            except Exception as e:
                return self._json({"error": {"message": str(e), "type": "bridge_error"}}, 500)

        cid = f"chatcmpl-{uuid.uuid4().hex[:16]}"
        created = int(time.time())
        prose, tool_calls = parse_tool_calls(reply)

        if not stream:
            msg = {"role": "assistant", "content": prose or None}
            if tool_calls:
                msg["tool_calls"] = to_openai_tool_calls(tool_calls)
            return self._json({
                "id": cid, "object": "chat.completion", "created": created,
                "model": req.get("model") or MODEL_ID,
                "choices": [{"index": 0, "message": msg,
                             "finish_reason": "tool_calls" if tool_calls else "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

        # ---------- 流式 ----------
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def emit(obj):
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        base = {"id": cid, "object": "chat.completion.chunk", "created": created,
                "model": req.get("model") or MODEL_ID}

        def delta(d, finish=None):
            emit({**base, "choices": [{"index": 0, "delta": d, "finish_reason": finish}]})

        try:
            delta({"role": "assistant", "content": ""})
            if tool_calls:
                delta({"tool_calls": to_openai_tool_calls(tool_calls)})
                delta({}, "tool_calls")
            else:
                step = 24
                for i in range(0, len(prose), step):
                    delta({"content": prose[i:i + step]})
                delta({}, "stop")
            emit({"id": cid, "object": "chat.completion.chunk", "choices": [],
                  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------- 核心：把请求发给网页版 ----------

    def _complete(self, messages, tools):
        """失败自动重连一次（CDP 会话可能因页面导航/关闭而失效）。"""
        global _cdp, _session_key
        last = None
        for attempt in range(2):
            try:
                return self._do_complete(messages, tools)
            except Exception as e:
                last = e
                if attempt == 0:
                    print("[bridge] 请求失败，重置连接重试: %s" % e, flush=True)
                    try:
                        if _cdp:
                            _cdp.close()
                    except Exception:
                        pass
                    _cdp = None
                    _session_key = None
                    time.sleep(1.0)
        raise last

    def _do_complete(self, messages, tools):
        global _session_key

        sig = session_signature(messages)
        new_chat = (sig != _session_key)

        cdp = ensure_cdp(force_new_chat=new_chat)
        _session_key = sig

        if new_chat:
            prompt = flatten_messages(messages, tools)
        else:
            # 同一会话：只发最后一条（通常是 tool_result 或新的 user）
            last = messages[-1] if messages else {}
            role = last.get("role")
            content = last.get("content")
            if isinstance(content, list):
                content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
            if role == "tool":
                prompt = f"<tool_result name=\"{last.get('name') or last.get('tool_call_id')}\">\n{content}\n</tool_result>"
            else:
                prompt = content or ""

        return agent_mod.ask(cdp, prompt, timeout=300)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"web-deepseek-bridge 监听 http://{HOST}:{PORT}/v1", flush=True)
    print(f"模型名: {MODEL_ID}", flush=True)
    print("下一步：在 DSH 设置里添加 provider，api=openai-completions，"
          f"baseURL=http://{HOST}:{PORT}/v1，models=[{MODEL_ID}]", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("退出")


if __name__ == "__main__":
    main()
