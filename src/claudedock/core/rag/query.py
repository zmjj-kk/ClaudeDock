from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod

from claudedock.core.events.bus import EventBus
from claudedock.core.llm.base import LLMProvider

# 逐行解析时去掉的项目符号前缀（Markdown 无序列表）
_BULLET_RE = re.compile(r"^[-*•·]\s*")
# 逐行解析时去掉的数字编号前缀（"1." "2、" "3)" "4）"）
_NUMBER_RE = re.compile(r"^\d+[.、)）]\s*")


class QueryProcessor(ABC):
    # 对原始 query 做检索前变换，返回 1..N 条子查询（多查询即拓展）
    @abstractmethod
    async def transform(self, query: str) -> list[str]: ...


# 查询优化：统一空白规范化，压缩首尾与内部连续空白为单个空格
def optimize_query(query: str) -> str:
    return " ".join(query.split())


# 解析 LLM 输出为查询列表：优先按 JSON 数组，退化逐行（去围栏/项目符号/编号）
def parse_query_list(text: str) -> list[str]:
    raw = text.strip()
    if not raw:
        return []
    # 剥离 markdown 围栏，便于整体 JSON 解析
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
    # 去掉首行语言标签（如 "json"）
    if raw and raw[0] not in "[{" and "\n" in raw:
        raw = raw.split("\n", 1)[1].strip()
    # 优先按 JSON 字符串数组解析
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, list):
        return [str(x).strip() for x in data if str(x).strip()]
    # 退化为逐行：去项目符号与编号
    out: list[str] = []
    for line in text.splitlines():
        s = _NUMBER_RE.sub("", _BULLET_RE.sub("", line.strip())).strip()
        if s and not s.startswith("```"):
            out.append(s)
    return out


# 去重并按 limit 截断；首项保持原始查询，其后为拓展查询
def _dedupe_queries(queries: list[str], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for q in queries:
        q = optimize_query(q)
        if q and q not in seen:
            seen.add(q)
            out.append(q)
        if len(out) >= limit:
            break
    return out


# 构造查询改写+拓展的提示词：要求只输出 JSON 字符串数组
def _prompt(expand_n: int, query: str) -> str:
    return (
        "你是检索查询优化器。针对给定的原始查询，生成 "
        f"{expand_n} 条用于知识库检索的改写/拓展查询：\n"
        "1. 第 1 条：把口语化查询改写成更利于检索的书面查询"
        "（补齐关键信息、保留专有名词与术语）；\n"
        "2. 其余各条：从不同角度、同义表达、可能的子问题出发做拓展。\n"
        "只输出一个 JSON 字符串数组，不要任何解释、编号或多余文本。\n"
        f"原始查询：{query}"
    )


class LlmQueryProcessor(QueryProcessor):
    # 初始化 LLM 查询处理器：provider 复用项目 LLMProvider 协议，expand_n 为拓展条数
    def __init__(self, provider: LLMProvider, *, expand_n: int = 3) -> None:
        self._provider = provider
        self._expand_n = max(1, expand_n)

    # 调用 LLM 生成改写/拓展查询；异常或解析失败安全回退为原始查询，绝不阻断检索
    async def transform(self, query: str) -> list[str]:
        base = optimize_query(query)
        variants: list[str] = []
        try:
            bus = EventBus()
            resp = await self._provider.chat(
                messages=[{"role": "user", "content": _prompt(self._expand_n, query)}],
                tool_schemas=[],
                bus=bus,
                run_id="rag-query-enhance",
                system="You are a retrieval query optimizer. Output only a JSON array of strings.",
            )
            variants = parse_query_list(resp.text)
        except Exception:
            variants = []
        # 原始查询始终保留在首位，其后追加 LLM 拓展，去重并限幅
        return _dedupe_queries([base, *variants], limit=1 + self._expand_n)
