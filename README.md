# graph-wiki: Markdown Knowledge Graph Analyzer

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-132%2B7%20subtests%20hermetic-brightgreen)](tests/)

从 Markdown `[[wikilinks]]` 或 ontology JSONL 构建有向图，进行全量图计算分析。

```
输入: Markdown 文件 [[wikilinks]] / ontology JSONL 实体关系
计算: networkx PageRank + Louvain 社区检测 + 桥接节点 + 最短路径 + 扩散分析
输出: 单源 11 维 / wiki+ontology 12 维评分 + D1-D11 指标 + P0/P1/P2 修复清单
运维: 周检脚本 + 回归基线 + 自动告警门禁
```

**输出语言**：CLI 的人类可读输出（横幅、进度、表格、详细结果）全部为简体中文，不提供英文界面；`--json` / `--scorecard` 的机器可读键名保持英文。

## 架构

```
graph_loader.py           ← 统一图构建入口（wiki / ontology / 离线 ragflow dump）
    ↓
graph-compute.py          ← 图算法（PageRank / Louvain / 桥接 / 最短路）
graph-diagnose.py         ← 结构诊断 → P0/P1/P2 任务清单
graph-test-suite.py       ← 单源 11 维；wiki+ontology 追加 ontology_alignment（本体关系对齐）评分
```

## 安装

建议使用项目虚拟环境，避免系统 Python 的依赖冲突。Debian/Ubuntu 需先安装 venv 组件（如 `sudo apt install python3-venv` 或对应版本的 `python3.12-venv`）。

```bash
cd /path/to/graph-wiki
python3 -m venv .venv        # Python 3.14 缺 ensurepip 时改用：rm -rf .venv && python3.12 -m venv .venv
.venv/bin/python -m pip install -r scripts/requirements.txt -r tests/requirements-dev.txt
```

> **Python 3.14 注意**：system `python3` 为 3.14 且未装 `python3.14-venv` 时，`python3 -m venv .venv` 会以 `The virtual environment was not created successfully because ensurepip is not available` 失败（venv 引导问题，不是依赖问题）。装不了 apt 包或没有 sudo 时，回退到 `rm -rf .venv && python3.12 -m venv .venv`，之后所有命令都用 `.venv/bin/python` 执行。
> **必须先删残留**：上一步失败后会留下一个**残缺的 `.venv/`**（有 `python` 符链，没有 `pip`）。直接在它上面跑 `python3.12 -m venv .venv` 会以 exit 0 “成功”，但 `python -m pip` 仍报 `No module named pip`——必须先 `rm -rf .venv` 再建。

## 快速开始

### 自包含示例语料（克隆即可跑）

```bash
.venv/bin/python scripts/graph-test-suite.py \
  --source wiki,ontology \
  --wiki-root examples/mini-wiki \
  --ontology-path examples/mini-ontology.jsonl
```

`examples/` 还包含 `examples/mini-ragflow.jsonl`（离线 chunk dump 样例），可直接体验 ragflow 模式：

```bash
.venv/bin/python scripts/graph-test-suite.py \
  --source ragflow --ragflow-path examples/mini-ragflow.jsonl
```

### Wiki 模式（分析你自己的库）

```bash
cd /path/to/your/wiki
/path/to/graph-wiki/.venv/bin/python /path/to/graph-wiki/scripts/graph-test-suite.py   # 1. 跑分（11 项 × 10 分 + 10 bonus 上限 = 120 分）
/path/to/graph-wiki/.venv/bin/python /path/to/graph-wiki/scripts/graph-diagnose.py     # 2. 诊断（结构问题 → 任务清单）
# 3. 按 P0 → P1 → P2 执行修复，回到步骤 1
```

### Wiki + Ontology 治理模式

```bash
.venv/bin/python scripts/graph-diagnose.py \
  --source wiki,ontology --wiki-root /path/to/wiki \
  --ontology-path /path/to/ontology.jsonl --json
.venv/bin/python scripts/graph-test-suite.py \
  --source wiki,ontology --wiki-root /path/to/wiki \
  --ontology-path /path/to/ontology.jsonl
```

### 回归测试

```bash
.venv/bin/python -m pytest tests/ -v
```

测试全部使用仓库内置的合成数据（`examples/mini-*`），不依赖外部服务与网络。

## 数据路径

