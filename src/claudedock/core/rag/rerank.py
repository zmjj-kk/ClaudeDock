from __future__ import annotations

import asyncio
import logging
import math
from abc import ABC, abstractmethod

import httpx

# rerank 网络请求的重试退避秒数（连接/读中断时重试）
_RETRY_BACKOFF_S = (1.0, 2.0)
_MAX_RETRIES = 2

log = logging.getLogger(__name__)


class Reranker(ABC):
    # 对候选文本按与 query 的相关性打分，返回与输入顺序对齐的分数列表
    @abstractmethod
    async def rerank(self, query: str, texts: list[str]) -> list[float]: ...

    # 关闭底层网络资源；无资源的实现默认空操作
    async def aclose(self) -> None:
        return None


class ZhipuReranker(Reranker):
    # 初始化智谱 rerank 客户端；client 可在测试时注入
    def __init__(
        self,
        api_key: str,
        model: str = "rerank",
        base_url: str = "https://open.bigmodel.cn/api/paas/v4",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    # 关闭 httpx 异步客户端，释放连接池
    async def aclose(self) -> None:
        await self._client.aclose()

    # 调用 /rerank 获取相关性分，按 results[].index 映射回输入顺序；空输入直接返回空列表
    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        payload = {"model": self._model, "query": query, "documents": texts, "top_n": len(texts)}
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await self._client.post("/rerank", json=payload)
                resp.raise_for_status()
                data = resp.json()
                items = data.get("results", [])
                if len(items) != len(texts) or {item.get("index") for item in items} != set(
                    range(len(texts))
                ):
                    raise ValueError("rerank response count or indices do not match input")
                scores = [0.0] * len(texts)
                for item in data.get("results", []):
                    idx = item.get("index")
                    if isinstance(idx, int) and 0 <= idx < len(texts):
                        scores[idx] = float(item.get("relevance_score", 0.0))
                if not all(math.isfinite(score) for score in scores):
                    raise ValueError("non-finite rerank score")
                return scores
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                if attempt == _MAX_RETRIES:
                    raise
                log.warning(
                    "rerank request failed (attempt %d/%d): %s",
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    exc,
                )
                await asyncio.sleep(_RETRY_BACKOFF_S[attempt])
        raise RuntimeError("unreachable")  # pragma: no cover
