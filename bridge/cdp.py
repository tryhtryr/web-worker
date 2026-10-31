"""
CDP（Chrome DevTools Protocol）浏览器控制层
支持 Edge / Chrome，用于启动浏览器、连接调试端口、执行 JS、监听网络事件。
"""
import collections
import json
import os
import shutil
import subprocess
import threading
import time

import requests
import websocket


BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_browser():
    for p in BROWSER_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    for name in ("chrome.exe", "msedge.exe"):
        p = shutil.which(name)
        if p:
            return p
    return None


class Browser:
    """管理一个带远程调试端口的浏览器实例。"""

    def __init__(self, port=9222, user_data_dir=None, browser_path=None):
        self.port = port
        self.user_data_dir = user_data_dir or os.path.join(
            os.path.expanduser("~"), ".eagent_profile")
        self.browser_path = browser_path or find_browser()
        self.proc = None

    def launch(self, url="https://chat.deepseek.com"):
        if not self.browser_path:
            raise RuntimeError("找不到 Chrome 或 Edge")
        os.makedirs(self.user_data_dir, exist_ok=True)
        args = [
            self.browser_path,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.user_data_dir}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            url,
        ]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        return self.proc

    def is_alive(self):
        try:
            requests.get(f"http://127.0.0.1:{self.port}/json/version", timeout=1)
            return True
        except Exception:
            return False

    def wait_ready(self, timeout=25):
        end = time.time() + timeout
        while time.time() < end:
            if self.is_alive():
                return True
            time.sleep(0.4)
        return False

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def list_pages(self):
        r = requests.get(f"http://127.0.0.1:{self.port}/json", timeout=5)
        r.raise_for_status()
        return [t for t in r.json() if t.get("type") == "page"]

    def wait_page(self, url_substr="deepseek", timeout=30):
        end = time.time() + timeout
        last = None
        while time.time() < end:
            try:
                pages = self.list_pages()
                for p in pages:
                    if url_substr in (p.get("url") or ""):
                        return p
                if pages:
                    last = pages[0]
            except Exception:
                pass
            time.sleep(0.5)
        if last:
            return last
        raise RuntimeError("没有找到可用的页面")


class CDP:
    """带后台读线程的 CDP 客户端：支持同步调用 + 事件监听。"""

    def __init__(self, ws_url, timeout=30):
        self.ws = websocket.create_connection(ws_url, timeout=timeout,
                                              suppress_origin=True)
        self._id = 0
        self._lock = threading.Lock()
        self._pending = {}
        self.events = collections.deque(maxlen=5000)
        self._ev_lock = threading.Lock()
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ---------- 后台读 ----------

    def _read_loop(self):
        while self._alive:
            try:
                raw = self.ws.recv()
            except Exception:
                break
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            mid = data.get("id")
            if mid is not None and mid in self._pending:
                slot = self._pending.pop(mid)
                slot["result"] = data
                slot["done"].set()
            else:
                with self._ev_lock:
                    self.events.append(data)

    def call(self, method, params=None, timeout=30):
        with self._lock:
            self._id += 1
            mid = self._id
        slot = {"done": threading.Event(), "result": None}
        self._pending[mid] = slot
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        if not slot["done"].wait(timeout):
            self._pending.pop(mid, None)
            raise TimeoutError(f"CDP 调用超时: {method}")
        data = slot["result"] or {}
        if "error" in data:
            raise RuntimeError(f"CDP 错误: {data['error']}")
        return data.get("result", {})

    def eval(self, expr, timeout=30):
        res = self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True,
        }, timeout=timeout)
        if res.get("exceptionDetails"):
            raise RuntimeError(f"JS 异常: {res['exceptionDetails'].get('text')}")
        return res.get("result", {}).get("value")

    # ---------- 事件 ----------

    def clear_events(self):
        with self._ev_lock:
            self.events.clear()

    def take_event(self, method, predicate=None):
        with self._ev_lock:
            for i, ev in enumerate(self.events):
                if ev.get("method") != method:
                    continue
                if predicate and not predicate(ev.get("params") or {}):
                    continue
                del self.events[i]
                return ev.get("params") or {}
        return None

    def wait_event(self, method, predicate=None, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            ev = self.take_event(method, predicate)
            if ev is not None:
                return ev
            time.sleep(0.15)
        return None

    def close(self):
        self._alive = False
        try:
            self.ws.close()
        except Exception:
            pass


def connect_page(browser, url_substr="deepseek", timeout=30):
    page = browser.wait_page(url_substr, timeout)
    return CDP(page["webSocketDebuggerUrl"])
