"""
意图路由智能体 (Intent Router Agent)
融合了接待智能体和意图识别智能体的功能，统一处理用户输入

职责：
1. 快速简单检测（正则匹配，不调用LLM）
2. 意图分类 + 实体提取 + 复杂度评估（调用LLM）
3. 路由决策
"""

import re
import json
import logging
from typing import Dict, List, Any, Optional, TYPE_CHECKING
from enum import Enum
from pydantic import BaseModel, Field, ConfigDict
from pathlib import Path

from app.agent_framework.core.base_agent import BaseAgent
from app.agent_framework.llm.base_adapter import BaseLLMAdapter
from app.agent_framework.tools.tool_manager import ToolManager
from app.services.prompt_service import PromptEngine
from app.multi_agent_system.agents.base_agent_prompt import load_agent_prompt
from app.multi_agent_system.clarification_service import (
    ClarificationService,
    ClarificationRequest,
    ClarificationType
)

if TYPE_CHECKING:
    from app.skills.skill_registry import SkillRegistry

logger = logging.getLogger(__name__)


class IntentCategory(str, Enum):
    """意图分类枚举"""
    GREETING = "greeting"
    CHIT_CHAT = "chit_chat"
    KNOWLEDGE_QUERY = "knowledge_query"
    DOCUMENT_SEARCH = "document_search"
    FINANCIAL_ANALYSIS = "financial_analysis"
    ACCOUNTING_QUERY = "accounting_query"
    INVESTMENT_ADVISORY = "investment_advisory"
    COST_CONTROL = "cost_control"
    TAX_CALCULATION = "tax_calculation"
    TAX_PLANNING = "tax_planning"
    TAX_COMPLIANCE = "tax_compliance"
    TAX_DECLARATION = "tax_declaration"
    CONTRACT_REVIEW = "contract_review"
    LEGAL_CONSULTATION = "legal_consultation"
    COMPLIANCE_CHECK = "compliance_check"
    IP_PROTECTION = "ip_protection"
    REPORT_GENERATION = "report_generation"
    DATA_EXTRACTION = "data_extraction"
    RISK_ANALYSIS = "risk_analysis"
    COMPLEX_TASK = "complex_task"
    MULTI_SPECIALIST = "multi_specialist"
    UNKNOWN = "unknown"


