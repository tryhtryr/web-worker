"""
llm.py —— 桥接服务的最小模型调用层

只保留 bridge.py 需要的东西：
  scan_spans(text)          从文本里找出所有配对完整的 JSON 对象
  split_prose(text)         拆成（说明文字, 工具调用列表）
  ask(cdp, text, timeout)   发一句话给网页版并等回复（网络抓取优先）

（从 eagent/agent.py 抽出来的精简版，去掉了本地工具/Agent 循环的依赖）
"""
import json
import re
import time

import deepseek_web as ds


# ---------------------------------------------------------------- JSON 扫描

def _loads_lenient(s):
    try:
        return json.loads(s)
    except Exception:
        pass
    # 模型可能写了单反斜杠（非法转义），尝试修复
    try:
        return json.loads(s.replace("\\", "\\\\"))
    except Exception:
        return None


def scan_spans(text):
    """返回所有配对完整的 JSON 对象及位置：[(start, end, obj)]"""
    found = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        if j < n:
            obj = _loads_lenient(text[i:j + 1])
            if obj is not None:
                found.append((i, j + 1, obj))
            i = j + 1
        else:
            break
    return found


def split_prose(text):
    """把回复拆成（说明文字, 工具调用列表）。"""
    t = text or ""
    spans = [(s, e, o) for s, e, o in scan_spans(t)
             if isinstance(o, dict) and ("tool" in o or "tool_calls" in o)]
    if not spans:
        return t.strip(), []

    parts, calls, last = [], [], 0
    for s, e, o in spans:
        parts.append(t[last:s])
        last = e
        calls.append(o)
    parts.append(t[last:])

    prose = " ".join("".join(parts).split())
    prose = re.sub(r"^(json|复制|下载|\s)+", "", prose)
    prose = re.sub(r"(json|复制|下载)\s*$", "", prose).strip()
    return prose, calls


# ---------------------------------------------------------------- 对话

def ask(cdp, text, timeout=300, on_update=None):
    """发一句话给网页版，返回回复文本。

    优先从 /api/v0/chat/completion 的 SSE 原始响应里取文本（不被网页渲染破坏），
    失败时降级为 DOM 轮询。
    """
    before = ""
    try:
        before = (cdp.eval(ds.JS_READ) or {}).get("text") or ""
    except Exception:
        pass

    try:
        ds.begin_capture(cdp)
    except Exception:
        pass

    r = ds.send_message(cdp, text)
    if not r.get("ok"):
        raise RuntimeError(f"发送失败: {r}")

    # 1) 网络抓取
    try:
        body = ds.capture_reply(cdp, url_hint="/api/v0/chat/completion",
                                first_wait=60, total_wait=timeout)
        if body:
            txt = ds.extract_sse_text(body)
            if txt:
                if on_update:
                    on_update(txt)
                return txt
    except Exception:
        pass

    # 2) 兜底：DOM 轮询
    end = time.time() + timeout
    cur, stable = "", 0
    while time.time() < end:
        try:
            info = cdp.eval(ds.JS_READ) or {}
        except Exception:
            info = {}
        t = info.get("text") or ""
        if t and t != before:
            if t != cur:
                cur, stable = t, 0
                if on_update:
                    on_update(cur)
            else:
                stable += 1
                gen = False
                try:
                    gen = bool(cdp.eval(ds.JS_GENERATING))
                except Exception:
                    pass
                if not gen and stable >= 4:
                    return cur
        time.sleep(0.45)
    return cur
