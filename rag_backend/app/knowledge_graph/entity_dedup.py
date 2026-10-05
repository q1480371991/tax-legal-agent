"""实体去重归一（Entity Dedup / Resolution）

把指向同一现实实体的不同名称字符串归并成一个规范名，避免知识图谱碎裂。

问题：抽取器会产出「南京中科水治理股份有限公司」「南京中科水治理有限公司（联合体牵头人）」
      「平安银行」「平安银行股份有限公司」等 —— 字符串不同、实为同一实体，
      入库后成为多个节点，图谱关系被割裂。

三层策略（由保守到激进，默认只启用前两层）：
  ① 规则归一化：去掉纯类型注释括号、公司后缀、角色说明括号
  ② 精确合并：归一化后完全相同的，直接合并（保留最短名作规范名，其余记入 aliases）
  ③ 相似度合并（可选，默认关闭）：向量相似度超阈值 → 视为同一实体

关键约束：**含数字的名称不参与合并** —— 「毛利率（9.22%）」「毛利率（8.64%）」是
同一指标的不同期数值，合并会丢信息，属"指标+数值"类而非重复实体。
"""

import logging
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 纯类型注释括号：括号内是类型说明，去掉后仍是同一实体
_TYPE_ANNOT = re.compile(
    r'[（(](?:会计准则规范|期间|会计期间|资产科目|会计科目|计量属性|报告章节|'
    r'财务说明项|业务状态|专项说明|现金流量表科目|会计准则|定义|说明|规范|'
    r'科目|属性|类型|类别|口径|单位)[）)]'
)
# 角色/附属说明括号：如「（联合体牵头人）」「（本公司董事会）」
_ROLE_ANNOT = re.compile(
    r'[（(](?:联合体牵头人|本公司董事会|旗下[^）)]*|原称[:：][^）)]*|'
    r'简称[:：][^）)]*|以下简称[^）)]*)[）)]'
)
# 公司/机构后缀（按长度降序，先匹配长的）
_SUFFIXES = (
    "股份有限公司", "有限责任公司", "集团有限公司", "有限公司",
    "集团", "公司", "事务所", "分行", "支行",
)

_DIGIT = re.compile(r'\d')


def normalize_name(name: str, strip_suffix: bool = True) -> str:
    """归一化实体名。

    Args:
        name: 原始实体名
        strip_suffix: 是否剥离公司后缀（用于跨写法合并；纯展示时可不剥）

    Returns:
        归一化后的名称
    """
    if not name:
        return ""
    n = name.strip()
    n = _TYPE_ANNOT.sub("", n)
    n = _ROLE_ANNOT.sub("", n)
    if strip_suffix:
        for suf in _SUFFIXES:
            if n.endswith(suf) and len(n) > len(suf) + 1:
                n = n[: -len(suf)]
                break
    return n.strip()


def has_number(name: str) -> bool:
    """名称含数字 → 视为「指标+数值」类，不参与合并。"""
    return bool(_DIGIT.search(name or ""))


class EntityDedup:
    """实体消解器：把同一实体的多种写法归并。"""

    def __init__(self, similarity_threshold: float = 0.92, enable_similarity: bool = False):
        """
        Args:
            similarity_threshold: 相似度合并阈值（仅 enable_similarity=True 时生效）
            enable_similarity: 是否启用向量相似度合并（需 embedding 服务，成本较高）
        """
        self.similarity_threshold = similarity_threshold
        self.enable_similarity = enable_similarity

    def resolve(
        self, entities: List[Dict],
    ) -> Tuple[List[Dict], Dict[str, str]]:
        """对实体列表做消解。

        Args:
            entities: [{"name","type",...}, ...]

        Returns:
            (消解后的实体列表, 别名映射 {别名: 规范名})
        """
        if not entities:
            return [], {}

        # 按 (归一化名, 类型) 分组；含数字的不参与合并
        groups: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
        passthrough: List[Dict] = []          # 不参与合并的（含数字）
        for ent in entities:
            name = (ent.get("name") or "").strip()
            if not name:
                continue
            if has_number(name):
                passthrough.append(ent)
                continue
            key = (normalize_name(name), ent.get("type", "ENTITY"))
            groups[key].append(ent)

        resolved: List[Dict] = list(passthrough)
        alias_map: Dict[str, str] = {}

        for (norm, etype), members in groups.items():
            if len(members) == 1:
                resolved.append(members[0])
                continue
            # 选最短名作规范名（通常最干净），其余进 aliases
            members_sorted = sorted(members, key=lambda e: len(e.get("name", "")))
            canonical = members_sorted[0]
            aliases = [m.get("name") for m in members_sorted[1:] if m.get("name")]
            merged = dict(canonical)
            merged["aliases"] = sorted(set((merged.get("aliases") or []) + aliases))
            # 置信度取最高
            confs = [m.get("confidence", 0) for m in members if m.get("confidence") is not None]
            if confs:
                merged["confidence"] = max(confs)
            # 合并 properties
            props = {}
            for m in members:
                if isinstance(m.get("properties"), dict):
                    props.update(m["properties"])
            if props:
                merged["properties"] = props
            resolved.append(merged)
            for a in aliases:
                alias_map[a] = canonical.get("name", "")

        if alias_map:
            logger.info(f"[EntityDedup] 合并 {len(alias_map)} 个别名 → {len(groups)} 组")
        return resolved, alias_map

    def resolve_names(self, names: List[str], entity_type: str = "ENTITY") -> List[str]:
        """轻量版：只对名称列表去重（用于不需要完整实体对象的场景）。"""
        ents = [{"name": n, "type": entity_type} for n in names if n]
        out, _ = self.resolve(ents)
        return [e["name"] for e in out]


entity_dedup = EntityDedup()
