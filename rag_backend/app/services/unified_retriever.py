"""
统一检索服务 (Unified Retriever Service)

整合 Memory、RAG 和 Graph，根据智能路由结果执行相应的检索
增强支持知识图谱检索
"""

import logging
import uuid
from typing import List, Dict, Any, Optional
from sqlalchemy import select

from app.core.config import settings
from app.db import AsyncSessionLocal
from app.models.chunk import DocumentChunk
from app.services.smart_router import smart_router, RouteMode
from app.services.search_service import search_service
from app.services.query_analyzer import query_analyzer
from app.services.hybrid_search import hybrid_search_engine
from app.services.rerank_service import rerank_service
from app.services.embedding_service import embedding_service
from app.services.cliff_pruner import cliff_prune
from app.services.context_assembler import context_assembler
from app.memory_system.memory_manager import MemoryManager
from app.memory_system.base_memory import MemoryItem

logger = logging.getLogger(__name__)


class UnifiedRetriever:
    """
    统一检索器
    
    根据智能路由结果，自动选择使用 Memory、RAG 或混合检索
    """
    
    def __init__(self):
        """初始化统一检索器"""
        pass
    
    async def retrieve(
        self,
        query: str,
        kb_id: str,
        session_id: str,
        user_id: str,
        top_k: int = 5,
        enable_routing: bool = True,
        enable_graph: bool = True,
        enable_rerank: bool = True,
        tenant_id: Optional[str] = None,
        _skip_metric_filter: bool = False,
    ) -> Dict[str, Any]:
        """
        统一检索接口 (v3: Hybrid Search + Reranker + Domain Assembly)

        完整链路：
        QueryAnalyzer -> Hybrid Search (Dense+BM25+RRF) -> Reranker
        -> Cliff Prune -> Temporal Dedup -> Relationship Expansion
        -> Auto-Merging -> Domain-Aware Prompt Assembly
        """
        graph_results: Dict[str, Any] = {}
        use_graph = False

        if enable_routing:
            route_mode = await smart_router.route(query)
        else:
            route_mode = RouteMode.HYBRID

        memory_results: Dict[str, List[MemoryItem]] = {}

        # ── Step 1: 查询解析 ──
        query_meta = query_analyzer.analyze(query)

        if route_mode == RouteMode.GREETING:
            return {
                "mode": route_mode.value, "rag_results": [],
                "memory_results": {}, "graph_results": {},
                "combined_context": "", "query": query, "use_graph": False,
            }

        # ── Step 2: 构建过滤条件 ──
        metadata_filter = query_analyzer.build_metadata_filter(query_meta)

        # 时效过滤仅限 tax 领域（finance/legal 没有 effective_date 字段）
        domain = query_meta.get("domain")
        if domain == "tax":
            temporal_filter = query_analyzer.build_temporal_filter(query_meta)
            if temporal_filter:
                metadata_filter = {**(metadata_filter or {}), **temporal_filter}

        # 财务指标 JSONB 数组过滤（评估模式下可跳过）
        jsonb_array_filter = None
        if not _skip_metric_filter:
            metric = query_meta.get("filters", {}).get("metric")
            jsonb_array_filter = {"metrics": metric} if metric else None

        # ── Step 3: 混合检索 (Hybrid Search + RRF) ──
        candidates = await hybrid_search_engine.search(
            query=query,
            tenant_id=tenant_id,
            domain=domain,
            metadata_filter=metadata_filter,
            jsonb_array_filter=jsonb_array_filter,
        )

        # ── Step 3.5: 软过滤回退 ──
        # 查询解析出的过滤条件（year/quarter/tax_type/region）是启发式推断的，
        # 而 chunk 的 meta_info 未必有这些字段（如 quarter 全库为 0、year 仅约一半）。
        # 严格 AND 过滤会因某个字段缺失而把结果清空（实测"比亚迪…开发支出"
        # 解析出 year=2026&quarter=Q1 → 命中 0 条）。
        # 对策：带过滤为空时，逐步放宽（先去掉最不可靠的字段，最终不带过滤重试）。
        if not candidates and (metadata_filter or jsonb_array_filter):
            relaxed = dict(metadata_filter or {})
            # 按可靠性从低到高移除：quarter/region 最不可靠，year 次之
            for weak in ("quarter", "region", "tax_type", "year"):
                relaxed.pop(weak, None)
                candidates = await hybrid_search_engine.search(
                    query=query, tenant_id=tenant_id, domain=domain,
                    metadata_filter=relaxed or None,
                    jsonb_array_filter=jsonb_array_filter,
                )
                if candidates:
                    logger.info(
                        f"[软过滤] 严格过滤命中 0，放宽到 {relaxed or '无过滤'} 后命中 {len(candidates)} 条"
                    )
                    break
            if not candidates and jsonb_array_filter:
                # 最后连指标过滤也去掉（它同样可能过严）
                candidates = await hybrid_search_engine.search(
                    query=query, tenant_id=tenant_id, domain=domain,
                    metadata_filter=None, jsonb_array_filter=None,
                )
                if candidates:
                    logger.info(f"[软过滤] 连指标过滤也放宽，命中 {len(candidates)} 条")

        # ── Step 4: 级联图谱扩展（在 rerank 之前，让图谱 chunk 也参与精排）──
        # 顺序很重要：原实现在 rerank 之后才做图谱，导致图谱 chunk 是"硬塞在末尾"、
        # 且不参与 RRF 排序。这里提前到 rerank 前，与第一阶段结果做三源 RRF。
        #   ① 实体来源是【第一阶段检索到的 chunk】（而非 query）—— 对模糊提问更鲁棒
        #   ② 用实体的 chunk_ids 反查回【更多原本没检索到的 chunk】
        graph_chunks: List[Dict] = []
        if enable_graph and settings.ENABLE_KNOWLEDGE_GRAPH:
            graph_chunks, graph_results = await self._cascade_graph_expand(
                query=query, chunks=candidates, kb_id=kb_id, tenant_id=tenant_id, top_k=top_k,
            )
            # 三源 RRF：dense + sparse + graph
            if graph_chunks:
                candidates = self._fuse_three_sources(
                    candidates, graph_chunks, domain, top_k=max(50, top_k * 10),
                )
            use_graph = bool(graph_results.get("found"))

        # ── Step 5: Reranker + Cliff Prune ──
        if enable_rerank:
            pruned = await self._rerank_and_prune(query, candidates, top_k=top_k)
        else:
            # 关闭重排序：保留 RRF 融合顺序，仅按 top_k 截断
            # 把已有的 RRF/向量分数回填到 rerank_score，保证上层 sources 展示与排序正常
            for c in candidates:
                if "rerank_score" not in c:
                    c["rerank_score"] = c.get("score") or c.get("rrf_score") or 0.0
            pruned = candidates[: max(1, top_k)]

        # ── Step 5.5: 时序去重 ──
        deduped = await hybrid_search_engine.temporal_dedup(
            chunks=pruned, query_meta=query_meta,
        )

        # ── Step 6: 关系展开 ──
        enriched = await self._enrich_results(deduped)

        # ── Step 6.5: Auto-Merging (仅 general) ──
        if domain in (None, "general"):
            enriched = await hybrid_search_engine.auto_merge(enriched)

        # ── Step 7: 多态 Prompt 组装（含图谱段）──
        combined_context = await context_assembler.assemble(
            chunks=enriched, domain=domain, query=query,
        )
        # 把图谱信息拼进上下文（原实现在组装之后才检索图谱，导致图谱永远进不了上下文）
        if graph_results and graph_results.get("context"):
            combined_context = graph_results["context"] + "\n" + combined_context

        return {
            "mode": route_mode.value,
            "rag_results": enriched,
            "memory_results": memory_results,
            "graph_results": graph_results,
            "combined_context": combined_context,
            "query": query,
            "use_graph": use_graph,
        }

    async def _cascade_graph_expand(
        self, query: str, chunks: List[Dict], kb_id: str,
        tenant_id: Optional[str], top_k: int,
    ):
        """级联图谱扩展：chunk → 抽实体 → 图谱多跳 → 反查回 chunk。

        Returns: (图谱新增的 chunk 列表（含 graph_rank）, 图谱结果字典)

        注意：不再在这里直接合并到 chunks —— 由调用方用 _fuse_three_sources
        做三源 RRF，让图谱 chunk 按【跳数】参与排序（跳数小=更相关）。
        """
        graph_results: Dict[str, Any] = {"entities": [], "relations": [], "context": "", "found": False}
        try:
            resolved_tenant = tenant_id
            if not resolved_tenant and kb_id:
                from app.db.session import AsyncSessionLocal as _ASL
                from app.models.knowledge_base import KnowledgeBase
                async with _ASL() as db:
                    row = (await db.execute(
                        select(KnowledgeBase.tenant_id).where(KnowledgeBase.id == kb_id)
                    )).scalar_one_or_none()
                    if row:
                        resolved_tenant = str(row)
            if not resolved_tenant:
                return [], graph_results

            # ① 从【检索到的 chunk】抽实体（级联的关键 —— 不是从 query 抽）
            chunk_text = "\n".join((c.get("content") or "")[:600] for c in chunks[:5])
            seed_entities = await self._extract_seed_entities(
                query, chunk_text, True, resolved_tenant)
            if not seed_entities:
                return [], graph_results

            # ② 图谱多跳 + 用 chunk_ids 反查（带跳数，供 graph_rank 排序）
            from app.knowledge_graph.neo4j_manager import Neo4jManager
            mgr = Neo4jManager(uri=settings.NEO4J_URI, user=settings.NEO4J_USER,
                               password=settings.NEO4J_PASSWORD)
            try:
                graph_hits = mgr.find_chunk_ids_by_entities(
                    entity_names=seed_entities, tenant_id=resolved_tenant, max_depth=1, limit=40,
                )
            finally:
                mgr.close()

            hit_ids = [cid for cid in graph_hits.get("chunk_ids", []) if cid]
            distances = graph_hits.get("chunk_distances", {}) or {}
            entities = graph_hits.get("entities", [])

            # ③ 取回图谱命中的 chunk（原本不在第一阶段结果里的），按跳数排序
            existing_ids = {str(c.get("id")) for c in chunks}
            new_ids = [cid for cid in hit_ids if cid not in existing_ids][: top_k * 2]
            # graph_rank：跳数升序（跳数越小排名越前）
            new_ids.sort(key=lambda cid: distances.get(cid, 9))

            graph_chunks: List[Dict] = []
            if new_ids:
                from app.db.session import AsyncSessionLocal as _ASL2
                from app.models.chunk import DocumentChunk
                import uuid as _uuid
                async with _ASL2() as db:
                    try:
                        uuids = [_uuid.UUID(x) for x in new_ids]
                    except (ValueError, AttributeError):
                        uuids = []
                    if uuids:
                        rows = (await db.execute(
                            select(DocumentChunk).where(DocumentChunk.id.in_(uuids))
                        )).scalars().all()
                        by_id = {str(r.id): r for r in rows}
                        for rank, cid in enumerate(new_ids, 1):
                            r = by_id.get(cid)
                            if not r:
                                continue
                            graph_chunks.append({
                                "id": str(r.id), "content": r.content,
                                "domain": r.domain,
                                "meta_info": r.meta_info or {},
                                "graph_rank": rank,          # 供三源 RRF 用
                                "graph_hop": distances.get(cid, 1),
                                "from_graph": True,
                            })
                logger.info(
                    f"[级联图谱] 种子 {len(seed_entities)} → 图谱新增 {len(graph_chunks)} chunk "
                    f"(跳数分布: {sorted(set(distances.get(c,1) for c in new_ids))})"
                )

            # ④ 构造图谱上下文（实体 + 关系，供 prompt 使用）
            relations = graph_hits.get("relations", []) or []
            if entities:
                by_type: Dict[str, List[str]] = {}
                for e in entities[:20]:
                    by_type.setdefault(e.get("type", "UNKNOWN"), [])
                    if e["name"] not in by_type[e["type"]]:
                        by_type[e["type"]].append(e["name"])
                parts = ["【知识图谱信息】",
                         f"（由检索内容中的实体扩展，种子: {', '.join(seed_entities[:5])}）"]
                for t, names in by_type.items():
                    parts.append(f"  [{t}] {', '.join(names[:8])}")
                # 关系段：把「A --关系--> B」也拼进去 ——
                # 这是图谱相对向量检索的独特价值（向量算不出实体间的语义关系），
                # 此前只拼实体名、丢弃了关系（relations 恒为空）。
                if relations:
                    parts.append("  关系：")
                    for r in relations[:15]:
                        parts.append(f"    {r.get('source','')} --{r.get('type','')}--> {r.get('target','')}")
                graph_results = {
                    "entities": entities, "relations": relations,
                    "context": "\n".join(parts) + "\n",
                    "found": True, "seed_entities": seed_entities,
                    "graph_chunk_ids": hit_ids,
                }
            return graph_chunks, graph_results
        except Exception as e:
            logger.warning(f"[级联图谱] 扩展失败（不影响主检索）: {e}")
            return [], graph_results

    # 三源 RRF 权重 (dense, sparse, graph) —— 按领域调参
    # 图谱权重普遍较低：图谱 chunk 是"关联扩展"而非"精确命中"，宜作补充而非主导。
    _RRF_DOMAIN_WEIGHTS = {
        "legal":   (0.35, 0.40, 0.25),
        "tax":     (0.35, 0.35, 0.30),
        "finance": (0.30, 0.45, 0.25),
        "general": (0.40, 0.40, 0.20),
        None:      (0.35, 0.40, 0.25),
    }
    _RRF_K = 60

    def _fuse_three_sources(
        self, candidates: List[Dict], graph_chunks: List[Dict],
        domain: Optional[str], top_k: int = 50,
    ) -> List[Dict]:
        """三源 RRF 融合：dense + sparse + graph。

        与两源 RRF 的区别：图谱路的排名依据是【跳数】——
        直接相连（hop=1）比隔两跳更相关，是天然的排序信号。

        Args:
            candidates: 第一阶段结果（含 dense_rank / sparse_rank / content）
            graph_chunks: 图谱扩展来的新 chunk（含 graph_rank / content）
            domain: 领域，决定三路权重
        """
        w_dense, w_sparse, w_graph = self._RRF_DOMAIN_WEIGHTS.get(
            domain, self._RRF_DOMAIN_WEIGHTS[None]
        )
        k = self._RRF_K
        scores: Dict[str, float] = {}
        info: Dict[str, dict] = {}

        def _ensure(cid: str) -> dict:
            if cid not in info:
                info[cid] = {}
            return info[cid]

        for c in candidates:
            cid = str(c.get("id"))
            if not cid:
                continue
            s = 0.0
            dr = c.get("dense_rank")
            if dr:
                s += w_dense / (k + dr)
            sr = c.get("sparse_rank")
            if sr:
                s += w_sparse / (k + sr)
            scores[cid] = scores.get(cid, 0.0) + s
            e = _ensure(cid)
            e.update({"content": c.get("content", ""), "domain": c.get("domain"),
                      "meta_info": c.get("meta_info", {})})
            e["chunk"] = c
            if dr: e["dense_rank"] = dr
            if sr: e["sparse_rank"] = sr

        for gc in graph_chunks:
            cid = str(gc.get("id"))
            if not cid:
                continue
            gr = gc.get("graph_rank") or 1
            scores[cid] = scores.get(cid, 0.0) + w_graph / (k + gr)
            e = _ensure(cid)
            e.setdefault("content", gc.get("content", ""))
            e.setdefault("domain", gc.get("domain"))
            e.setdefault("meta_info", gc.get("meta_info", {}))
            e["chunk"] = gc
            e["graph_rank"] = gr
            e["from_graph"] = True

        # ── 图谱保底配额（必须在 top_k 截断【之前】决定）──
        # 问题：图谱路权重天然低（它是「关联补充」而非「精确命中」），
        # 三源 RRF 后图谱 chunk 会全部沉底，若先截断 top_k 再保底，
        # 图谱 chunk 早已被截掉（实测「融合后 50 条，图谱剩 0」）—— 级联白做。
        # 对策：先按 graph_rank（跳数）选出要保底的图谱 chunk，
        # 把它们并入截断结果，再整体截断。
        # 配额保守（≤2 条），避免图谱噪音挤掉精确命中。
        graph_ids = [cid for cid in scores if info.get(cid, {}).get("from_graph")]
        graph_ids.sort(key=lambda cid: info[cid].get("graph_rank") or 99)
        graph_quota = min(2, max(1, len(graph_ids) // 3)) if graph_ids else 0
        guaranteed = graph_ids[:graph_quota]

        # 常规排名截断到 top_k，再确保保底项在其中
        ordered = sorted(scores, key=scores.get, reverse=True)[:top_k]
        if guaranteed:
            oset = set(ordered)
            # 保底项插到第 2 位之后（不喧宾夺主，但保证进入下游可见范围）
            insert_at = min(2, len(ordered))
            for gid in reversed(guaranteed):
                if gid not in oset:
                    ordered.insert(insert_at, gid)
                    oset.add(gid)
            # 保底可能导致超出 top_k，从末尾裁掉非保底项
            if len(ordered) > top_k:
                gset = set(guaranteed)
                trimmed = []
                for cid in ordered:
                    if len(trimmed) >= top_k and cid not in gset:
                        continue
                    trimmed.append(cid)
                ordered = trimmed[:max(top_k, len(gset))]

        fused: List[Dict] = []
        for cid in ordered:
            e = info[cid]
            chunk = dict(e.get("chunk") or {})
            chunk["id"] = cid
            chunk["content"] = e.get("content", "")
            chunk["domain"] = e.get("domain")
            chunk["meta_info"] = e.get("meta_info", {})
            chunk["rrf_score"] = round(scores[cid], 6)
            for key in ("dense_rank", "sparse_rank", "graph_rank", "from_graph"):
                if key in e:
                    chunk[key] = e[key]
            fused.append(chunk)

        return fused


    # 通用词黑名单：这些词做种子会匹配到大量无关 chunk（如"企业"命中"企业所得税""万科企业股份有限公司"）
    _SEED_STOPWORDS = {
        "企业", "公司", "集团", "银行", "子公司", "母公司", "控股", "股份",
        "所得税", "税率", "税额", "税种", "收入", "成本", "费用", "利润",
        "资产", "负债", "权益", "现金", "余额", "金额", "合计", "本期",
        "报告", "年度", "期间", "项目", "业务", "服务", "产品", "客户",
        "上述", "本次", "该", "此", "相关", "其他", "以下", "以上",
    }
    # 有区分度的实体类型（优先选这些做种子）
    _SEED_PREFERRED_TYPES = {"COMPANY", "PERSON", "CONTRACT", "REGULATION",
                             "TAX_TYPE", "TAX_POLICY", "PRODUCT", "EVENT", "LOCATION"}

    # 公司/机构后缀（剥离后得到"核心名"，用于简称匹配）
    _NAME_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司", "集团有限公司",
                      "集团", "公司", "事务所", "银行", "分行", "董事会", "委员会")

    def _name_matches(self, entity_name: str, text: str) -> bool:
        """实体名与文本的双向匹配。

        处理两种情形：
        1. 实体全名出现在文本里：'万科企业股份有限公司' ⊂ 文本
        2. 文本里提到实体简称：query='万科有哪些子公司'，实体='万科企业股份有限公司'
           → 剥离公司后缀得核心名 '万科企业'，再看其前缀（≥2字）是否在文本中。
        """
        if not text:
            return False
        if entity_name in text:
            return True
        # 剥后缀得核心名
        core = entity_name
        for suf in self._NAME_SUFFIXES:
            if core.endswith(suf) and len(core) > len(suf):
                core = core[:-len(suf)]
                break
        if len(core) < 2:
            return False
        # 核心名的前缀（2 字起，逐级加长）出现在文本中即算命中
        for L in range(2, len(core) + 1):
            if core[:L] in text:
                # 至少匹配到 2 个连续字，且不是纯通用词
                seg = core[:L]
                if seg not in self._SEED_STOPWORDS:
                    return True
        return False

    async def _extract_seed_entities(self, query: str, chunk_text: str,
                                     need_graph: bool, tenant_id: Optional[str] = None) -> List[str]:
        """级联实体抽取：优先从【检索到的 chunk】抽，回退到 query。

        对模糊提问（query 里没实体）尤为关键 —— chunk 里有实体，query 里没有。

        精度控制（关键）：种子实体必须【有区分度】——
        - 排除通用词（企业/公司/所得税/税率…），否则会匹配到海量无关 chunk
        - 优先具体类型（COMPANY/PERSON/CONTRACT…），跳过 DATE_PERIOD 等泛类型
        - 长度 ≥ 4（2-3 字的中文词多为通用词）
        """
        names: List[str] = []

        # 一次拉取图谱里【有区分度】的实体（类型限定 + 长度 ≥ 4 + 按 chunk 数排序）。
        # 关键：必须在 Cypher 里排序+限流，否则无序 LIMIT 会扫不到靠后的重要实体
        # （如"万科企业股份有限公司"有 108 个 chunk，却因无序截断而漏掉）。
        candidates: List[tuple] = []   # (name, type)
        try:
            from app.knowledge_graph.neo4j_manager import Neo4jManager
            mgr = Neo4jManager(uri=settings.NEO4J_URI, user=settings.NEO4J_USER,
                               password=settings.NEO4J_PASSWORD)
            try:
                with mgr.driver.session(database=settings.NEO4J_DATABASE) as s:
                    rows = s.run(
                        "MATCH (e:Entity) "
                        "WHERE (e.tenant_id IS NULL OR e.tenant_id = $t) "
                        "AND e.name IS NOT NULL AND size(e.name) >= 4 "
                        "AND e.type IN $types "
                        "RETURN e.name AS name, e.type AS type "
                        "ORDER BY size(coalesce(e.chunk_ids,[])) DESC LIMIT 3000",
                        t=str(tenant_id) if tenant_id else None,
                        types=list(self._SEED_PREFERRED_TYPES))
                    for r in rows:
                        n = r["name"]
                        if n and n not in self._SEED_STOPWORDS:
                            candidates.append((n, r["type"]))
            finally:
                mgr.close()
        except Exception as e:
            logger.debug(f"[级联图谱] 实体候选拉取失败: {e}")

        # ① 优先：名字与 query 匹配的实体（最可靠）
        for n, t in candidates:
            if self._name_matches(n, query):
                names.append(n)
                if len(names) >= 8:
                    break

        # ② 补充：从【检索到的 chunk】里匹配（处理"这家公司"这类 query 无实体）
        if not names and chunk_text:
            for n, t in candidates:
                if self._name_matches(n, chunk_text):
                    names.append(n)
                    if len(names) >= 8:
                        break


        # ③ 最后的回退：jieba 从 query 抽（过滤通用词）
        if not names:
            try:
                from app.services.graph_retriever import graph_retriever
                raw = await graph_retriever.extract_entities_from_query(query)
                names = [n for n in raw if len(n) >= 4 and n not in self._SEED_STOPWORDS]
            except Exception:
                names = []
        return names[:8]


    async def _rerank_and_prune(
        self, query: str, candidates: List[Dict], top_k: int = 10,
    ) -> List[Dict]:
        """Reranker + MMR 多样性 + Cliff Prune 精排截断

        top_k 控制最终返回的文档块数量；rerank/MMR 使用更宽的候选池，
        最后由 cliff_prune 按分数断崖在 [min_results, top_k] 之间动态截断。
        """
        if not candidates:
            return []

        # 最终返回上限，候选池取其 2 倍（至少 20）以保证精排有足够素材
        final_k = max(1, top_k)
        pool_k = max(20, final_k * 2)

        try:
            reranked = await rerank_service.rerank(
                query=query,
                documents=[c["content"] for c in candidates],
                top_k=pool_k, max_chars_per_doc=1000,
            )
        except Exception as e:
            logger.warning(f"[Reranker] 失败，降级为 RRF 排序: {e}")
            return candidates[:final_k]

        for rr in reranked:
            if rr.index < len(candidates):
                candidates[rr.index]["rerank_score"] = rr.relevance_score

        # MMR 多样性重排（候选数取 max(final_k, 10)，给 cliff_prune 留选择空间）
        mmr_result = await self._mmr_rerank(
            query=query, candidates=candidates,
            top_k=max(final_k, 10), lambda_param=0.6,
        )

        pruned = cliff_prune(
            items=mmr_result, score_key="rerank_score",
            min_results=min(3, final_k), max_results=final_k, cliff_threshold=0.15,
        )

        # ── 图谱 chunk 保底补回 ──
        # 图谱 chunk 是「关联扩展」而非「精确命中」，按相关性打分天然偏低，
        # 会被 cliff_prune 截掉 —— 那级联融合就白做了。
        # 这里把图谱 chunk 单独补回（不占常规配额，最多 2 条），确保「图谱补充关联」
        # 的设计意图真正落地。它们已通过 RRF 前段插入证明过相关性。
        pruned_ids = {str(c.get("id")) for c in pruned}
        graph_survivors = [
            c for c in candidates
            if c.get("from_graph") and str(c.get("id")) not in pruned_ids
        ]
        # 按 graph_rank（跳数）取最优的 2 条
        graph_survivors.sort(key=lambda c: c.get("graph_rank") or 99)
        for gc in graph_survivors[:2]:
            gc.setdefault("rerank_score", 0.0)
            pruned.append(gc)
        if graph_survivors[:2]:
            logger.info(f"[级联图谱] 补回 {len(graph_survivors[:2])} 条图谱 chunk（免于 cliff 截断）")

        return pruned

    async def _mmr_rerank(
        self, query: str, candidates: List[Dict],
        top_k: int = 15, lambda_param: float = 0.6,
    ) -> List[Dict]:
        """
        MMR (Maximal Marginal Relevance) 多样性重排。
        选出的结果既相关又不多样化，避免多个相似 chunk 同时进入上下文。
        """
        scored = [c for c in candidates if c.get("rerank_score") is not None]
        scored.sort(key=lambda x: x["rerank_score"], reverse=True)

        if len(scored) <= top_k:
            return scored

        # ⚡ 性能关键：一次性批量计算所有候选向量 + 查询向量（2 次网络请求），
        # 之后纯内存做 MMR。旧实现在双重循环里逐个 get_embedding，对 ~20 候选
        # 会发起 O(top_k × pool) ≈ 上百次串行 embedding 网络请求，单轮检索阻塞 ~40s。
        try:
            query_emb = await embedding_service.get_embedding(query)
            if not query_emb:
                return scored[:top_k]
            cand_embs = await embedding_service.get_embeddings(
                [c["content"][:500] for c in scored]
            )
            if not cand_embs or len(cand_embs) != len(scored):
                logger.warning("[MMR] 候选向量数量不匹配，跳过 MMR")
                return scored[:top_k]
        except Exception as e:
            logger.warning(f"[MMR] 向量计算失败，跳过: {e}")
            return scored[:top_k]

        # 预计算每个候选与查询的相似度，纯内存 MMR 选择
        sim_to_query = [self._cosine_sim(emb, query_emb) for emb in cand_embs]

        selected_idx = [0]
        selected_embs = [cand_embs[0]]
        pool_idx = list(range(1, len(scored)))

        while len(selected_idx) < top_k and pool_idx:
            best_pos = -1
            best_score = -float("inf")
            for pos, idx in enumerate(pool_idx):
                # 🔧 修复：与已选**文档**向量比较（旧代码错误地用 query_emb，导致多样性失效）
                max_sim_to_sel = max(
                    self._cosine_sim(cand_embs[idx], se) for se in selected_embs
                )
                mmr_score = lambda_param * sim_to_query[idx] - (1 - lambda_param) * max_sim_to_sel
                if mmr_score > best_score:
                    best_score = mmr_score
                    best_pos = pos
            if best_pos < 0:
                break
            chosen = pool_idx.pop(best_pos)
            selected_idx.append(chosen)
            selected_embs.append(cand_embs[chosen])

        return [scored[i] for i in selected_idx]

    @staticmethod
    def _cosine_sim(a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0

    async def _enrich_results(self, results: List[Dict]) -> List[Dict]:
        """富化结果：PARENT 摘要 + PREVIOUS/NEXT 展开"""
        if not results:
            return results
        enriched = []
        for chunk in results:
            chunk_id = chunk.get("id")
            if chunk_id:
                parent_summary = await self._resolve_parent_summary(str(chunk_id))
                if parent_summary:
                    chunk["parent_summary"] = parent_summary
                domain = chunk.get("domain")
                if domain == "tax":
                    pn = await self._resolve_prev_next(str(chunk_id))
                    if pn.get("previous"):
                        chunk["prev_content"] = pn["previous"]
                    if pn.get("next"):
                        chunk["next_content"] = pn["next"]
            enriched.append(chunk)
        return enriched

    async def _retrieve_from_memory(
        self, query: str, session_id: str, user_id: str, top_k: int
    ) -> Dict[str, List[MemoryItem]]:
        """从 Memory 检索"""
        try:
            memory_manager = MemoryManager(session_id, user_id)
            results = await memory_manager.retrieve_context(
                query=query,
                use_working=True,
                use_episodic=True,
                use_semantic=True,
                top_k=top_k
            )
            return results
        except Exception as e:
            print(f"⚠️ [Memory 检索] 失败: {e}")
            return {"working": [], "episodic": [], "semantic": []}
    
    async def get_formatted_context_for_llm(
        self,
        query: str,
        kb_id: str,
        session_id: str,
        user_id: str,
        max_tokens: int = 2000
    ) -> str:
        """
        获取格式化的上下文，用于传递给 LLM
        
        Args:
            query: 用户查询
            kb_id: 知识库ID
            session_id: 会话ID
            user_id: 用户ID
            max_tokens: 最大 token 数（粗略估算）
            
        Returns:
            格式化的上下文字符串
        """
        result = await self.retrieve(
            query=query,
            kb_id=kb_id,
            session_id=session_id,
            user_id=user_id,
            top_k=5
        )
        
        context = result["combined_context"]
        
        # 简单的长度控制（1 token ≈ 1.5 字符）
        max_chars = max_tokens * 1.5
        if len(context) > max_chars:
            context = context[:int(max_chars)] + "\n...(内容过长，已截断)"
        
        return context

    async def _resolve_parent_summary(
        self,
        chunk_id: str,
    ) -> str | None:
        """
        通过 PARENT 关系获取父节点摘要（语义锚点）。

        优先级：
        1. parent.summary（50 字摘要，最优方案）
        2. parent.content[:300]（降级方案）
        3. None（无 PARENT）
        """
        if not chunk_id:
            return None

        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(DocumentChunk).where(DocumentChunk.id == chunk_id)
                )
                chunk = result.scalar_one_or_none()
                if not chunk or not chunk.relationships:
                    return None

                parent_id = chunk.relationships.get("PARENT")
                if parent_id is None:
                    return None

                # 关系里存的是「同一文档内的 chunk_index」（与 chunker 分配的位置一致），
                # 不是数据库 UUID，因此按 (document_id, chunk_index) 定位父节点。
                try:
                    parent_idx = int(parent_id)
                except (ValueError, TypeError):
                    return None

                parent_result = await db.execute(
                    select(DocumentChunk).where(
                        DocumentChunk.document_id == chunk.document_id,
                        DocumentChunk.chunk_index == parent_idx,
                    )
                )
                parent = parent_result.scalar_one_or_none()
                if not parent:
                    return None

                # 一级：summary（50 字摘要）
                if parent.summary and len(parent.summary) > 5:
                    return parent.summary

                # 二级降级：content 前 300 字符
                return parent.content[:300]

        except Exception as e:
            logger.warning(f"[UnifiedRetriever] 解析 PARENT 摘要失败: {e}")
            return None

    async def _resolve_prev_next(
        self,
        chunk_id: str,
        max_chars: int = 200,
    ) -> dict:
        """
        通过 PREVIOUS/NEXT 关系获取相邻条款。

        Args:
            chunk_id: 当前 chunk 的 ID
            max_chars: 相邻条款最大字符数

        Returns:
            {"previous": str | None, "next": str | None}
        """
        if not chunk_id:
            return {"previous": None, "next": None}

        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(DocumentChunk).where(DocumentChunk.id == chunk_id)
                )
                chunk = result.scalar_one_or_none()
                if not chunk or not chunk.relationships:
                    return {"previous": None, "next": None}

                prev_content = None
                next_content = None

                prev_id = chunk.relationships.get("PREVIOUS")
                if prev_id is not None:
                    try:
                        prev_idx = int(prev_id)
                    except (ValueError, TypeError):
                        prev_idx = None
                    if prev_idx is not None:
                        prev_result = await db.execute(
                            select(DocumentChunk).where(
                                DocumentChunk.document_id == chunk.document_id,
                                DocumentChunk.chunk_index == prev_idx,
                            )
                        )
                        prev = prev_result.scalar_one_or_none()
                        if prev:
                            prev_content = prev.content[:max_chars]

                next_id = chunk.relationships.get("NEXT")
                if next_id is not None:
                    try:
                        next_idx = int(next_id)
                    except (ValueError, TypeError):
                        next_idx = None
                    if next_idx is not None:
                        next_result = await db.execute(
                            select(DocumentChunk).where(
                                DocumentChunk.document_id == chunk.document_id,
                                DocumentChunk.chunk_index == next_idx,
                            )
                        )
                        nxt = next_result.scalar_one_or_none()
                        if nxt:
                            next_content = nxt.content[:max_chars]

                return {"previous": prev_content, "next": next_content}

        except Exception as e:
            logger.warning(f"[UnifiedRetriever] 解析 PREVIOUS/NEXT 失败: {e}")
            return {"previous": None, "next": None}


# 全局单例
unified_retriever = UnifiedRetriever()