class ComplexityLevel(str, Enum):
    """复杂度等级"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


class RoutingStrategy(str, Enum):
    """路由策略"""
    DIRECT_ANSWER = "direct_answer"
    RAG_RETRIEVAL = "rag_retrieval"
    SINGLE_SPECIALIST = "single_specialist"
    MULTI_SPECIALIST_PARALLEL = "multi_specialist_parallel"
    REPORT_QUEUE = "report_queue"
    # 注：原 MULTI_SPECIALIST_SEQUENTIAL 已移除。本系统里专家之间没有依赖关系
    #     （都基于同一份检索结果各答各的），串行只会更慢且无收益；该枚举也从未
    #     被 _determine_routing_strategy 产生过，属于纯死代码。


class ExtractedEntity(BaseModel):
    """提取的实体"""
    entity_type: str = Field(..., description="实体类型")
    entity_value: str = Field(..., description="实体值")
    confidence: float = Field(..., ge=0.0, le=1.0, description="置信度")
    source_text: str = Field(..., description="来源文本")


class IntentAnalysisResult(BaseModel):
    """意图分析结果"""
    intent: IntentCategory = Field(..., description="主要意图")
    sub_intent: Optional[IntentCategory] = Field(None, description="子意图")
    entities: List[ExtractedEntity] = Field(default_factory=list, description="提取的实体")
    complexity: ComplexityLevel = Field(..., description="复杂度")
    requires_specialists: List[str] = Field(default_factory=list, description="需要的专家列表")
    routing_strategy: RoutingStrategy = Field(..., description="路由策略")
    suggested_params: Dict[str, Any] = Field(default_factory=dict, description="建议参数")
    confidence: float = Field(..., ge=0.0, le=1.0, description="整体置信度")
    needs_human_review: bool = Field(False, description="是否需要人工审核")
    reasoning: str = Field("", description="推理过程")
    needs_report_generation: bool = Field(False, description="是否需要生成报告")


class IntentRoutingResult(BaseModel):
    """意图路由结果（统一返回类型）"""
    is_simple: bool = Field(False, description="是否为简单问题")
    simple_response: Optional[str] = Field(None, description="简单问题的回答")
    intent_result: Optional[IntentAnalysisResult] = Field(None, description="意图分析结果")
    clarification_request: Optional[ClarificationRequest] = Field(None, description="追问请求（当输入模糊时）")
    
    model_config = ConfigDict(
        arbitrary_types_allowed=True
    )


# ── 跨领域请求的句式判定 ──
# 为什么不用词表：单纯"出现 ≥2 个领域词"会把「财务和法务的区别是什么」
# （对比题，单专家即可）也判成跨领域。真正表示"要多个领域分别处理"的是
# 【跨领域请求的结构】：涉及/涵盖/从…角度 + 两个领域 + 处理动词。
_DOMAIN_WORDS = {
    "finance": ("财务", "会计", "报表", "资金", "成本", "预算"),
    "tax": ("税务", "税", "纳税", "发票", "税收"),
    "legal": ("法务", "法律", "合同", "合规", "诉讼"),
}
# "请求跨领域处理"的结构性信号
_CROSS_DOMAIN_CUES = (
    "涉及", "涵盖", "结合", "分别从", "从.*角度", "多个方面", "各方面",
    "综合", "全面", "以及",
    # 并列/递进连接词 —— 表示"多个领域都要处理"
    # 注：中英文逗号也在此列。中文里用逗号罗列多个诉求极常见
    #     （「有税务风险，合同有没有问题，财务健不健康」），
    #     原先只收了顿号「、」，导致这类请求被误判为单领域。
    "和", "与", "、", "，", ",", "并", "同时", "既", "又",
)
# "对比/区别"类 —— 会命中领域词但只需单专家
_CONTRAST_CUES = ("区别", "差异", "不同", "比较", "对比", "哪个更", "谁更")


def _detect_cross_domain(text_lower: str) -> list:
    """检测"需要多个领域专家分别处理"的请求，返回领域列表。

    判据为【结构信号 + 多领域 + 非对比题】，而非单纯词表命中：
      「分析…风险，涉及财务和法务」→ 跨领域 ✅
      「财务和法务的区别是什么」   → 对比题，单专家 ❌
    """
    if any(c in text_lower for c in _CONTRAST_CUES):
        return []
    # 命中的领域（去重）
    hit = [d for d, words in _DOMAIN_WORDS.items()
           if any(w in text_lower for w in words)]
    if len(hit) < 2:
        return []
    # 需要"请求跨领域处理"的结构信号 —— 单有多个领域词不够
    import re as _re
    has_cue = any(
        (_re.search(cue, text_lower) if cue == "从.*角度" else cue in text_lower)
        for cue in _CROSS_DOMAIN_CUES
    )
    return hit if has_cue else []


# ── 报告类意图的句式判定 ──
# 为什么不用单字匹配：中文里"报告"既可是"话题"（「XX报告中的YY」= 查询），
# 也可是"动作对象"（「生成一份报告」= 要做事）。单字匹配无法区分语境，
# 会把「报告中的开发支出是多少」误判为"要生成报告"。
# 解法：判据的形态从【出现某词】升级为【动作词 + 报告 同现】。
_REPORT_ACTION_WORDS = (
    "生成", "输出", "写", "撰写", "起草", "出具", "给我", "帮我出",
    "做一份", "做份", "整理", "汇总成", "编一份",
)
_REPORT_NOUNS = ("报告", "分析文档", "分析材料", "分析报告", "总结")


def _is_report_request(text_lower: str) -> bool:
    """判断是否为"生成报告"类请求（动作句式），而非"报告中的…"这类话题查询。"""
    # 先排除修饰结构：「报告中的 / 报告里 / 报告内 / 报告的内容」—— 这些是查询
    if any(f"{n}中的" in text_lower or f"{n}里" in text_lower
           or f"{n}内" in text_lower or f"{n}的内容" in text_lower
           for n in ("报告", "报表")):
        return False
    # 动作词 + 报告类名词 同现
    return any(n in text_lower for n in _REPORT_NOUNS) and any(
        a in text_lower for a in _REPORT_ACTION_WORDS
    )


class IntentRouterAgent(BaseAgent):
    """
    意图路由智能体
    
    融合了ReceptionistAgent和IntentAgent的功能：
    1. 快速简单检测（正则，不调用LLM）
    2. 意图分类+实体提取+复杂度评估（调用LLM）
    3. 路由决策
    
    优势：
    - 减少LLM调用次数（从2次减少到1-2次）
    - 消除职责重复
    - 简化编排器逻辑
    """
    
    SIMPLE_PATTERNS = {
        "greeting": r"^(你好|您好|hi|hello|嗨|hey)[\s,，.]*",
        "weather": r".*?(天气|weather).*",
        "time": r".*?(时间|time|现在几点).*",
        "help": r".*?(help|帮助|怎么用|如何使用).*",
        "thanks": r"^.*?(谢谢|thanks|感谢)[\s,，.]*",
        "config_query": r".*?(有没有打开|是否启用|开启了吗|关闭了吗|当前状态|当前配置|我的设置|会话状态)",
        # 仅匹配不包含具体领域的通用技能询问（^ 锚定开头防止 re.search 从中间位置绕过负向先行断言）
        # 带"财务/税务/法律/金融"等领域的应路由到对应 specialist 展示实际注册的技能
        "skill_query": r"^(?=.*(?:技能|skill|能力))(?=.*(?:有哪|是什么|有哪些|有什么|列出|介绍|展示))(?!.*(?:财务|金融|税务|税收|法律|法务|合规|合同|投资|审计)).*",
    }
    SKILL_QUERY_FALLBACK = (
        "我具备以下领域的专业技能：\n\n"
        "**财务领域**：投资分析、财务数据分析、成本控制、预算管理、财务报表分析等\n"
        "**税务领域**：增值税计算、企业所得税、税务筹划、发票管理等\n"
        "**法律领域**：合同审查、知识产权、劳动法、合规检查等\n\n"
        "请告诉我您具体想了解哪个领域，我可以为您详细说明该领域可用的功能和技能。"
    )
    
    ENTITY_PATTERNS = {
        "money": {
            "patterns": [
                r"([¥$€£]?\d+(?:,\d{3})*(?:\.\d{2})?)",
                r"(\d+(?:,\d{3})*(?:\.\d{2})?\s*(?:万|亿|千|百)?\s*(?:元|美元|欧元|英镑))",
            ],
            "entity_type": "金额"
        },
        "percentage": {
            "patterns": [
                r"(\d+(?:\.\d+)?%)",
                r"百分之(\d+(?:\.\d+)?)",
            ],
            "entity_type": "百分比"
        },
        "date": {
            "patterns": [
                r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)",
                r"(\d{4}年\d{1,2}月)",
            ],
            "entity_type": "日期"
        },
        "tax_type": {
            "patterns": [
                r"(增值税|企业所得税|个人所得税|消费税|关税|城建税|教育费附加|地方教育附加)",
            ],
            "entity_type": "税种"
        },
        "contract_type": {
            "patterns": [
                r"(采购合同|销售合同|服务合同|租赁合同|劳动合同|咨询合同)",
            ],
            "entity_type": "合同类型"
        },
    }
    
    COMPLEXITY_RULES = {
        ComplexityLevel.LOW: [
            r"(你好|您好|hi|hello)",
            r"(现在几点|今天几号|当前时间)",
            r"(什么是|什么叫|定义)",
        ],
        ComplexityLevel.MEDIUM: [
            r"(计算|分析|查询)",
            r"(如何|怎么|怎样)",
        ],
        ComplexityLevel.HIGH: [
            r"(比较|对比|差异)",
            r"(优化|改进|提升)",
        ],
        ComplexityLevel.VERY_HIGH: [
            r"(报告|报表|生成.*报告)",
            r"(审查|审计|检查)",
        ]
    }
    
    def __init__(
        self,
        llm_adapter: BaseLLMAdapter,
        tool_manager: ToolManager,
        confidence_threshold: float = 0.7,
        max_iterations: int = 3,
        timeout: float = 30.0,
        specialist_descriptions: str = "",
        intent_mapping: Dict[str, str] = None,
        skill_registry: Optional['SkillRegistry'] = None,  # 🆕 技能系统
    ):
        """
        初始化意图路由智能体

        Args:
            llm_adapter: 大模型适配器
            tool_manager: 工具管理器
            confidence_threshold: 置信度阈值
            max_iterations: 最大迭代次数
            timeout: 超时时间
            specialist_descriptions: 专家能力描述
            intent_mapping: 意图到专家的映射
            skill_registry: 技能注册表（可选）
        """
        self.confidence_threshold = confidence_threshold
        self.prompt_engine = PromptEngine()
        self._specialist_descriptions = specialist_descriptions
        self._intent_mapping = intent_mapping or {}
        self._classification_prompt_cache: Optional[str] = None

        self.clarification_service = ClarificationService(
            min_query_length=5,
            confidence_threshold=0.6,
            enable_clarification=True
        )

        system_prompt = self._load_system_prompt()

        super().__init__(
            llm_adapter=llm_adapter,
            tool_manager=tool_manager,
            system_prompt=system_prompt,
            max_iterations=max_iterations,
            timeout=timeout,
            skill_registry=skill_registry,  # 🆕 技能系统
        )
        
        print("🎯 [意图路由智能体] 初始化完成")
        print("   - 快速简单检测: 启用")
        print("   - LLM意图分类: 启用")
        print("   - 置信度阈值: " + str(self.confidence_threshold))
    
    def _load_system_prompt(self) -> str:
        """从外部文件加载系统提示词"""
        try:
            return load_agent_prompt(
                agent_name="intent_router",
                filename="system.md",
                context=self._get_prompt_context()
            )
        except Exception as e:
            logger.debug(f"[意图路由智能体] 加载提示词失败，使用默认提示词: {e}")
            return self._build_default_prompt()
    
    def _get_prompt_context(self) -> Dict[str, Any]:
        """获取提示词渲染上下文"""
        mapping_text = self._format_intent_mapping()
        return {
            "intent_categories": [c.value for c in IntentCategory],
            "complexity_levels": [c.value for c in ComplexityLevel],
            "routing_strategies": [c.value for c in RoutingStrategy],
            "specialist_descriptions": self._specialist_descriptions or "暂无专家配置",
            "intents_specialist_mapping": mapping_text,
        }
    
    def _format_intent_mapping(self) -> str:
        """将意图映射格式化为可读文本"""
        if not self._intent_mapping:
            return "暂无映射配置"
        lines = []
        for intent, specialist in sorted(self._intent_mapping.items()):
            lines.append(f"- **{intent}** → {specialist}")
        return "\n".join(lines)
    
    def _build_default_prompt(self) -> str:
        """构建默认提示词"""
        return """# 意图路由智能体

