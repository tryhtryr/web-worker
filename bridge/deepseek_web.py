"""
DeepSeek 网页版自动化
通过 CDP 在 chat.deepseek.com 页面里执行 JS 来输入/发送/读取回复。
页面结构可能变化，所以每个环节都有多套选择器和降级策略。
"""
import json
import time

# ---------- 页面探测 ----------

JS_PROBE = r"""
(() => {
  const vis = e => e.offsetParent !== null || e.getClientRects().length > 0;
  const pick = sels => {
    for (const s of sels) {
      const els = [...document.querySelectorAll(s)].filter(vis);
      if (els.length) return {sel: s, count: els.length};
    }
    return {sel: null, count: 0};
  };
  return {
    url: location.href,
    title: document.title,
    input: pick(['textarea', 'div[contenteditable="true"]', '[role="textbox"]']),
    msg: pick(['.ds-markdown', '[class*="markdown"]', '[class*="message"]']),
    buttons: [...document.querySelectorAll('button, [role="button"]')]
      .filter(vis).slice(0, 40)
      .map(b => ({label: (b.getAttribute('aria-label')||'').slice(0,20),
                  text: (b.innerText||'').trim().slice(0,20)}))
      .filter(b => b.label || b.text),
    bodyLen: document.body ? document.body.innerText.length : 0,
  };
})()
"""


def probe(cdp):
    """返回页面结构诊断信息，便于排查选择器失效。"""
    return cdp.eval(JS_PROBE)


# ---------- 填充输入框 ----------

def _js_fill(text):
    return r"""
(() => {
  const text = %s;
  const vis = e => e.offsetParent !== null || e.getClientRects().length > 0;
  let el = null;
  for (const s of ['textarea', 'div[contenteditable="true"]', '[role="textbox"]']) {
    const els = [...document.querySelectorAll(s)].filter(vis);
    if (els.length) { el = els[els.length - 1]; break; }
  }
  if (!el) return {ok: false, err: '找不到输入框'};
  el.focus();
  if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, text);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  } else {
    // contenteditable
    el.textContent = text;
    el.dispatchEvent(new InputEvent('input', {bubbles: true, data: text, inputType: 'insertText'}));
  }
  return {ok: true, tag: el.tagName, len: text.length};
})()
""" % json.dumps(text)


def _js_submit():
    return r"""
(() => {
  const vis = e => e.offsetParent !== null || e.getClientRects().length > 0;
  // 找输入框
  let ta = null;
  for (const s of ['textarea', 'div[contenteditable="true"]', '[role="textbox"]']) {
    const els = [...document.querySelectorAll(s)].filter(vis);
    if (els.length) { ta = els[els.length - 1]; break; }
  }
  if (!ta) return {ok: false, err: '找不到输入框'};

  // 1) 首选：圆形的 primary 按钮（DeepSeek 的发送键特征）
  let btns = [...document.querySelectorAll('button, [role="button"], div[class*="ds-button"]')].filter(vis);
  let send = btns.find(b => /ds-button--primary/.test(b.className || '') &&
                             /circle/.test(b.className || ''));
  // 2) 次选：输入框所在容器内的最后一个按钮
  if (!send) {
    let box = ta.closest('form') || ta.parentElement;
    for (let i = 0; i < 3 && box; i++) {
      const cands = [...box.querySelectorAll('button, [role="button"], div[class*="ds-button"]')].filter(vis);
      if (cands.length) { send = cands[cands.length - 1]; break; }
      box = box.parentElement;
    }
  }
  if (send) {
    send.click();
    return {ok: true, how: 'click', cls: (send.className || '').toString().slice(0, 60)};
  }
  // 3) 兜底：Enter
  for (const type of ['keydown', 'keypress', 'keyup']) {
    ta.dispatchEvent(new KeyboardEvent(type, {
      key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true
    }));
  }
  return {ok: true, how: 'enter'};
})()
"""


def _js_len():
    return r"""
(() => {
  const vis = e => e.offsetParent !== null || e.getClientRects().length > 0;
  let ta = null;
  for (const s of ['textarea', 'div[contenteditable="true"]', '[role="textbox"]']) {
    const els = [...document.querySelectorAll(s)].filter(vis);
    if (els.length) { ta = els[els.length - 1]; break; }
  }
  if (!ta) return -1;
  return ta.value !== undefined ? ta.value.length : ta.textContent.length;
})()
"""


