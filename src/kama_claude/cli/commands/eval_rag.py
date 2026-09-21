from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import cast

from kama_claude.core.config import KamaConfig, RagConfig
from kama_claude.core.llm.provider import AnthropicProvider
from kama_claude.core.rag.factory import build_store
from kama_claude.eval.rag_dataset import RagEvalCase, load_dataset
from kama_claude.eval.rag_metrics import evaluate_retrieval
from kama_claude.eval.ragas_runner import generate_answers, ragas_available, run_generation_eval


# 同步入口：按层/后端分发评估并打印结果，失败时打印错误并退出
def cmd_eval_rag(args: argparse.Namespace, config: KamaConfig) -> None:
    try:
        asyncio.run(_eval_rag(args, config))
    except Exception as exc:
        print(f"error: eval failed: {exc}", file=sys.stderr)
        sys.exit(1)


# 异步主流程：载入数据集，按 --layer 依次跑检索层与生成层
async def _eval_rag(args: argparse.Namespace, config: KamaConfig) -> None:
    dataset = load_dataset(Path(args.dataset).expanduser())
    if not dataset:
        print(f"error: 评测集为空或不存在：{args.dataset}", file=sys.stderr)
        sys.exit(1)

    top_k = args.top_k or config.rag.top_k
    knowledge_dir = Path(config.rag.knowledge_dir).expanduser()

    if args.layer in ("retrieval", "both"):
        backends = ["bm25", "embedding", "hybrid"] if args.backend == "all" else [args.backend]
        print(f"检索层（top_k={top_k}，{len(dataset)} 条）：")
        for backend in backends:
            if backend in ("embedding", "hybrid") and not config.rag.api_key:
                print(f"  [{backend:9s}] 跳过（未配置 KAMA_RAG_API_KEY）")
                continue
            # --rerank 时对同一 backend 并行跑 baseline 与 +rerank，直观对比精排增益
            variants = [(backend, False)]
            if args.rerank:
                variants.append((f"{backend}+rerank", True))
            for tag, use_rerank in variants:
                try:
                    metrics, n = await _retrieval_row(
                        config.rag, knowledge_dir, dataset, top_k, backend, use_rerank
                    )
                    print(
                        f"  [{tag:14s}] Hit@{top_k}={metrics['hit_at_k']:.3f}  "
                        f"MRR={metrics['mrr']:.3f}  Recall@{top_k}={metrics['recall_at_k']:.3f}"
                        f"  （{n} chunks）"
                    )
                except Exception as exc:
                    print(f"  [{tag:14s}] 失败：{exc}")

    if args.layer in ("generation", "both"):
        if not ragas_available():
            print("需要 ragas：运行 `uv sync --group eval` 安装后再跑生成层", file=sys.stderr)
            sys.exit(1)
        backend = config.rag.retriever if args.backend == "all" else args.backend
        store = build_store(config.rag, retriever=backend)
        await store.load_dir(knowledge_dir)
        provider = AnthropicProvider(config.llm.default_model)
        answer_rows = await generate_answers(store, provider, dataset, top_k)

        print("\n生成层（RAGAs）：")
        result = run_generation_eval(
            answer_rows,
            provider=provider,
            embed_model=config.rag.embedding_model,
            embed_base_url=config.rag.embedding_base_url,
            embed_api_key=config.rag.api_key,
        )
        if result is None:
            sys.exit(1)
        metrics = cast(dict[str, float], result["metrics"])
        for name, score in metrics.items():
            print(f"  {name}: {score:.3f}")
        if args.output:
            _save_output(args.output, result)


# 对指定 backend 构建 store、加载知识库并跑检索评测，返回 (指标, chunk 数)
async def _retrieval_row(
    rag: RagConfig,
    knowledge_dir: Path,
    dataset: list[RagEvalCase],
    top_k: int,
    backend: str,
    use_rerank: bool,
) -> tuple[dict[str, float], int]:
    store = build_store(rag, retriever=backend, rerank=use_rerank)
    n = await store.load_dir(knowledge_dir)
    ranked_rows: list[tuple[list[str], set[str]]] = []
    for case in dataset:
        hits = await store.search(case.query, top_k)
        ranked = list(dict.fromkeys(c.doc_id for c in hits))
        ranked_rows.append((ranked, set(case.relevant_docs)))
    return evaluate_retrieval(ranked_rows, top_k), n


# 将 RAGAs 逐条结果与聚合分数落盘为 JSON
def _save_output(path: str, result: dict[str, object]) -> None:
    payload = {"metrics": result["metrics"], "rows": result["rows"]}
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n结果已写入：{path}")