## 角色定位
你是专业的意图路由智能体，负责理解用户问题、分析意图、评估复杂度，并决定最佳处理策略。

## 核心能力

### 1. 快速简单检测（优先执行，不调用LLM）
以下情况直接返回回答：
- 问候语："你好"、"您好"、"hi"等 → 返回友好问候
- 感谢："谢谢"、"感谢"等 → 返回礼貌回复
- 帮助请求：包含"帮助"、"怎么用"等 → 返回使用指南
- 时间查询：包含"现在几点"、"今天几号"等 → 返回当前时间

### 2. 意图分类
识别以下意图类别：
- **日常类**: greeting(问候), chit_chat(闲聊)
- **知识类**: knowledge_query(知识查询), document_search(文档搜索)
- **财务类**: financial_analysis, accounting_query, investment_advisory, cost_control, risk_analysis
- **税务类**: tax_calculation, tax_planning, tax_compliance, tax_declaration
- **法务类**: contract_review, legal_consultation, compliance_check, ip_protection
- **报告类**: report_generation, data_extraction
- **复杂类**: complex_task, multi_specialist

### 3. 实体提取
识别的实体类型：
- 金额: 数字+货币单位
- 日期: 具体日期或日期范围
- 税种: 增值税、所得税等
- 合同类型: 采购合同、服务合同等

### 4. 复杂度评估
- **low**: 简单计算、单一事实查询、定义类问题
- **medium**: 单一领域分析、需要工具计算
- **high**: 多领域交叉、需要多步推理
- **very_high**: 综合审查、多专家协作、报告生成

### 5. 路由策略
- **direct_answer**: 直接回答（问候、闲聊）
- **rag_retrieval**: RAG检索（知识查询）
- **single_specialist**: 单专家处理
- **multi_specialist_parallel**: 多专家并行处理（专家间无依赖，并行即可）
- **report_queue**: 报告队列

## 输出格式
```json
{
  "intent": "意图类别",
  "sub_intent": "子意图（可选）",
  "entities": [...],
  "complexity": "low/medium/high/very_high",
  "requires_specialists": ["specialist1"],
  "routing_strategy": "routing_strategy",
  "confidence": 0.0-1.0,
  "needs_human_review": true/false,
  "reasoning": "推理过程"
}
```"""
    
    def _is_simple_greeting(self, text: str) -> Optional[str]:
        """检测简单问候语（正则匹配，不调用LLM）"""
        text_lower = text.lower().strip()
        
        if re.search(self.SIMPLE_PATTERNS["greeting"], text_lower):
            return self._build_greeting_response()
        
        if re.search(self.SIMPLE_PATTERNS["thanks"], text_lower):
            return "不客气！很高兴能帮助您。请问还有什么其他问题吗？"
        
        if re.search(self.SIMPLE_PATTERNS["help"], text_lower):
            return self._build_help_response()
        
        if re.search(self.SIMPLE_PATTERNS["config_query"], text_lower):
            return "__CONFIG_QUERY__"

        if re.search(self.SIMPLE_PATTERNS["skill_query"], text_lower):
            return self.SKILL_QUERY_FALLBACK

        return None
    
    def _build_greeting_response(self) -> str:
        """构建问候响应"""
        from datetime import datetime
        hour = datetime.now().hour
        
        if hour < 12:
            time_greeting = "上午好"
        elif hour < 18:
            time_greeting = "下午好"
        else:
            time_greeting = "晚上好"
        
        return f"{time_greeting}！欢迎使用企业智能助手。我是您的AI助手，可以帮助您解答财务、税务、法律等方面的问题。请问有什么可以帮到您的？"
    
    def _build_help_response(self) -> str:
        """构建帮助响应"""
        return """📖 **智能助手使用指南**

我可以帮助您处理以下类型的问题：

**💰 财务分析**
- 财务报表分析
- 成本控制建议
- 投资风险评估

**📋 税务咨询**
- 税务政策查询
- 发票管理指导
- 税务筹划建议

**⚖️ 法律顾问**
- 合同审查
- 合规检查
- 法律咨询

**📄 报告生成**
- 财务分析报告
- 风险评估报告
- 综合审查报告

**💬 其他**
- 企业知识查询
- 常见问题解答

