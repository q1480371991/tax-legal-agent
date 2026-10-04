"""
通用领域切块策略 (General Chunker)

核心设计：
1. Auto-Merging 双粒度切分：256 Token LEAF + 1024 Token PARENT
2. 复用现有 StructuredDocumentChunker 作为基础引擎
3. LEAF 用于向量检索，PARENT 用于命中后的上下文展开
"""

import logging
from typing import List
from app.chunkers.base_chunker import ChunkResult, ChunkStrategy
from app.models.structured_document import StructuredDocument
from app.chunkers.structured_document_chunker import StructuredDocumentChunker

logger = logging.getLogger(__name__)


class GeneralChunker:
    """
    通用文档切块器。

    包装现有的 StructuredDocumentChunker，在其输出上叠加
    Auto-Merging 双粒度策略。
    """

    LEAF_TOKEN_TARGET = 256       # 小块 Token 目标
    PARENT_TOKEN_TARGET = 1024    # 大块 Token 目标

    def __init__(self):
        self._inner = StructuredDocumentChunker()

    def chunk(
        self,
        structured_doc: StructuredDocument,
        chunk_tokens: int = 800,
        overlap_tokens: int = 80,
    ) -> List[ChunkResult]:
        """
        对通用文档执行双粒度切块。

        Args:
            structured_doc: 结构化文档
            chunk_tokens: 目标 Token 数量（未使用，使用内部常量）
            overlap_tokens: 重叠 Token 数量

        Returns:
            LEAF + PARENT 双层节点列表
        """
        # Step 1: 用现有 chunker 生成父块（1024 token 目标）
        parent_chunks = self._inner.chunk_structured_document(
            structured_doc,
            chunk_tokens=self.PARENT_TOKEN_TARGET,
            overlap_tokens=overlap_tokens,
        )

        # 标记为 parent
        for chunk in parent_chunks:
            chunk.domain = "general"
            chunk.node_type = "parent"

        # Step 2: 对每个父块做细分（256 token 目标）
        # 先收集分组，待 LEAF 总数确定后再统一分配 chunk_index。
        # 返回顺序固定为 [所有 LEAF, 所有 PARENT]，chunk_index 与该顺序一致，
        # 从而与存储层 enumerate 写库的 chunk_index 列对齐（关系引用依赖此约定）。
        grouped: List[tuple] = []
        leaf_chunks: List[ChunkResult] = []
        for parent in parent_chunks:
            leaves = self._split_into_leaves(parent)
            for leaf in leaves:
                leaf.domain = "general"
                leaf.node_type = "leaf"
            grouped.append((parent, leaves))
            leaf_chunks.extend(leaves)

        for i, leaf in enumerate(leaf_chunks):
            leaf.chunk_index = i
        parent_offset = len(leaf_chunks)
        for j, parent in enumerate(parent_chunks):
            parent.chunk_index = parent_offset + j

        # Step 3: 建立 LEAF → PARENT 与 PARENT → CHILDREN 关系
        for parent, leaves in grouped:
            parent.relationships = {
                "CHILDREN": [leaf.chunk_index for leaf in leaves]
            }
            for leaf in leaves:
                leaf.relationships = {"PARENT": parent.chunk_index}

        all_chunks = leaf_chunks + parent_chunks

        logger.info(
            f"[GeneralChunker] 切块完成: "
            f"{len(leaf_chunks)} 个 LEAF, {len(parent_chunks)} 个 PARENT"
        )
        return all_chunks

    def _split_into_leaves(self, parent: ChunkResult) -> List[ChunkResult]:
        """
        将父块细分为小叶子块。

        以句号/换行为边界，每个叶子块约 LEAF_TOKEN_TARGET token。
        """
        text = parent.content
        if not text.strip():
            return []

        # 按句号/换行分割句子
        sentences = []
        current = ""
        for char in text:
            current += char
            if char in "。！？.!?\n":
                sentences.append(current.strip())
                current = ""
        if current.strip():
            sentences.append(current.strip())

        if not sentences:
            return [parent]

        # 合并句子到目标 Token 大小
        leaves: List[ChunkResult] = []
        current_leaf = ""
        current_tokens = 0
        leaf_start = 0

        for sentence in sentences:
            sentence_tokens = ChunkStrategy.approx_token_len(sentence)

            if current_tokens + sentence_tokens > self.LEAF_TOKEN_TARGET and current_leaf:
                # 当前叶子已满，保存
                leaves.append(
                    ChunkResult(
                        content=current_leaf.strip(),
                        start=leaf_start,
                        end=leaf_start + len(current_leaf),
                        tokens=current_tokens,
                        heading_path=parent.heading_path,
                        domain="general",
                        node_type="leaf",
                        metadata=dict(parent.metadata),
                    )
                )
                leaf_start += len(current_leaf)
                current_leaf = ""
                current_tokens = 0

            current_leaf += sentence
            current_tokens += sentence_tokens

        # 最后一块
        if current_leaf.strip():
            leaves.append(
                ChunkResult(
                    content=current_leaf.strip(),
                    start=leaf_start,
                    end=leaf_start + len(current_leaf),
                    tokens=current_tokens,
                    heading_path=parent.heading_path,
                    domain="general",
                    node_type="leaf",
                    metadata=dict(parent.metadata),
                )
            )

        return leaves if leaves else [parent]


# 全局单例
general_chunker = GeneralChunker()