- `examples/mini-wiki/`、`examples/mini-ontology.jsonl`、`examples/mini-ragflow.jsonl`：自包含示例语料，所有默认路径指向这里。
- Wiki 根目录通过 `--wiki-root /path/to/wiki` 或 `WIKI_ROOT` 显式配置。
- Ontology 通过 `--ontology-path /path/to/ontology.jsonl` 或 `ONTOLOGY_PATH` 显式配置。

## 工具速查

| 工具 | 功能 | 关键输出 |
|------|------|---------|
| `graph_loader.py` | 统一图构建 | `(DiGraph, meta_dict)` — wiki / ontology / 离线 ragflow |
| `graph-test-suite.py` | 量化跑分 | wiki-only 120 分；wiki+ontology 追加 D11 后 130 分；含 ragflow 为 140 分（各项 10 分 + 10 分 bonus 上限） |
| `graph-diagnose.py` | 结构诊断 | D1-D7 结构问题 + 多源 D8-D10 融合指标 |
| `graph-compute.py` | 图算法 | PageRank Top15 / 社区 / 桥接 / 最短路 |
| `canary-gate.py` | 质量门禁 | PASS/WARN/REVIEW/BLOCK 四态退出码 |
| `weekly-health-check.py` | 周检自动化 | 评分回归检测 / 退行告警 / 基线对比 / 0-1 退出码 |

### 回归基线路径（issue #1）

`wiki_analyzer.py baseline-save`（写入方）、`weekly-health-check.py` 与 `canary-gate.py`
（读取方）共用同一套解析规则（`scripts/baseline_paths.py`）：同一组
`--source` / `--wiki-root` / `--ontology-path` 必然指向同一个文件。

| 源 | 基线路径 |
|----|---------|
| 仅 `wiki` / `ragflow` | `<wiki_root>/.graph-baseline.json` |
| 仅 `ontology` | `dirname(--ontology-path)/.graph-baseline.json` |
| 多源 | `<base_dir>/.graph-baseline-<slug>.json`（`base_dir`：含 ontology 时为 ontology 目录，否则为 wiki 根） |

旧版单源文件 `<wiki_root>/.regression-baseline.json` 在新文件不存在时仍会读取，
并在输出中给出迁移提示。周检在**没有基线**时显式报 `(无基线)` 且 `pass: false`
——回归对比未执行不算通过。

### 基线里的 `by_severity` 语义

周检按严重度（P0/P1/P2）对比时，对基线中的 `diagnosis_summary.by_severity` 分四种情况处理：

| 基线内容 | 处理 | 理由 |
|---------|------|------|
| 正常字典（如 `{"P0": 0, "P1": 0, "P2": 2}`） | 逐级比较；缺某个键时按 0 | 兼容旧写入方省略零计数的行为 |
| 空对象 `{}` 且 `total == 0` | 按「各级均为 0」正常比较 | 旧写入方在**零问题**时的真实产物 |
| 空对象 `{}` 且 `total > 0` | 判为**数据不自洽**：进 `errors`，`pass: false` | 声称有问题却说不出分级，不能凭空把基线当 0（那会伪造「0 → N」的假增量） |
| 缺 `by_severity` / 缺 `diagnosis_summary` / 值为 `None` | 判为**无法比较**：进 `errors`，`pass: false` | 无信息量时静默放行正是假通过通道 |

也就是说：**没有分级数据时，周检一律判「未通过」而不是「未退化」**——但会区分「确实是零」与「说不清楚」。

### `linkrot_summary` 字段语义

`graph-diagnose --json` 的 `linkrot_summary` 里，三个计数**不是同一个口径**，读之前先看这张表：

| 字段 | 含义 |
|------|------|
| `total_broken` | **原始引用次数**（raw wikilink references），不是去重后的工作项 |
| `unique_broken` | 去重后的 **`(source, target)` 条目数**——不是不同的坏目标页面数 |
| `duplicated_entries` | 被合并掉的引用数 = `sum(occurrences - 1)` |
| `entries_with_duplicates` | `occurrences > 1` 的条目数 |

恒等式恒成立，可直接当自检用：

```
total_broken == unique_broken + duplicated_entries
```

**`unique_broken` 最容易读错。** 同一个缺失页面被两个页面引用，会算成 **2**，而不是 1——因为去重键是 `(source, target)` 而不是 `target`：修 A 页的 `[[gone]]` 和修 B 页的 `[[gone]]` 是两个独立动作。