请直接输入您的问题，我会尽力为您解答！"""

    def _extract_entities(self, text: str) -> List[ExtractedEntity]:
        """提取实体（正则匹配）"""
        entities = []
        
        for entity_name, config in self.ENTITY_PATTERNS.items():
            for pattern in config["patterns"]:
                matches = re.finditer(pattern, text)
                for match in matches:
                    entities.append(ExtractedEntity(
                        entity_type=config["entity_type"],
                        entity_value=match.group(1) if match.groups() else match.group(),
                        confidence=0.9,
                        source_text=text[max(0, match.start()-10):min(len(text), match.end()+10)]
                    ))
        
        return entities
    
    def _assess_complexity(
        self,
        text: str,
        intent: IntentCategory,
        entities: List[ExtractedEntity]
    ) -> ComplexityLevel:
        """评估问题复杂度"""
        if intent in [IntentCategory.GREETING, IntentCategory.CHIT_CHAT]:
            return ComplexityLevel.LOW
        
        if intent == IntentCategory.REPORT_GENERATION:
            return ComplexityLevel.VERY_HIGH
        
        complexity_score = 0
        
        for level, patterns in self.COMPLEXITY_RULES.items():
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    complexity_score += {
                        ComplexityLevel.LOW: 1,
                        ComplexityLevel.MEDIUM: 2,
                        ComplexityLevel.HIGH: 3,
                        ComplexityLevel.VERY_HIGH: 4
                    }[level]
        
        complexity_score += len(entities) * 0.5
        
        if "和" in text or "或" in text or "还是" in text:
            complexity_score += 2
        
        if complexity_score <= 2:
            return ComplexityLevel.LOW
        elif complexity_score <= 4:
            return ComplexityLevel.MEDIUM
        elif complexity_score <= 6:
            return ComplexityLevel.HIGH
        else:
            return ComplexityLevel.VERY_HIGH
    
    def _resolve_routing_strategy(
        self,
        llm_routing,
        intent: IntentCategory,
        complexity: ComplexityLevel,
    ) -> RoutingStrategy:
        """决定路由策略。★ LLM 判断优先，规则仅降级。

        为什么把 routing_strategy 也交给 LLM：它要区分的是"查事实 vs 做分析"，
        而这与领域无关 —— 「比亚迪的开发支出是多少」（查，走 RAG）和
        「分析一下我们公司的偿债能力」（析，派专家）都是 finance。
        用 intent 标签去推这件事推不准（LLM 会把后者也标成 knowledge_query），
        用专家列表也推不准（两者都有 finance）。既然分类调用本来就在跑，
        就直接问它，规则只在 LLM 不可用时兜底。
        """
        if isinstance(llm_routing, str):
            try:
                return RoutingStrategy(llm_routing.strip().lower())
            except ValueError:
                logger.warning("[IntentRouter] LLM 返回未知路由 %r，降级到规则", llm_routing)
        elif isinstance(llm_routing, RoutingStrategy):
            return llm_routing

        return self._determine_routing_strategy(intent, complexity)

    def _determine_routing_strategy(
        self,
        intent: IntentCategory,
        complexity: ComplexityLevel
    ) -> RoutingStrategy:
        """【降级路径】按意图+复杂度决定路由策略（LLM 不可用时才走这里）。"""
        if intent in [IntentCategory.GREETING, IntentCategory.CHIT_CHAT]:
            return RoutingStrategy.DIRECT_ANSWER
        
        if intent in [IntentCategory.KNOWLEDGE_QUERY, IntentCategory.DOCUMENT_SEARCH]:
            return RoutingStrategy.RAG_RETRIEVAL
        
        if intent == IntentCategory.REPORT_GENERATION:
            return RoutingStrategy.REPORT_QUEUE
        
        if intent in [IntentCategory.COMPLEX_TASK, IntentCategory.MULTI_SPECIALIST]:
            return RoutingStrategy.MULTI_SPECIALIST_PARALLEL
        
        if complexity in [ComplexityLevel.LOW, ComplexityLevel.MEDIUM]:
            return RoutingStrategy.SINGLE_SPECIALIST
        
        if complexity in [ComplexityLevel.HIGH, ComplexityLevel.VERY_HIGH]:
            return RoutingStrategy.MULTI_SPECIALIST_PARALLEL
        
        return RoutingStrategy.SINGLE_SPECIALIST
    
    # LLM 可能返回中文或带后缀的写法，统一归一化到内部标识
    _SPECIALIST_ALIASES = {
        "finance": "finance", "financial": "finance", "财务": "finance",
        "财务会计": "finance", "finance_specialist": "finance",
        "tax": "tax", "税务": "tax", "tax_specialist": "tax",
        "legal": "legal", "law": "legal", "法律": "legal", "法务": "legal",
        "legal_specialist": "legal",
    }

    @classmethod
    def _normalize_specialists(cls, raw) -> Optional[List[str]]:
        """把 LLM 给的专家列表归一化；无法解析时返回 None（表示"没拿到有效判断"）。"""
        if raw is None:
            return None
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return None
        out: List[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            key = cls._SPECIALIST_ALIASES.get(item.strip().lower())
            if key and key not in out:
                out.append(key)
        return out

    def _determine_required_specialists(
        self,
        intent: IntentCategory,
        routing_strategy: RoutingStrategy,
        text: str = "",
        llm_specialists: Optional[List[str]] = None,
    ) -> List[str]:
        """确定需要哪些专家。

        ★ 判断权在 LLM，规则只做降级。

        判断"这段话涉及几个领域"是【语义理解】而非字符串匹配。曾经用
        _CROSS_DOMAIN_CUES 词表做这件事，实测反复失败：
          「…税务风险，合同有没有问题，财务健不健康」→ 逗号不在词表，漏判
          「…有税务隐患？顺便合同会不会有法律问题」 → "顺便"不在词表，漏判
          「分析一下我们公司的偿债能力」           → "偿债"不在领域词表，漏判
        每漏一次补一个词，永远补不完，且失败是静默的（只是少派专家，不报错）。
        改为直接问 LLM（分类调用本来就在跑，加字段零额外延迟），规则仅兜底。

        Args:
            intent: 意图
            routing_strategy: 路由策略
            text: 用户原始输入（降级路径与校验用）
            llm_specialists: LLM 给出的专家列表（None=未提供，走降级）
        """
        # ── 1. LLM 判断优先 ──
        if llm_specialists is not None:
            normalized = self._normalize_specialists(llm_specialists)
            if normalized is not None:
                # 校验：LLM 说单专家、而文本命中 3 个领域词时记一条 warning。
                # 只用于【观察 LLM 判断是否退化】，不用它改写路由 ——
                # 词表判错过太多次，不能反过来否决 LLM。
                if len(normalized) == 1 and text:
                    hits = [d for d, words in _DOMAIN_WORDS.items()
                            if any(w in text.lower() for w in words)]
                    if len(hits) >= 3:
                        logger.warning(
                            "[IntentRouter] 路由存疑：LLM 判单专家(%s)，但文本命中 %d 个领域词 %s | %s",
                            normalized[0], len(hits), hits, text[:50],
                        )
                logger.info("[IntentRouter] 专家由 LLM 判定: %s", normalized)
                return normalized

        # ── 2. 降级：LLM 不可用时的规则兜底 ──
        logger.info("[IntentRouter] 专家判定降级到规则（LLM 未提供判断）")
        intent_specialist_map = {
            IntentCategory.FINANCIAL_ANALYSIS: ["finance"],
            IntentCategory.ACCOUNTING_QUERY: ["finance"],
            IntentCategory.INVESTMENT_ADVISORY: ["finance"],
            IntentCategory.COST_CONTROL: ["finance"],
            IntentCategory.RISK_ANALYSIS: ["finance"],
            IntentCategory.TAX_CALCULATION: ["tax"],
            IntentCategory.TAX_PLANNING: ["tax"],
            IntentCategory.TAX_COMPLIANCE: ["tax"],
            IntentCategory.TAX_DECLARATION: ["tax"],
            IntentCategory.CONTRACT_REVIEW: ["legal"],
            IntentCategory.LEGAL_CONSULTATION: ["legal"],
            IntentCategory.COMPLIANCE_CHECK: ["finance", "tax", "legal"],
            IntentCategory.IP_PROTECTION: ["legal"],
        }

        specialists = intent_specialist_map.get(intent, [])

        # 显式跨领域请求（"涉及财务和法务"）优先于单领域意图判定 ——
        # 否则这类请求只会派单专家，多专家协作形同虚设。
        if text:
            cross = _detect_cross_domain(text.lower())
            if len(cross) >= 2:
                logger.info(f"[IntentRouter] 检测到跨领域请求，专家: {cross}")
                return cross

        if intent in [IntentCategory.COMPLEX_TASK, IntentCategory.MULTI_SPECIALIST]:
            specialists = ["finance", "tax", "legal"]

        # 注：这里原有一段 `if routing_strategy in [PARALLEL, SEQUENTIAL]` 的分支，
        #     但它永远不可达：COMPLEX_TASK/MULTI_SPECIALIST 已在上面被赋成三专家，
        #     而 _determine_routing_strategy 对其它领域意图只会返回
        #     SINGLE_SPECIALIST（有 specialists）或 MULTI_SPECIALIST_PARALLEL
        #     （其对应的 COMPLEX_TASK/MULTI_SPECIALIST 已被覆盖）。已删除。

        # 找不到对应专家时返回【空列表】，表示"没有专家"。不要用 ["general"] 当哨兵：
        # 它非空，会让 `if specialists:` 之类的判断误以为有专家，
        # 进而把纯知识检索也放行到专家分支（orchestrator 里已有一处 == ["general"] 的
        # 补丁，就是为了绕开这个哨兵）。
        if specialists:
            return specialists
        return []
    
    def _should_require_human_review(
        self,
        confidence: float,
        complexity: ComplexityLevel,
        intent: IntentCategory
    ) -> bool:
        """判断是否需要人工审核"""
        if confidence < self.confidence_threshold:
            return True
        
        if complexity == ComplexityLevel.VERY_HIGH:
            return True
        
        if intent == IntentCategory.UNKNOWN:
            return True
        
        return False
    
    def _classify_intent_rule_based(
        self,
        text: str
    ) -> Dict[str, Any]:
        """基于规则的意图分类"""
        text_lower = text.lower()
        
        report_keywords = [
            "生成报告", "生成一份报告", "输出一份报告",
            "给我一份报告", "给我报告", "需要报告",
            "生成分析报告", "生成财务报告", "生成税务报告",
        ]
        needs_report = any(kw in text_lower for kw in report_keywords)
        
        multi_patterns = [
            (["政策", "影响"], IntentCategory.KNOWLEDGE_QUERY, "政策影响分析"),
            (["政策", "优惠"], IntentCategory.KNOWLEDGE_QUERY, "政策优惠分析"),
            (["政策", "解读"], IntentCategory.KNOWLEDGE_QUERY, "政策解读"),
            (["政策", "咨询"], IntentCategory.KNOWLEDGE_QUERY, "政策咨询"),
            (["企业", "税务", "风险"], IntentCategory.TAX_COMPLIANCE, "企业税务风险"),
            (["税务", "筹划"], IntentCategory.TAX_PLANNING, "税务筹划"),
            (["税务", "合规"], IntentCategory.TAX_COMPLIANCE, "税务合规"),
            (["发票", "管理"], IntentCategory.TAX_DECLARATION, "发票管理"),
            (["发票", "风险"], IntentCategory.TAX_COMPLIANCE, "发票风险"),
            (["企业", "财务", "风险"], IntentCategory.RISK_ANALYSIS, "企业财务风险"),
            (["财务系统", "风险"], IntentCategory.RISK_ANALYSIS, "财务系统风险"),
            # 🆕 技能询问: 当"技能/能力"与领域关键词同时出现时路由到对应专家
            (["财务", "技能"], IntentCategory.FINANCIAL_ANALYSIS, "财务技能询问"),
            (["财务", "能力"], IntentCategory.FINANCIAL_ANALYSIS, "财务能力询问"),
            (["税务", "技能"], IntentCategory.TAX_CALCULATION, "税务技能询问"),
            (["税务", "能力"], IntentCategory.TAX_CALCULATION, "税务能力询问"),
            (["法律", "技能"], IntentCategory.LEGAL_CONSULTATION, "法律技能询问"),
            (["法律", "能力"], IntentCategory.LEGAL_CONSULTATION, "法律能力询问"),
            (["法务", "技能"], IntentCategory.LEGAL_CONSULTATION, "法务技能询问"),
            (["法务", "能力"], IntentCategory.LEGAL_CONSULTATION, "法务能力询问"),
        ]
        
        for keywords, intent, _ in multi_patterns:
            if all(kw in text_lower for kw in keywords):
                return {
                    "intent": intent,
                    "confidence": 0.9,
                    "reasoning": f"检测到关键词: {', '.join(keywords)}",
                    "needs_report_generation": needs_report
                }
        
        keyword_map = {
            "税务": IntentCategory.TAX_CALCULATION,
            "发票": IntentCategory.TAX_DECLARATION,
            "税": IntentCategory.TAX_CALCULATION,
            "财务": IntentCategory.FINANCIAL_ANALYSIS,
            "报表": IntentCategory.FINANCIAL_ANALYSIS,
            "利润": IntentCategory.FINANCIAL_ANALYSIS,
            "盈利": IntentCategory.FINANCIAL_ANALYSIS,
            "合同": IntentCategory.CONTRACT_REVIEW,
            "法律": IntentCategory.LEGAL_CONSULTATION,
            "法务": IntentCategory.LEGAL_CONSULTATION,
            "政策": IntentCategory.KNOWLEDGE_QUERY,
            "法规": IntentCategory.KNOWLEDGE_QUERY,
            "通知": IntentCategory.KNOWLEDGE_QUERY,
            "补贴": IntentCategory.KNOWLEDGE_QUERY,
            "优惠": IntentCategory.KNOWLEDGE_QUERY,
            "合规": IntentCategory.COMPLIANCE_CHECK,
            # 「报告」不在此处单字匹配 —— 见 _is_report_request()：
            # "报告"是话题词（「XX报告中的YY」是查询），只有出现在【动作句式】
            # （生成/输出/给我…报告）里才表示"要生成报告"。
            "查询": IntentCategory.KNOWLEDGE_QUERY,
            "知识库": IntentCategory.KNOWLEDGE_QUERY,
            # 🆕 技能关键词与领域关键词的多词组合已在 multi_patterns 中处理
        }
        
        # 报告类意图：仅在【动作句式】下成立 —— 见 _is_report_request。
        # 判定的形态是"动作 + 报告"，而非"出现'报告'二字"。这样
        # 「生成一份财务报告」→ report_generation ✅
        # 「XX报告中的开发支出是多少」→ 不命中，继续走领域词匹配 ✅
        if _is_report_request(text_lower):
            return {
                "intent": IntentCategory.REPORT_GENERATION,
                "confidence": 0.9,
                "reasoning": "检测到报告生成请求（动作句式）",
                "needs_report_generation": True,
            }

        for keyword, intent in keyword_map.items():
            if keyword in text_lower:
                return {
                    "intent": intent,
                    "confidence": 0.8,
                    "reasoning": f"检测到关键词: {keyword}",
                    "needs_report_generation": needs_report
                }
        
        return {
            "intent": IntentCategory.KNOWLEDGE_QUERY,
            "confidence": 0.5,
            "reasoning": "未能明确分类，归类为知识查询",
            "needs_report_generation": needs_report
        }
    
    async def _classify_intent_llm(
        self,
        text: str,
        entities: List[ExtractedEntity],
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """使用LLM进行意图分类"""
        entity_str = ", ".join([
            f"{e.entity_type}: {e.entity_value}"
            for e in entities[:5]
        ])
        
        classification_prompt_path = Path(__file__).parent.parent.parent / "prompts" / "system" / "intent_classification_prompt.md"
        
        if classification_prompt_path.exists():
            try:
                with open(classification_prompt_path, 'r', encoding='utf-8') as f:
                    prompt_template = f.read()
            except Exception:
                prompt_template = None
        else:
            prompt_template = None
        
        if not prompt_template:
            prompt_template = """分析以下用户输入的意图：

