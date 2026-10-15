from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from kama_claude.core.config import PermissionConfig

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.monitoring.telemetry import observe
from kama_claude.core.permissions.policy import PermissionDecision
from kama_claude.core.security.context import digest
from kama_claude.core.security.prompts import CLASSIFIER_SYSTEM

Reviewer = Callable[[str, str, int], Awaitable[str]]


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    verdict: Literal["SAFE", "UNSAFE", "UNCERTAIN", "allow", "ask", "deny"]
    risk: Literal["low", "medium", "high"]
    reason: str = Field(min_length=1, max_length=256)


@dataclass(frozen=True)
class Classification:
    action: PermissionDecision
    reason_code: str
    stage: int = 0


class RiskClassifier:
    # 使用独立无工具提供者并限制超时、并发、调用配额与熔断窗口
    def __init__(
        self,
        reviewer: Reviewer,
        *,
        model_version: str,
        timeout_s: float = 10,
        max_calls: int = 100,
        concurrency: int = 2,
        stage1_tokens: int = 128,
        stage2_tokens: int = 4096,
    ) -> None:
        self._reviewer = reviewer
        self.model_version = model_version
        self.timeout_s = timeout_s
        self.max_calls = max_calls
        self._slots = asyncio.Semaphore(concurrency)
        self._calls = 0
        self._failures = 0
        self._open_until = 0.0
        self._cache: dict[str, tuple[float, Classification]] = {}
        self._budgets = (stage1_tokens, stage2_tokens)

    # 记录审查成本、阶段和延迟，观测数据不包含参数、秘密或模型理由正文
    async def classify(self, data: dict[str, object]) -> Classification:
        attributes: dict[str, object] = {"model": self.model_version}
        started = time.monotonic()
        previous_calls = self._calls
        with observe("security.classifier", attributes):
            result = await self._classify(data)
            attributes.update(
                {
                    "stage": result.stage,
                    "action": str(result.action),
                    "reason_code": result.reason_code,
                    "model_calls": self._calls - previous_calls,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                }
            )
            return result

    # 对未决普通编辑执行两阶段严格 JSON 审查；任何失败回退人工审批
    async def _classify(self, data: dict[str, object]) -> Classification:
        key = digest({"data": data, "model": self.model_version})
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        fallback = Classification(PermissionDecision.ASK, "classifier_unavailable")
        if time.monotonic() < self._open_until or self._calls >= self.max_calls:
            return fallback
        try:
            async with asyncio.timeout(self.timeout_s):
                async with self._slots:
                    for stage, budget in enumerate(self._budgets, 1):
                        if self._calls >= self.max_calls:
                            return fallback
                        self._calls += 1
                        payload = json.dumps({"stage": stage, "data": data}, ensure_ascii=False)
                        if len(payload.encode()) > 65536:
                            return Classification(PermissionDecision.ASK, "classifier_input_limit")
                        raw = await self._reviewer(CLASSIFIER_SYSTEM, payload, budget)
                        if len(raw) > 8192:
                            raise ValueError("oversized classifier response")
                        review = Review.model_validate_json(raw)
                        if stage == 1:
                            if review.verdict not in {"SAFE", "UNSAFE", "UNCERTAIN"}:
                                raise ValueError("invalid stage one verdict")
                            if review.verdict == "SAFE" and review.risk == "low":
                                decision = Classification(
                                    PermissionDecision.ALLOW, "classifier_safe", 1
                                )
                                break
                        else:
                            if review.verdict not in {"allow", "ask", "deny"}:
                                raise ValueError("invalid stage two verdict")
                            action = PermissionDecision(review.verdict)
                            if review.risk != "low" and action == PermissionDecision.ALLOW:
                                action = PermissionDecision.ASK
                            decision = Classification(action, "classifier_deep", 2)
            self._failures = 0
            # 只缓存低风险批准；缓存绑定完整作用域、规则和模型版本
            if decision.action == PermissionDecision.ALLOW:
                if len(self._cache) >= 512:
                    self._cache.clear()
                self._cache[key] = (time.monotonic() + 30, decision)
            return decision
        except Exception:
            self._failures += 1
            if self._failures >= 3:
                self._open_until = time.monotonic() + 60
            return fallback


# 显式启用并配置独立凭据后才创建审查器，缺配置时保持人工审批
def build_classifier(config: PermissionConfig) -> RiskClassifier | None:
    import os

    key = os.getenv("KAMA_SECURITY_CLASSIFIER_API_KEY")
    if not config.classifier_enabled or not config.classifier_model or not key:
        return None

    # 独立客户端不接收工具，不向主 Agent 输出审查内容，也不自动重试请求
    async def review(system: str, payload: str, max_tokens: int) -> str:
        import anthropic

        async with anthropic.AsyncAnthropic(
            api_key=key,
            max_retries=0,
            timeout=config.classifier_timeout_s,
        ) as client:
            response = await client.messages.create(
                model=config.classifier_model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": payload}],
            )
            if response.stop_reason != "end_turn":
                raise ValueError("classifier output incomplete")
            return "".join(block.text for block in response.content if block.type == "text")

    return RiskClassifier(
        review,
        model_version=config.classifier_model,
        timeout_s=config.classifier_timeout_s,
        max_calls=config.classifier_max_calls,
    )
