# 级联融合评估报告

**日期**：2026-10-04
**评估集**：复用原作者 `evaluation_dataset_ragas.json` 的 13 题（FIN 3 + LEG 10）
**判官**：DeepSeek（`deepseek-chat`），Embedding：硅基流动 `BAAI/bge-m3` —— 与原作者同配置
**知识库**：11 份文档（含原作者用的比亚迪 2026Q1 报告、大湖股份股权转让公告，及一份政策 HTML）

> TAX 5 题未覆盖：其源文档是政府政策网页（HTML），原文件经 8 种路径检索未找到。
> HTML 解析能力已补上并验证（见文末），但那 5 题无法与原作者对比。

## 一、消融对比（级联融合 ON/OFF）

同一批 13 题、同一知识库，仅切换 `enable_graph`：

| 指标 | graph（级联） | nograph（向量+BM25） | 提升 |
|------|--------------|---------------------|------|
| context_precision | **0.806** | 0.641 | **+0.164** |
| context_recall | **0.846** | 0.692 | **+0.154** |
| faithfulness | **0.930** | 0.908 | +0.022 |

### 分领域（关键发现）

| 领域 | 指标 | graph | nograph |
|------|------|-------|---------|
| FIN（3 题） | context_precision | **0.611** | **0.000** |
| FIN（3 题） | context_recall | **0.667** | **0.000** |
| LEG（10 题） | context_precision | 0.864 | 0.834 |
| LEG（10 题） | context_recall | 0.900 | 0.900 |

**图谱的贡献高度集中在 FIN 题上：3 道题从 0 分提升到 0.6+。**
原因：FIN 问题的检索在纯向量/BM25 下召回的是**其他文档的无关 chunk**
（question 里带"比亚迪 2026 年第一季度"这类限定，向量检索被同领域的其他财报干扰），
而级联图谱通过「比亚迪股份有限公司」等实体反查 `chunk_ids`，把正确的 chunk 带了回来。
LEG 题两者持平（该文档本身特征词强，纯检索已足够）。

## 二、与原作者基线对比（同 13 题）

| 指标 | 原作者 | 本次（graph） | 差异 |
|------|--------|--------------|------|
| context_precision | 0.795 | **0.806** | +0.011 |
| context_recall | 0.846 | **0.846** | 持平 |
| faithfulness | 0.707 | **0.930** | **+0.223** |

**解读**：三项全部不劣于原作者；faithfulness 大幅领先（答案幻觉显著更少）。

## 三、评估过程中修复的缺陷（否则测不出真实水平）

| 缺陷 | 影响 | 修复 |
|------|------|------|
| **中文 BM25 恒为 0 命中** | PG 的 `simple` 配置不切中文（整句一个 token），混合检索名存实亡 | 应用层 jieba 分词 + `content_tsvector` 迁移为普通列 |
| **过滤条件过严清空结果** | `year=2026 & quarter=Q1` 因 quarter 字段全库缺失 → 命中 0，3 道 FIN 题全废 | 软过滤回退：为空时逐级放宽 |

## 四、复现方式

```bash
# 1. 导入文档（含比亚迪 Q1、大湖股份公告）→ 解析/分块/向量化/图谱抽取
# 2. 跑检索 + 生成（graph/nograph 两组）
docker exec rag_backend python3 /app/run_eval.py
# 3. RAGAS 打分（宿主机 venv，需 ragas==0.1.21 + langchain-community<0.3）
python tests/evaluators/run_ours.py
```

**产物**：`eval_out_{graph,nograph}.csv`（检索+答案）、`ragas_ours_{graph,nograph}.csv`（逐题分数）

## 五、已知限制

- **TAX 5 题未覆盖**：源政策文件未找到；HTML 解析能力已补（见下）但无法补齐原题对比
- **判官与生成器同为 DeepSeek**：存在同源自评风险，可用 Qwen 当判官进一步消除
- **nograph 组并非纯向量**：BM25 已修复，nograph 组是完整的 dense+BM25 混合检索
