"""测试安全模块：路径验证、密钥脱敏、审计日志。"""

from __future__ import annotations

import json
from pathlib import Path

from claudedock.core.security import (
    AuditLogger,
    PathValidator,
    SecretManager,
    is_safe_path,
    mask_secret,
)


# 功能：验证 mask_secret 正确脱敏 API key
# 设计：测试不同长度的密钥，确保仅保留前 N 个字符，其余替换为 ***
def test_mask_secret_truncates_correctly() -> None:
    assert mask_secret("sk-ant-1234567890abcdef", visible_chars=7) == "sk-ant-***"
    assert mask_secret("short", visible_chars=4) == "shor***"
    assert mask_secret("abc", visible_chars=4) == "***"


# 功能：验证 SecretManager 递归脱敏字典中的敏感信息
# 设计：构造包含 API key 的嵌套字典，断言脱敏后不含原始密钥
def test_secret_manager_sanitize_dict() -> None:
    data = {
        "api_key": "sk-ant-1234567890abcdefghij",
        "nested": {
            "token": "Bearer abc123def456ghi789",
            "safe": "normal text",
        },
    }
    sanitized = SecretManager.sanitize(data)
    assert sanitized["api_key"] == "sk-ant-***"
    assert sanitized["nested"]["token"] == "Bearer ***"
    assert sanitized["nested"]["safe"] == "normal text"


# 功能：验证 PathValidator 阻止路径遍历攻击
# 设计：尝试访问 base 目录外的路径，断言验证失败
def test_path_validator_blocks_traversal(tmp_path: Path) -> None:
    validator = PathValidator(allowed_base=tmp_path)
    is_safe, reason = validator.validate(tmp_path / ".." / ".." / "etc" / "passwd")
    assert not is_safe
    assert "escapes working directory" in reason


# 功能：验证 PathValidator 允许访问 base 目录内的合法路径
# 设计：访问 base 目录内的文件，断言验证通过
def test_path_validator_allows_safe_path(tmp_path: Path) -> None:
    safe_file = tmp_path / "test.txt"
    safe_file.write_text("test")
    validator = PathValidator(allowed_base=tmp_path)
    is_safe, reason = validator.validate(safe_file)
    assert is_safe
    assert reason == "OK"


# 功能：验证 PathValidator 阻止访问受限目录
# 设计：尝试访问 /etc（受限目录），断言验证失败
def test_path_validator_blocks_restricted_paths() -> None:
    validator = PathValidator(allowed_base=Path("/tmp"))
    is_safe, _ = validator.validate("/etc/passwd")
    assert not is_safe


# 功能：验证 is_safe_path 便捷函数正确工作
# 设计：使用临时目录作为 base，测试安全和不安全路径
def test_is_safe_path_convenience_function(tmp_path: Path) -> None:
    assert is_safe_path(tmp_path / "test.txt", base=tmp_path)
    assert not is_safe_path("/etc/passwd", base=tmp_path)


# 功能：验证 AuditLogger 正确记录审计事件到文件
# 设计：创建临时审计日志文件，写入事件后读取验证 JSON 格式
def test_audit_logger_writes_event(tmp_path: Path) -> None:
    log_file = tmp_path / "audit.log"
    logger = AuditLogger(log_file=log_file)

    logger.log_permission_decision(
        session_id="sess_123",
        tool_name="bash",
        decision="allow",
        reason="user_approved",
    )

    # 验证日志文件已创建且包含 JSON 行
    assert log_file.exists()
    lines = log_file.read_text().strip().split("\n")
    assert len(lines) == 1

    event = json.loads(lines[0])
    assert event["type"] == "permission_grant"
    assert event["session_id"] == "sess_123"
    assert event["details"]["tool"] == "bash"


# 功能：验证 AuditLogger 记录工具调用时自动脱敏参数
# 设计：调用 log_tool_call 传入含敏感信息的参数，验证日志中已脱敏
def test_audit_logger_sanitizes_tool_params(tmp_path: Path) -> None:
    log_file = tmp_path / "audit.log"
    logger = AuditLogger(log_file=log_file)

    logger.log_tool_call(
        session_id="sess_456",
        tool_name="api_call",
        params={"api_key": "sk-ant-secret123456"},
        outcome="success",
    )

    event = json.loads(log_file.read_text().strip())
    # 验证参数已脱敏
    assert event["details"]["params"]["api_key"] == "[REDACTED]"


# 功能：验证 SecretManager 识别并脱敏多种 API key 格式
# 设计：测试 Anthropic、OpenAI、OAuth token 等常见格式
def test_secret_manager_detects_multiple_formats() -> None:
    text = "Keys: sk-ant-abc123 and sk-proj-xyz789 and Bearer token123"
    sanitized = SecretManager.sanitize(text)
    assert "sk-ant-***" in sanitized
    assert "sk-***" in sanitized
    assert "Bearer ***" in sanitized
    assert "abc123" not in sanitized
    assert "xyz789" not in sanitized
