from __future__ import annotations


class PromptText(str):
    stable_prefix: str
    dynamic_suffix: str

    # 保持旧提供者的字符串接口，同时为支持缓存的提供者提供明确分段
    def __new__(cls, stable_prefix: str, dynamic_suffix: str) -> PromptText:
        instance = super().__new__(cls, stable_prefix + dynamic_suffix)
        instance.stable_prefix = stable_prefix
        instance.dynamic_suffix = dynamic_suffix
        return instance
