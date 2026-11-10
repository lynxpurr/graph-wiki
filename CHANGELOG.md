# Changelog

## Unreleased — code regions come from a CommonMark parser

### Fixed

- **`~~~` 围栏与缩进代码块里的 wikilink 不再被当成死链。** 代码区判定原来只认反引号，
  于是 `~~~` 围栏、4 空格 / tab 缩进的代码块被读成正文：里面的链接按「指向不存在的页面」
  计入 `total_broken`，同时 `code_block_exempt` 少计。
- **代码区判定改用 CommonMark 解析器（markdown-it-py，已是 `scripts/requirements.txt` 的
  运行时依赖），替换手写行扫描。** 手写掩码每加一条规则就在接缝处漏一条 —— 围栏字符、
  围栏长度、缩进归属、列表续行、表格上下文都不是行扫描能还原的。
  - 块级（围栏 / 缩进代码块）取自解析器的 `token.map`；
  - 行内代码按 CommonMark 规则做反引号串**等长**配对，且只在块级代码区之外扫描
    （旧实现全文扫描，围栏内的反引号串可能拖到正文里才配对）。
  - 顺带把「每条链接扫一遍全文」降为「每个文件解析一次」。

### Measured

改动前后对 8 个语料做全字段对比（5 个自造 corpus + claims fixture + 本仓 + 一个真实 wiki）：
`linkrot_summary` 只有一处在真实 wiki 上变化 —— 一段 4 空格缩进的引用转录
`code_block_exempt` 32 → 33，`total_broken` 不变（该链接目标页存在，从来不是死链）；
其余七个语料逐字段 IDENTICAL。

### Documented

- README 豁免表补明 `code_block_exempt` 的覆盖面：反引号围栏、`~~~` 围栏、缩进代码块、行内代码。
- README 正则口径一句改正：原文写「至少要有一个非空白字符」，实测单空格 `[[ ]]` 也进解析
  并计入 `empty_target_skipped`，应为「至少要有一个字符」。
- README 补一句：空目标类按**解析后** `target` 判定，`[[#section]]` 这类同页锚点剥掉锚点后
  也是空串、同样落这一类；样本的 `as_written` 可区分二者。
- README 补**扫描面**口径：只有非隐藏目录下的 `.md` 参与图构建（`.obsidian/`、`.githooks/`、
  `.backup-*/`、`node_modules/` 被排除）。`.githooks/README.md` 单独当 wiki-root 跑是 5 条
  `code_block_exempt`，放进整库跑是 0 条——它不参与扫描。跨版本比计数前先确认目录结构一致。
- README 补**「4 空格缩进 = 代码块」的适用边界**：CommonMark 规定缩进代码块不能打断段落，
  段落中缩进 4 列的行是段落延续、仍是正文。`    [[x]]` 前有空行 → 豁免；接在段落后面 → 计死链。

### Test

6 条新回归（共 248 passed）：`~~~` 围栏、4 空格缩进块、未闭合围栏、
列表续行**不是**代码（非零对照，防漏报）、段落续行不是代码、行内代码无回归。

## Unreleased — empty wikilink targets are a malformed link, not link-rot

### Changed

- **`[[   ]]`（空白目标）不再计入死链。** 之前它被当作「指向不存在页面」记进 `total_broken`，
  产生一条 `README -> ""` 的 D4 条目——一个作者无法按「缺失页面」去修的东西。现在按豁免处理，
  与代码围栏（#6）、`skill:` 前缀（#10）同一标准：**豁免但必须可观测**。
  - 新增 `linkrot_summary.empty_target_skipped` 计数；样本的位置与另两个豁免一致，
    出现在 `diagnosis` 的 D4d 条目 `evidence.samples` 里，不单独占 summary 字段；
  - `diagnosis` 里新增 D4d P2 条目披露豁免量，不静默丢弃。
- **豁免样本保留原文写法（三类统一）。** 解析器在 yield 之前就 strip 并剥掉 `|label` / `#anchor`，
  所以四种畸形写法（`[[   ]]`、`[[   |label]]`、`[[   #section]]`、`[[　]]`）
  在样本里 `target` 全是 `""`，彼此无法区分。三类豁免样本（空目标、代码围栏、`skill:` 前缀）
  一律新增 `as_written` 字段保留原文，`target` 与计数口径不变——只增加可定位性，不动任何判定。
  - 键集统一是有原因的：`[[   ]]` 写进代码围栏会按 `code_block_exempt` 计数，`target` 同样
    归一成 `""`。此前以「另两类豁免的 target 本身带内容，不存在这个歧义」为由只给空目标加，
    该断言被这条语料证伪，故改为三类一致。
