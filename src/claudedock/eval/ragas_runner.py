from __future__ import annotations

import asyncio
import sys

from claudedock.core.events.bus import EventBus
from claudedock.core.llm.base import LLMProvider
from claudedock.core.rag.store import RagStore
from claudedock.eval.rag_dataset import RagEvalCase


# 组装"仅据上下文作答"的提示词，隔离生成质量与工具决策能力
def _answer_prompt(query: str, contexts: list[str]) -> str:
    joined = "\n\n".join(f"[{i}] {c}" for i, c in enumerate(contexts, 1))
    return (
        "根据以下检索到的资料回答用户问题。"
        "只依据资料内容作答，资料没有的信息不要编造；若资料不足以回答，请明确说明。\n\n"
        f"资料：\n{joined}\n\n"
        f"问题：{query}\n"
        "回答："
    )


# 对每条评测 case 检索上下文并调用 LLM 生成答案，返回 RAGAs 输入行
async def generate_answers(
    store: RagStore,
    provider: LLMProvider,
    cases: list[RagEvalCase],
    top_k: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    bus = EventBus()
    for case in cases:
        chunks = await store.search(case.query, top_k)
        contexts = [c.text for c in chunks]
        resp = await provider.chat(
            messages=[{"role": "user", "content": _answer_prompt(case.query, contexts)}],
            tool_schemas=[],
            bus=bus,
            run_id=f"eval-{case.id}",
            system="Answer the user's question using only the provided reference material.",
        )
        rows.append(
            {
                "user_input": case.query,
                "response": resp.text,
                "retrieved_contexts": contexts,
                "reference": case.ground_truth,
            }
        )
    return rows


# 检测 ragas 及 langchain 包装是否已安装（不触发实际评测）
def ragas_available() -> bool:
    try:
        import langchain_openai  # noqa: F401
        import ragas  # noqa: F401
        return True
    except ImportError:
        return False


# 用 RAGAs 三指标评估生成层；ragas 未安装时提示返回 None，缺 embedding key 时跳过 AnswerRelevancy
def run_generation_eval(
    rows: list[dict[str, object]],
    *,
    provider: LLMProvider,
    embed_model: str = "embedding-3",
    embed_base_url: str = "https://open.bigmodel.cn/api/paas/v4",
    embed_api_key: str = "",
) -> dict[str, object] | None:
    if not ragas_available():
        print("需要 ragas：运行 `uv sync --group eval` 安装后重试", file=sys.stderr)
        return None

    from langchain_core.outputs import Generation, LLMResult
    from langchain_openai import OpenAIEmbeddings
    from ragas import EvaluationDataset, RunConfig, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms.base import BaseRagasLLM
    from ragas.metrics import AnswerRelevancy, ContextRecall, Faithfulness

    # 用项目自带 AnthropicProvider 包装 ragas 裁判 LLM，避免引入 langchain-anthropic
    class _ProviderRagasLLM(BaseRagasLLM):
        # 同步兜底入口：ragas 评估实际走 agenerate_text，这里仅作兼容
        def generate_text(self, prompt, n=1, temperature=None, stop=None, callbacks=None):
            return asyncio.run(
                self.agenerate_text(
                    prompt, n=n, temperature=temperature, stop=stop, callbacks=callbacks
                )
            )

        # 异步生成：取出 PromptValue 文本交给 AnthropicProvider，包装成 LLMResult
        async def agenerate_text(self, prompt, n=1, temperature=None, stop=None, callbacks=None):
            bus = EventBus()
            texts = []
            for _ in range(max(n, 1)):
                resp = await provider.chat(
                    messages=[{"role": "user", "content": prompt.to_string()}],
                    tool_schemas=[],
                    bus=bus,
                    run_id="ragas-judge",
                )
                texts.append(resp.text)
            return LLMResult(generations=[[Generation(text=t) for t in texts]])

        # 跳过 finish_reason 检查：judge 输出短，不存在 max_tokens 截断，避免告警
        def is_finished(self, response: object) -> bool:
            return True

    run_config = RunConfig(max_workers=4, timeout=600)
    judge = _ProviderRagasLLM(run_config=run_config)

    metrics: list[object] = [Faithfulness(), ContextRecall()]
    eval_kwargs: dict[str, object] = {}
    if embed_api_key:
        emb = LangchainEmbeddingsWrapper(
            OpenAIEmbeddings(model=embed_model, base_url=embed_base_url, api_key=embed_api_key)
        )
        metrics.append(AnswerRelevancy())
        eval_kwargs["embeddings"] = emb
    else:
        print("警告：未配置 embedding key，跳过 Answer Relevancy", file=sys.stderr)

    dataset = EvaluationDataset.from_list(rows)
    result = evaluate(
        dataset,
        metrics=metrics,
        llm=judge,
        run_config=run_config,
        **eval_kwargs,
    )
    df = result.to_pandas()
    numeric = df.select_dtypes("number")
    scores = {col: float(numeric[col].mean()) for col in numeric.columns}
    return {"metrics": scores, "rows": df.to_dict(orient="records")}