用户输入：{user_input}

已识别的实体：{entities}

请返回JSON格式的意图分析：
{{
  "intent": "意图类别",
  "sub_intent": "子意图（可选）",
  "requires_specialists": ["finance"],
  "routing_strategy": "rag_retrieval",
  "confidence": 0.85,
  "params": {{"建议参数"}},
  "reasoning": "推理过程",
  "needs_clarification": false,
  "clarification_question": "",
  "clarification_options": []
}}

注意：
1. 如果是问候语，返回 intent: "greeting"
2. 如果是闲聊，返回 intent: "chit_chat"
3. 如果用户要求生成报告，设置 needs_report_generation: true

★ 关于 intent 的"查询 vs 分析"（重要，容易混）：
   **查询**：问一个【已有的事实/数值】，答案能从文档或数据里"找出来"。
       多为 "是多少 / 是什么 / 有哪些 / 在哪里"
       → knowledge_query, document_search
      例：「比亚迪2026年第一季度报告中的开发支出是多少」→ knowledge_query
          「万科2025年年报的营业收入是多少」       → knowledge_query
          「大湖股份和业绩补偿方是什么关系」       → knowledge_query

   **分析**：要求【判断、评估、比较、给建议】，答案必须"做出来"而不只是"找出来"。
       多为 "分析 / 评估 / 怎么样 / 有没有问题 / 该如何 / 给我建议"
       → financial_analysis, risk_analysis, tax_* , contract_review, legal_consultation …
      例：「分析一下我们公司的偿债能力」   → financial_analysis（不是 knowledge_query）
          「我们公司的资产负债率是多少」   → financial_analysis（需要取数计算，不是查文档）
          「帮我看看合同里有什么法律风险」 → contract_review

   判别口诀：**"找答案" 还是 "做判断"**。前者 knowledge_query，后者归到对应领域。

