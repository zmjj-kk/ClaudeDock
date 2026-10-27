from __future__ import annotations

import json

import httpx
import pytest

from claudedock.core.rag import rerank
from claudedock.core.rag.rerank import ZhipuReranker


# 功能：验证完整且乱序的 rerank 结果按 index 映射回输入顺序
# 设计：MockTransport 返回完整的乱序结果，断言零分与有效分数均保持输入顺序
async def test_rerank_maps_results_by_index() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "rerank"
        assert body["query"] == "q"
        assert body["documents"] == ["a", "b", "c"]
        assert body["top_n"] == 3
        return httpx.Response(
            200,
            json={
                "model": "glm-rerank",
                "results": [
                    {"index": 1, "relevance_score": 0.0},
                    {"index": 2, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.3},
                ],
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://test.local"
    )
    r = ZhipuReranker("k", client=client)
    assert await r.rerank("q", ["a", "b", "c"]) == [0.3, 0.0, 0.9]


# 功能：验证 rerank 空输入直接返回空列表，不发起任何网络请求
# 设计：传入空列表，断言结果为空且 handler 未被调用（若被调用会标记 called）
async def test_rerank_empty_input() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"results": []})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://test.local"
    )
    r = ZhipuReranker("k", client=client)
    assert await r.rerank("q", []) == []
    assert not called


# 功能：验证连接中断后按退避重试，最终成功返回结果
# 设计：handler 首次抛 ConnectError、第二次正常返回；压平退避秒数以加速测试，断言重试后拿到分数
async def test_rerank_retries_on_connect_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rerank, "_RETRY_BACKOFF_S", (0.0, 0.0))
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.7}]})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://test.local"
    )
    r = ZhipuReranker("k", client=client)
    assert await r.rerank("q", ["a"]) == [0.7]
    assert attempts == 2
