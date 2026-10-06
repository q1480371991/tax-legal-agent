import pytest

pytest.importorskip("pydantic_settings")

from app.agent_framework.components.result_synthesizer import ResultSynthesizer
from app.multi_agent_system.agents.finance_specialist import (
    FinanceSpecialist,
    FinancialAnalysisResult,
    FinancialDomain,
    FinancialEntity,
    FinancialQueryResult,
)
from app.services.agent_tracer import _coerce_text


def test_agent_tracer_coerces_structured_tool_output_to_text():
    assert _coerce_text({"intent": "tax_calculation", "confidence": 0.9}) == (
        '{"intent": "tax_calculation", "confidence": 0.9}'
    )


def test_result_synthesizer_merge_preserves_string_details():
    synthesizer = ResultSynthesizer(llm_adapter=None)

    response = synthesizer._generate_merge_response({
        "summary": {},
        "details": {"tax": {"content": "企业所得税分析明细"}}
    })

    assert "企业所得税分析明细" in response


def test_result_synthesizer_prefers_public_report_over_internal_fields():
    synthesizer = ResultSynthesizer(llm_adapter=None)

    response = synthesizer._generate_merge_response({
        "summary": {
            "success": [{"value": True, "source": "tax", "confidence": 0.8}],
            "has_tax_db_data": [{"value": True, "source": "tax", "confidence": 0.8}],
            "tax_data": [{"value": {"total_revenue": 910965492.0}, "source": "tax", "confidence": 0.8}],
            "tax_type": [{"value": "TaxType.OTHER", "source": "tax", "confidence": 0.8}],
        },
        "details": {
            "tax": {
                "success": True,
                "has_tax_db_data": True,
                "tax_data": {"total_revenue": 910965492.0},
                "analysis_report": "# 税务分析报告\n\n建议进行详细的合规性审查。",
            }
        },
    })

    assert "税务分析报告" in response
    assert "建议进行详细的合规性审查" in response
    assert "has_tax_db_data" not in response
    assert "tax_data" not in response
    assert "TaxType.OTHER" not in response


def test_finance_specialist_generates_public_report_from_db_summary():
    analysis = FinancialAnalysisResult(
        domain=FinancialDomain.FINANCIAL_STATEMENT,
        financial_indicators={},
        key_metrics=[],
        risk_factors=["资金链风险"],
        recommendations=["建立财务预警机制"],
        confidence=0.9,
    )

    report = FinanceSpecialist._generate_analysis_report(
        None,
        user_input="分析企业财务风险",
        domain=FinancialDomain.FINANCIAL_STATEMENT,
        analysis=analysis,
        risk_assessment={"risk_level": "high", "risk_factors": ["资金链风险"]},
        recommendations=["建立财务预警机制"],
        data_summary={
            "total_revenue": 910965492.0,
            "total_expenses": 595054447.0,
            "total_profit": 315911045.0,
            "avg_profit_margin": 34.48,
            "fiscal_years": [2024, 2025],
        },
        has_financial_data=True,
    )

    assert "企业财务风险分析报告" in report
    assert "营业收入" in report
    assert "910,965,492.00 元" in report
    assert "核心指标" in report
    assert "风险判断" in report
    assert "数据来源与限制" in report
    assert "domain:" not in report


def test_finance_timeout_fallback_keeps_db_data_public_report():
    result = FinanceSpecialist._build_timeout_fallback_result(
        None,
        user_input="分析企业财务风险",
        domain=FinancialDomain.FINANCIAL_STATEMENT,
        entities=FinancialEntity(),
        query_result=FinancialQueryResult(
            has_data=True,
            data_summary={
                "total_revenue": 910965492.0,
                "total_expenses": 595054447.0,
                "total_profit": 315911045.0,
                "avg_profit_margin": 34.48,
                "fiscal_years": [2024, 2025],
            },
        ),
        rag_enabled=True,
        error_message="LLM 调用超时，已改用企业数据生成兜底分析",
    )

    assert result["success"] is True
    assert result["degraded"] is True
    assert result["has_financial_db_data"] is True
    assert "analysis_report" in result
    assert "910,965,492.00" in result["analysis_report"]
    assert "未知领域" not in result["analysis_report"]