**豁免项一律可观测，不静默丢弃。** 三种链接按设计不计入死链，但各自留计数：

| 字段 | 豁免理由 |
|------|---------|
| `code_block_exempt` | 代码围栏 / 行内代码内的 wikilink——代码示例不是引用 |
| `skill_scheme_exempt` | `skill:` / `skills:` 前缀——不是页面链接 |
| `empty_target_skipped` | 空或全空白的 `[[   ]]` 目标——**写残的链接，不是指向缺失页面** |

每个豁免量同时以 P2 条目出现在 `diagnosis` 里，附带样本，避免「豁免」变成「静默消失」。

注意正则口径：`[[ ]]` 至少要有一个非空白字符才会被解析成 wikilink，所以`[[]]`（空括号）根本不进解析、也不计入 `empty_target_skipped`；`[[   ]]`（空格）会进解析然后被豁免计数。两者在 diff 里长得一样，行为不同。

### weekly-health-check.py 退出码（行为变更）

周检现在会按判定结果设置退出码，cron / CI 直接读 `$?` 即可，不必再 grep 文本：

| 退出码 | 含义 |
|--------|------|
| 0 | 判定通过（无告警且无执行错误） |
| 1 | 判定未通过——退化告警、无基线、基线损坏（非法 JSON）、基线缺严重度数据、内部执行错误 |

`--json` 模式下同样适用：JSON 照常打到 stdout，退出码仍反映判定结果
（`--json | jq` 的管道状态请分别检查，`$?` 取的是管道最后一环）。

**行为变更**：旧版本无论判定如何都退出 0，只看退出码的调用方会把失败当成功。
如果某个调用方依赖旧行为（例如「周检失败也要继续后续步骤」），请显式加上
`--exit-zero`：它无条件返回 0，但**不改变**输出文本与 `pass` 字段——判定结果
依旧如实打印。该开关可与 `--json` 组合使用。

### graph-test-suite.py 退出码与验收阈值

| 退出码 | 含义 |
|--------|------|
| 0 | 运行完成（默认表格或 `--json`）——**不代表**已达验收阈值，请读输出中的分数 |
| 1 | 仅 `--scorecard` / `--quiet`：存在单项 < 4 分的维度（stderr 打印 `Failing dimensions`） |
| 2 | 参数错误（argparse / 无效的 `--diagnosis-json` / canary 数据导出失败） |

`--json` 时 stdout 严格只输出 JSON（D8 进度行等人类可读信息静默），可直接 `| jq`。验收阈值固定为 **60 分（即 60%，评级 B 起点）**，每次运行结束打印 `验收阈值: 60 分（60%，评级 B 起点）→ 通过/未通过`；`--json` 模式下对应 `summary.acceptance_threshold` 与 `summary.acceptance_passed`。

## 依赖

- Python 3.10+（SQLite 3.35+）
- `networkx`
- `scipy`
- `python-louvain` (community detection)
- `markdown-it-py` (PR claims gate block structure, `scripts/verify-claims.py`)
## 开发约定：推送前隐私门禁

这是一个公开仓库。`.githooks/pre-push` 在每次 `push` 前机械扫描待推送内容，命中即阻断：

```bash
git config core.hooksPath .githooks   # 每个 clone 设一次
```

扫描的是**形状**而不是名单——绝对家目录路径、私网地址、凭证前缀（`sk-` / `ghp_` / `AKIA` 等）、
个人邮箱域、聊天平台 ID 形状。真实姓名、主机名与内网地址一律不进仓库，写在本地的未跟踪文件里：

```
$(git rev-parse --git-dir)/privacy-patterns.local   # 每行一个扩展正则
```

它位于 `.git` 内，无法被提交。这一点是刻意的：把真实用户名写进扫描脚本，等于用扫描脚本泄露它要防的东西。

需要绕过时用 `git push --no-verify`，并在 PR 里点名是哪一处误报。


## 适用场景

适用于任何使用 `[[wikilinks]]` 双向链接的 Markdown 知识库（Obsidian、Foam、自定义 wiki 等），以及使用 ontology JSONL 格式的实体关系图谱数据。

**隐私**：所有分析都在本机进行；工具只读取你显式指定的 wiki / ontology 路径，不发送任何数据到网络（ragflow realtime 模式除外——仅在显式提供 API key 时启用，且显式指定的离线 dump 永远优先）。

