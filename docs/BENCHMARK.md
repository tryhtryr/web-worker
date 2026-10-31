# 基准测试

同一个项目、同一套验收标准，对比"自己写"与"派给网页版"的真实 token 消耗。

---

## 测试项目

**taskman** —— 多文件 Python CLI 任务管理工具

```
taskman/
├── taskman/
│   ├── __init__.py
│   ├── models.py      任务数据模型
│   ├── store.py       JSON 持久化
│   ├── report.py      统计报告
│   └── cli.py         命令行入口
├── tests/
│   ├── test_models.py
│   ├── test_store.py
│   ├── test_report.py
│   └── test_cli.py
└── README.md
```

验收标准：`pytest` 全部通过 + CLI 冒烟（add/list/done/stats）。

---

## 对照组：规格写进提示词（已淘汰）

| 指标 | A 自己写 | B v1 |
|---|---|---|
| 输出 token | 15,969 | 9,984 |
| 输入 token | 4,221 | 5,472 |
| 缓存读取 | 425,472 | 674,048 |
| 加权成本 | 1.00× | **1.24×（更贵）** |

原因：提示词本身 430 行，写进上下文后每一步都要重读。
详见 `docs/HOW_IT_WORKS.md` 第七节。

## 当前方案：规格从磁盘读

同样的项目，改用当前流程：

```bash
python skill/ask_web.py --spec spec.md --task-file tasks.txt --out-dir out --pytest
```

4 个任务串行派活。

| 指标 | A 自己写 | **B 派活 v2** | 变化 |
|---|---|---|---|
| **输出 token** | 15,969 | **2,610** | **−83.7%** |
| 输入 token | 4,221 | **917** | −78.3% |
| 步数 | 15 | **5** | −66.7% |
| LLM 耗时 | 71.8 s | **27.5 s** | −61.7% |
| 代码行数 | 888 | **1,413** | +59% |
| 测试数 | 46 | **117** | +154% |
| 测试通过 | 46/46 | **117/117** | 持平 |
| 墙钟耗时 | 52 s | ~250 s | +380% |

### 各轮明细

```
[1/4] 2 个文件, 306 行, 141s → models.py(123行), test_models.py(183行)
[2/4] 2 个文件, 411 行,  25s → store.py(169行), test_store.py(242行)
[3/4] 2 个文件, 395 行,  19s → report.py(148行), test_report.py(247行)
[4/4] 5 个文件, 425 行,  20s → __init__.py, __main__.py, cli.py, test_cli.py, README.md
[pytest cwd] taskman
[pytest] 117 passed in 0.54s
```

第一轮 141 秒（冷启动 + 页面导航），后续每轮 19~25 秒。

---

## 关键结论

### 1. 省 token 的机制不是"输出少"，而是"步数少"

每多一步，整个上下文（含缓存）就要重读一遍。

| 做法 | 步数 | 每步额外负担 |
|---|---|---|
| 自己写 | 15 | — |
| 派活 v1（提示词进上下文） | 16 | 4300 token × 剩余步数 |
| **派活 v2（规格在磁盘）** | **5** | **0** |

### 2. 什么规模才值得派活

| 产出规模 | 结论 |
|---|---|
| < 500 行 | ❌ 自己写 |
| 500~1500 行 | ⚠️ 收益不明显（v1 的教训） |
| **> 2000 行 / > 5 文件** | ✅ **派活** |
| 批量同构（20 个相似文件） | ✅✅ 强烈推荐 |

### 3. 必须能自动验收

如果派活后还得读全文检查，等于把产物拉进上下文，省的 token 全还回去了。

**正确做法**：`--pytest` 让脚本跑测试，只回一行摘要。

### 4. 时间成本是硬代价

墙钟从 52 秒涨到 250 秒（4.8 倍）。紧急任务别派活。

---

## 复现方法

```bash
# 1. 启动桥接
scripts\start_bridge.bat

# 2. 准备规格文件（参考 docs/HOW_IT_WORKS.md 的项目结构）

# 3. 写任务列表 tasks.txt（每行一个任务）
#    实现 models.py 和 test_models.py
#    实现 store.py 和 test_store.py
#    ...

# 4. 派活
python skill/ask_web.py --spec spec.md --task-file tasks.txt --out-dir out --pytest

# 5. 记录你（调用方）的 token 消耗
#    DSH 用户：自行统计本会话的 token 增量
```

---

