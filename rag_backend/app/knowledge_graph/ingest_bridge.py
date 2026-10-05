"""知识图谱摄取桥接 (KG Ingest Bridge)

把「文档切块」与「知识图谱抽取」连起来：对已入库的 chunk 做实体/关系抽取，
写入 Neo4j，并把实体关联回 Postgres 的 chunk（跨库桥接）。

设计要点：
- 关系抽取按「chunk 批次」进行，每批文本里已包含全部上下文，
  抽出的关系两端实体必然来自本批（源头验证天然成立）。
- 实体 → chunk 的关联：用实体名在 chunk 文本中的位置（str.find）反查，
  为每个实体记录它出现在哪些 chunk 里，写成 Entity.chunk_ids 属性，
  供检索阶段按 chunk 反查实体（级联融合的桥接基础）。
- 默认不开启：只有当 settings.ENABLE_KNOWLEDGE_GRAPH 为真时才生效，
  避免未启用时白白消耗 LLM 额度。
"""

import logging
from typing import Dict, List, Optional, Tuple

from app.core.config import settings
from app.knowledge_graph.entity_dedup import entity_dedup

logger = logging.getLogger(__name__)

# 每批抽取的 chunk 数量：太小则关系稀疏，太大则超出 LLM 上下文窗口。
_BATCH_CHUNKS = 10


