import uuid
from sqlalchemy import Column, String, Integer, Text, DateTime, ForeignKey, func, Float, event
from sqlalchemy.dialects.postgresql import UUID, JSONB, TSVECTOR
from pgvector.sqlalchemy import Vector
from app.db.base import Base


class DocumentChunk(Base):
    """
    文档切片模型
    存储从文档中提取出来的文本片段
    """
    __tablename__ = "document_chunks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # 外键：关联到 Document 表
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)

    # 序号：这是第几段
    chunk_index = Column(Integer, nullable=False)

    # 内容：切分后的文本
    content = Column(Text, nullable=False)

    # 元数据：可以存这段文字所在的页码等信息
    meta_info = Column(JSONB, default={})

    # ✅ 向量嵌入：使用 pgvector Vector 类型，支持 HNSW/IVFFlat 索引
    # 向量维度：1024 (适配 embedding-3, bge-m3 等模型)
    embedding = Column(Vector(1024), nullable=True)

    # 新增字段：支持智能切块元数据
    heading_path = Column(String, nullable=True)  # 标题路径(如: "第一章 > 1.1节")
    chunk_start = Column(Integer, nullable=True)  # 在原文中的起始位置
    chunk_end = Column(Integer, nullable=True)    # 在原文中的结束位置
    token_count = Column(Integer, nullable=True)  # Token 数量

    # 租户隔离字段
    tenant_id = Column(String(50), nullable=True)  # 租户ID，用于多租户隔离

    # ==== v2 增强字段 ====
    domain = Column(String(20), nullable=True, index=True)        # finance/tax/legal/general
    node_type = Column(String(10), nullable=True, index=True)     # root/parent/leaf
    summary = Column(String(500), nullable=True)                  # PARENT 节点摘要
    relationships = Column(JSONB, default={})                     # {"PARENT": "uuid", "CHILDREN": [...], ...}
    node_hash = Column(String(64), nullable=True, index=True)     # 内容哈希，用于增量更新去重

    created_at = Column(DateTime(timezone=True), default=func.now(),server_default=func.now())

    # 全文检索向量：由应用层用 jieba 分词后写入（中文必须分词，PG 的 simple
    # 配置会把整句中文当成一个 token，导致 BM25 恒为 0 命中）。
    # 该列原为 GENERATED 列，已迁移为普通列，由下方事件监听自动维护。
    content_tsvector = Column(TSVECTOR, nullable=True)

    # 建立关系：方便以后通过 chunk.document 访问父文档
    # document = relationship("Document", back_populates="chunks")
    # (暂时先注释掉关系反向引用，避免要修改 document.py，保持简单)


@event.listens_for(DocumentChunk, "before_insert")
@event.listens_for(DocumentChunk, "before_update")
def _fill_content_tsvector(mapper, connection, target):
    """写入/更新前，用 jieba 分词填充 content_tsvector。

    覆盖所有 ORM 写入路径（入库、回填、更新），无需逐个修改构造点。
    """
    try:
        from app.utils.chinese_fts import build_document_text
        from sqlalchemy import text as _text
        doc_text = build_document_text(target.content or "")
        if doc_text:
            # 借数据库的 to_tsvector 生成 tsvector（与检索侧一致）
            conn = connection
            res = conn.execute(
                _text("SELECT to_tsvector('simple', :t)"),
                {"t": doc_text},
            )
            target.content_tsvector = res.scalar()
    except Exception:
        # 分词失败不应阻断入库；BM25 对该块退化为不可检索
        pass

