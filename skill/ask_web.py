#!/usr/bin/env python3
"""
ask_web.py —— 把任务派给 DeepSeek 网页版（免费）执行。

核心设计目标：**让规格和产物都不进入调用方的上下文**。

用法
----
# 推荐：规格从磁盘读，产物自动拆分落盘，只回一行摘要
python ask_web.py --spec spec.md --task "实现 models.py 和 test_models.py" --out-dir out --pytest

# 一次性派多个任务（串行）
python ask_web.py --spec spec.md --task-file tasks.txt --out-dir out --pytest

# 旧用法仍然兼容
python ask_web.py "直接给提示词"
python ask_web.py --prompt-file task.md --out result.md

参数
----
--spec FILE       规格文件（从磁盘读，不经过调用方上下文）
--task TEXT       本次要做的部分（越短越好，20~50 字）
--task-file FILE  多个 task，每行一个（可带 `#` 注释）
--out-dir DIR     把 <<<FILE:xxx>>> 标记的文件自动写入该目录
--pytest          跑完自动在 out-dir 里执行 pytest，只输出摘要
--summary         只输出摘要（--out-dir 模式下默认开启）
--timeout N       单次超时秒数，默认 900

退出码：0 全部成功；1 失败
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

BRIDGE = os.environ.get("WEB_WORKER_BRIDGE", "http://127.0.0.1:8787/v1")
MODEL = os.environ.get("WEB_WORKER_MODEL", "deepseek-web")

PROTOCOL = """【输出协议 —— 严格遵守】

把要交付的每个文件用下面的标记分隔输出，不要有任何额外解释：

<<<FILE:相对路径.py>>>
文件完整内容
<<<FILE:另一个文件.py>>>
文件完整内容
<<<END>>>

不要输出 `<<<FILE:` 之外的内容。
"""


# ---------------------------------------------------------------- 桥接

def healthy():
    try:
        root = BRIDGE.rsplit("/v1", 1)[0]
        with urllib.request.urlopen(root + "/health", timeout=4) as r:
            return json.load(r).get("ok") is True
    except Exception:
        return False


def ask(prompt, system=None, timeout=900):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    payload = {"model": MODEL, "messages": msgs, "stream": False}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BRIDGE + "/chat/completions", data=data,
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        obj = json.load(r)
    ch = (obj.get("choices") or [{}])[0]
    text = (ch.get("message") or {}).get("content") or ""
    return text, ch.get("finish_reason") or "stop"


# ---------------------------------------------------------------- 拆分与落盘

FILE_MARK = re.compile(r"<<<FILE:([^>\n]+)>>>")


def split_files(text):
    """把 <<<FILE:xxx>>> 标记切成 {文件名: 内容}。"""
    parts = FILE_MARK.split(text or "")
    files = {}
    for i in range(1, len(parts) - 1, 2):
        name = parts[i].strip().replace("\\", "/")
        body = parts[i + 1]
        body = body.split("<<<END>>>")[0]
        body = re.sub(r"^\s*```[a-zA-Z]*\s*\n", "", body)
        body = re.sub(r"\n```\s*$", "", body)
        if name:
            files[name] = body.strip("\n") + "\n"
    return files


def write_files(files, out_dir):
    """按相对路径落盘，返回 [(相对路径, 行数)]。"""
    written = []
    os.makedirs(out_dir, exist_ok=True)
    for name, body in files.items():
        safe = name.replace("..", "_").lstrip("/")
        path = os.path.join(out_dir, safe)
        os.makedirs(os.path.dirname(path) or out_dir, exist_ok=True)
        io.open(path, "w", encoding="utf-8", newline="\n").write(body)
        written.append((safe, body.count("\n")))
    return written


def detect_project_root(written, out_dir):
    """从写出的文件路径推断项目根目录。

    例：文件都在 taskman/ 下 -> 返回 out_dir/taskman
        文件分散在多个顶层目录 -> 返回 out_dir
    """
    tops = set()
    for name, _ in written:
        parts = name.replace("\\", "/").split("/")
        if len(parts) > 1:
            tops.add(parts[0])
        else:
            return out_dir          # 有文件直接在根下
    if len(tops) == 1:
        return os.path.join(out_dir, tops.pop())
    return out_dir


def run_pytest(cwd):
    """跑 pytest，只返回最后一行摘要。"""
    try:
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header", "-x", "--tb=no"],
            cwd=cwd, capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace")
        out = (p.stdout or "") + (p.stderr or "")
        lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
        tail = lines[-1] if lines else "(无输出)"
        return p.returncode == 0, tail
    except Exception as e:
        return False, f"pytest 执行失败: {e}"


# ---------------------------------------------------------------- 主流程

def build_prompt(spec_text, task):
    return f"""{PROTOCOL}

