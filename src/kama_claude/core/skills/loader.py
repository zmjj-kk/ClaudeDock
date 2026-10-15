from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class Skill:
    name: str
    description: str
    system_prompt_template: str
    allowed_tools: list[str] = field(default_factory=list)
    # skill 所在目录（目录式 skill 才有），用于安全解析资源文件
    directory: Path | None = None
    # 可渐进加载的资源文件路径（相对 skill 目录，如 references/xxx.md）
    resources: list[str] = field(default_factory=list)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# 扫描 skill 目录内除 SKILL.md 之外的所有资源文件，返回相对路径列表
def _scan_resources(directory: Path) -> list[str]:
    return sorted(
        p.relative_to(directory).as_posix()
        for p in directory.rglob("*")
        if p.is_file()
        and p.name != "SKILL.md"
        and p.resolve().is_relative_to(directory.resolve())
        and p.stat().st_size <= 1024 * 1024
    )


# 解析 Markdown skill 文件，提取 frontmatter 和正文 system prompt
def _parse_skill_file(path: Path) -> Skill:
    if path.stat().st_size > 256 * 1024:
        raise ValueError("skill file exceeds 256 KiB")
    text = path.read_text(encoding="utf-8")
    name = path.stem
    description = ""
    allowed_tools: list[str] = []
    body = text

    m = _FRONTMATTER_RE.match(text)
    if m:
        front = m.group(1)
        body = text[m.end() :]
        lines = front.splitlines()
        i = 0
        in_allowed_tools = False
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if stripped.startswith("name:"):
                name = stripped[len("name:") :].strip().strip('"').strip("'")
                in_allowed_tools = False
            elif stripped.startswith("description:"):
                val = stripped[len("description:") :].strip().strip('"').strip("'")
                in_allowed_tools = False
                # YAML 块标量：> (折叠) 或 | (保留换行)，后续缩进行是内容
                if val in (">", "|"):
                    fold = val == ">"
                    parts: list[str] = []
                    i += 1
                    while i < len(lines) and (
                        lines[i].startswith(" ") or lines[i].startswith("\t")
                    ):
                        parts.append(lines[i].strip())
                        i += 1
                    description = (" ".join(parts) if fold else "\n".join(parts)).strip()
                    continue
                else:
                    description = val
            elif stripped.startswith("allowed_tools:"):
                val = stripped[len("allowed_tools:") :].strip()
                in_allowed_tools = False
                if val:
                    # 内联列表 allowed_tools: [a, b] 或单值 allowed_tools: read_file
                    if val.startswith("[") and val.endswith("]"):
                        allowed_tools = [
                            x.strip().strip('"').strip("'")
                            for x in val[1:-1].split(",")
                            if x.strip()
                        ]
                    else:
                        allowed_tools = [val.strip().strip('"').strip("'")]
                else:
                    # 块列表：后续缩进的 "- item" 行都属于 allowed_tools
                    in_allowed_tools = True
            elif in_allowed_tools and stripped.startswith("- "):
                allowed_tools.append(stripped[2:].strip())
            else:
                in_allowed_tools = False
            i += 1

    return Skill(
        name=name,
        description=description,
        system_prompt_template=body.strip(),
        allowed_tools=allowed_tools,
    )


# 按三级优先级（项目本地 > 用户全局 > 内建）查找并解析 skill
class SkillLoader:
    _BUILTIN_DIR = Path(__file__).parent / "builtin"

    # 按优先级查找 skill 文件；目录式 skill 附加 directory 与资源清单；未找到返回 None
    def resolve(self, name: str) -> Skill | None:
        for path in self._search_paths(name):
            if path.exists():
                try:
                    skill = _parse_skill_file(path)
                except Exception:
                    log.warning("Failed to parse skill %s; trying next path", path)
                    continue
                # 规范名以文件名为准，忽略 frontmatter 里可能不一致的 name 字段
                skill.name = name
                if path.name == "SKILL.md":
                    skill.directory = path.parent
                    skill.resources = _scan_resources(path.parent)
                return skill
        return None

    # 返回候选路径列表，同时支持扁平文件（name.md）和目录式（name/SKILL.md）两种格式
    def _search_paths(self, name: str) -> list[Path]:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("invalid skill name")
        dirs = [
            Path(".kama/skills"),
            Path("~/.kama/skills").expanduser(),
            self._BUILTIN_DIR,
        ]
        paths: list[Path] = []
        for d in dirs:
            paths.append(d / f"{name}.md")
            paths.append(d / name / "SKILL.md")
        return paths

    # 列出所有可用 Skill 对象（含描述），项目本地覆盖同名内建；规范名以文件名为准
    def list_all_skills(self) -> list[Skill]:
        seen: dict[str, Skill] = {}
        for d in [
            self._BUILTIN_DIR,
            Path("~/.kama/skills").expanduser(),
            Path(".kama/skills"),
        ]:
            if d.exists():
                for f in sorted(d.glob("*.md")):
                    try:
                        skill = _parse_skill_file(f)
                        skill.name = f.stem
                        if skill.name in seen:
                            log.warning("skill conflict: %s; using directory priority", skill.name)
                        seen[skill.name] = skill
                    except Exception:
                        log.warning("Failed to parse skill %s; skipping", f)
                for f in sorted(d.glob("*/SKILL.md")):
                    try:
                        skill = _parse_skill_file(f)
                        skill.name = f.parent.name
                        skill.directory = f.parent
                        skill.resources = _scan_resources(f.parent)
                        if skill.name in seen:
                            log.warning("skill conflict: %s; using directory priority", skill.name)
                        seen[skill.name] = skill
                    except Exception:
                        log.warning("Failed to parse skill %s; skipping", f)
        return list(seen.values())

    # 生成紧凑技能索引：每项一行 name + description，供启动时注入 system prompt
    def skill_index(self) -> list[str]:
        return [f"{s.name}: {s.description}" for s in self.list_all_skills()]

    # 将 $ARGUMENTS 替换为用户传入的参数字符串
    def render_prompt(self, skill: Skill, arguments: str) -> str:
        return skill.system_prompt_template.replace("$ARGUMENTS", arguments)
