from __future__ import annotations

import re
from typing import Any

_KEY = re.compile(
    r"(?i)^(?:api[_-]?key|token|secret|password|authorization|credential|"
    r".*_(?:token|secret|password|api_key))$"
)
_VALUE = re.compile(r"sk-(?:ant-)?[A-Za-z0-9_-]{8,}|(?i:bearer)\s+[A-Za-z0-9._-]+")
_ASSIGN = re.compile(
    r"(?i)((?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)


# 递归清除敏感字段和文本中的常见密钥，保持事件关联键不变
def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]"
            if _KEY.search(str(k)) and not (k == "token" and value.get("type") == "llm.token")
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _ASSIGN.sub(r"\1[REDACTED]", _VALUE.sub("[REDACTED]", value))
    return value
