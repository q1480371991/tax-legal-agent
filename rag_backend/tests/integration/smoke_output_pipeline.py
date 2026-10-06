"""输出链路冒烟脚本 —— 一次跑完关键题型，输出「耗时 / 反思轮次 / 答案长度」。

为什么是脚本而不是 pytest：
  本仓库的 `tests/conftest.py` 有 autouse fixture 会把 POSTGRES_DB 覆盖成
  `test_db`，pytest 收集到本目录就会连不上库。脚本直接跑可绕过。

用法（容器内）：
    docker exec rag_backend python tests/integration/smoke_output_pipeline.py
    docker exec rag_backend python tests/integration/smoke_output_pipeline.py --reflect off
    docker exec rag_backend python tests/integration/smoke_output_pipeline.py --preview

关注三件事：
  1. **轮次** —— 反思轮次应恒为 0 或 1。≥2 说明有内容被打回重做（本次修复的症状）。
  2. **长度台阶** —— `专家原文`（text_answer）→ `入库 content` → `最终答案`
     三级长度。content 相对原文塌缩即为截断点（本次 bug 正发生在这一级）。
  3. **耗时** —— 只作参考，阈值按题型给（多专家本来就慢，不是 bug）。
     判断"卡住"要看**轮次**，不要只看秒数。
"""

import argparse
import asyncio
import logging
import os
import sys
import time

# 自带路径引导：无论从仓库根目录还是 tests/integration 下调用，都能 import app。
# （直接 python 跑脚本时 sys.path[0] 是脚本所在目录，找不到仓库根的 app 包）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

TENANT_ID = "company_test_corp_175efce8"
USER_ID = "8e42b2ed-9348-4d27-b4aa-a2049d3b336e"

# kind 决定耗时预算 —— 多专家天然比单专家慢，不该用同一把尺子量。
BUDGET = {"single": 30, "rag": 25, "multi": 75}

CASES = [
    # ── 本次修复的主战场：答案含公式/代码块，最易被 sanitize 截断 ──
    {"label": "A1 单专家·含公式", "q": "个人所得税的税率是多少", "kind": "single"},
    {"label": "A2 单专家·含计算", "q": "企业所得税怎么计算", "kind": "single"},
    # ── RAG 事实题：走真实知识库，验证检索与图谱不受影响 ──
    {"label": "B1 RAG·事实", "q": "比亚迪2026年第一季度报告中的开发支出是多少", "kind": "rag"},
    {"label": "B2 RAG·事实", "q": "万科2025年年报的营业收入是多少", "kind": "rag"},
    # ── 图谱关系题 ──
    {"label": "C1 图谱·关系", "q": "大湖股份和业绩补偿方是什么关系", "kind": "rag"},
    # ── 多专家：走 ResultSynthesizer，与单专家是两条不同的组装路径 ──
    {"label": "D1 多专家·财务+税务", "q": "分析比亚迪的财务状况和税务风险", "kind": "multi"},
]


async def run_case(orch, label: str, query: str) -> dict:
    # 统计本用例的反思轮次：反思节点每轮都会打印 "[反思] 合并 N 个专家结果"
    rounds = {"n": 0}

    class _ReflectionCounter(logging.Handler):
        def emit(self, record):
            if "[反思] 合并" in record.getMessage():
                rounds["n"] += 1

    counter = _ReflectionCounter(level=logging.INFO)
    reflection_logger = logging.getLogger("app.multi_agent_system.orchestrator")
    reflection_logger.addHandler(counter)

    t0 = time.time()
    try:
        ctx = await orch.process_user_request(
            user_input=query,
            session_id=None,
            history=[],
            metadata={"enable_reflection": True},
        )
    finally:
        reflection_logger.removeHandler(counter)
    elapsed = time.time() - t0

    final = ctx.final_response or ""
    specialists = getattr(ctx, "specialist_results", None) or []

    # 专家原文（text_answer）与入库 content 的长度差，就是"最后一公里"的损耗
    raw_len = 0
    content_len = 0
    for s in specialists:
        if not isinstance(s, dict):
            continue
        raw_len += len(str((s.get("data") or {}).get("text_answer") or ""))
        content_len += len(str(s.get("content") or ""))

    return {
        "label": label,
        "query": query,
        "elapsed": elapsed,
        "rounds": rounds["n"],
        "raw_len": raw_len,
        "content_len": content_len,
        "final_len": len(final),
        "final": final,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reflect", choices=["on", "off"], default="on",
                        help="是否开启反思审核（默认 on，即线上行为）")
    parser.add_argument("--preview", action="store_true",
                        help="打印每条答案的前 400 字符，用于人工判断内容质量")
    args = parser.parse_args()

    from app.multi_agent_system.orchestrator import AgentOrchestrator

    orch = AgentOrchestrator(tenant_id=TENANT_ID, user_id=USER_ID)
    await orch.initialize()
    orch.enable_reflection = args.reflect == "on"

    results = []
    for case in CASES:
        label, query = case["label"], case["q"]
        print(f"\n{'─' * 70}\n▶ {label} | {query}", flush=True)
        try:
            r = await run_case(orch, label, query)
            r["kind"] = case["kind"]
            results.append(r)
            if args.preview:
                print(f"  ── 答案预览 ──\n{(r['final'] or '')[:400]}", flush=True)
        except Exception as exc:  # 单条失败不阻断整体
            print(f"  ✗ 执行异常: {exc}", flush=True)
            results.append({"label": label, "query": query, "kind": case["kind"], "error": str(exc)})

    print(f"\n{'=' * 70}\n汇总（反思: {args.reflect}）\n{'=' * 70}")
    print(f"{'用例':<22}{'耗时':>8}{'轮次':>6}{'专家原文':>10}{'入库content':>13}{'最终答案':>10}")
    print("-" * 70)

    problems = []
    for r in results:
        if "error" in r:
            print(f"{r['label']:<22}{'ERR':>8}  {r['error'][:40]}")
            problems.append(f"{r['label']}: 执行异常")
            continue

        print(f"{r['label']:<22}{r['elapsed']:>7.1f}s{r['rounds']:>6}{r['raw_len']:>10}"
              f"{r['content_len']:>13}{r['final_len']:>10}")

        budget = BUDGET.get(r.get("kind", "single"), 30)

        # 断言一：最终答案不得为空
        if r["final_len"] < 50:
            problems.append(f"{r['label']}: 最终答案过短 ({r['final_len']})")
        # 断言二：入库 content 相对专家原文不得塌缩超过一半（本次 bug 的核心症状）
        if r["raw_len"] > 200 and r["content_len"] < r["raw_len"] * 0.5:
            problems.append(
                f"{r['label']}: content 塌缩 {r['raw_len']}→{r['content_len']}（疑似被截断）"
            )
        # 断言三：反思轮次 —— 0 或 1 都正常；≥2 才是打回循环
        if r["rounds"] > 1:
            problems.append(f"{r['label']}: 反思 {r['rounds']} 轮（打回循环）")
        # 断言四：耗时超该题型预算（只作提示，不单独定性为 bug）
        if r["elapsed"] > budget:
            problems.append(
                f"{r['label']}: 耗时 {r['elapsed']:.1f}s 超出 {r['kind']} 预算 {budget}s"
            )

    print("=" * 70)
    if problems:
        print("⚠️  发现问题：")
        for p in problems:
            print(f"   · {p}")
        return 1

    print("✅ 全部用例通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
