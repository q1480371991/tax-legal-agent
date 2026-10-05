"""多智能体意图判定与澄清的不变量测试（需要真实 LLM）。

为什么需要
──────────
多 agent 链路的两个判据缺陷，都属于"不崩溃、日志干净、只是行为不对"：

  ① **过度澄清**：`clarification_service._check_input_clarity` 把
     `intent == "complex_task"` 当成"模糊输入"而反问用户。
     但 COMPLEX_TASK 的语义是"任务复杂"（该派多专家），不是"用户没说清"
     —— 结果简单事实查询（如"比亚迪…开发支出是多少"）被要求澄清。

  ② **多专家漏判**：`intent_router_agent._determine_required_specialists`
     只按 intent 枚举决定派几个专家，没用查询里的**显式跨领域信号**
     （如"涉及财务和法务"）。除非 LLM 恰好判成 COMPLEX_TASK，
     否则只派单专家 —— 跨领域请求得不到多专家协作。

运行（需要 LLM 可用）：
    docker exec rag_backend python3 -m pytest tests/integration/test_intent_invariants.py -v
"""

import pytest


@pytest.fixture(scope="module")
async def intent_agent():
    """构造真实的 IntentRouterAgent。

    它需要 llm_adapter + tool_manager —— 这些由 AgentOrchestrator 在 initialize()
    里组装好，故直接取编排器的实例，避免重复搭建依赖。
    """
    from app.multi_agent_system import AgentOrchestrator
    try:
        o = AgentOrchestrator(tenant_id="invariant-test", user_id="invariant-test")
        await o.initialize()
        agent = getattr(o, "intent_router", None)
        if agent is None:
            pytest.skip("编排器未暴露 intent_router，跳过")
        return agent
    except Exception as e:
        pytest.skip(f"无法初始化编排器（需要 LLM 可用）: {e}")


# ══════════════════════════════════════════════════════════════
# 不变量 1：明确的简单事实查询，不应触发"澄清追问"
# ══════════════════════════════════════════════════════════════
@pytest.mark.integration
@pytest.mark.asyncio
async def test_clear_factual_query_not_clarified(intent_agent):
    """信息完整的简单事实查询不该被反问「请明确问题范围」。

    查询要素齐全（主体 + 时间 + 指标），用户已表达清楚，
    反问只会损害体验。这条能拦住"complex_task 被误当模糊输入"的缺陷。
    """
    q = "比亚迪2026年第一季度报告中的开发支出是多少"
    result = await intent_agent.run(q)

    intent = None
    confidence = 1.0
    if result.intent_result:
        intent = result.intent_result.intent.value
        confidence = result.intent_result.confidence

    req = await intent_agent.clarification_service.detect_ambiguous_input(
        query=q, intent=intent or "unknown", confidence=confidence,
        entities=[], routing_strategy=None,
    )
    assert req is None, (
        f"「{q}」要素齐全却被要求澄清：{req.question if req else ''}"
        f"（intent={intent}, confidence={confidence}）。"
        f"注意 COMPLEX_TASK 表示「任务复杂」而非「用户没说清」，不应触发澄清。"
    )


# ══════════════════════════════════════════════════════════════
# 不变量 2：显式跨领域请求，应派多专家
# ══════════════════════════════════════════════════════════════
@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_domain_request_routes_multi_specialist(intent_agent):
    """查询里显式提到 ≥2 个领域时，应派多个专家。

    如「分析…风险，涉及财务和法务」—— 用户已明确要求跨领域，
    应触发多专家协作，而不是只派一个。
    """
    q = "分析大湖股份股权转让的风险，涉及财务和法务两个方面"
    result = await intent_agent.run(q)
    assert result.intent_result is not None
    specialists = result.intent_result.requires_specialists
    assert len(specialists) >= 2, (
        f"「{q}」显式要求财务+法务，却只派了 {specialists}。"
        f"（intent={result.intent_result.intent.value}, "
        f"strategy={result.intent_result.routing_strategy.value}）"
    )


# ══════════════════════════════════════════════════════════════
# 不变量 3：单一的领域问题，不应过度派专家
# ══════════════════════════════════════════════════════════════
@pytest.mark.integration
@pytest.mark.asyncio
async def test_single_domain_stays_single_specialist(intent_agent):
    """只涉及一个领域的问题，不该派全部三个专家（避免无谓开销）。"""
    q = "企业所得税的税率是多少"
    result = await intent_agent.run(q)
    assert result.intent_result is not None
    specialists = result.intent_result.requires_specialists
    assert len(specialists) <= 2, (
        f"「{q}」只涉及税务，却派了 {specialists}（过度派发）"
    )
