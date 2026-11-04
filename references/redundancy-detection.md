# Wiki 冗余检测

检测 wiki 中内容高度相似的页面对，避免重复概念分裂。

脚本：`scripts/redundancy-detector.py`（规划中，尚未实现）

## 三级方案

### L1: Jaccard Bigram（零依赖）

字符级 bigram Jaccard 相似度。秒级，适合预筛。

- **优势**：零依赖，即时运行
- **劣势**：语义弱——"深度学习训练"和"机器学习推理"可能被误判为相似
- **使用**：`python3 redundancy-detector.py --threshold 0.7 --json`

### L2: sentence-transformers（推荐）

使用 `all-MiniLM-L6-v2` 模型编码全文（384 维），余弦相似度。

- **优势**：强语义理解，准确率高
- **劣势**：首次运行需下载 ~80MB 模型（后续缓存），大 wiki 需分钟级
- **安装**：`pip install sentence-transformers`
- **使用**：`python3 redundancy-detector.py --l2 --threshold 0.80 --json`

备选模型：
- `paraphrase-multilingual-MiniLM-L12-v2`（多语言，118MB）
- `all-mpnet-base-v2`（质量更高，420MB）

### L3: TEI API

调用外部 TEI（Text Embeddings Inference）服务的 `/embed` endpoint。

- **优势**：生产级，毫秒级推理，零启动
- **劣势**：需要 TEI 服务运行中（如 RAGFlow 的 embedding 服务）
- **使用**：`python3 redundancy-detector.py --tei http://localhost:8080 --json`
- **端口**：TEI embed 通常 8080，rerank 通常 8081

## 阈值建议

| 阈值 | 含义 | 动作 |
|------|------|------|
| > 0.90 | 近乎相同 | 合并或删除一个 |
| 0.80-0.90 | 高度相似 | 审查是否应合并 |
| 0.70-0.80 | 中度相似 | 添加交叉引用，确保互补 |
| < 0.70 | 不同主题 | 不处理 |

## 文本预处理

脚本自动：
1. 去除 YAML frontmatter
2. 去除 wikilink 语法但保留链接文字
3. 去除 markdown 标记（`#*`>[]`）
4. 压缩空白
5. 截取前 2000 字符（平衡准确度和速度）

## 输出格式

```json
{
  "total_pages": 320,
  "total_pairs_checked": 51040,
  "duplicate_pairs": 12,
  "threshold": 0.80,
  "mode": "l2",
  "top_matches": [
    {
      "page_a": "concepts/token-optimization",
      "title_a": "Token 优化",
      "page_b": "concepts/token-caching",
      "title_b": "Token 缓存",
      "similarity": 0.87,
      "mode": "l2"
    }
  ]
}
```

## 性能

| Wiki 规模 | L1 (Jaccard) | L2 (s-transformers) | L3 (TEI) |
|-----------|-------------|---------------------|----------|
| 100 页 | <1s | ~2s | <1s |
| 400 页 | ~3s | ~8s | ~1s |
| 1000 页 | ~20s | ~45s | ~3s |

L2 首次运行需下载模型（~30s），后续从缓存加载（~1s）。