class KGIngestBridge:
    """文档入库 → 知识图谱构建 的桥接器。"""

    def __init__(self):
        self._extractor = None
        self._neo4j = None

    # ── 延迟初始化 ──
    def _entity_extractor(self):
        if self._extractor is None:
            from app.knowledge_graph.entity_extractor import entity_extractor
            self._extractor = entity_extractor
        return self._extractor

    def _manager(self):
        if self._neo4j is None:
            from app.knowledge_graph.neo4j_manager import Neo4jManager
            self._neo4j = Neo4jManager(
                uri=settings.NEO4J_URI,
                user=settings.NEO4J_USER,
                password=settings.NEO4J_PASSWORD,
            )
        return self._neo4j

    async def build_from_chunks(
        self,
        chunks: List[Tuple[str, str]],
        tenant_id: str,
        document_id: str,
    ) -> Dict[str, int]:
        """从一组 chunk 构建知识图谱。

        Args:
            chunks: [(chunk_id(UUID字符串), content), ...]
            tenant_id: 租户 ID
            document_id: 文档 ID（用于日志）

        Returns:
            {"entities": n, "relations": m}
        """
        if not getattr(settings, "ENABLE_KNOWLEDGE_GRAPH", False):
            logger.debug("[KGIngest] 知识图谱未启用，跳过")
            return {"entities": 0, "relations": 0}

        if not chunks:
            return {"entities": 0, "relations": 0}

        try:
            manager = self._manager()
            if not manager.driver:
                logger.warning("[KGIngest] Neo4j 未连接，跳过图谱构建")
                return {"entities": 0, "relations": 0}

            extractor = self._entity_extractor()
            extracted = await self._extract_by_batches(chunks, extractor)
            if not extracted["entities"]:
                logger.info(f"[KGIngest] 文档 {document_id} 未抽取到实体")
                return {"entities": 0, "relations": 0}

            # 实体名 → 出现它的 chunk_id 列表（跨库桥接）
            name_to_chunks: Dict[str, List[str]] = extracted["name_to_chunks"]

            # ── 实体消解：把同一实体的多种写法归并成一个规范名 ──
            # 如「南京中科水治理有限公司（联合体牵头人）」与「南京中科水治理股份有限公司」
            # 应成为同一个节点，否则图谱关系被割裂。
            entities, alias_map = entity_dedup.resolve(extracted["entities"])
            relations = self._rewrite_relations(extracted["relations"], alias_map)
            # chunk 关联也按规范名归并（否则别名下的 chunk 关联会丢）
            name_to_chunks = self._remap_name_to_chunks(name_to_chunks, alias_map)

            created_e = manager.batch_create_entities(entities, tenant_id)
            created_r = manager.batch_create_relations(relations, tenant_id)

            # 把 chunk_ids 回写到实体节点属性上
            await self._attach_chunk_ids(manager, name_to_chunks, tenant_id)

            logger.info(
                f"[KGIngest] 文档 {document_id} 图谱构建完成: "
                f"实体 {created_e}, 关系 {created_r}"
                + (f"（消解 {len(alias_map)} 个别名）" if alias_map else "")
            )
            return {"entities": created_e, "relations": created_r}

        except Exception as e:
            # 图谱构建属增强能力，失败不得影响主入库流程
            logger.warning(f"[KGIngest] 图谱构建失败（不影响入库）: {e}")
            return {"entities": 0, "relations": 0}

    async def _extract_by_batches(
        self,
        chunks: List[Tuple[str, str]],
        extractor,
    ) -> Dict:
        """按批抽取实体和关系，并记录每个实体出现在哪些 chunk。"""
        entities_by_key: Dict[str, Dict] = {}
        relations_by_key: Dict[str, Dict] = {}
        name_to_chunks: Dict[str, List[str]] = {}

        for start in range(0, len(chunks), _BATCH_CHUNKS):
            batch = chunks[start:start + _BATCH_CHUNKS]
            batch_text = "\n\n".join(content for _, content in batch)
            if not batch_text.strip():
                continue

            try:
                entities = await extractor.extract(batch_text)
            except Exception as e:
                logger.warning(f"[KGIngest] 批次实体抽取失败: {e}")
                continue

            if not entities:
                continue

            # 汇总实体（按 名称+类型 去重）
            for ent in entities:
                name = (ent.get("name") or "").strip()
                if not name:
                    continue
                key = f"{name}|{ent.get('type', 'ENTITY')}"
                if key not in entities_by_key:
                    entities_by_key[key] = ent
                # 关联 chunk：实体名出现在哪个 chunk 里
                for cid, content in batch:
                    if name and name in content:
                        name_to_chunks.setdefault(name, [])
                        if cid not in name_to_chunks[name]:
                            name_to_chunks[name].append(cid)

            # 关系抽取（本批范围内，两端实体同批 → 源头验证成立）
            try:
                relations = await self._relation_extractor().extract(
                    batch_text, entities
                )
            except Exception as e:
                logger.warning(f"[KGIngest] 批次关系抽取失败: {e}")
                relations = []

            for rel in relations:
                src = (rel.get("source") or rel.get("source_name") or "").strip()
                tgt = (rel.get("target") or rel.get("target_name") or "").strip()
                if not src or not tgt:
                    continue
                key = f"{src}|{rel.get('relation_type', rel.get('type', ''))}|{tgt}"
                if key not in relations_by_key:
                    relations_by_key[key] = rel

        return {
            "entities": list(entities_by_key.values()),
            "relations": list(relations_by_key.values()),
            "name_to_chunks": name_to_chunks,
        }

    def _relation_extractor(self):
        from app.knowledge_graph.relation_extractor import relation_extractor
        return relation_extractor

    @staticmethod
    def _rewrite_relations(relations: List[Dict], alias_map: Dict[str, str]) -> List[Dict]:
        """按消解结果把关系两端的别名替换为规范名。

        否则实体合并后，关系仍指向旧名，会在图里形成悬空引用。
        """
        if not alias_map or not relations:
            return relations
        out = []
        seen = set()
        for rel in relations:
            src = alias_map.get(rel.get("source", ""), rel.get("source", ""))
            tgt = alias_map.get(rel.get("target", ""), rel.get("target", ""))
            if not src or not tgt or src == tgt:
                continue
            r = dict(rel)
            r["source"], r["target"] = src, tgt
            key = f"{src}|{r.get('type')}|{tgt}"
            if key not in seen:
                seen.add(key)
                out.append(r)
        return out

    @staticmethod
    def _remap_name_to_chunks(name_to_chunks: Dict[str, List[str]],
                              alias_map: Dict[str, str]) -> Dict[str, List[str]]:
        """把「实体名 → chunk_ids」的 map 按消解结果归并到规范名。"""
        if not alias_map:
            return name_to_chunks
        out: Dict[str, List[str]] = {}
        for name, cids in name_to_chunks.items():
            canon = alias_map.get(name, name)
            bucket = out.setdefault(canon, [])
            for cid in cids:
                if cid not in bucket:
                    bucket.append(cid)
        return out

    async def _attach_chunk_ids(self, manager, name_to_chunks: Dict[str, List[str]], tenant_id: str) -> None:
        """把 chunk_ids 写回实体的属性，建立「实体 ↔ 文档块」跨库桥接。"""
        if not name_to_chunks or not manager.driver:
            return
        try:
            with manager.driver.session(database=settings.NEO4J_DATABASE) as session:
                rows = [
                    {"name": name, "chunk_ids": cids}
                    for name, cids in name_to_chunks.items()
                ]
                session.run(
                    """
                    UNWIND $rows AS row
                    MATCH (e:Entity {name: row.name, tenant_id: $tenant_id})
                    SET e.chunk_ids = row.chunk_ids
                    """,
                    rows=rows,
                    tenant_id=str(tenant_id),
                )
        except Exception as e:
            logger.warning(f"[KGIngest] 回写 chunk_ids 失败: {e}")

    def close(self):
        if self._neo4j:
            try:
                self._neo4j.close()
            except Exception:
                pass
            self._neo4j = None


kg_ingest_bridge = KGIngestBridge()
