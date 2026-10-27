"""密钥管理：API key 脱敏、轮换、安全存储。"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# 脱敏敏感字符串（API key、token 等）
def mask_secret(secret: str, visible_chars: int = 4) -> str:
    """将敏感字符串脱敏，仅保留前几个字符

    Args:
        secret: 原始密钥
        visible_chars: 保留可见字符数

    Returns:
        脱敏后的字符串（例如 "sk-ant-***"）
    """
    if len(secret) <= visible_chars:
        return "***"
    return secret[:visible_chars] + "***"


class SecretManager:
    """密钥管理器：自动检测和脱敏敏感信息"""

    # 常见 API key 模式
    SECRET_PATTERNS = [
        (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "sk-ant-***"),  # Anthropic
        (re.compile(r"sk-(?!ant-)[A-Za-z0-9_-]+"), "sk-***"),  # OpenAI / 其它 sk- 前缀
        (re.compile(r"Bearer [A-Za-z0-9-._~+/]+=*"), "Bearer ***"),  # OAuth tokens
        (re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"), "***"),  # Base64 编码的密钥
    ]

    @classmethod
    def sanitize(cls, data: Any) -> Any:
        """递归脱敏数据结构中的敏感信息

        Args:
            data: 任意数据结构（dict、list、str 等）

        Returns:
            脱敏后的数据副本
        """
        if isinstance(data, dict):
            return {k: cls.sanitize(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [cls.sanitize(item) for item in data]
        elif isinstance(data, str):
            return cls._mask_string(data)
        return data

    @classmethod
    def _mask_string(cls, text: str) -> str:
        """检测并脱敏字符串中的密钥"""
        for pattern, replacement in cls.SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    @classmethod
    def check_api_key_validity(cls, api_key: str) -> bool:
        """检查 API key 格式是否有效（不验证真实性）"""
        # Anthropic API key 格式检查
        if api_key.startswith("sk-ant-"):
            return len(api_key) > 20
        return False

    @classmethod
    def log_api_key_rotation(cls, old_key_prefix: str, new_key_prefix: str) -> None:
        """记录 API key 轮换事件到审计日志"""
        logger.info(
            "API key rotated",
            extra={
                "old_key": mask_secret(old_key_prefix),
                "new_key": mask_secret(new_key_prefix),
                "action": "rotate_api_key",
            },
        )