★ 关于 requires_specialists（重要）：
   判断这个问题需要哪几位领域专家来回答。
   可选值只有三个："finance"（财务）、"tax"（税务）、"legal"（法律）。

   判据：**用户是否提出了分属不同领域的多个诉求**。
   连接方式无关紧要 —— 逗号、顿号、"和"、"顺便"、"另外"都一样，
   看的是【诉求的领域归属】而不是【出现了什么连接词】。

   ✅ 需要多专家（用户提了多个领域的诉求）：
      「我们公司有什么税务风险，合同有没有问题，财务健不健康」
        → ["finance", "tax", "legal"]   三个诉求分属三个领域
      「账面利润不错，但是不是有税务隐患？顺便合同条款会不会有法律问题」
        → ["tax", "legal"]              "顺便"引出的是另一个领域
      「结合财务数据和最新税收政策给出建议」
        → ["finance", "tax"]

   ❌ 只需单专家（即使出现了多个领域词）：
      「财务和法务的区别是什么」
        → ["finance"]  这是【对比/解释】题，是对两个概念做比较，
                        不是"既要财务分析又要法务分析"
      「税务筹划和税务合规有什么不同」
        → ["tax"]      同上，对比题
      「分析一下我们公司的偿债能力」
        → ["finance"]  只有一个诉求

   ⚠️ 判断口诀：
      问的是"它们之间有什么不同/关系" → 对比题，单专家
      问的是"帮我把这几件事都办了"   → 多专家

4. 无法归入任何领域（如纯粹的常识问答、知识库事实查询）→ 返回空数组 []

★ 关于 routing_strategy（重要）：
   决定这条问题【由谁作答】，取值只能是下列之一：
     "direct_answer"  —— 问候/闲聊，不检索不派专家
     "rag_retrieval"  —— 从知识库【找事实】作答，不派专家
     "single_specialist"         —— 交给【一位】领域专家
     "multi_specialist_parallel" —— 交给【多位】专家（与 requires_specialists 对应）
     "report_queue"   —— 用户要求生成报告

   ⚠️ 最容易搞混的是 rag_retrieval 与 single_specialist ——
      **两者可以是同一个领域**。真正的判别不是"问得多简单"，
      而是【答案在哪】：

      · 答案能在【企业知识库的文档】里读到
        （年报里的数字、公告里的关系、合同里的条款原文）
        → rag_retrieval

        「比亚迪2026年第一季度报告中的开发支出是多少」
          → rag_retrieval + ["finance"]    数字写在年报里，检索即可
        「万科2025年年报的营业收入是多少」
          → rag_retrieval + ["finance"]    同上
        「大湖股份和业绩补偿方是什么关系」
          → rag_retrieval + []             关系写在公告里

      · 需要【专业判断/计算】，或答案属于该领域的【通用专业知识】
        （法规条文、税率表、折旧方法、合同风险要点 —— 这些不在企业文档里，
          而在专家的知识里）
        → single_specialist

        「个人所得税的税率是多少」
          → single_specialist + ["tax"]
            税率表是法定知识，不在企业年报里；税务专家能直接给出完整税率表
        「分析一下我们公司的偿债能力」
          → single_specialist + ["finance"]   要评估判断，还要取企业数据计算
        「财务和法务的区别是什么」
          → single_specialist + ["finance"]   概念解释，靠专业知识
        「帮我看看合同里有什么法律风险」
          → single_specialist + ["legal"]     要专业审查判断

   ⚠️ 判断口诀：
      **答案能在企业文档里"读到" → rag_retrieval；
        要靠"专业判断/通用专业知识" → 派专家。**
      注意「是多少」并不意味着一定走 RAG —— 若该事实企业文档里没有，
      而属于通用专业知识（如税率、法规），仍应派专家。

   注意 requires_specialists 与 routing_strategy 要自洽：
     · rag_retrieval 时 specialists 表示"这条内容属于哪个领域"（可给可空），
       但**不会真的调用专家**，最终由知识库检索直接作答
     · single/multi_specialist 时 specialists 必须非空，且多专家时用 multi_*

