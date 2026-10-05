"""检索链路的端到端不变量测试（需要真实服务：Postgres / Neo4j / 向量服务）。

为什么需要这一类测试
────────────────────
本项目的降级/兜底极多（全仓库 1487 处 `except Exception`、220 处 `return []`），
导致大量 bug **不崩溃、日志干净、只是结果不对** —— 例如：
  · 中文 BM25 不切词 → 查询恒空，混合检索退化为单路
  · 过滤条件过严 → 命中 0 条被当成"知识库没内容"
  · 图谱 chunk 保底在截断之后 → 图谱产出被静默丢弃
  · 上下文组装在图谱检索之前 → 图谱永远进不了 prompt
这些都不是单元测试（全 mock）能发现的。

本文件用**真实数据**跑完整链路，断言若干**不变量**。任何一条不变量被违反，
即说明链路上又出现了新的静默失效。

运行（需要真实服务）：
    docker exec rag_backend python3 -m pytest tests/integration/test_retrieval_invariants.py -v

注意：若知识库为空，本文件会自动跳过（无数据无法验证不变量）。
"""

import os
import pytest

# 真实环境的租户与知识库（可通过环境变量覆盖）
TENANT_ID = os.getenv("INVARIANT_TENANT_ID", "company_test_corp_175efce8")
KB_ID = os.getenv("INVARIANT_KB_ID", "41e6a39a-0c2f-4a61-a871-75edd3efb64a")

# 这些查询针对已知存在于知识库中的文档（比亚迪年报、大湖股份公告等）
FACT_QUERY = "比亚迪2026年第一季度报告中的开发支出是多少"
RELATION_QUERY = "大湖股份和业绩补偿方是什么关系"


async def _kb_has_chunks() -> bool:
    """知识库是否有数据 —— 无数据则跳过全部断言。

    注意：这里【不吞异常】。本文件存在的意义就是防"静默失效"，
    若连连通性检查都静默返回 False，会在数据库连不上时伪装成"无数据"而跳过，
    那这些不变量永远得不到验证（真实踩过：conftest 把 POSTGRES_DB 改成 test_db，
    导致连接失败 → has_data=False → 全部 skip，看起来"测试通过"）。
    """
    from app.db.session import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as db:
        r = await db.execute(text(
            "SELECT count(*) FROM document_chunks WHERE tenant_id = :t"
        ), {"t": TENANT_ID})
        return (r.scalar() or 0) > 0


@pytest.fixture(scope="module")
async def has_data():
    if not await _kb_has_chunks():
        pytest.skip("知识库无数据，跳过端到端不变量测试")
    return True


@pytest.fixture(scope="module")
async def retrieve():
    from app.services.unified_retriever import unified_retriever

    async def _call(query: str, enable_graph: bool = True):
        return await unified_retriever.retrieve(
            query=query, kb_id=KB_ID, session_id="invariant-test",
            user_id="admin", tenant_id=TENANT_ID, top_k=5,
            enable_graph=enable_graph, enable_rerank=True,
        )
    return _call


# ══════════════════════════════════════════════════════════════
# 不变量 1：知识库有相关内容时，检索不能返回空
# ══════════════════════════════════════════════════════════════
@pytest.mark.integration
@pytest.mark.asyncio
async def test_retrieval_not_empty_for_known_fact(has_data, retrieve):
    """已知事实型查询（文档确实存在）必须返回结果。

    这条能拦住：过滤条件过严清空结果、中文 BM25 恒空导致降级、
    cliff_prune 把结果全截掉 等"静默返回空"的失效。
    """
    r = await retrieve(FACT_QUERY)
    assert len(r.get("rag_results", [])) > 0, (
        f"检索「{FACT_QUERY}」返回空 —— 知识库中该文档确实存在，"
        f"说明检索链路有静默失效（过滤/分词/截断）。"
        f"mode={r.get('mode')}"
    )
    assert r.get("combined_context", "").strip(), "rag_results 非空但上下文为空"


# ══════════════════════════════════════════════════════════════
# 不变量 2：图谱声称 found，就必须有实际产出
# ══════════════════════════════════════════════════════════════
@pytest.mark.integration
@pytest.mark.asyncio
async def test_graph_found_implies_output(has_data, retrieve):
    """graph_results.found 为真时，entities/relations 不能都为空。"""
    r = await retrieve(RELATION_QUERY, enable_graph=True)
    gr = r.get("graph_results") or {}
    if gr.get("found"):
        assert gr.get("entities") or gr.get("relations"), (
            "图谱 found=True 但 entities 与 relations 均为空 —— 图谱产出被丢弃"
        )


