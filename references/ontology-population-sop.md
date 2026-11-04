# Ontology 填充 SOP

本文用于把 wiki 中已有、可验证的高价值概念分批纳入你的 ontology 文件（默认 `examples/mini-ontology.jsonl`）。目标是提高覆盖率与 ontology_alignment（本体关系对齐），同时避免为了跑分制造没有 wiki 证据的实体或关系。

## 1. 候选筛选

实体候选必须同时满足：

- wiki 图中 `in-degree >= 10`
- 当前 ontology 中尚无该实体
- wiki 中存在对应的 `.md` 页面

优先按 PageRank 排序。写入前确认页面 basename 唯一、frontmatter `title` 可解析，并记录 `source_theorist`。`entity.id` 使用页面 slug 的 basename，不带 `concepts/`、`entities/` 或 `.md`。

```json
{"op":"create","entity":{"id":"example-strategy","type":"操作策略","properties":{"title":"示例策略","description":"...","theorist":"sample-theorist"}}}
```

已有实体需要补字段时追加 `update`，不要重写历史记录：

```json
{"op":"update","entity":{"id":"example-strategy","properties":{"theorist":"sample-theorist"}}}
```

## 2. 分批节奏

每批只增加 3-5 个实体，并在每批后运行一次：

```bash
.venv/bin/python scripts/graph-test-suite.py --source wiki,ontology \
  --ontology-path examples/mini-ontology.jsonl --scorecard
```

连续执行 5 批后停下来复盘 D8、D9、D11 和 stderr 警告，再决定下一组候选。任何一批出现映射数下降、冲突增加或 D11 明显回退，都先修正该批，不继续堆数据。

## 3. 关系录入原则

添加关系前先查看源页面的 `[[wikilink]]`。只有 wiki 中存在同方向边时，才把该关系写入 ontology：

1. 确认 from/to 两个实体都已创建并能映射到 wiki 页面
2. 在 from 页面确认存在指向 to 的有效 wikilink
3. 选择允许的 ontology relation 类型
4. 追加 `relate` 记录并运行 D11 验证

不要仅凭语义相近、共同理论家或希望提高连通性来添加关系。反向 wiki 边不能验证正向 ontology 关系。

## 4. D8 加权覆盖率

D8 默认不是全量页面覆盖率，而是 wiki PageRank Top-N 的加权治理视角：

```text
D8 coverage = Top-N 中已映射节点数 / min(N, wiki 节点数)
D8 score = round(coverage * 10)
```

`D8_TOP_N` 默认是 `100`。因此优先填充高 PageRank 且高入度的真实页面，比填充大量边缘节点更有效。用 `--debug D8` 查看每个 Top-N 节点的映射状态。

## 5. D11 验证率

D11 衡量 ontology 关系中有多少被 wiki 同方向边独立验证：

```text
分子 = 被 wiki 同方向边验证的 ontology 关系数
分母 = ontology 关系总数
alignment_ratio = 分子 / 分母
D11 score = min(10, alignment_ratio * 10)
```

每增加一条尚未被 wiki 验证的关系只会增加分母，可能降低 D11。用 `--debug D11` 逐条检查 `Verified` 状态；先补 wiki 中真实缺失的关系证据，再追加 ontology 关系。

## 6. 验证清单

每批完成后检查：

- stderr 的 `[WARN] ... ontology entities not mapped`，确认没有 ID 或页面错误
- stderr 的 `[D8]` 列表，确认目标 Top-N 节点已映射
- `--debug D9` 中 declared theorist 与 wiki theorist 一致
- `--debug D11` 中新增关系为 `Verified: YES`
- `--scorecard` 的总分、D8、D9、D11 不出现非预期回退

## 7. 常见陷阱

- **无声跳过**：旧版本会忽略未映射 ontology ID。现在必须查看 stderr 警告，并逐一确认未映射实体是否有意保留。
- **ID 路径前缀**：使用 `concepts/example-strategy` 会降低可移植性。始终使用 `example-strategy` 这类 basename。
- **title 引号**：wiki title 可以使用双引号、单引号或不加引号；空引号表示空标题。不要写依赖单一引号形式的解析或生成逻辑。
- **模糊匹配歧义**：多个页面或实体命中同一 title 时不会自动融合。检查 `.merge-conflicts.json` 后人工修正 ID/title。
- **先写关系后建实体**：loader 会跳过引用未知节点的关系。先创建两个端点，再追加 `relate`。
- **用关系冲分**：没有 wiki 同向边的关系会降低 D11；ontology 关系必须可追溯到 wiki 证据。
