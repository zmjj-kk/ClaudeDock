from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kama_claude.core.compact.budget import truncate_tool_results

if TYPE_CHECKING:
    from kama_claude.core.compact.compactor import CompactionResult, Compactor

__all__ = ["Compactor", "CompactionResult", "truncate_tool_results"]


# 延迟导出压缩器，避免纯预算或提示分段导入触发运行时循环依赖
def __getattr__(name: str) -> Any:
    if name in {"Compactor", "CompactionResult"}:
        from kama_claude.core.compact import compactor

        return getattr(compactor, name)
    raise AttributeError(name)
