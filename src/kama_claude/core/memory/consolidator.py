from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from kama_claude.core.config import MemoryConfig
from kama_claude.core.memory.model import MemoryEntry, MemoryStatus, new_memory_id

log = logging.getLogger(__name__)

# 单次送入 LLM 审查的条目上限，超出则按更新倒序截断，避免 token 爆炸
_MAX_REVIEW = 200

# 整合提示词：要求 LLM 只输出 JSON 对象，含去重合并、冲突消解、抽象提炼三类操作
_CONSOLIDATE_PROMPT = """\
你是长期记忆库的整合器。审查下面每条记忆（含 id / kind / project /
importance / tags / status / updated_at / content），输出一个 JSON 对象
（不要任何解释），结构如下：
{"merge": [{"ids": ["...", "..."], "content": "合并后的完整表述",
"kind": "...", "importance": 0.5, "tags": ["..."]}],
 "retire": [{"id": "...", "status": "superseded"}]}
规则：
1. merge：语义相同或互补的多条记忆合并为一条更完整的表述，保留更高 importance
   与并集 tags；被合并的 id 会自动标记为 superseded。
2. retire：冲突消解与衰减——矛盾或过时的记忆标记为 superseded（被更新版本取代）
   或 expired（已失效）；保留 updated_at 最新的一条，旧的退休。
3. 不要生成新记忆（抽象提炼不新增条目，只在 merge 里升华表述）；没有需要操作时
   输出 {"merge": [], "retire": []}。
只输出 JSON，不要任何解释。
"""


@dataclass
class ConsolidationResult:
    # 合并/升华后的新条目，替换被合并的多条
    replace: list[MemoryEntry] = field(default_factory=list)
    # 需退休的条目：id → 新状态（superseded/expired）
    retire: list[tuple[str, MemoryStatus]] = field(default_factory=list)
    # 需物理删除的残留条目 id（合并后清理）
    delete: list[str] = field(default_factory=list)


class Consolidator:
    # 初始化整合器，绑定衰减/整合相关配置
    def __init__(self, config: MemoryConfig) -> None:
        self._config = config

    # 调用 LLM 审查记忆并返回整合结果；调用失败或空响应时返回 None 不阻断
    async def consolidate(
        self, provider: object, entries: list[MemoryEntry]
    ) -> ConsolidationResult | None:
        active = [e for e in entries if e.status == "active"]
        if not active:
            return None
        review = active[:_MAX_REVIEW]
        payload = "\n".join(
            json.dumps(e.to_dict(), ensure_ascii=False) for e in review
        )
        try:
            from kama_claude.core.events.bus import EventBus

            response = await provider.chat(  # type: ignore[attr-defined]
                messages=[
                    {"role": "user", "content": f"{_CONSOLIDATE_PROMPT}\n\n{payload}"}
                ],
                tool_schemas=[],
                bus=EventBus(),
                run_id="memory-consolidate",
                system="You are a memory consolidation assistant. Output only JSON.",
            )
        except Exception:
            log.exception("consolidator: LLM call failed, skipping consolidation")
            return None

        plan = _parse_plan(response.text)
        if plan is None:
            return None
        return self._apply_plan(plan, review)

    # 把 LLM 输出的整合计划翻译为可落库的操作，并校验 id 只引用本次审查过的条目
    def _apply_plan(
        self, plan: dict[str, object], reviewed: list[MemoryEntry]
    ) -> ConsolidationResult:
        by_id = {e.id: e for e in reviewed}
        result = ConsolidationResult()

        merged_ids: set[str] = set()
        merge_items = plan.get("merge", [])
        if not isinstance(merge_items, list):
            merge_items = []
        for item in merge_items:
            if not isinstance(item, dict):
                continue
            ids = [str(x) for x in item.get("ids", []) if str(x) in by_id]
            content = str(item.get("content", "")).strip()
            if not ids or not content or len(ids) < 2:
                continue
            src = [by_id[i] for i in ids]
            kind = item.get("kind") or src[0].kind
            importance = float(item.get("importance", max(e.importance for e in src)))
            tags_raw = item.get("tags", [])
            tags = list(dict.fromkeys(t for e in src for t in e.tags))
            if isinstance(tags_raw, list):
                tags = list(dict.fromkeys([str(t) for t in tags_raw] + tags))
            merged_ids |= set(ids)
            result.replace.append(
                MemoryEntry(
                    id=new_memory_id(content),
                    content=content,
                    kind=kind,
                    project=src[0].project,
                    importance=max(0.0, min(1.0, importance)),
                    tags=tags,
                    created_at=min(e.created_at for e in src),
                )
            )
            for i in ids:
                result.retire.append((i, "superseded"))
                result.delete.append(i)

        retire_items = plan.get("retire", [])
        if not isinstance(retire_items, list):
            retire_items = []
        for item in retire_items:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("id", ""))
            status = item.get("status", "superseded")
            if mid in by_id and status in ("superseded", "expired") and mid not in merged_ids:
                result.retire.append((mid, status))

        return result


# 解析 LLM 输出为整合计划 dict：剥离围栏、容忍前后噪声，失败返回 None
def _parse_plan(text: str) -> dict[str, object] | None:
    raw = text.strip()
    if not raw:
        return None
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return data