# ══════════════════════════════════════════════════════════════
# 不变量 3：图谱关系若存在，必须出现在上下文中
# ══════════════════════════════════════════════════════════════
@pytest.mark.integration
@pytest.mark.asyncio
async def test_graph_relations_reach_context(has_data, retrieve):
    """有图谱关系时，上下文里必须能看到它们（A --关系--> B 形式）。

    这条能拦住：图谱检索在上下文组装【之后】执行 → 图谱永远进不了 prompt。
    """
    r = await retrieve(RELATION_QUERY, enable_graph=True)
    gr = r.get("graph_results") or {}
    if gr.get("relations"):
        assert "-->" in r.get("combined_context", ""), (
            f"图谱返回了 {len(gr['relations'])} 条关系，但上下文中没有关系段"
        )


# ══════════════════════════════════════════════════════════════
# 不变量 4：图谱实体若存在，必须出现在上下文中
# ══════════════════════════════════════════════════════════════
@pytest.mark.integration
@pytest.mark.asyncio
async def test_graph_entities_reach_context(has_data, retrieve):
    r = await retrieve(RELATION_QUERY, enable_graph=True)
    gr = r.get("graph_results") or {}
    ents = gr.get("entities") or []
    if ents:
        assert "知识图谱" in r.get("combined_context", ""), (
            f"图谱返回 {len(ents)} 个实体，但上下文里没有图谱段"
        )


# ══════════════════════════════════════════════════════════════
# 不变量 5：PARENT 节点必须有摘要（Parent-Child 检索的前提）
# ══════════════════════════════════════════════════════════════
@pytest.mark.integration
@pytest.mark.asyncio
async def test_parent_nodes_have_summary(has_data):
    """node_type='parent' 的 chunk 必须填了 summary。

    这条能拦住：Phase 2 摘要生成因参数为空被整段跳过（历史 bug）。
    """
    from app.db.session import AsyncSessionLocal
    from sqlalchemy import text
    async with AsyncSessionLocal() as db:
        r = await db.execute(text(
            "SELECT count(*) AS total, count(summary) AS with_summary "
            "FROM document_chunks WHERE tenant_id = :t AND node_type = 'parent'"
        ), {"t": TENANT_ID})
        row = r.mappings().first()
    total, with_summary = row["total"], row["with_summary"]
    if total == 0:
        pytest.skip("该租户无 PARENT 节点（legal 文档才产生 PARENT）")
    assert with_summary > 0, (
        f"{total} 个 PARENT 节点全部无 summary —— 摘要生成未生效"
    )


# ══════════════════════════════════════════════════════════════
# 不变量 6：BM25 必须是活的（中文分词生效）
# ══════════════════════════════════════════════════════════════
@pytest.mark.integration
@pytest.mark.asyncio
async def test_bm25_returns_results_for_chinese(has_data):
    """中文查询必须能命中 BM25。

    这条能拦住：simple 配置不切词 → 中文 BM25 恒为 0 命中、
    混合检索静默退化为纯向量。
    """
    from app.services.search_service import search_service
    r = await search_service.bm25_search(query="开发支出", top_k=10, tenant_id=TENANT_ID)
    assert len(r) > 0, (
        "中文 BM25 检索返回 0 条 —— 分词或 tsvector 填充有问题，"
        "混合检索已退化为单路"
    )


# ══════════════════════════════════════════════════════════════
# 不变量 7：agentic 多轮可达（评估不足时应能继续）
# ══════════════════════════════════════════════════════════════
@pytest.mark.integration
@pytest.mark.asyncio
async def test_agentic_multi_round_reachable(has_data):
    """评估判定"不足"时，should_continue 应为 True（多轮升级的前提）。

    这条能拦住：前置短路把"检索方式不对"误判为"知识库没内容"而提前停止，
    导致「第一轮不够 → 第二轮升级走图谱」这条链永不可达。
    """
    from app.langgraph.agentic_rag_nodes import ResultEvaluator
    from app.langgraph.agentic_rag_state import EvaluationResult

    ev = ResultEvaluator(llm_service=None)
    # 构造：检索结果严重不足（覆盖度低、相关性低），但知识库确有内容
    state = {
        "query": "某企业2026年第一季度的开发支出",
        "all_results": [{"content": "无关内容", "score": 0.1}],
        "iteration_count": 1,
        "max_iterations": 3,
        "evaluation": EvaluationResult(
            is_sufficient=False, coverage_score=0.0, relevance_score=0.0,
            completeness_score=0.0, overall_score=0.0,
            missing_aspects=[], reasoning="检索结果与问题无关",
        ),
    }
    state = await ev.evaluate(state)
    assert state.get("should_continue") is True, (
        "评估判定结果不足（overall=0）却不出继续 —— 多轮升级路径不可达。"
        "注意：'检索方式不对'（应重试）与'知识库无此内容'（可停止）在分数上"
        "无法区分，短路条件需要额外判据，不能只看分数。"
    )
