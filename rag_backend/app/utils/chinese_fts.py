"""中文全文检索分词工具。

背景：PostgreSQL 的 `simple` 配置对中文不切词 —— 它把连续汉字当作**一个 token**。
导致 `plainto_tsquery('simple', '开发支出')` 生成 `'开发支出'`，
而文档侧 `to_tsvector('simple', '不满足上述条件的开发支出')` 生成的是
`'不满足上述条件的开发支出'` 这样的整串 token，两者匹配不上 → BM25 检索中文基本失效。

解法：在**应用层用 jieba 分词**，把中文切成词，再用 `&`（AND）或 `|`（OR）拼接成
tsquery，让 PG 按 token 匹配。

注意：文档侧的 `content_tsvector` 是 GENERATED 列（由 to_tsvector('simple', content)
自动生成），仍是整串中文 token —— 所以文档侧也要做同样处理才能完全对齐。
本模块同时提供 `build_document_terms`（供维护 fts 列/触发器用）。

实现选择：查询侧用 OR 拼接（任一词命中即可，召回优先），配合 ts_rank 排序。
对中文召回更友好；精确度由后续 rerank 环节补足。
"""
import logging
import re
from typing import List

logger = logging.getLogger(__name__)

# 仅保留中英文数字，其余（标点/空格）作为分隔
_KEEP = re.compile(r"[^\w一-鿿]+")

# 中文停用词：这些词做检索词会命中海量无关文档
_STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "或", "及", "等", "这", "那", "其",
    "有", "为", "被", "把", "对", "从", "到", "中", "上", "下", "之", "也",
    "什么", "哪些", "多少", "如何", "怎么", "为什么", "是否", "可以", "需要",
    "什么", "请问", "一下", "关于", "对于", "根据", "按照", "以及", "并且",
}


def _tokenize(text: str) -> List[str]:
    """中文分词 + 清洗。"""
    if not text:
        return []
    try:
        import jieba
        words = jieba.lcut(text)
    except ImportError:
        # 无 jieba 时退化为按非字母数字切分（英文可用，中文效果差）
        logger.warning("[FTS] jieba 未安装，退化为简单切分")
        words = _KEEP.split(text)

    out: List[str] = []
    for w in words:
        w = _KEEP.sub("", w).strip()
        if not w or len(w) < 2:          # 单字噪声多，丢弃
            continue
        if w in _STOPWORDS:
            continue
        # 纯数字/单字符数字串也丢掉（对检索无区分度）
        if w.isdigit():
            continue
        out.append(w)
    # 去重保序
    seen = set()
    uniq = []
    for w in out:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq


def _escape_term(term: str) -> str:
    """转义 tsquery 里的特殊字符，并加引号防被当语法解析。"""
    safe = term.replace("'", "''").replace("\\", "\\\\")
    return f"'{safe}'"


def build_tsquery(text: str, mode: str = "or") -> str:
    """把自然语言查询转成 PostgreSQL tsquery 字符串。

    Args:
        text: 查询原文
        mode: "or"（任一词命中，召回优先）或 "and"（全部词命中，精度优先）

    Returns:
        tsquery 字符串，如 `'开发支出' | '比亚迪'`；无有效词时返回空串
    """
    terms = _tokenize(text)
    if not terms:
        return ""
    op = " | " if mode == "or" else " & "
    return op.join(_escape_term(t) for t in terms)


def build_document_text(text: str) -> str:
    """把文档正文预处理成「空格分隔的词串」，供 to_tsvector 使用。

    配合 `to_tsvector('simple', <本函数输出>)`，文档侧也会变成词级 token，
    与查询侧对齐。
    """
    return " ".join(_tokenize(text))
