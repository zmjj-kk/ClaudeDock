from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kama_claude.core.permissions.policy import PermissionDecision


@dataclass(frozen=True)
class Rule:
    source: Literal["global", "user", "project", "session"]
    tool: str
    action: PermissionDecision
    argument_hash: str = ""

    # 规则仅匹配完整工具名和可选精确参数，不执行不可信正则表达式
    def matches(self, tool: str, argument_hash: str) -> bool:
        return fnmatch.fnmatchcase(tool, self.tool) and (
            not self.argument_hash or self.argument_hash == argument_hash
        )


# 下级来源只能收紧权限，所有 deny 优先，其次 ask，最后受信精确 allow
def evaluate_rules(
    rules: tuple[Rule, ...], tool: str, argument_hash: str
) -> PermissionDecision | None:
    matched = [rule for rule in rules if rule.matches(tool, argument_hash)]
    if any(rule.action == PermissionDecision.DENY for rule in matched):
        return PermissionDecision.DENY
    if any(rule.action == PermissionDecision.ASK for rule in matched):
        return PermissionDecision.ASK
    if any(
        rule.action == PermissionDecision.ALLOW
        and rule.source in {"global", "user"}
        and rule.argument_hash
        and rule.tool == tool
        for rule in matched
    ):
        return PermissionDecision.ALLOW
    return None


# 安全加载分层策略；格式错误中止启动，项目 allow 配置不能扩权
def load_rules(
    paths: tuple[tuple[Path, Literal["global", "user", "project", "session"]], ...],
) -> tuple[Rule, ...]:
    rules: list[Rule] = []
    for path, source in paths:
        if not path.exists():
            continue
        if path.is_symlink() or path.stat().st_mode & 0o022:
            raise ValueError("security policy cannot be a symlink")
        data = tomllib.loads(path.read_text())
        for item in data.get("rules", []):
            if not isinstance(item, dict) or set(item) - {"tool", "action", "argument_hash"}:
                raise ValueError("invalid policy rule")
            tool, action = item.get("tool"), item.get("action")
            argument_hash = item.get("argument_hash", "")
            if not isinstance(tool, str) or not tool or not isinstance(argument_hash, str):
                raise ValueError("invalid rule scope")
            if argument_hash and (
                len(argument_hash) != 64 or any(c not in "0123456789abcdef" for c in argument_hash)
            ):
                raise ValueError("invalid rule argument hash")
            rules.append(Rule(source, tool, PermissionDecision(str(action)), argument_hash))
    return tuple(rules)