★ 关于 confidence：
   你对这个判断的把握，0.0~1.0。字段明确、单一领域且无歧义时给 0.85 以上；
   需要推断、边界模糊时给低一些。不要一律给 0.5 —— 低于阈值的会被规则层覆盖。

★ 关于 needs_clarification（重要）：
   判断"这个问题是否【信息不足到无法作答】，必须先问用户"。
   只在【平行候选无法用常识区分】时才设为 true ——
   即：你不确定用户指哪一个，且猜错的代价是"答非所问"。

   必须设为 true 的情况（举例）：
     「税率是多少」→ 哪个税种？企业所得税/增值税/个人所得税…无法区分 → true
        clarification_question: "您想了解哪个税种的税率？"
        clarification_options: ["企业所得税的税率是多少", "增值税的税率是多少", "个人所得税的税率是多少"]
     「折旧怎么算」→ 哪种资产的折旧？方法也不同 → true

   必须设为 false 的情况（举例）：
     「企业所得税税率是多少」→ 税种已明确 → false
     「比亚迪开发支出是多少」→ 主体+指标明确 → false
     「我们公司上月开了发票，怎么申报增值税」→ 信息足够 → false
     「你好」→ 问候语 → false

   判断原则（不要猜）：
   · 有明确主体/对象（公司名、税种、指标、日期）→ 通常 false，直接答
   · 只有"泛泛的类别词"（税率、费用、成本、风险）且没有限定 → 通常 true
   · 若能从常识推断出最可能的答案 → false（不要为小概率歧义打断用户）
   · 不确定时倾向于 false（先答，答偏了用户会补充）—— 追问只用于【明显无法作答】

   question 要求：具体、指出歧义点（如"您想了解哪个税种？"），
                  不要写"请详细描述您的问题"这类空泛的话。
   options 要求（重要）：
     2-4 个【完整、可直接作答的问题】—— 用户点击后系统会【把该选项原样作为
     新查询发送】，所以选项必须自身信息完整，不能只是"话题词"。
       ❌ 反例（信息不足，点了也答不好）: ["个人所得税", "增值税"]
       ✅ 正例（可直接作答）: ["个人所得税的税率是多少", "增值税的税率是多少"]
     每个选项都应补全原问题缺失的那部分信息，使其成为一个可独立回答的问题。"""
        
        text_lower = text.lower()
        report_keywords = [
            "生成报告", "生成一份报告", "输出一份报告",
            "给我一份报告", "给我报告", "需要报告",
            "生成分析报告", "生成财务报告", "生成税务报告",
            "生成分析文档", "生成分析材料", "请生成报告"
        ]
        needs_report = any(kw in text_lower for kw in report_keywords)
        
        prompt = prompt_template.format(
            user_input=text,
            entities=entity_str or "无"
        )
        
        try:
            response = await self.llm_adapter.agenerate(
                prompts=[prompt],
                temperature=0.1,
                max_tokens=500
            )
            
            result_text = response.content.strip()
            
            if result_text.startswith("```"):
                lines = result_text.split("\n")
                result_text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
            
            result = json.loads(result_text)
            
            if "intent" in result and isinstance(result["intent"], str):
                try:
                    result["intent"] = IntentCategory(result["intent"])
                except ValueError:
                    result["intent"] = IntentCategory.UNKNOWN
                
                if result.get("sub_intent"):
                    try:
                        result["sub_intent"] = IntentCategory(result["sub_intent"])
                    except ValueError:
                        result["sub_intent"] = None
            
            confidence = result.get("confidence", 0.0)
            detected_intent = result.get("intent", IntentCategory.UNKNOWN)
            
            generic_intents = {
                IntentCategory.UNKNOWN,
                IntentCategory.MULTI_SPECIALIST,
                IntentCategory.COMPLEX_TASK,
                IntentCategory.KNOWLEDGE_QUERY
            }
            
            rule_result = self._classify_intent_rule_based(text)
            rule_confidence = rule_result.get("confidence", 0.0)
            
            should_use_rule = False
            # ① LLM 判"不知道"（UNKNOWN）时，规则层的任何结果都比 unknown 有信息 ——
            #    不再要求 rule_confidence >= 0.8。原实现要求高置信度，导致
            #    LLM=unknown(0.95) + 规则=knowledge_query(0.5) 时兜底失效，
            #    unknown 被采纳 → requires_specialists 为空 → 触发"多专家兜底"
            #    派出全部三个专家（过度派发）。
            #    注意：这是"信息量不对称"—— unknown 是"无信息"，规则结果至少是"有倾向"。
            if detected_intent == IntentCategory.UNKNOWN and rule_result.get("intent") != IntentCategory.UNKNOWN:
                should_use_rule = True
                print("⚠️ [意图路由智能体] LLM判unknown，改用规则结果")
            # ② LLM 判了通用意图且规则高置信 → 规则覆盖
            elif detected_intent in generic_intents and rule_confidence >= 0.8:
                should_use_rule = True
                print("⚠️ [意图路由智能体] LLM返回通用意图，使用规则匹配补充")
            # ③ LLM 自身没把握 → 规则更可信则用规则
            elif confidence <= self.confidence_threshold and rule_confidence > confidence:
                should_use_rule = True
                print(f"⚠️ [意图路由智能体] 置信度({confidence})<=阈值({self.confidence_threshold})，使用规则匹配补充")
            
            if should_use_rule:
                # 只替换【意图/置信度】等规则更可信的字段，
                # 但保留 LLM 的【澄清判断】与【专家判断】—— 它们都是语义判断，
                # 规则层给不出（原实现整个 result = rule_result，把这些一起丢了）：
                #   · clarification_*：LLM 判出的"需澄清"会失效
                #   · requires_specialists：LLM 判出的"需要哪几位专家"会失效，
                #     退回硬编码的意图→专家表，跨领域请求被降级成单专家
                _preserve = {
                    "needs_clarification": result.get("needs_clarification"),
                    "clarification_question": result.get("clarification_question"),
                    "clarification_options": result.get("clarification_options"),
                    "requires_specialists": result.get("requires_specialists"),
                    "routing_strategy": result.get("routing_strategy"),
                }
                result = rule_result
                for _k, _v in _preserve.items():
                    if _v not in (None, ""):
                        result[_k] = _v
            
            result["needs_report_generation"] = needs_report
            
            return result
            
        except json.JSONDecodeError as e:
            print(f"⚠️ [意图路由智能体] LLM返回格式错误，使用规则匹配: {e}")
            return self._classify_intent_rule_based(text)
        except Exception as e:
            print(f"⚠️ [意图路由智能体] LLM分类失败，使用规则匹配: {e}")
            return self._classify_intent_rule_based(text)
    
    async def run(
        self,
        user_input: str,
        history: List[Dict] = None,
        context: Dict[str, Any] = None,
        **kwargs
    ) -> IntentRoutingResult:
        """
        执行意图路由主流程
        
        步骤：
        1. 快速检测简单问题（正则，不调用LLM）
        2. 如果不是简单问题，调用LLM进行意图分类
        3. 实体提取 + 复杂度评估 + 路由决策
        
        Args:
            user_input: 用户输入
            history: 对话历史
            context: 上下文信息
            **kwargs: 其他参数
            
        Returns:
            IntentRoutingResult: 统一的路由结果
        """
        print(f"🎯 [意图路由智能体] 处理输入: {user_input[:50]}...")
        
        simple_response = self._is_simple_greeting(user_input)
        if simple_response:
            print("✅ [意图路由智能体] 简单问题，直接返回")
            return IntentRoutingResult(
                is_simple=True,
                simple_response=simple_response,
                intent_result=None
            )
        
        entities = self._extract_entities(user_input)
        
        rule_based_result = self._classify_intent_rule_based(user_input)
        if rule_based_result["confidence"] >= 0.9:
            print(f"✅ [意图路由智能体] 规则匹配命中，跳过LLM: {rule_based_result['intent'].value}")
            intent_result_dict = rule_based_result
        else:
            intent_result_dict = await self._classify_intent_llm(user_input, entities, context or {})
        
        complexity = self._assess_complexity(
            user_input,
            intent_result_dict["intent"],
            entities
        )
        
        routing_strategy = self._resolve_routing_strategy(
            intent_result_dict.get("routing_strategy"),
            intent_result_dict["intent"],
            complexity,
        )

        specialists = self._determine_required_specialists(
            intent_result_dict["intent"],
            routing_strategy,
            text=user_input,
            llm_specialists=intent_result_dict.get("requires_specialists"),
        )

        # ── 最后校验：路由与专家自洽 ──
        # 正常情况 LLM 已给出自洽的一组（判别口诀见 prompt）。
        # ⚠️ 这里【不要】因为"有专家"就把 rag_retrieval 升格成单专家 ——
        #    rag_retrieval 时 specialists 只是"这条内容属于哪个领域"的标注
        #    （「比亚迪开发支出是多少」→ rag + ["finance"]），并不会真的调用专家。
        #    曾经加过这条升格规则，反而把 LLM 的正确判断改坏了。
        # 只兜两类真正的不一致：
        if routing_strategy in (RoutingStrategy.SINGLE_SPECIALIST,
                                RoutingStrategy.MULTI_SPECIALIST_PARALLEL) and not specialists:
            logger.info("[IntentRouter] 路由=%s 但无专家，退为知识库检索", routing_strategy)
            routing_strategy = RoutingStrategy.RAG_RETRIEVAL
        elif len(specialists) > 1 and routing_strategy == RoutingStrategy.SINGLE_SPECIALIST:
            logger.info(
                "[IntentRouter] 专家数(%d)与路由=单专家不一致，修正为多专家并行",
                len(specialists),
            )
            routing_strategy = RoutingStrategy.MULTI_SPECIALIST_PARALLEL

        needs_review = self._should_require_human_review(
            intent_result_dict["confidence"],
            complexity,
            intent_result_dict["intent"]
        )
        
        intent_result = IntentAnalysisResult(
            intent=intent_result_dict["intent"],
            sub_intent=intent_result_dict.get("sub_intent"),
            entities=entities,
            complexity=complexity,
            requires_specialists=specialists,
            routing_strategy=routing_strategy,
            suggested_params=intent_result_dict.get("params", {}),
            confidence=intent_result_dict["confidence"],
            needs_human_review=needs_review,
            reasoning=intent_result_dict.get("reasoning", ""),
            needs_report_generation=intent_result_dict.get("needs_report_generation", False)
        )
        
        print("✅ [意图路由智能体] 分析完成")
        print(f"   意图: {intent_result.intent.value}")
        print(f"   复杂度: {intent_result.complexity.value}")
        print(f"   路由: {intent_result.routing_strategy.value}")
        print(f"   置信度: {intent_result.confidence:.2f}")
        
        intent_str = (
            intent_result.intent.value 
            if hasattr(intent_result.intent, 'value') 
            else str(intent_result.intent)
        )
        routing_str = (
            intent_result.routing_strategy.value
            if hasattr(intent_result.routing_strategy, 'value')
            else str(intent_result.routing_strategy)
        )
        
        # ── 澄清判断：优先用 LLM 的判断（Claude Code 式）──
        # 为什么不用启发式规则：本项目三次尝试（单字判意图 / 置信度当澄清依据 /
        # 实体锚点）都因"启发式判断不了语义"而失败。LLM 判意图时已在同一 prompt
        # 里被要求判断"是否信息不足"，直接采用其结果，不额外调用。
        # 启发式（clarification_service）降级为兜底 —— 仅当 LLM 未给出判断时使用
        # （如走了规则分类分支、或 LLM 输出解析失败）。
        clarification = None
        _llm_needs_clar = intent_result_dict.get("needs_clarification")
        if _llm_needs_clar:
            _question = (intent_result_dict.get("clarification_question") or "").strip()
            _options = intent_result_dict.get("clarification_options") or []
            if _question and isinstance(_options, list):
                clarification = ClarificationRequest(
                    type=ClarificationType.INTENT_CLARIFICATION,
                    question=_question,
                    suggestions=[str(o) for o in _options][:4],
                    reason="意图存在多个平行候选，需用户明确",
                    required=True,
                    placeholder="也可直接输入您的具体问题...",
                )
                logger.info(f"[IntentRouter] LLM 判定需澄清: {_question[:40]}")
        elif _llm_needs_clar is None:
            # LLM 未参与判断（规则分支）→ 退回启发式兜底
            clarification = await self.clarification_service.detect_ambiguous_input(
                query=user_input,
                intent=intent_str,
                confidence=intent_result.confidence,
                entities=[e.model_dump() for e in intent_result.entities],
                routing_strategy=routing_str
            )
        # _llm_needs_clar is False → 明确不需要澄清，不再走启发式（避免规则误判覆盖 LLM 判断）
        
        return IntentRoutingResult(
            is_simple=False,
            simple_response=None,
            intent_result=intent_result,
            clarification_request=clarification
        )
    
    async def stream_run(self, user_input: str, history: List[Dict] = None, **kwargs):
        """流式执行（暂不支持，返回完整结果）"""
        result = await self.run(user_input, history, **kwargs)
        yield result
