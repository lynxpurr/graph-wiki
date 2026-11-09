# Wiki 扩展指标（D8/D9/D10）

补充 graph-diagnose.py 的 7 项诊断之外的三项新指标。脚本：`scripts/extra-metrics.py`（规划中，尚未实现）

## D8: 出链密度分布

### 检测内容

统计 wiki 页面的出链数分布：

| 桶 | 含义 |
|-----|------|
| `<2` | 出链 <2（信息孤岛回潮信号） |
| `2-4` | 出链 2-4（偏低但可接受） |
| `5-9` | 出链 5-9（健康区间） |
| `10+` | 出链 10+（高连接度） |

### 告警阈值

- `<2` 出链页面超过基线的 +10 页 → 告警
- 重点：高入度但 <2 出链的页面（被广泛引用但不回馈）— 这是海绵节点的变体

### 输出格式

JSON:
```json
{
  "outlink_density": {
    "distribution": {"<2": 45, "2-4": 120, "5-9": 89, "10+": 55},
    "low_outlink_count": 45,
    "low_outlink_pct": 14.6,
    "low_outlink_top10": [{"slug": "...", "in_degree": 5, "out_degree": 0}]
  }
}
```

## D9: 索引漂移

### 检测内容

对比两个指标：
1. **磁盘实际**：`os.walk()` 扫描的 `.md` 文件数（排除 index.md, log.md, SCHEMA.md）
2. **index.md 声称**：正则 `Total pages:\s*(\d+)` 提取的数字

### 额外检查

- index.md 中的 wikilink 数
- index.md 中的 section 数
- index.md 中的死链（指向不存在的页面）

### 告警阈值

- 漂移 > ±3 页 → 告警
- index.md 中有死链 → 提示修复

### 输出格式

JSON:
```json
{
  "index_drift": {
    "disk_md_count": 320,
    "index_claimed_count": 362,
    "drift": 29,
    "index_wikilinks": 380,
    "index_sections": 5,
    "broken_links_in_index": 3,
    "broken_targets": ["concepts/old-page", "..."]
  }
}
```

## D10: 自检抽样

### 基础版（零依赖）

随机选 5 页，检查：
- Frontmatter 完整性：has_title, has_created, has_updated, has_theorist
- Wikilink 解析率：多少 wikilink 指向存在的页面
- 内容规模：行数、字符数
- 自引用检测

### LLM 版（需 DEEPSEEK_API_KEY）

对每页调用 DeepSeek，判断：
1. 标题是否准确反映内容 (yes/no/warn)
2. 内容是否有实质性信息 (yes/no/warn)
3. wikilinks 的目标是否与该页相关 (yes/no/warn)

```bash
DEEPSEEK_API_KEY=sk-xxx python3 extra-metrics.py --selfcheck --json
```

### 成本

5 页 × 200 token/页（提问）+ ~150 token/页（回答）≈ 1750 token，约 $0.001

## 接入周检

在 `.graph-baseline.json` 的 `diagnose` 块中添加（旧版单源文件名
`.regression-baseline.json` 仍向后兼容可读）：

```json
"outlink_low": 45,
"index_drift": 0
```

weekly-health-check.py 会自动读取并对比。
