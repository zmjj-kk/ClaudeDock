from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from jsonschema.validators import validator_for

from kama_claude.core.tools.base import BaseTool


class ToolRegistry:
    # 建立运行内工具白名单、模式校验器及可选冻结快照
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._validators: dict[str, Any] = {}
        self._active: set[str] | None = None
        self._snapshots: dict[str, dict[str, object]] = {}
        self._versions: dict[str, str] = {}
        self._identities: dict[str, str] = {}

    # 注册工具；拒绝同名覆盖，避免外部工具替换内置权限边界
    def register(self, tool: BaseTool) -> None:
        if self._active is not None:
            raise ValueError("tool registry is frozen for deferred discovery")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._check_schema(tool.input_schema)
        validator = validator_for(tool.input_schema)
        validator.check_schema(tool.input_schema)
        self._validators[tool.name] = validator(tool.input_schema)
        self._tools[tool.name] = tool
        self._identities[tool.name] = tool.security_identity()
        self._validators[tool.name] = validator(deepcopy(tool.input_schema))

    # 禁止外部引用与不可信正则，避免远程 Schema 读取或消耗宿主 CPU
    def _check_schema(self, schema: object, depth: int = 0) -> None:
        if depth > 32:
            raise ValueError("tool schema exceeds nesting limit")
        if isinstance(schema, dict):
            reference = schema.get("$ref")
            if reference is not None and not str(reference).startswith("#"):
                raise ValueError("remote schema references are forbidden")
            if "pattern" in schema or "patternProperties" in schema:
                raise ValueError("untrusted schema regex is forbidden")
            for value in schema.values():
                self._check_schema(value, depth + 1)
        elif isinstance(schema, list):
            for value in schema:
                self._check_schema(value, depth + 1)

    # 在授权之前验证外部工具的原始 JSON 输入，拒绝缺失和类型错误
    def validate(self, name: str, params: dict[str, object]) -> None:
        if name in self._tools and self._tools[name].security_identity() != self._identities[name]:
            raise ValueError("tool schema or identity changed during run; rebuild registry")
        if self._active is not None and name in self._snapshots:
            tool = self._tools[name]
            if (
                tool.input_schema != self._snapshots[name]["input_schema"]
                or tool.version != self._versions[name]
            ):
                raise ValueError("tool schema changed during run; rebuild registry")
        validator = self._validators.get(name)
        if validator is None:
            return
        error = next(validator.iter_errors(params), None)
        if error is not None:
            raise ValueError(str(error.message))

    # 导出工具版本、风险类别和幂等性用于审计
    def manifest(self) -> list[dict[str, object]]:
        return [
            {
                "name": t.name,
                "version": t.version,
                "risk_level": t.risk_level,
                "idempotent": t.idempotent,
            }
            for t in self._tools.values()
        ]

    # 按名称查找工具，不存在返回 None
    def get(self, name: str) -> BaseTool | None:
        if self._active is not None and name not in self._active:
            return None
        return self._tools.get(name)

    # 在单个运行内冻结授权集合，已选择工具只增不减以稳定缓存
    def enable_deferred(self, always_loaded: set[str]) -> None:
        if self._active is not None:
            raise ValueError("deferred registry already configured")
        self._snapshots = {str(schema["name"]): deepcopy(schema) for schema in self.tool_schemas()}
        self._active = set(self._tools).intersection(always_loaded)
        self._versions = {name: tool.version for name, tool in self._tools.items()}
        for name, schema in self._snapshots.items():
            validator = validator_for(schema["input_schema"])
            self._validators[name] = validator(deepcopy(schema["input_schema"]))

    # 仅在已有授权工具中做确定性排序搜索，不暴露未注册服务器
    def discover(self, query: str, limit: int = 8) -> list[dict[str, object]]:
        words = query.casefold().split()[:16]
        ranked = []
        for name, tool in self._tools.items():
            if name == "tool_search":
                continue
            haystack = (name + " " + tool.description).casefold()
            score = sum(word in haystack for word in words)
            if words and not score:
                continue
            ranked.append(
                (
                    score,
                    name,
                    {
                        "name": name,
                        "description": tool.description[:240],
                        "risk_level": tool.risk_level,
                        "available": True,
                        "loaded": self._active is None or name in self._active,
                    },
                )
            )
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [entry for _, _, entry in ranked[: max(1, min(limit, 8))]]

    # 按确定名称解锁本轮工具，返回稳定版本摘要而非更改权限
    def resolve_tools(self, names: list[str]) -> list[dict[str, str]]:
        if len(names) > 8 or any(name not in self._tools for name in names):
            raise ValueError("unknown or unauthorized tool")
        if self._active is not None:
            self._active.update(names)
        result = []
        for name in sorted(set(names)):
            schema = self._snapshots.get(name)
            if schema is None:
                schema = next(s for s in self.tool_schemas() if s["name"] == name)
            version = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
            result.append({"name": name, "schema_hash": version})
        return result

    # 返回已选择工具的稳定 Schema，未启用延迟发现时保持旧接口
    def tool_schemas(self) -> list[dict[str, object]]:
        if self._active is not None:
            return [deepcopy(self._snapshots[name]) for name in sorted(self._active)]
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in self._tools.values()
        ]
