from __future__ import annotations

from kama_claude.eval.rag_metrics import (
    empty_rate,
    evaluate_retrieval,
    filter_respect_rate,
    hit_at_k,
    mrr,
    recall_at_k,
)


# 功能：验证 Hit@K 在 top-K 命中相关文档时返回 1，否则返回 0
# 设计：覆盖命中在 K 内、命中在 K 外、完全未命中三种边界
def test_hit_at_k() -> None:
    ranked = ["a", "b", "c"]
    relevant = {"b", "d"}
    assert hit_at_k(ranked, relevant, 2) == 1.0
    assert hit_at_k(ranked, relevant, 1) == 0.0
    assert hit_at_k(ranked, {"x"}, 3) == 0.0


# 功能：验证 MRR 为首个命中相关文档的排名倒数，未命中为 0
# 设计：命中第 2 位断言 0.5，无关相关集断言 0
def test_mrr() -> None:
    assert mrr(["a", "b", "c"], {"b"}) == 0.5
    assert mrr(["a", "b", "c"], {"x"}) == 0.0


# 功能：验证 Recall@K 为 top-K 命中相关文档占全部相关文档比例，空相关集安全返回 0
# 设计：多相关仅召回部分时断言比例，空集避免除零
def test_recall_at_k() -> None:
    assert recall_at_k(["a", "b"], {"a", "b", "c"}, 2) == 2 / 3
    assert recall_at_k(["a", "b"], set(), 2) == 0.0


# 功能：验证 evaluate_retrieval 聚合多 case 的三指标平均分，空输入返回全 0
# 设计：两条已知 case 手算均值与聚合结果一致
def test_evaluate_retrieval() -> None:
    rows = [
        (["a", "b"], {"b"}),
        (["x", "y"], {"y", "z"}),
    ]
    out = evaluate_retrieval(rows, k=2)
    assert out["hit_at_k"] == 1.0
    assert out["mrr"] == 0.5
    assert out["recall_at_k"] == 0.75
    assert evaluate_retrieval([], k=2) == {"hit_at_k": 0.0, "mrr": 0.0, "recall_at_k": 0.0}


# 功能：验证 filter_respect_rate 计算 top-K 中命中允许集合的文档占比，空结果判 0 而非满分
# 设计：部分命中断言 0.5、全命中断言 1.0；空结果断言 0.0，使过滤失效导致的"召回为空"
#       在指标上暴露，而不是被当作"没有越界"掩盖过去
def test_filter_respect_rate() -> None:
    assert filter_respect_rate(["a", "c"], {"a", "b"}, 2) == 0.5
    assert filter_respect_rate(["a", "b"], {"a", "b"}, 2) == 1.0
    assert filter_respect_rate([], {"a"}, 2) == 0.0


# 功能：验证 empty_rate 统计无召回 case 的占比，区分"过滤正确无命中"与"召回整体失效"
# 设计：四条 case 中两条为空断言 0.5，空输入断言 0.0 避免除零
def test_empty_rate() -> None:
    assert empty_rate([["a"], [], ["b"], []]) == 0.5
    assert empty_rate([]) == 0.0
