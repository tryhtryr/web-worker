# web-worker

把 **DeepSeek 网页版** 变成可编程的免费劳动力，让 **DeepSeek Harness（DSH）** 只花少量 API token 就能完成大批量开发任务。

> **API 指挥，网页版干活。**

---

## 这是什么

两个组件：

| 组件 | 作用 |
|---|---|
| **bridge** | 把 DeepSeek 网页版包装成 **OpenAI 兼容 API**（`/v1/chat/completions`） |
| **skill** | DSH 技能：把批量生成任务派给网页版，规格和产物都不进入 DSH 上下文 |

```
┌──────────────┐   HTTP    ┌──────────┐   CDP    ┌──────────────┐
│ DSH (API)    │ ────────► │ bridge   │ ───────► │ Edge 浏览器   │
│ 拆解/验证    │ ◄──────── │ 8787     │ ◄─────── │ chat.deepseek │
└──────────────┘           └──────────┘          └──────────────┘
       │
       └─ skill/ask_web.py 负责调用 bridge
```

---

## 实测效果

同一个项目（多文件 Python CLI + 测试）三种做法对比：

| 指标 | 自己写 | **派给网页版** |
|---|---|---|
| API 输出 token | 15,969 | **2,610** |
| API 输入 token | 4,221 | **917** |
| 步数 | 15 | **5** |
| 代码行数 | 888 | **1,413** |
| 测试通过 | 46/46 | **117/117** |
| LLM 耗时 | 71.8s | **27.5s** |
| 墙钟耗时 | 52s | ~250s |

**API 输出 token 降低 84%，产出反而更多。**

关键设计：**规格从磁盘读、产物写回磁盘，两者都不进入调用方上下文。**

---

## 快速开始

### 1. 环境要求

- Windows 10/11
- Edge 或 Chrome（自带即可）
- Python 3.9+

```bash
pip install -r requirements.txt
```

### 2. 启动桥接

```bash
cd bridge
python bridge.py
```

首次运行会自动打开浏览器，**需要手动登录 DeepSeek**（登录态保存在 `%USERPROFILE%\.eagent_profile`，以后不用再登）。

看到这行就成功了：

```
web-deepseek-bridge 监听 http://127.0.0.1:8787/v1
模型名: deepseek-web
```

验证：

```bash
curl http://127.0.0.1:8787/health
# {"ok": true, "service": "web-deepseek-bridge"}
```

### 3. 安装技能（DSH 用户）

```bash
# 把 skill/ 拷到 DSH 技能目录
xcopy /E /I skill "%USERPROFILE%\.dsh\skills\web-worker"
```

重启 DSH 后，技能目录里会出现 `web-worker`。

### 4. 派活

```bash
python skill/ask_web.py \
  --spec path/to/spec.md \
  --task "实现 models.py 和 test_models.py" \
  --out-dir path/to/out \
  --pytest
```

输出：

```
[1/4] 2 个文件, 306 行, 141s → taskman/models.py(123行), taskman/tests/test_models.py(183行)
[pytest cwd] taskman
[pytest] 117 passed in 0.54s
```

---

## 目录结构

```
web-worker/
├── README.md
├── LICENSE
├── requirements.txt
├── .gitignore
├── skill/
│   ├── SKILL.md           DSH 技能说明（含阈值与常见坑）
│   └── ask_web.py         派活脚本
├── bridge/
│   ├── bridge.py          OpenAI 兼容 HTTP 服务
│   ├── cdp.py             CDP 浏览器控制（含网络事件监听）
│   ├── deepseek_web.py    页面交互与 SSE 解析
│   └── llm.py             最小模型调用层
├── scripts/
│   ├── start_bridge.bat   启动桥接
│   └── install_skill.bat  安装技能到 DSH
└── docs/
    ├── HOW_IT_WORKS.md    原理详解
    └── BENCHMARK.md       基准测试数据
```

---

## 配置

桥接通过环境变量配置：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WEBDS_HOST` | `127.0.0.1` | 监听地址 |
| `WEBDS_PORT` | `8787` | 服务端口 |
| `WEBDS_DEBUG_PORT` | `9222` | 浏览器调试端口 |

技能脚本通过环境变量配置：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WEB_WORKER_BRIDGE` | `http://127.0.0.1:8787/v1` | 桥接地址 |
| `WEB_WORKER_MODEL` | `deepseek-web` | 模型名 |

---

## 什么时候值得派活

**硬门槛：产出量 ≥ 说明量的 5 倍。**

| 产出规模 | 建议 |
|---|---|
| < 500 行 | ❌ 自己写 |
| 500~1500 行 | ⚠️ 收益不明显 |
| **> 2000 行 / > 5 个文件** | ✅ **派活** |
| 批量同构（20 个相似文件） | ✅✅ 强烈推荐 |

**必须能自动验收**（pytest / 编译 / lint），否则你得读全文，等于白干。

---

## 已知限制

| 限制 | 说明 |
|---|---|
| **单并发** | 桥接是串行的，同时两个请求会排队 |
| **延迟高** | 每轮 10~60 秒，比 API 慢一个数量级 |
| **无 token 统计** | `usage` 字段全是 0 |
| **依赖页面结构** | DeepSeek 改版后 `deepseek_web.py` 的选择器要跟着改 |
| **违反用户协议** | 自动化操作网页版属于灰色地带，账号有被限制的风险 |
| **质量波动** | 复杂逻辑容易出错，必须验收 |

---

## 常见问题

### 桥接启动后请求超时

浏览器没登录，或页面结构变了。点开浏览器看一眼，重新登录后重试。

### `未解析到文件标记`

模型没按协议输出。在 `--task` 里重申"只输出 `<<<FILE:...>>>` 格式"。

### pytest 报 `ImportError`

跑错目录了。看输出里的 `[pytest cwd]`，或用 `--pytest-cwd <项目根>` 手动指定。

### 能不能不用 DSH，直接调用？

可以。桥接是标准 OpenAI 接口：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="x")
r = client.chat.completions.create(
    model="deepseek-web",
    messages=[{"role": "user", "content": "你好"}])
print(r.choices[0].message.content)
```

---

## 免责声明

本项目通过 **CDP 自动化操作浏览器**，**不是反向代理、不逆向协议**，但仍然**违反了 [DeepSeek 用户协议](https://cdn.deepseek.com/policies/zh-CN/deepseek-terms-of-use.html)**。

- 仅供学习交流，不适用于生产
- 高频使用可能被 DeepSeek 限制账号
- 网页版生成的代码可能有问题，**必须验收**
- 作者不承担任何责任

---

## License

MIT