- **`[[]]`（空括号）本来就不是 wikilink**：解析正则要求 1 个以上非空白字符，它不进解析，
  也不计入任何豁免计数。两者在 diff 里难以区分，行为不同，已由测试固定。

### Documented

- `linkrot_summary` 新增 `unique_broken_meaning` 与 `empty_target_skipped_meaning` 两个
  自述字段，并在 README 补一节字段语义：**`unique_broken` 是 `(source, target)` 条目数，
  不是不同的坏目标页面数**——同一个缺失页面被两个页面引用会算 2。此前只能靠 `dedupe_key`
  反推，容易读成「多少个坏页面」。

### Why

来自一次独立测试：外部测试方在真实 wiki 上注入空目标，观察到 `total_broken` 从 3 涨到 4，
且 `unique_broken` 的语义无法从输出自证。两条都不是崩溃级缺陷，是「数字看着对、含义得追问」。

## Unreleased — pre-push privacy gate

### Added

- **`.githooks/pre-push`** blocks a push that would put personal data into this public repository.
  It scans tracked content for *shapes* — absolute home paths, private ranges, credential prefixes,
  personal mail domains, chat-platform id forms — plus any extra pattern listed in the untracked
  `$(git rev-parse --git-dir)/privacy-patterns.local`. Enable with `git config core.hooksPath .githooks`.
- Real names and hostnames stay out of the script on purpose: a scanner that hardcodes the value it
  guards leaks that value the moment the repository is public.

### Notes

- The 10.x private range is deliberately not matched: changelogs are full of version strings like
  `10.5.2`, and a gate that cries wolf gets bypassed by habit.

## Unreleased — claims gate: block structure comes from a CommonMark parse

### Changed (gate internals)

- **The claims gate no longer guesses block structure from lines.** `fence_mask`, `quote_mask`,
  `strip_quote_prefix` and their regexes are gone. `markdown-it-py` parses the PR body and the
  gate reads, for every marker line, the innermost block that owns it plus its ancestor chain.
  A claims block is accepted only as a document-level claim (`html_block` or `paragraph` owned
  by the document root); anything else is rejected and the container is named.
- **Marker identity stays line-based** (`visible_key`), so invisible padding still cannot turn a
  marker into prose or prose into a marker. Identity and structure no longer share a definition —
  that shared definition is where four rounds of bypasses lived.
- **New runtime dependency**: `markdown-it-py>=3.0`. Without it the gate exits 5 instead of falling
  back to line masks: a gate that silently checks less than it claims is worse than no gate.

### Fixed

- **Tab-indented quote inside a list item** (round 9, T4f): after `- a`, a tab puts `> note`
  inside the list item block quote, because the tab width depends on the container the line sits
  in — yet every absolute-indent regex read it as top level and let a hidden second block pass.
- **Indented code blocks (4+ spaces or a tab)** hiding a claims block are rejected and named
  (`indented code block`) instead of counting as a real block. This closes the known gap from
  round 7 (A2), which the mask era left open on purpose.

### Tests

- 48 -> 52 cases: indented code, tab-indented marker, tab-indented quote inside a list item,
  marker nested in a list item.

## Unreleased — `weekly-health-check` exit codes + severity baseline semantics

### Changed (behavior change)

- **`weekly-health-check.py` now exits non-zero when the check fails** (0 = pass, 1 = fail:
  regression alerts, missing baseline, corrupt baseline, missing severity data, internal errors).
  Previously the process always exited 0 and the verdict lived only in the text output, so
  cron/CI reading `$?` saw a false green. `--json` keeps stdout as pure JSON and the exit code
  still reflects the verdict.
- **`--exit-zero` escape hatch**: forces exit 0 for callers that relied on the old behavior.
  It does not alter the report or the `pass` field. Combinable with `--json`.

### Fixed

- **`by_severity: {}` no longer skips severity comparison**: an empty mapping means "all
  severities are zero" (the writer always normalises every P0/P1/P2 key), so a P0 increase
  against an empty-object baseline now raises an alert instead of passing silently.
