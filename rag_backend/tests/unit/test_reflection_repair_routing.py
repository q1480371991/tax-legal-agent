import pytest

pytest.importorskip("pydantic_settings")

from app.multi_agent_system.agents.tax_specialist import TaxEntity, TaxSpecialist
from app.multi_agent_system.orchestrator import AgentOrchestrator


def test_tax_guidance_skips_enterprise_data_query():
    should_query = TaxSpecialist._should_query_enterprise_data(
        None,
        "税务申报指导",
        TaxEntity(tax_type="other"),
        {},
    )

    assert should_query is False


def test_company_tax_calculation_queries_enterprise_data():
    should_query = TaxSpecialist._should_query_enterprise_data(
        None,
        "我司今年企业所得税测算",
        TaxEntity(tax_type="income_tax"),
        {},
    )

    assert should_query is True


def test_reflection_repair_target_uses_domain_specialist():
    """打回重做必须落到【实际跑过】的领域专家，而不是永远 finance。"""
    orch = AgentOrchestrator(tenant_id="tenant", user_id="user")

    plan = orch._build_repair_plan(
        specialist_results=[{"specialist_type": "tax", "source": "tax"}],
        fallback_target="tax_specialist",
    )

    assert plan["target"] == "tax_specialist"
    assert plan["targets"] == ["tax_specialist"]


def test_reflection_repair_plan_falls_back_when_specialist_unknown():
    orch = AgentOrchestrator(tenant_id="tenant", user_id="user")

    plan = orch._build_repair_plan(
        specialist_results=[{"source": "unknown_source"}],
        fallback_target="finance_specialist",
    )

    assert plan["targets"] == ["finance_specialist"]


def test_retry_query_injects_review_issues_only_when_retrying():
    """重做时把审查意见拼进 query —— 否则专家等于把同一问题原样再答一遍。"""
    from app.multi_agent_system.orchestrator import _build_retry_query

    base = {"user_query": "个人所得税的税率是多少", "retry_count": 0}
    assert _build_retry_query(base) == "个人所得税的税率是多少"

    retrying = {
        "user_query": "个人所得税的税率是多少",
        "retry_count": 1,
        "reflection_result": {
            "score": 0.42,
            "acceptable": False,
            "issues": [
                {"dimension": "completeness", "description": "未覆盖经营所得税率",
                 "suggestion": "补充五级超额累进表"},
                {"dimension": "accuracy", "description": "速算扣除数有误"},
            ],
        },
    }
    out = _build_retry_query(retrying)
    assert "个人所得税的税率是多少" in out          # 原始问题仍在
    assert "完整性：未覆盖经营所得税率" in out       # 维度已中文化
    assert "改进建议：补充五级超额累进表" in out
    assert "准确性：速算扣除数有误" in out
    assert "0.42" in out


def test_retry_query_falls_back_when_no_issues():
    """issues 为空时退回原始问题，不能产生空壳提示。"""
    from app.multi_agent_system.orchestrator import _build_retry_query

    assert _build_retry_query({
        "user_query": "Q", "retry_count": 1, "reflection_result": {"score": 0.4, "issues": []},
    }) == "Q"


def test_has_db_data_accepts_every_specialist_flag():
    """数据来源标记各专家字段名不同 —— 反思快速通道必须全都认。

    历史 bug：只检查 has_financial_db_data，而该字段只有 finance 专家会设置，
    导致税务/法律专家永远拿不到快速通道，每次白跑一遍 LLM 审查。
    """
    from app.multi_agent_system.orchestrator import _has_db_data

    assert _has_db_data({"has_financial_db_data": True}) is True
    assert _has_db_data({"has_tax_db_data": True}) is True
    assert _has_db_data({"has_legal_db_data": True}) is True
    assert _has_db_data({"has_db_data": True}) is True
    # 也可能嵌在 data 里
    assert _has_db_data({"data": {"has_tax_db_data": True}}) is True
    # 都没有标记
    assert _has_db_data({"confidence": 1.0}) is False
    assert _has_db_data(None) is False
