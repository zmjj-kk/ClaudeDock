from __future__ import annotations

import pytest

from claudedock.core.compact.token_budget import ModelCapacity, check_budget, estimate_tokens
from claudedock.core.config import CompactionConfig
from claudedock.core.llm.context_errors import is_context_error


# 功能：验证工具模式与系统提示全部计入窗口，输出预留独立扣减
# 设计：工具定义占主要容量，使仅统计历史的错误算法必然漏报
def test_schemas_and_output_reservation() -> None:
    report = check_budget([], [{"description": "x" * 1000}], "rules", ModelCapacity(1200, 200))
    assert report.over_limit
    assert report.tools_tokens > 1000
    assert report.max_input == 940


# 功能：验证中文与未知模型不会被误判为低占用
# 设计：使用 UTF-8 多字节文本与空容量声明覆盖两种危险回退
def test_unicode_and_unknown_window() -> None:
    assert estimate_tokens("中文" * 100) >= 600
    assert check_budget([], [], "", ModelCapacity(None)).over_limit


# 功能：验证非法水位、长度和恢复次数配置被拒绝
# 设计：参数化边界配置避免错误参数导致无界恢复或负数预算
@pytest.mark.parametrize("values", [
    {"low_watermark": 0.9}, {"reactive_max_retries": 3},
    {"recent_turns_min": 0}, {"model_context_window": -1},
])
# 功能：验证非法水位、长度和恢复次数配置被拒绝
# 设计：参数化边界配置避免错误参数导致无界恢复或负数预算
def test_invalid_configuration(values: dict[str, object]) -> None:
    config = CompactionConfig()
    for key, value in values.items():
        setattr(config, key, value)
    with pytest.raises(ValueError):
        config.validate()


# 功能：验证仅明确容量错误触发恢复，普通参数与鉴权错误不触发
# 设计：对供应商常见错误体做正负对照，杜绝所有四百错误统一压缩
def test_context_error_classification() -> None:
    assert is_context_error({"error": {"type": "context_window_exceeded"}})
    assert is_context_error({"error": {"type": "invalid_request_error", "message": "prompt is too long: 201 > 200"}})
    assert not is_context_error({"error": {"type": "invalid_request_error", "message": "invalid tools"}})
    assert not is_context_error({"error": {"type": "authentication_error"}})