- **Missing severity data fails loudly**: a baseline without `diagnosis_summary`, without
  `by_severity`, or with `by_severity: null` is now reported as an error ("基线缺少严重度数据，
  无法比较") and fails the check instead of being treated as "no baseline, nothing to compare".

## v3.10.1 (2026-11-06) — Documentation Coherence + CLI Integrity

### Fixed

- **`--json` stdout purity**: progress lines are fully silenced in JSON mode, so `--json | jq` works; the same
  holds when stderr is merged into stdout (common in CI and agent harnesses). Previously a D8 progress line
  could corrupt the JSON stream in merged-output contexts.
- **Denominator formula documented**: `max_score = 10 x scoring rows + 10 bonus ceiling`, aligned across
  README, the tool banner and JSON output for all three source combinations (120 / 130 / 140). The banner
  previously showed `items x 10` only, which disagreed with the actual denominator.
- **Examples unified on `.venv/bin/python`**: every documented command now uses the virtualenv interpreter
  instead of a bare `python3`.
- **Python 3.14 bootstrap**: documented that `python3 -m venv` fails with
  `ensurepip is not available` when `python3` is 3.14 without `python3.14-venv`, plus the
  `python3.12 -m venv` fallback.
- **Acceptance threshold surfaced**: the 60-point (60%, grade B floor) acceptance line is now printed and
  exposed as `acceptance_threshold` / `acceptance_passed` in `--json`.
- **Output language stated**: human-readable CLI output (banner, progress, tables, results) is Simplified
  Chinese; machine-readable keys stay English.
- **D-numbering namespace clarified**: the appended scoring rows `ontology_alignment` and `ragflow_coverage`
  were mislabeled `D11` / `D12` in the results header, colliding with the `D11` (cross-theorist density) and
  `D12` (entity type diversity) dimensions in the README table. They are now labeled by name. **JSON keys are
  unchanged** (`multi_source_scores.D11` / `.D12`) to preserve backward compatibility with
  `canary-gate.py` and the test suite.

### Engineering

- Pre-release audit: `gitleaks` full-history scan 0 leaks; `bandit` 0 Medium / 0 High (7 Low);
  `pytest` 132 passed + 7 subtests.
- Verified by two independent zero-context documentation trials (same-model and cross-model), both reaching
  a documented first successful run with no blocking gaps.

## v3.10.0 (2026-07-28) — Content Expansion + Engineering Consolidation

### Decision

- **Option C selected**: Content expansion + engineering consolidation. Options A (link injection) and B (threshold loosening) rejected with high-confidence evidence.
- D3 frozen at 6/10: structural ceiling proven — even large cross-link injection cannot push weighted purity below the 60% band, because untagged isolated nodes form pure communities. 60% threshold is mathematically unreachable given natural subject clustering.
- D10 frozen at 8/10: null-model baseline (200-run Erdős-Rényi, $\rho$=0.8467±0.0175) shows observed $\rho$=0.8636 at 84.5th percentile (p=0.16, not significant at $\alpha$=0.05). Threshold modifications would reward statistical noise.

### Engineering

- M4: Fixed broken pytest environment — created isolated `.venv`, installed deps, all tests pass (131 passed + 7 subtests, 0 failures).
- M5: Tagged v3.9.0, merged develop → main, pushed to origin.
- M6a: Fixed `--community all` CLI export in `graph-compute.py`.
- M6b: Added `--no-write` / `read_only` mode to suppress `.merge-conflicts.json` writes on read-only filesystems.
- M1: Injected D3/D10 calibration freeze notes + v3.10.0 decision evidence into `DESIGN_scoring_architecture.md`.

### Content

- M2: Ontology coverage expanded across the tracked subject groups (entity and relation counts increased).
- M3: New wiki pages added for the same subject groups, all with ≥3 cross-subject wikilinks and zero broken links.
- D11 alignment maintained at 100% (+61 coverage).

### Score

- Reference baseline: maintained, not increased (by design).
- Content expansion enriches the graph without chasing score thresholds. 126 is upside only.

---

## v3.9.0 (2026-07-28)

### Metric redesign

- D3 社区-理论家对齐改为按社区大小加权的平均纯度，避免单节点社区与大型社区等权；基准库基线由 `4/10` 提升至 `6/10`。
- D5 互惠链接采用知识图谱校准阈值：`10-35%` 得 10 分，`35-50%` 得 8 分，仅 `>50%` 保留回音室警告；基准库基线由 `6/10` 提升至 `10/10`。
- 新增 `scripts/null-model-baseline.py`，以定向 Erdos-Renyi `G(n,m)` 图输出 PR-入度 Spearman rho 与互惠比的 JSON 基线、标准差、95% CI 和观测值百分位。
- 所有 7 处 `community_louvain.best_partition()` 调用固定 `random_state=42`，社区划分和 D3 得分可复现。

### Score

- 基准库基线分数提升 6 分（A+ 档）。
- D3 加权纯度落入 60%-<80% 档，D5 互惠比处于 10-35% 健康区间；D10 保持 `8/10`，评分阈值未改。
- 同规模 100-run null model：rho `0.8449 +/- 0.0187`，实际 rho `0.8636` 位于第 86 百分位。

## v3.8.0 (2026-07-28)

### Known limitations

- D3 社区-理论家对齐存在已知天花板：参考基线中存在大量单节点社区。当前对社区纯度做不加权平均，使单节点社区与大型社区同权，
  其平均值缺乏解释力。该指标计划在 v3.9.0 重设计，包括社区规模加权和
  单节点社区处理策略。

## v3.7.1 (2026-07-26)

### P0 阻断修复

- `graph_loader.py`: `_build_wiki()` 解析 frontmatter `ontology_mappings`，端到端映射闭环。
- `canary-gate.py` + `graph-test-suite.py`: Canary 门禁接入真实数据流（`--export-canary-data` / `--canary-data`）。
- 评分公式百分比化：`pct = total/max_score * 100` 取代绝对阈值，修复等级膨胀。

### P1 安全与质量

- 三级隐私边界：`ONTOLOGY_OUTPUT` env var 路由 Public/Sanitized/Confidential 数据流。
- 硬编码私有路径消除，默认值切为 `examples/mini-wiki` 便携 fixture。
- D9 test 同步新语义，`.gitignore` 修正，`LICENSE`（MIT）添加。
- 文档同步：`README.md` 与脚本 CLI 对齐。

### P2 工程优化

- `scripts/_graph_utils.py`: 公共图加载模块，消除脚本间重复代码。
- `graph_loader.py` + 客户端脚本: 错误处理加固（`exit 1` + `ERROR:` stderr）。
- `weekly-health-check.py`: severity baseline 比较修复（`by_severity.P0/P1/P2`）。
- `requirements.txt` / `requirements-dev.txt`: 版本标准化。

### Mini 语料转向

- Wiki/Ontology/RAGFlow 全部转向 Computer Science 基础（16 页面, 16 实体, 66 关系, 7 chunks）。
- 6 个缺失 wikilink 页面补全 + L2 措辞修正。
- Mini corpus 评分: `100.18/130 (77%)` 2-source, `113.06/140 (81%)` 3-source。

### 审查


## v3.7.0 (2026-07-26)

### 评分模型

- Bonus 公式从 `(D8+D10)*0.5` 扩展为 `(D8+D9+D10)/3`，D9 consistency 维度进入得分体系。
- `D10_PRIMARY` 默认值从 `'wiki'` 切换为 `'ontology'`。

### 自动化工具（Phase 2-4）

- `suggest-mappings.py`：三层匹配管道（字符串 + 语义 + 图结构），自动推荐 ontology 候选映射。
- `_matching.py`：匹配算法库（字符串相似度 + 社区亲和度）。
- `orphan-report.py`：Top-100 PR 未映射节点分类（mappable/needs_entity/ambiguous）+ spec 输出。
- `boost-stub.py`：低 PR 映射节点 wikilink 注入建议（社区感知 + 现有链接排除）。

### 测试

- `tests/test_suggest_mappings.py`：14 个 unit tests。
- `tests/test_orphan_report.py`：5 个 unit tests。
- `tests/test_boost_stub.py`：6 个 unit tests。

### 终态评分

```
总分: 118/120 (98%) A+
raw=108 + bonus=10
D8-D11: 40/40 ✅ 满分
Ontology: 108 实体, 88 关系
```

---

## v3.6.2 (2026-07-26)

### Added

- `canary-gate.py`: D8/D11 多源维度门禁规则。
- `graph-test-suite.py`: `--quiet` CI 模式和维度失败退出码。
- `tests/test_scorecard.py`: scorecard JSON schema 验证。
- `CONTRIBUTING.md` 和 `CHANGELOG.md`。

### Enhanced

- `README.md`: 评分维度说明、API 文档和 CI 集成示例。
- Scorecard JSON: 补充 `d8_detail` 和 `d11_detail` 字段。

## v3.6.1 (2026-07-26)

### Added

- D9 consistency: ontology type 与 wiki theorist 映射。
- `--debug` 模式（D8/D9/D10/D11/fusion/mappings）。
- `D10_PRIMARY` 环境变量。
- `tests/test_title_extraction.py`。
- `references/ontology-population-sop.md`。

### Changed

- D8: 改为 PageRank Top-100 加权覆盖率（`D8_TOP_N` 可配置）。
- `graph_loader.py`: unmapped entity stderr 警告。
- Scorecard: 权重公式透明化。

### Fixed

- `graph_loader.py`: title regex 兼容无引号 frontmatter。