【项目规格】
{spec_text}

【本次任务】
{task}
"""


def one_round(spec_text, task, out_dir, do_pytest, timeout, label="", pytest_cwd=None):
    prompt = build_prompt(spec_text, task)
    t0 = time.time()
    reply, finish = ask(prompt, timeout=timeout)
    dt = time.time() - t0
    if not reply.strip():
        return False, f"{label}网页版返回空内容（检查浏览器是否登录）"

    files = split_files(reply)
    if not files:
        head = reply.strip().splitlines()[:3]
        return False, f"{label}未解析到文件标记，返回开头: {' / '.join(head)[:200]}"

    written = write_files(files, out_dir)
    desc = ", ".join(f"{n}({ln}行)" for n, ln in written)
    msg = f"{label}{len(written)} 个文件, {sum(l for _, l in written)} 行, {dt:.0f}s → {desc}"
    if finish == "length":
        msg += ("\n⚠ 回复被网页版**截断**（达到单轮输出上限）\n"
                "   → 文件可能不完整，请把这个任务拆小（每个文件 <400 行 / 每次只派 1~2 个文件）后重派")

    if do_pytest:
        root = pytest_cwd or detect_project_root(written, out_dir)
        ok, tail = run_pytest(root)
        if root != out_dir:
            msg += f"\n[pytest cwd] {os.path.relpath(root, out_dir)}"
        msg += f"\n[pytest] {tail}"
        return ok, msg
    return True, msg


def load_tasks(args):
    if args.task:
        return [args.task]
    if args.task_file:
        tasks = []
        for line in io.open(args.task_file, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                tasks.append(line)
        return tasks
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?", help="直接给提示词（旧用法）")
    ap.add_argument("--spec", help="规格文件路径（从磁盘读，不进上下文）")
    ap.add_argument("--task", help="本次任务描述（简短）")
    ap.add_argument("--task-file", help="多个任务，每行一个")
    ap.add_argument("--prompt-file", help="从文件读完整提示词（旧用法）")
    ap.add_argument("--out", help="结果写文件（旧用法）")
    ap.add_argument("--out-dir", help="按标记自动拆分落盘的目录")
    ap.add_argument("--pytest", action="store_true", help="跑完自动执行 pytest")
    ap.add_argument("--pytest-cwd", help="pytest 的工作目录（默认自动探测）")
    ap.add_argument("--system", help="系统提示词")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    if not healthy():
        print("错误：桥接服务未运行。请先运行 C:\\fw\\DSH-启动桥接.bat", file=sys.stderr)
        return 1

    # ---- 新模式：spec + task + out-dir ----
    if args.spec and args.out_dir:
        if not os.path.isfile(args.spec):
            print(f"错误：规格文件不存在 {args.spec}", file=sys.stderr)
            return 1
        spec_text = io.open(args.spec, encoding="utf-8").read()
        tasks = load_tasks(args)
        if not tasks:
            print("错误：需要 --task 或 --task-file", file=sys.stderr)
            return 1

        all_ok = True
        for i, t in enumerate(tasks, 1):
            label = f"[{i}/{len(tasks)}] " if len(tasks) > 1 else ""
            # 多个任务时只在最后一轮跑 pytest（前面文件不全必然报错）
            do_py = args.pytest and (i == len(tasks))
            ok, msg = one_round(spec_text, t, args.out_dir, do_py,
                                args.timeout, label, args.pytest_cwd)
            print(msg, flush=True)
            all_ok = all_ok and ok
        return 0 if all_ok else 1

    # ---- 旧模式 ----
    if args.prompt_file:
        prompt = io.open(args.prompt_file, encoding="utf-8").read()
    elif args.prompt:
        prompt = args.prompt
    else:
        prompt = sys.stdin.read()

    if not prompt.strip():
        print("错误：提示词为空", file=sys.stderr)
        return 1

    try:
        reply, _finish = ask(prompt, args.system, args.timeout)
    except Exception as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1

    if not reply.strip():
        print("错误：网页版返回空内容", file=sys.stderr)
        return 1

    if args.out:
        io.open(args.out, "w", encoding="utf-8", newline="\n").write(reply)
        print(f"已写入 {args.out}（{len(reply)} 字符）", file=sys.stderr)
    else:
        sys.stdout.write(reply)
        if not reply.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
