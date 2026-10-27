from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any

from claudedock.core.storage import atomic_write

current_journal: ContextVar[RunJournal | None] = ContextVar("run_journal", default=None)


class RunJournal:
    # 加载或建立版本化运行记录，持久化待决副作用与恢复上下文
    def __init__(self, path: Path, run_id: str, goal: str) -> None:
        self._lock = RLock()
        self.path = path
        self.data: dict[str, Any] = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {
                "schema_version": 1,
                "run_id": run_id,
                "goal": goal,
                "state": "queued",
                "operations": {},
            }
        )

    # 原子提交运行状态及操作日志
    def save(self) -> None:
        with self._lock:
            atomic_write(self.path, json.dumps(self.data, ensure_ascii=False))

    # 持久化可恢复上下文，保留最近操作与原始目标
    def checkpoint(self, messages: list[dict[str, Any]], state: str = "running") -> None:
        with self._lock:
            self.data.update(messages=messages, state=state)
            self.save()

    # 工具调用先登记稳定操作号，未决旧操作拒绝盲目重试
    def prepare(
        self,
        tool_id: str,
        name: str,
        params: dict[str, Any],
        *,
        identity: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
            operations = self.data["operations"]
            previous = operations.get(tool_id)
            if previous is not None:
                if not isinstance(previous, dict):
                    raise ValueError("invalid operation journal")
                if previous["name"] != name or previous["digest"] != digest:
                    raise ValueError("operation id reused with different input")
                if identity is not None and previous.get("tool_identity") != identity:
                    raise ValueError("operation tool identity changed; verify before recovery")
                return deepcopy(previous)
            operations[tool_id] = {
                "operation_id": f"{self.data['run_id']}:{tool_id}",
                "name": name,
                "tool_identity": identity,
                "digest": digest,
                "state": "prepared",
            }
            self.save()
            return None

    # 多调用审批状态在同一锁内汇总，单个审批结束不会掩盖其他等待者
    def approval_wait(self, tool_id: str, waiting: bool) -> None:
        with self._lock:
            self.data["operations"][tool_id]["awaiting_approval"] = waiting
            self.data["state"] = (
                "waiting_approval"
                if any(
                    operation.get("awaiting_approval", False)
                    for operation in self.data["operations"].values()
                )
                else "running"
            )
            self.save()

    # 在执行、成功、失败或未知窗口同步写操作状态
    def update(self, tool_id: str, state: str, **details: Any) -> None:
        with self._lock:
            self.data["operations"][tool_id].update(state=state, **details)
            self.save()