# =========================================================================
# sanitize / 格式化契约 —— 防"最后一公里把好答案砍掉"
#
# 真实事故：问「个人所得税的税率是多少」，税务专家产出了 1855 字符、
# 含七级税率表的完整回答；但 `_sanitize_llm_text` 用【第一个】``` 代码块
# 的内容替换了整篇回答（该回答里有一个计算公式代码块），最终只剩 81 字符。
# 反思节点拿这 81 字符去评审 → 0.16 分 → 判定不合格 → 连续打回重做 3 轮
# → 用户侧表现为"点完很久没反应，手动中断才出结果"。
#
# 这里锁死三条契约：
#   ① 正文中的代码块不得吞掉整篇回答
#   ② 整篇被 ``` 或 JSON 包裹时仍要能解出正文
#   ③ 纯文本必须原样透传（专家输出的主体是自然语言，不是 JSON）
# =========================================================================

from app.multi_agent_system.orchestrator import AgentOrchestrator, Nodes


_TAX_ANSWER = """# 个人所得税税率详解

我国个人所得税按所得类型适用不同税率。

```
应纳税额 = 应纳税所得额 x 适用税率 - 速算扣除数
```

## 一、综合所得（3%~45% 七级超额累进）

| 级数 | 全年应纳税所得额 | 税率 |
|:---:|:---|:---:|
| 1 | 不超过 36,000 元 | 3% |
| 2 | 36,000 ~ 144,000 元 | 10% |

## 二、经营所得（5%~35% 五级超额累进）

## 三、利息股息红利等适用 20% 比例税率
"""


def test_sanitize_keeps_full_answer_when_code_block_embedded():
    """正文里出现代码块时，整篇回答必须保留，不能只剩代码块内容。"""
    out = Nodes._sanitize_llm_text(_TAX_ANSWER)

    # 代码块之外的正文必须还在
    assert "个人所得税税率详解" in out
    assert "综合所得" in out
    assert "经营所得" in out
    assert "20% 比例税率" in out
    # 长度不能塌缩到"只剩一个代码块"
    assert len(out) > len(_TAX_ANSWER) * 0.9


def test_sanitize_unwraps_whole_answer_fence():
    """整篇被 ``` 包裹时，解包后应保留全部正文（正文自身的代码块保留）。"""
    wrapped = "```\n" + _TAX_ANSWER + "\n```"
    out = Nodes._sanitize_llm_text(wrapped)

    assert "个人所得税税率详解" in out
    assert "综合所得" in out
    # 最外层围栏应已去除
    assert not out.startswith("```")
    assert not out.endswith("```")


def test_sanitize_extracts_body_from_json_wrapped_payload():
    """整篇是 JSON 时，仍应提取出正文而非原样抛给用户。"""
    payload = (
        '{"executive_summary": "企业所得税税率为 25%，小微企业可享受优惠税率。",'
        ' "domain": "tax"}'
    )
    assert Nodes._sanitize_llm_text(payload) == "企业所得税税率为 25%，小微企业可享受优惠税率。"

    fenced = "```json\n" + payload + "\n```"
    assert Nodes._sanitize_llm_text(fenced) == "企业所得税税率为 25%，小微企业可享受优惠税率。"


def test_sanitize_still_blocks_tool_call_residue():
    """工具调用残留必须继续被拦截（回归保护：别为了保正文而放宽这里）。"""
    out = Nodes._sanitize_llm_text('<tool_call>{"name": "query_financial_data"}')
    assert "query_financial_data" not in out
    assert "稍后重试" in out


def test_tax_and_legal_formatters_use_text_answer():
    """税务/法律格式化器必须优先使用 text_answer。

    历史上只有 `_format_finance_result` 读了 text_answer，税务/法律没读，
    于是退回模板拼装，产出「税种类型: 未知税种 / 适用税率: 未提供」的空壳报告。
    """
    orch = AgentOrchestrator.__new__(AgentOrchestrator)  # 不跑 __init__（会连库）
    text_answer = "个人所得税综合所得适用 3%~45% 七级超额累进税率。"

    assert "七级超额累进税率" in orch._format_tax_result({"success": True, "text_answer": text_answer})
    assert "七级超额累进税率" in orch._format_legal_result({"success": True, "text_answer": text_answer})
    # 不得再出现空壳模板
    assert "未知税种" not in orch._format_tax_result({"success": True, "text_answer": text_answer})
