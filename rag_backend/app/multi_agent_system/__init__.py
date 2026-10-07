"""
多智能体系统模块

导出核心类和组件
"""

from .state import (
    AuditState,
    AuditType,
    RiskLevel,
    Finding,
    Conflict,
    Report,
    create_initial_state,
    TaskStatus,
    TaskPriority,
    AgentRole,
    TaskContext,
    AgentRegistry,
    BlackboardEvent
)

from .coordinator import AgentCoordinator
from .orchestrator import AgentOrchestrator, OrchestrationContext

# 为了向后兼容，添加别名
MultiAgentCoordinator = AgentCoordinator
from .message_bus import MessageBus, MessageType, AgentMessage
from .task_decomposer import TaskDecomposer, DocumentType, AuditPriority
from .result_merger import ResultMerger

# Agent 相关
from .agents.base_specialist import BaseSpecialistAgent
from .agents.intent_router_agent import (
    IntentRouterAgent,
    IntentRoutingResult,
    IntentCategory,
    ComplexityLevel,
    RoutingStrategy,
    IntentAnalysisResult
)

__all__ = [
    # 状态管理
    "AuditState",
    "AuditType", 
    "RiskLevel",
    "Finding",
    "Conflict",
    "Report",
    "create_initial_state",
    
    # 任务管理（整合自 task_blackboard.py）
    "TaskStatus",
    "TaskPriority",
    "AgentRole",
    "TaskContext",
    "AgentRegistry",
    "BlackboardEvent",
    
    # 核心组件
    # 注：原 AgentRouter / RouteDecision / RouteMode / RoutingRule 已移除 ——
    #     它所在的 agent_router.py 从未被实例化，是"黑板规则路由"那套的遗留；
    #     实际路由由 intent_router_agent + routing/unified_router 承担。
    #     （另注意 app/services/smart_router.py 里有一个同名的 RouteMode，与本处无关）
    "AgentCoordinator",
    "AgentOrchestrator",
    "OrchestrationContext",
    "MultiAgentCoordinator",  # 别名
    "MessageBus",
    "MessageType",
    "AgentMessage",
    "TaskDecomposer",
    "DocumentType",
    "AuditPriority",
    "ResultMerger",
    
    # Agent 基类
    "BaseSpecialistAgent",
    "IntentRouterAgent",
    "IntentRoutingResult",
    "IntentCategory",
    "ComplexityLevel",
    "RoutingStrategy",
    "IntentAnalysisResult"
]