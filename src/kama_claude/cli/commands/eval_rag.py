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
from kama_claude.core.rag.store import RagChunk
from kama_claude.eval.rag_dataset import RagEvalCase, load_dataset
from kama_claude.eval.rag_metrics import empty_rate, evaluate_retrieval, filter_respect_rate
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
        # --query-enhance 时需要真实 LLM 做查询改写；未配 key 则跳过该变体
        enhance_provider = None
        if args.query_enhance:
            try:
                enhance_provider = AnthropicProvider(config.llm.default_model)
            except SystemExit:
                print("  query 增强需要 ANTHROPIC_API_KEY，跳过")
        for backend in backends:
            if backend in ("embedding", "hybrid") and not config.rag.api_key:
                print(f"  [{backend:9s}] 跳过（未配置 KAMA_RAG_API_KEY）")
                continue
            # --rerank/--query-enhance 时对同一 backend 追加对应变体，直观对比增益
            variants = [(backend, False, False)]
            if args.rerank:
                variants.append((f"{backend}+rerank", True, False))
            if args.query_enhance and enhance_provider is not None:
                variants.append((f"{backend}+query", False, True))
            for tag, use_rerank, use_enhance in variants:
                try:
                    metrics, n = await _retrieval_row(
                        config.rag, knowledge_dir, dataset, top_k, backend,
                        use_rerank, use_enhance, enhance_provider,
                    )
                    print(
                        f"  [{tag:14s}] Hit@{top_k}={metrics['hit_at_k']:.3f}  "
                        f"MRR={metrics['mrr']:.3f}  Recall@{top_k}={metrics['recall_at_k']:.3f}"
                        f"  空结果率={metrics['empty_rate']:.3f}"
                        f"  （{n} chunks）"
                    )
                    if args.filter_check:
                        rate, nf = await _filter_respect(
                            config.rag, knowledge_dir, dataset, top_k, backend
                        )
                        print(
                            f"  [{tag:14s}] 过滤合规率={rate:.3f}  （{nf} 条带 filter 的 case）"
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
    use_enhance: bool = False,
    enhance_provider: AnthropicProvider | None = None,
) -> tuple[dict[str, float], int]:
    store = build_store(
        rag,
        retriever=backend,
        rerank=use_rerank,
        provider=enhance_provider,
        query_enhance=use_enhance,
    )
    n = await store.load_dir(knowledge_dir)
    ranked_rows: list[tuple[list[str], set[str]]] = []
    empty_rows: list[list[str]] = []
    for case in dataset:
        hits = await store.search(case.query, top_k, filter=case.filter or None)
        ranked = list(dict.fromkeys(c.doc_id for c in hits))
        ranked_rows.append((ranked, set(case.relevant_docs)))
        empty_rows.append(ranked)
    metrics = evaluate_retrieval(ranked_rows, top_k)
    # 空结果率随指标一并输出：过滤/召回失效常表现为"返回空"，需与命中率对照观察
    metrics["empty_rate"] = empty_rate(empty_rows)
    return metrics, n


# 计算带 filter 的 case 的过滤合规率，返回 (平均合规率, 带 filter 的 case 数)
async def _filter_respect(
    rag: RagConfig,
    knowledge_dir: Path,
    dataset: list[RagEvalCase],
    top_k: int,
    backend: str,
) -> tuple[float, int]:
    store = build_store(rag, retriever=backend)
    await store.load_dir(knowledge_dir)
    rates: list[float] = []
    for case in dataset:
        if not case.filter:
            continue
        hits = await store.search(case.query, top_k, filter=case.filter)
        ranked = list(dict.fromkeys(c.doc_id for c in hits))
        allowed = {c.doc_id for c in store._chunks if _chunk_matches(c, case.filter)}
        rates.append(filter_respect_rate(ranked, allowed, top_k))
    if not rates:
        return 1.0, 0
    return sum(rates) / len(rates), len(rates)


# 判断单个 chunk 是否命中过滤条件（与 RagStore._match 语义一致）
def _chunk_matches(chunk: RagChunk, cond: dict[str, object]) -> bool:
    from kama_claude.core.rag.filter import matches

    for k, v in cond.items():
        if chunk.metadata.get(k) is None or not matches(chunk.metadata[k], v):
            return False
    return True


# 将 RAGAs 逐条结果与聚合分数落盘为 JSON
def _save_output(path: str, result: dict[str, object]) -> None:
    payload = {"metrics": result["metrics"], "rows": result["rows"]}
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n结果已写入：{path}")
