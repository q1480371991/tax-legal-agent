"""意图路由回归脚本 —— 改路由逻辑前后对比用。

只调用 IntentRouterAgent（不跑完整流水线），所以很快（每条 1~3 秒）。
断言的是【路由决策】，不是答案质量。

用法：
    docker exec rag_backend python tests/integration/route_regression.py            # 跑并打印表格
    docker exec rag_backend python tests/integration/route_regression.py --json out.json   # 存基线
    docker exec rag_backend python tests/integration/route_regression.py --diff base.json  # 与基线对比

判据说明（expect）：
    set        → 专家集合必须【精确等于】该集合
    "single"   → 必须恰好 1 个专家（对比题用：具体是哪位不固定，但绝不能多派）
    None       → 不断言（如问候短路分支）
"""

import argparse
import asyncio
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# (问题, expect, 备注)
CASES = [
    # ── 跨领域：各种连接方式都要识别出来（这正是词表方案漏掉的地方）──
    ("我们公司现在有什么税务风险，合同有没有问题，财务健不健康",
     {"finance", "tax", "legal"}, "跨领域·逗号罗列"),
    ("我们公司有税务风险、合同也有问题",
     {"tax", "legal"}, "跨领域·顿号"),
    ("请从财务和法务的角度分析这个合同",
     {"finance", "legal"}, "跨领域·从…角度"),
    ("结合财务数据和最新税收政策给出综合建议",
     {"finance", "tax"}, "跨领域·结合"),
    ("分析一下公司的财务状况，另外看看合同有没有法律风险，再评估税务合规性",
     {"finance", "tax", "legal"}, "跨领域·另外/再（词表没有的连接词）"),
    # 注：这句的"账面利润不错"是【前提背景】而非诉求，真实诉求是
    #     税务隐患 + 合同法律问题两项，故只应派 tax/legal。
    ("公司账面利润不错，但是不是有税务隐患？顺便合同条款会不会有法律问题",
     {"tax", "legal"}, "跨领域·顺便/会不会（口语化连接）"),

    # ── 对比题：命中多领域词，但只需【一个】专家（负例，不能误判成多专家）──
    ("财务和法务的区别是什么", "single", "对比题·负例"),
    ("税务筹划和税务合规有什么不同", "single", "对比题·负例"),

    # ── 单领域 ──
    ("个人所得税的税率是多少", {"tax"}, "单领域·税务"),
    ("帮我看看合同里有什么法律风险", {"legal"}, "单领域·法律"),
    ("分析一下我们公司的偿债能力", {"finance"}, "单领域·财务·偿债能力（词表曾漏判）"),
    ("我们公司的资产负债率是多少", {"finance"}, "单领域·财务·资产负债率"),

    # ── 简单问题：不派专家、不调 LLM ──
    ("你好", None, "问候·短路"),
    ("谢谢", None, "感谢·短路"),

    # ── 知识问答：走 RAG 直接作答，不派专家 ──
    # 断言的是【路由】为 rag_retrieval（这几条最终由 _generate_rag_answer 作答，
    # 不会调用专家）。专家列表本身不强制为空 —— 它是对内容的领域判断，
    # 真正决定"跑不跑专家"的是路由。
    ("比亚迪2026年第一季度报告中的开发支出是多少", "rag", "知识检索·走 RAG"),
    ("万科2025年年报的营业收入是多少", "rag", "知识检索·走 RAG"),
    ("大湖股份和业绩补偿方是什么关系", "rag", "知识检索·走 RAG"),
]


async def run_all() -> list:
    from app.multi_agent_system.agents.intent_router_agent import IntentRouterAgent
    from app.agent_framework.llm import create_llm_adapter
    from app.agent_framework.tools.tool_manager import ToolManager

    agent = IntentRouterAgent(llm_adapter=create_llm_adapter(), tool_manager=ToolManager())
    out = []
    for q, expect, note in CASES:
        try:
            res = await agent.run(user_input=q)
        except Exception as exc:
            out.append({"q": q, "note": note, "error": str(exc)[:80]})
            continue

        if res.is_simple:
            out.append({
                "q": q, "note": note, "simple": True,
                "intent": None, "routing": None, "specialists": [],
                "expect": sorted(expect) if expect else None,
                "pass": expect is None,
            })
            continue

        ir = res.intent_result
        if ir is None:
            out.append({"q": q, "note": note, "error": "intent_result is None"})
            continue

        specialists = sorted(ir.requires_specialists or [])
        intent = getattr(ir.intent, "value", ir.intent)
        routing = getattr(ir.routing_strategy, "value", ir.routing_strategy)

        # 断言
        if expect is None:
            ok = True                                    # 不断言（问候短路等）
        elif expect == "single":
            ok = len(specialists) == 1                   # 恰好 1 个（对比题）
        elif expect == "rag":
            ok = routing == "rag_retrieval"              # 只断言路由
        else:
            ok = set(specialists) == set(expect)         # 精确匹配

        out.append({
            "q": q, "note": note, "intent": intent, "routing": routing,
            "specialists": specialists,
            "expect": (sorted(expect) if isinstance(expect, set)
                       else (expect if expect is None else [expect])),
            "pass": ok,
        })
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--json", help="把结果写到文件（作为基线）")
    p.add_argument("--diff", help="与基线文件对比")
    args = p.parse_args()

    results = asyncio.run(run_all())

    print(f"{'结果':<4}{'专家':<28}{'路由':<26}{'意图':<18}备注")
    print("-" * 118)
    for r in results:
        if "error" in r:
            print(f"{'ERR':<4}{'-':<28}{'-':<26}{'-':<18}{r['note']}  {r['error']}")
            continue
        mark = "OK" if r["pass"] else "FAIL"
        specs = ",".join(r["specialists"]) or "(无)"
        print(f"{mark:<4}{specs:<28}{str(r['routing'] or '-'):<26}"
              f"{str(r['intent'] or '-'):<18}{r['note']}")

    failed = [r for r in results if not r.get("pass")]
    print("-" * 118)
    print(f"通过 {len(results) - len(failed)}/{len(results)}")
    if failed:
        print("\n未通过：")
        for r in failed:
            print(f"  · {r['note']}: 期望 {r.get('expect')} 实际 {r.get('specialists')}")
            print(f"    {r['q']}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n已写入 {args.json}")

    if args.diff:
        with open(args.diff, encoding="utf-8") as f:
            base = json.load(f)
        base_map = {r["q"]: r for r in base}
        print("\n=== 与基线对比 ===")
        changed = 0
        for r in results:
            b = base_map.get(r["q"])
            if not b:
                continue
            if b.get("specialists") != r.get("specialists") or b.get("routing") != r.get("routing"):
                changed += 1
                print(f"  [{r['note']}]")
                print(f"    期望(基线) 专家={b.get('specialists')} 路由={b.get('routing')}")
                print(f"    实际(现在) 专家={r.get('specialists')} 路由={r.get('routing')}")
        print(f"共 {changed} 条发生变化")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