## 文档

- `DESIGN_scoring_architecture.md` — 评分体系设计与 D3/D10 冻结的统计论证
- `CHANGELOG.md` — 版本历史
- `references/` — 详细配方（孤岛缝合、冗余检测、扩展指标 等）

## 局限

- `redundancy-detector.py` 和 `extra-metrics.py` 尚未实现（见 `references/` 中的 L1/L2/L3 冗余检测方案和 D8/D9/D10 扩展指标说明）
- 脚本假设所有 `.md` 文件参与图构建；不支持目录级过滤配置
- 大型 wiki（>10K 页）的性能未经测试

## 评分维度 (v3.6)

### 主维度 (D1-D12)

| 维度 | 名称 | 满分 | 说明 |
|------|------|------|------|
| D1 | 多跳最短路径 | 10 | 3+ 跳概念对的最短路径发现 |
| D2 | 中心性三角 | 10 | PageRank vs Betweenness vs Closeness |
| D3 | 社区-理论家对齐 | 10 | Louvain 社区 vs 理论家来源一致性 |
| D4 | 结构洞检测 | 10 | 删除后网络碎裂最严重的枢纽节点 |
| D5 | 互惠链接 | 10 | 双向引用概念对检测 |
| D6 | 死胡同检测 | 10 | 出度为 0 的概念 |
| D7 | 图直径 | 10 | 最长最短路径与网络紧凑度 |
| D8 | 种子扩散 | 10 | 从枢纽概念可触达的概念覆盖率 |
| D9 | 自引用检测 | 10 | 概念是否意外引用自己 |
| D10 | PR-入度相关性 | 10 | PageRank 与简单入度的排序相关性 |
| D11 | 跨理论家密度 | 10 | 不同理论家概念间的链接覆盖率 |
| D12 | 实体类型多样性 | 10 | Ontology 实体类型的 Shannon 熵（仅 ontology 源）|

追加评分：`ontology_alignment`（wiki+ontology）、`ragflow_coverage`（ragflow 源）；多源融合三项（覆盖率/一致性/信息增益）参与 bonus，上限 10 分。诊断指标（孤立节点/链接腐烂）见 `graph-diagnose.py`，不计分。

**满分公式（与工具打印一致）**：`max_score = 10 × 评分行数 + 10 bonus 上限`。评分行数 = 实际执行的 10 分项数（含追加评分）：

| 源 | 评分行数 | 满分 |
|----|---------|------|
| `wiki` | 11（D1-D11） | **120** |
| `wiki,ontology` | 12（D1-D11 + `ontology_alignment`） | **130** |
| `ontology` | 12（D1-D11 + 实体类型多样性） | **130** |
| `wiki,ontology,ragflow` | 13（再加 `ragflow_coverage`） | **140** |

bonus = `round((覆盖率 + 一致性 + 信息增益) / 3)`，上限 10 分；即使只拿到部分 bonus，分母仍按 +10 计入，因此两源示例的总分行是 `100.14/130`（而不是 `/120`），三源示例是 `113.61/140`。

### 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `WIKI_ROOT` | 当前工作目录 | Wiki 根目录 |
| `ONTOLOGY_PATH` | `examples/mini-ontology.jsonl` | Ontology JSONL 路径 |
| `RAGFLOW_API_KEY` | 空 | ragflow realtime 模式（可选；显式离线路径时忽略） |
| `D8_TOP_N` | `100` | D8 加权覆盖率 Top-N |
| `D10_PRIMARY` | `ontology` | D10 主视角 (`wiki` / `ontology`) |

### API

```python
import sys
sys.path.insert(0, "scripts")

from graph_loader import build_graph
from scorecard import build_scorecard

graph, metadata = build_graph(
    "wiki,ontology",
    wiki_root="/path/to/wiki",
    ontology_path="/path/to/ontology.jsonl",
)
scorecard = build_scorecard(graph=graph, source="wiki,ontology")
print(scorecard["metrics"]["composite"]["health_score"])
```

### Canary Gate (CI 集成)

```bash
.venv/bin/python scripts/canary-gate.py \
  --source wiki \
  --wiki-root /path/to/wiki \
  --baseline baseline.json \
  --json

# 退出码: 0=PASS, 1=WARN, 2=REVIEW, 3=BLOCK
echo $?
```

## 许可

[MIT](LICENSE) — Copyright (c) 2026 lynxpurr