def wait_input(cdp, timeout=20):
    """等输入框出现。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            info = probe(cdp)
            if (info.get("input") or {}).get("sel"):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def send_message(cdp, text, settle=0.6):
    """填内容并提交，带校验与重试。"""
    wait_input(cdp, 25)
    r1 = cdp.eval(_js_fill(text))
    if not (r1 or {}).get("ok"):
        return {"ok": False, "stage": "fill", "detail": r1}

    # 等 React 处理完再提交
    time.sleep(settle)

    for attempt in range(3):
        r2 = cdp.eval(_js_submit())
        # 校验：输入框被清空 或 出现对话
        ok_sent = False
        for _ in range(12):
            time.sleep(0.25)
            n = cdp.eval(_js_len())
            if n == 0:
                ok_sent = True
                break
        if ok_sent:
            return {"ok": True, "fill": r1, "submit": r2, "attempts": attempt + 1}
        # 没发出去就再按一次 Enter
        try:
            cdp.eval("""(()=>{const vis=e=>e.offsetParent!==null;const els=[...document.querySelectorAll('textarea,div[contenteditable="true"]')].filter(vis);const ta=els[els.length-1];if(!ta)return 0;for(const t of ['keydown','keypress','keyup']){ta.dispatchEvent(new KeyboardEvent(t,{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true}));}return 1;})()""")
        except Exception:
            pass

    n = cdp.eval(_js_len())
    return {"ok": n == 0, "fill": r1, "submit": r2, "attempts": 3, "left": n}


# ---------- 读取回复 ----------

JS_READ = r"""
(() => {
  const vis = e => e.offsetParent !== null || e.getClientRects().length > 0;
  const sels = ['.ds-markdown', '[class*="ds-markdown"]', '[class*="markdown"]', '[class*="message"]'];
  for (const s of sels) {
    const els = [...document.querySelectorAll(s)].filter(vis);
    if (els.length) {
      const last = els[els.length - 1];
      return {sel: s, count: els.length, text: last.innerText || ''};
    }
  }
  return {sel: null, count: 0, text: ''};
})()
"""

# 判断是否还在生成（停止按钮存在 = 生成中）
JS_GENERATING = r"""
(() => {
  const vis = e => e.offsetParent !== null || e.getClientRects().length > 0;
  const btns = [...document.querySelectorAll('button, [role="button"], [aria-label]')].filter(vis);
  return btns.some(b => /停止|stop/i.test(b.getAttribute('aria-label') || '') || /停止/.test(b.innerText || ''));
})()
"""


class Reader:
    """轮询页面，抓取最后一条回复的完整文本。"""

    def __init__(self, cdp, stable_rounds=4, interval=0.4):
        self.cdp = cdp
        self.stable_rounds = stable_rounds
        self.interval = interval
        self._last = None
        self._stable = 0

    def reset(self):
        self._last = None
        self._stable = 0

    def poll(self):
        """返回 (text, done)。done=True 表示内容已稳定。"""
        info = self.cdp.eval(JS_READ) or {}
        text = info.get("text") or ""
        generating = False
        try:
            generating = bool(self.cdp.eval(JS_GENERATING))
        except Exception:
            pass

        if text != self._last:
            self._last = text
            self._stable = 0
            return text, False

        if text:
            self._stable += 1
            if not generating and self._stable >= self.stable_rounds:
                return text, True
        return text, False

    def wait(self, timeout=180, on_update=None):
        """阻塞等待回复完成，返回最终文本。"""
        end = time.time() + timeout
        last = ""
        while time.time() < end:
            text, done = self.poll()
            if text != last:
                last = text
                if on_update:
                    on_update(text)
            if done:
                return text
            time.sleep(self.interval)
        return last


# ---------------- 网络抓取（拿原始 markdown，绕开 DOM 渲染） ----------------

def begin_capture(cdp):
    """开网络监听，清空事件缓冲。"""
    try:
        cdp.call("Network.enable")
    except Exception:
        pass
    cdp.clear_events()


def capture_reply(cdp, url_hint="completion", first_wait=25, total_wait=300):
    """等待 AI 回复的完成请求，返回原始响应体（SSE 文本）。失败返回 None。"""
    t0 = time.time()
    rid = None
    while time.time() - t0 < first_wait:
        ev = cdp.take_event(
            "Network.requestWillBeSent",
            lambda p: url_hint in ((p.get("request") or {}).get("url") or ""))
        if ev:
            rid = ev.get("requestId")
            break
        time.sleep(0.12)
    if not rid:
        return None

    fin = cdp.wait_event("Network.loadingFinished",
                         lambda p: p.get("requestId") == rid,
                         timeout=total_wait)
    if not fin:
        return None
    try:
        body = cdp.call("Network.getResponseBody", {"requestId": rid}, timeout=30)
        return body.get("body")
    except Exception:
        return None


def extract_sse_text(body):
    """从 /api/v0/chat/completion 的 SSE 里还原最终回答（只取 RESPONSE 片段，忽略 THINK）。"""
    if not body:
        return ""
    import json as _json

    frags = []  # [{"type": ..., "content": ...}]

    def frag_index(path):
        parts = (path or "").split("/")
        if len(parts) >= 3 and parts[1] == "fragments":
            try:
                return int(parts[2])
            except Exception:
                return -1
        return None

    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = _json.loads(payload)
        except Exception:
            continue

        v = obj.get("v")
        p = obj.get("p") or ""
        o = obj.get("o") or ""

        # 1) 初始快照
        if isinstance(v, dict) and "response" in v:
            frags = []
            for f in (v["response"].get("fragments") or []):
                frags.append({"type": f.get("type", ""), "content": f.get("content") or ""})
            continue

        # 2) 新增片段（答案通常在 THINK 结束后作为一个新的 RESPONSE 片段加进来）
        if p == "response/fragments" and isinstance(v, list):
            for f in v:
                if isinstance(f, dict):
                    frags.append({"type": f.get("type", ""), "content": f.get("content") or ""})
            continue

        # 3) 追加到某个片段
        if not isinstance(v, str):
            continue
        i = frag_index(p)
        if not frags:
            frags.append({"type": "RESPONSE", "content": ""})
        if i is not None:
            t = i if i >= 0 else len(frags) - 1
            if 0 <= t < len(frags):
                frags[t]["content"] += v
        elif p == "":
            # 无路径 = 续写最后一个片段
            frags[-1]["content"] += v
        continue

    return "".join(f["content"] for f in frags if f.get("type") == "RESPONSE").strip()


def extract_sse_meta(body):
    """从 SSE 里提取状态元信息（是否被截断、token 用量、是否思考）。

    返回 {"status": str, "incomplete": bool, "tokens": int, "thinking": bool}
    """
    if not body:
        return {"status": "", "incomplete": False, "tokens": 0, "thinking": False}
    import json as _json

    status = ""
    incomplete = False
    tokens = 0
    thinking = False

    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = _json.loads(payload)
        except Exception:
            continue

        v = obj.get("v")
        p = obj.get("p") or ""

        # 初始快照里的 thinking_enabled / incomplete_message
        if isinstance(v, dict) and "response" in v:
            resp = v.get("response") or {}
            if resp.get("thinking_enabled"):
                thinking = True
            if resp.get("incomplete_message"):
                incomplete = True
            continue

        # response/status SET xxx
        if p == "response/status" and isinstance(v, str):
            status = v
            if v.upper() not in ("FINISHED",):
                incomplete = True
            continue

        # quasi_status / accumulated_token_usage
        if p == "response" and isinstance(v, list):
            for item in v:
                if not isinstance(item, dict):
                    continue
                if item.get("p") == "quasi_status":
                    q = str(item.get("v") or "")
                    if q and q.upper() not in ("FINISHED",):
                        incomplete = True
                elif item.get("p") == "accumulated_token_usage":
                    try:
                        tokens = int(item.get("v") or 0)
                    except Exception:
                        pass
            continue

    return {"status": status, "incomplete": incomplete,
            "tokens": tokens, "thinking": thinking}
