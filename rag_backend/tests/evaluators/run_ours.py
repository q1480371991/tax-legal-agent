"""对 eval_out_{graph,nograph}.csv 跑 RAGAS 打分"""
import os, ast, sys
import pandas as pd
from pathlib import Path

HERE = Path(__file__).parent
JUDGE_KEY = os.getenv("JUDGE_API_KEY", "")
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "deepseek-chat")
JUDGE_BASE = os.getenv("JUDGE_BASE_URL", "https://api.deepseek.com/v1")
EMB_KEY = os.getenv("EMB_KEY", "")
EMB_BASE = os.getenv("EMB_BASE", "https://api.siliconflow.cn/v1")

def score(mode):
    p = HERE / f"eval_out_{mode}.csv"
    df = pd.read_csv(p)
    def parse(v):
        try: return ast.literal_eval(v)
        except: return [v] if v else []
    df["retrieved_contexts"] = df["retrieved_contexts"].apply(parse)
    df["ground_truth"] = df["reference"]
    # 该 ragas 版本要求列名为 question / contexts / answer
    df = df.rename(columns={
        "user_input": "question",
        "retrieved_contexts": "contexts",
        "response": "answer",
    })
    # 强制类型：ragas 要求 question/answer/ground_truth 为 str，contexts 为 list[str]
    df["question"] = df["question"].astype(str)
    df["answer"] = df["answer"].astype(str)
    df["ground_truth"] = df["ground_truth"].astype(str)
    df["contexts"] = df["contexts"].apply(lambda x: [str(c) for c in x] if isinstance(x, list) else [str(x)])
    df = df[["question", "contexts", "answer", "ground_truth"]]

    from datasets import Dataset
    from ragas import evaluate
    from ragas.metrics import context_precision, context_recall, faithfulness
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings

    # pandas 3.x 的 str dtype 与 HuggingFace datasets 不兼容，显式转 object
    import pandas as _pd
    sub = df[["question","contexts","answer","ground_truth"]].astype(object)
    ds = Dataset.from_pandas(sub, preserve_index=False)
    judge = ChatOpenAI(model=JUDGE_MODEL, temperature=0, openai_api_key=JUDGE_KEY, openai_api_base=JUDGE_BASE)
    emb = OpenAIEmbeddings(model="BAAI/bge-m3", openai_api_key=EMB_KEY, openai_api_base=EMB_BASE)
    print(f"\n===== {mode} 打分中 =====", flush=True)
    res = evaluate(dataset=ds, metrics=[context_precision, context_recall, faithfulness], llm=judge, embeddings=emb)
    print(res, flush=True)
    out = res.to_pandas()
    out.to_csv(HERE / f"ragas_ours_{mode}.csv", index=False, encoding="utf-8")
    return {k: float(v) for k, v in res.items()}

if __name__ == "__main__":
    if not JUDGE_KEY or not EMB_KEY:
        print("需要 JUDGE_API_KEY 和 EMB_KEY 环境变量"); sys.exit(1)
    a = score("graph")
    b = score("nograph")
    print("\n" + "="*60)
    print("        对比汇总")
    print("="*60)
    print(f"{'指标':<22} {'graph':>8} {'nograph':>8} {'delta':>8}")
    for k in a:
        print(f"{k:<22} {a[k]:>8.3f} {b[k]:>8.3f} {a[k]-b[k]:>+8.3f}")
