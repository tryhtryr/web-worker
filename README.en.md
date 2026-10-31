# web-worker

Turn the **DeepSeek web chat** into programmable free labour, so **DeepSeek Harness (DSH)** can finish large batch development tasks using only a fraction of the API tokens.

> **The API commands. The web chat does the work.**

[简体中文](README.md) | **English**

---

## What it is

Two components:

| Component | Purpose |
|---|---|
| **bridge** | Wraps the DeepSeek web chat as an **OpenAI-compatible API** (`/v1/chat/completions`) |
| **skill** | A DSH skill that delegates bulk generation to the web chat — neither the spec nor the output enters DSH's context |

```
┌──────────────┐   HTTP    ┌──────────┐   CDP    ┌──────────────┐
│ DSH (API)    │ ────────► │ bridge   │ ───────► │ Edge browser │
│ plan/verify  │ ◄──────── │ :8787    │ ◄─────── │ chat.deepseek│
└──────────────┘           └──────────┘          └──────────────┘
       │
       └─ skill/ask_web.py calls the bridge
```

---

## Measured results

Same project (a multi-file Python CLI + tests), two approaches:

| Metric | Written by API | **Delegated to web chat** |
|---|---|---|
| API output tokens | 15,969 | **2,610** |
| API input tokens | 4,221 | **917** |
| Agent steps | 15 | **5** |
| Lines of code | 888 | **1,413** |
| Tests passing | 46/46 | **117/117** |
| LLM time | 71.8s | **27.5s** |
| Wall-clock time | 52s | ~250s |

**84% fewer output tokens, with more code produced.**

The key design: **specs are read from disk and outputs are written to disk — neither enters the caller's context.**

---

## Quick start

### 1. Requirements

- Windows 10/11
- Edge or Chrome (any Chromium browser)
- Python 3.9+

```bash
pip install -r requirements.txt
```

### 2. Start the bridge

```bash
cd bridge
python bridge.py
```

On first run a browser window opens — **you must log in to DeepSeek manually**. The session is kept in `%USERPROFILE%\.eagent_profile`, so you only log in once.

You are ready when you see:

```
web-deepseek-bridge listening on http://127.0.0.1:8787/v1
model: deepseek-web
```

Verify:

```bash
curl http://127.0.0.1:8787/health
# {"ok": true, "service": "web-deepseek-bridge"}
```

### 3. Install the skill (DSH users)

```bash
# copy skill/ into the DSH skills directory
xcopy /E /I skill "%USERPROFILE%\.dsh\skills\web-worker"
```

Restart DSH and `web-worker` shows up in the skill catalog.

### 4. Delegate work

```bash
python skill/ask_web.py \
  --spec path/to/spec.md \
  --task "implement models.py and test_models.py" \
  --out-dir path/to/out \
  --pytest
```

Output:

```
[1/4] 2 files, 306 lines, 141s → taskman/models.py(123 lines), taskman/tests/test_models.py(183 lines)
[pytest cwd] taskman
[pytest] 117 passed in 0.54s
```

---

## Repository layout

```
web-worker/
├── README.md
├── README.en.md
├── LICENSE
├── requirements.txt
├── .gitignore
├── skill/
│   ├── SKILL.md           DSH skill definition (thresholds and pitfalls)
│   └── ask_web.py         delegation CLI
├── bridge/
│   ├── bridge.py          OpenAI-compatible HTTP service
│   ├── cdp.py             CDP browser control (with network event capture)
│   ├── deepseek_web.py    page interaction and SSE parsing
│   └── llm.py             minimal model-call layer
├── scripts/
│   ├── start_bridge.bat   start the bridge
│   └── install_skill.bat  install the skill into DSH
└── docs/
    ├── HOW_IT_WORKS.md    implementation notes
    └── BENCHMARK.md       benchmark data
```

---

## Configuration

The bridge reads environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `WEBDS_HOST` | `127.0.0.1` | bind address |
| `WEBDS_PORT` | `8787` | service port |
| `WEBDS_DEBUG_PORT` | `9222` | browser debugging port |

The skill CLI reads:

| Variable | Default | Meaning |
|---|---|---|
| `WEB_WORKER_BRIDGE` | `http://127.0.0.1:8787/v1` | bridge endpoint |
| `WEB_WORKER_MODEL` | `deepseek-web` | model name |

---

## When is delegation worth it

**Hard threshold: output volume ≥ 5× the instruction volume.**

| Output size | Recommendation |
|---|---|
| < 500 lines | ❌ write it yourself |
| 500–1500 lines | ⚠️ marginal gain |
| **> 2000 lines / > 5 files** | ✅ **delegate** |
| Batch of similar files (e.g. 20) | ✅✅ strongly recommended |

**Automated verification is mandatory** (pytest / compiler / linter). If you have to read the whole output yourself, you lose all the savings.

---

## Known limitations

| Limitation | Notes |
|---|---|
| **Single concurrency** | The bridge is serial; two simultaneous requests queue up |
| **High latency** | 10–60 s per round, an order of magnitude slower than the API |
| **No token accounting** | `usage` fields are all zero |
| **Depends on page structure** | A DeepSeek redesign requires updating selectors in `deepseek_web.py` |
| **Violates the ToS** | Automating the web UI is a grey area; accounts can be restricted |
| **Unstable quality** | Complex logic often needs fixes; always verify |

---

## FAQ

### Requests time out after starting the bridge

The browser is not logged in, or the page structure changed. Open the browser window, log in again, and retry.

### `no file markers parsed`

The model did not follow the output protocol. Restate "output only `<<<FILE:...>>>` blocks" in `--task`.

### pytest reports `ImportError`

Wrong working directory. Check the `[pytest cwd]` line in the output, or pass `--pytest-cwd <project-root>`.

### Can I use it without DSH?

Yes — the bridge speaks plain OpenAI:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="x")
r = client.chat.completions.create(
    model="deepseek-web",
    messages=[{"role": "user", "content": "hello"}])
print(r.choices[0].message.content)
```

---

## Disclaimer

This project drives a browser through **CDP automation**. It is **not a reverse proxy** and does not reverse-engineer any protocol, but it still **violates the [DeepSeek Terms of Use](https://cdn.deepseek.com/policies/zh-CN/deepseek-terms-of-use.html)**.

- For learning and experimentation only — not production-ready
- Heavy usage may get your DeepSeek account restricted
- Code produced by the web chat can be wrong — **always verify**
- The authors accept no liability

---

## License

MIT
