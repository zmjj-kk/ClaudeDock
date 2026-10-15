from __future__ import annotations

import re

from kama_claude.core.rag.filter import MetaValue

# 围栏代码块起始/结束行（``` 或 ~~~，可带语言标识）
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# Markdown 标题行（1-6 级，标题文字非空）
_HEADING_RE = re.compile(r"^#{1,6}\s+\S")
# 句末标点：中文标点后直接切，英文标点后须跟空白或行尾以避免误拆小数/缩写
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？])\s*|(?<=[.!?])(?=\s|$)")
# frontmatter 键值行：key: value（key 为字母/数字/下划线/连字符）
_FRONTMATTER_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.+)$")


# 将长文本按固定大小切块，带固定重叠；优先在行边界切断，避免把表格/代码行切成两半
def _split_chunks(text: str, size: int, overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            # 回退到最近的行边界；若单行本身超过 size 则维持字符硬切
            nl = text.rfind("\n", start, end)
            if nl > start:
                end = nl + 1
        chunks.append(text[start:end].rstrip("\n"))
        if end >= n:
            break
        start = max(start + 1, end - overlap)
    return chunks


# 将文档逐行扫描为结构块，返回 (kind, text) 列表，kind ∈ {"heading","code","para"}
def _structural_blocks(text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    lines = text.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if _FENCE_RE.match(stripped):
            # 收集围栏内的所有行，代码块不参与句子切分
            buf = [line]
            i += 1
            while i < n and not _FENCE_RE.match(lines[i].strip()):
                buf.append(lines[i])
                i += 1
            if i < n:
                buf.append(lines[i])
                i += 1
            blocks.append(("code", "\n".join(buf)))
            continue
        if _HEADING_RE.match(stripped):
            blocks.append(("heading", line))
            i += 1
            continue
        # 普通段落：收集连续非空、非结构行
        buf = [line]
        i += 1
        while i < n:
            s = lines[i].strip()
            if not s or _FENCE_RE.match(s) or _HEADING_RE.match(s):
                break
            buf.append(lines[i])
            i += 1
        blocks.append(("para", "\n".join(buf)))
    return blocks


# 将段落按句末标点切分为句子列表
def _split_sentences(paragraph: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT_RE.split(paragraph)]
    return [p for p in parts if p]


# 将结构块进一步单元化：标题/代码块整体保留，超长段落按句切分，仍超长则硬切
def _block_units(block: tuple[str, str], size: int, overlap: int) -> list[str]:
    kind, text = block
    if kind in ("heading", "code"):
        return _split_chunks(text, size, overlap)
    # 段落：未超长直接一个单元，超长则先按句切，单句仍超长再硬切
    if len(text) <= size:
        return [text]
    units: list[str] = []
    for sentence in _split_sentences(text):
        units.extend(_split_chunks(sentence, size, overlap))
    return units


# 贪心合并相邻单元到 size 上限；overlap>0 时每块携带上一块尾部上下文，避免边界语义断裂
def _merge_units(units: list[str], size: int, overlap: int) -> list[str]:
    overlap = max(0, min(overlap, size - 1))
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for unit in units:
        unit = unit.strip()
        if not unit:
            continue
        if current and current_len + 1 + len(unit) > size:
            chunks.append("\n".join(current))
            # 下一块以上一块尾部 overlap 字符作为上下文前缀（计入 size 预算）
            prefix = chunks[-1][-overlap:] if overlap > 0 else ""
            current = [prefix] if prefix else []
            current_len = len(prefix)
        if current:
            current.append(unit)
            current_len += len(unit) + 1
        else:
            current.append(unit)
            current_len = len(unit)
    if current:
        chunks.append("\n".join(current))
    return chunks


# 语义边界切片：结构块 → 单元 → 合并，避免在句子/段落中间切断语义
def split_semantic(text: str, size: int, overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    units: list[str] = []
    for block in _structural_blocks(text):
        units.extend(_block_units(block, size, overlap))
    return _merge_units(units, size, overlap)


# 语义切片并保留章节上下文：返回 (chunk 文本, 所属标题)；
# 命中块自带标题时文本不变，否则前置所属上级标题，使孤立片段可判归属
def split_with_context(text: str, size: int, overlap: int) -> list[tuple[str, str]]:
    text = text.strip()
    if not text:
        return []
    # 逐块收集 (单元文本, 生效标题, 是否标题块)，标题块同时刷新生效标题
    units: list[tuple[str, str, bool]] = []
    heading = ""
    for kind, block in _structural_blocks(text):
        if kind == "heading":
            heading = block.strip()
        for unit in _block_units((kind, block), size, overlap):
            units.append((unit, heading, kind == "heading"))
    return _merge_with_context(units, size, overlap)


# 贪心合并带标题的单元，输出 (chunk 文本, 首单元所属标题)；标题不在块首时才补前缀
def _merge_with_context(
    units: list[tuple[str, str, bool]], size: int, overlap: int
) -> list[tuple[str, str]]:
    overlap = max(0, min(overlap, size - 1))
    merged: list[tuple[str, str]] = []
    current: list[str] = []
    current_len = 0
    current_heading = ""
    for unit, heading, _is_heading in units:
        unit = unit.strip()
        if not unit:
            continue
        if current and current_len + 1 + len(unit) > size:
            merged.append(("\n".join(current), current_heading))
            # 下一块以上一块尾部 overlap 字符作为上下文前缀（计入 size 预算）
            prefix = merged[-1][0][-overlap:] if overlap > 0 else ""
            current = [prefix] if prefix else []
            current_len = len(prefix)
        if not current:
            current_heading = heading
        current.append(unit)
        current_len += len(unit) + 1
    if current:
        merged.append(("\n".join(current), current_heading))
    return [(_with_heading(body, h), h) for body, h in merged]


# 若块首不是所属标题行则前置该标题，返回带上下文的块文本
def _with_heading(body: str, heading: str) -> str:
    if not heading:
        return body
    first_line = body.split("\n", 1)[0].strip()
    if first_line == heading:
        return body
    return f"{heading}\n{body}"


# 解析 Markdown frontmatter（--- 包着的 key: value 块）；值按标量或列表推断，无或格式错返回空
def parse_frontmatter(text: str) -> dict[str, MetaValue]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    meta: dict[str, MetaValue] = {}
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        m = _FRONTMATTER_KEY_RE.match(stripped)
        if m:
            raw = m.group(2).strip()
            items = _coerce_list(raw)
            meta[m.group(1)] = items if items is not None else _coerce(raw)
    return meta


# 去掉文档开头的 frontmatter 块，返回正文；无 frontmatter 或未闭合时原样返回
def strip_frontmatter(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[i + 1 :]).strip()
    return text


# 解析 frontmatter 的列表写法（[a, b]）为字符串列表；非列表写法返回 None
def _coerce_list(raw: str) -> list[str] | None:
    if not (raw.startswith("[") and raw.endswith("]")):
        return None
    inner = raw[1:-1].strip()
    if not inner:
        return []
    return [p.strip().strip("'\"") for p in inner.split(",") if p.strip()]


# 把 frontmatter 原始字符串值推断为标量：bool / int / float，否则保持字符串
def _coerce(raw: str) -> MetaValue:
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


# 提取文档的主题标签（仅用于 manifest 清单展示，不作过滤条件）：各级标题文本，无标题退回首行
def extract_topics(text: str) -> list[str]:
    topics: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        m = _HEADING_RE.match(stripped)
        if m:
            # 去掉前导 # 与随后的空白，保留标题文字
            topics.append(stripped.lstrip("#").strip())
    if not topics:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("---"):
                topics.append(stripped[:80])
                break
    return topics
