from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TaskHandoff:
    goal: str
    expected_output: str = ""
    relevant_files: list[str] = field(default_factory=list)
    known_constraints: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    tools_allowed: list[str] = field(default_factory=list)
    parent_run_id: str = ""
    budget: dict[str, int | float] = field(default_factory=dict)
    schema_version: int = 1

    # 将控制字段与模型提供的材料分开生成有界任务交接
    @classmethod
    def build(
        cls, goal: str, details: dict[str, Any], trusted_constraints: list[str],
        allowed_tools: list[str], parent_run_id: str,
        budget: dict[str, int | float] | None = None,
    ) -> TaskHandoff:
        return cls(
            goal=goal, expected_output=str(details.get("expected_output", ""))[:2048],
            relevant_files=[str(path)[:512] for path in details.get("relevant_files", [])[:32]],
            known_constraints=trusted_constraints[-8:] + [
                str(item)[:1024] for item in details.get("known_constraints", [])[:16]
            ],
            evidence_refs=[str(ref)[:512] for ref in details.get("evidence_refs", [])[:32]],
            tools_allowed=sorted(allowed_tools), parent_run_id=parent_run_id,
            budget=dict(budget or {}),
        )

    # 交接数据只作为用户任务材料，不获得系统指令权限
    def render(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


@dataclass
class ResultSummary:
    run_id: str
    status: str
    summary: str
    verified_actions: list[dict[str, str]] = field(default_factory=list)
    files_changed: list[dict[str, str]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    open_issues: list[str] = field(default_factory=list)
    confidence: str = "assistant_claims_unverified"
    schema_version: int = 1

    # 只把真实操作日志和当前工作区文件摘要标为执行证据
    @classmethod
    def build(
        cls, run_id: str, status: str, text: str, operations: dict[str, Any],
        messages: list[dict[str, Any]], workspace: Path,
    ) -> ResultSummary:
        result = cls(run_id, status, text[:2000], evidence_refs=[f"run:{run_id}:checkpoint"])
        calls: dict[str, dict[str, Any]] = {}
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_use":
                        calls[str(block.get("id", ""))] = block
        for tool_id, operation in list(operations.items())[-64:]:
            if operation.get("state") != "completed":
                result.open_issues.append(f"{tool_id}: {operation.get('state', 'unknown')}")
                continue
            result.verified_actions.append({
                "tool_use_id": tool_id, "tool_name": str(operation["name"]),
                "status": "executed_successfully",
            })
            call = calls.get(tool_id, {})
            if operation["name"] != "write_file":
                continue
            try:
                path = Path(str(call.get("input", {})["path"])).resolve(strict=True)
                if not path.is_relative_to(workspace.resolve()) or path.stat().st_size > 2097152:
                    continue
                result.files_changed.append({
                    "path": str(path.relative_to(workspace.resolve())),
                    "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "tool_use_id": tool_id,
                })
            except (OSError, KeyError, TypeError, ValueError):
                result.open_issues.append(f"{tool_id}: file state not verified")
        result.open_issues.append("Validate assistant conclusions against cited execution evidence")
        return result

    # 返回有界结构化结果，父任务无需继承完整探索历史
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# 父侧重新校验子任务返回的文件摘要，过期文件状态不再标为当前可验证
def verify_result_files(summary: dict[str, Any], workspace: Path) -> dict[str, Any]:
    from copy import deepcopy

    result = deepcopy(summary)
    for entry in result.get("files_changed", []):
        verified = False
        try:
            path = (workspace / entry["path"]).resolve(strict=True)
            if path.is_relative_to(workspace.resolve()) and path.stat().st_size <= 2097152:
                verified = hashlib.sha256(path.read_bytes()).hexdigest() == entry["digest"]
        except (OSError, KeyError, TypeError, ValueError):
            pass
        entry["verified_current"] = verified
        if not verified:
            result.setdefault("open_issues", []).append(
                f"File state changed or is unavailable: {entry.get('path', '')}"
            )
    return result
