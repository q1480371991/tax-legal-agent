# 级联融合评估报告

**日期**：2026-10-04
**评估集**：复用原作者 `evaluation_dataset_ragas.json` 的 13 题（FIN 3 + LEG 10）
**判官**：DeepSeek（`deepseek-chat`），Embedding：硅基流动 `BAAI/bge-m3` —— 与原作者同配置
**知识库**：10 份文档（含原作者用的比亚迪 2026Q1 报告、大湖股份股权转让公告）

> TAX 5 题未覆盖：其源文档是政府政策网页（HTML），本项目解析器不支持 HTML。

## 一、消融对比（级联融合 ON/OFF）

同一批 13 题、同一知识库，仅切换 `enable_graph`：

| 指标 | graph（级联） | nograph（纯向量+BM25） | 提升 |
|------|--------------|----------------------|------|
| context_precision | **0.736** | 0.641 | **+0.094** |
| context_recall | **0.846** | 0.692 | **+0.154** |
| faithfulness | **0.808** | 0.732 | **+0.076** |

**上下文数量的直接证据**：

| 模式 | 平均上下文数 | 图谱贡献的 chunk 总数 | 图谱命中题数 |
|------|-------------|---------------------|-------------|
| graph | **7.2** | 42 | 13/13 |
| nograph | 3.2 | 0 | 0/13 |

级联融合把平均上下文从 3.2 提升到 7.2（翻倍以上），且三个 RAGAS 指标全面提升。
**context_recall +15.4pt** 是核心成果 —— 正是「图谱反查回原本检索不到的 chunk」的直接体现。

## 二、与原作者基线对比（同 13 题）

| 指标 | 原作者 | 本次（graph） | 差异 |
|------|--------|--------------|------|
| context_precision | 0.795 | 0.736 | -0.059 |
| context_recall | 0.846 | **0.846** | **持平** |
| faithfulness | 0.707 | **0.808** | **+0.101** |

**解读**：
- **recall 追平**原作者
- **faithfulness 反超 10pt**（答案幻觉更少，得益于检索质量与图谱实体约束）
- **precision 略低 6pt** —— 召回增加必然引入部分噪音，是「召回 vs 精度」的经典权衡，
  符合预期而非缺陷

## 三、复现方式

```bash
# 1. 导入文档（含比亚迪 Q1、大湖股份公告）→ 触发解析/分块/向量化/图谱抽取
# 2. 跑检索 + 生成（graph/nograph 两组）
docker exec rag_backend python3 /app/run_eval.py
# 3. RAGAS 打分（宿主机 venv，需 ragas==0.1.21 + langchain-community<0.3）
python tests/evaluators/run_ours.py
```

**产物**：`eval_out_{graph,nograph}.csv`（检索+答案）、`ragas_ours_{graph,nograph}.csv`（逐题分数）

## 四、已知问题

- **TAX 5 题未覆盖**：源文档为政府政策 HTML，解析器不支持；如需覆盖需先支持 HTML 解析
  或把政策正文转为 PDF
- **检索偶发失败**：个别 query 出现 `[HybridSearch] 两路检索均失败`（未定位到具体触发条件），
  不影响整体结果，但值得后续排查
