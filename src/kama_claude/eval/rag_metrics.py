from __future__ import annotations


# 计算 Hit@K：top-K 中是否命中任一相关文档（命中 1，否则 0）
def hit_at_k(ranked_docs: list[str], relevant: set[str], k: int) -> float:
    return 1.0 if any(d in relevant for d in ranked_docs[:k]) else 0.0


# 计算 MRR：首个命中相关文档的排名倒数，未命中为 0
def mrr(ranked_docs: list[str], relevant: set[str]) -> float:
    for i, d in enumerate(ranked_docs):
        if d in relevant:
            return 1.0 / (i + 1)
    return 0.0


# 计算 Recall@K：top-K 命中的相关文档占全部相关文档的比例
def recall_at_k(ranked_docs: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(ranked_docs[:k]) & relevant) / len(relevant)


# 聚合一组 (ranked_docs, relevant) 的三项检索指标，返回平均分
def evaluate_retrieval(rows: list[tuple[list[str], set[str]]], k: int) -> dict[str, float]:
    n = len(rows)
    if n == 0:
        return {"hit_at_k": 0.0, "mrr": 0.0, "recall_at_k": 0.0}
    hit = sum(hit_at_k(r, rel, k) for r, rel in rows) / n
    mrr_sum = sum(mrr(r, rel) for r, rel in rows) / n
    rec = sum(recall_at_k(r, rel, k) for r, rel in rows) / n
    return {"hit_at_k": hit, "mrr": mrr_sum, "recall_at_k": rec}


# 计算过滤合规率：top-K 结果中命中"允许集合"的文档占比，衡量过滤是否真正收窄候选集
def filter_respect_rate(ranked_docs: list[str], allowed: set[str], k: int) -> float:
    if k <= 0 or not ranked_docs:
        return 1.0  # 无结果视为合规（空结果不越界）
    top = ranked_docs[:k]
    return len([d for d in top if d in allowed]) / len(top)
