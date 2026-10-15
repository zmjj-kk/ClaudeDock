from __future__ import annotations

from pathlib import Path

import pytest

from kama_claude.core.skills.loader import Skill, SkillLoader
from kama_claude.core.skills.tool import ReadSkillResourceTool, UseSkillTool


# 功能：skill_index 每项应为 "name: description" 格式，供启动时紧凑注入
# 设计：对每个内建 skill 断言索引行包含名字与描述，且不包含正文内容
def test_skill_index_format() -> None:
    loader = SkillLoader()
    index = loader.skill_index()
    assert index, "skill index should not be empty"
    for line in index:
        assert ": " in line
    review_line = next(line for line in index if line.startswith("review:"))
    assert "代码审查" in review_line or "review" in review_line.lower()
    assert "你是一位严格的代码审查员" not in review_line


# 功能：目录式 skill 应能解析出 directory 与 resources（渐进式加载第二层）
# 设计：写入 name/SKILL.md + references/x.md，断言 resources 含相对路径且 directory 正确
def test_directory_skill_resources(tmp_path: Path) -> None:
    skills_dir = tmp_path / ".kama" / "skills"
    skill_dir = skills_dir / "mydoc"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: mydoc\ndescription: doc skill\n---\nuse references $ARGUMENTS\n",
        encoding="utf-8",
    )
    (skill_dir / "references" / "guide.md").write_text("guide body", encoding="utf-8")

    import os

    cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        loader = SkillLoader()
        skill = loader.resolve("mydoc")
    finally:
        os.chdir(cwd)

    assert skill is not None
    assert skill.directory is not None
    assert skill.resources == ["references/guide.md"]


# 功能：UseSkillTool.invoke 应返回渲染后的正文，并附加 allowed_tools 建议与资源清单
# 设计：构造带 allowed_tools 与 resources 的 Skill，断言返回内容三部分齐全
@pytest.mark.asyncio
async def test_use_skill_tool_returns_body_and_hints() -> None:
    skill = Skill(
        name="review",
        description="code review",
        system_prompt_template="You are a reviewer: $ARGUMENTS",
        allowed_tools=["read_file", "bash"],
        resources=["references/checklist.md"],
    )

    class _FakeLoader:
        def resolve(self, name: str) -> Skill | None:
            return skill if name == "review" else None

        def render_prompt(self, s: Skill, arguments: str) -> str:
            return s.system_prompt_template.replace("$ARGUMENTS", arguments)

    tool = UseSkillTool(_FakeLoader())  # type: ignore[arg-type]
    result = await tool.invoke({"name": "review", "arguments": "src/app.py"})
    assert not result.is_error
    assert "You are a reviewer: src/app.py" in result.content
    assert "read_file" in result.content and "bash" in result.content
    assert "references/checklist.md" in result.content


# 功能：UseSkillTool 对未知 skill 应返回 is_error=True 的 ToolResult
# 设计：用 resolve 返回 None 的 loader，断言错误结果而非抛异常
@pytest.mark.asyncio
async def test_use_skill_tool_unknown() -> None:
    class _EmptyLoader:
        def resolve(self, name: str) -> Skill | None:
            return None

        def render_prompt(self, s: Skill, arguments: str) -> str:
            return ""

    tool = UseSkillTool(_EmptyLoader())  # type: ignore[arg-type]
    result = await tool.invoke({"name": "nope"})
    assert result.is_error
    assert result.error_type == "runtime_error"


# 功能：read_skill_resource 应拒绝读取未在资源清单中声明的文件（如 SKILL.md 本身）
# 设计：目录式 skill 仅有 notes.md 资源，读取 SKILL.md 应返回 "not declared" 错误
@pytest.mark.asyncio
async def test_read_skill_resource_rejects_undeclared(tmp_path: Path) -> None:
    import os

    skills_dir = tmp_path / ".kama" / "skills"
    skill_dir = skills_dir / "mydoc"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: mydoc\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    (skill_dir / "notes.md").write_text("hello resource", encoding="utf-8")

    cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        tool = ReadSkillResourceTool(SkillLoader())
        result = await tool.invoke({"name": "mydoc", "resource": "SKILL.md"})
    finally:
        os.chdir(cwd)

    assert result.is_error
    assert result.error_type == "runtime_error"
    assert "not declared" in result.content


# 功能：ReadSkillResourceTool 应读取 skill 目录内的资源文件内容
# 设计：构造真实目录式 skill，断言返回正文且路径受限在 skill 目录内
@pytest.mark.asyncio
async def test_read_skill_resource_reads_file(tmp_path: Path) -> None:
    import os

    skills_dir = tmp_path / ".kama" / "skills"
    skill_dir = skills_dir / "mydoc"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: mydoc\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    (skill_dir / "notes.md").write_text("hello resource", encoding="utf-8")

    cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        tool = ReadSkillResourceTool(SkillLoader())
        result = await tool.invoke({"name": "mydoc", "resource": "notes.md"})
    finally:
        os.chdir(cwd)

    assert not result.is_error
    assert result.content == "hello resource"


# 功能：ReadSkillResourceTool 应拒绝越出 skill 目录的路径
# 设计：用 ../ 路径请求，断言返回 is_error=True 且不读取目录外文件
@pytest.mark.asyncio
async def test_read_skill_resource_path_traversal(tmp_path: Path) -> None:
    import os

    skills_dir = tmp_path / ".kama" / "skills"
    skill_dir = skills_dir / "mydoc"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: mydoc\ndescription: d\n---\nbody\n", encoding="utf-8"
    )

    cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        tool = ReadSkillResourceTool(SkillLoader())
        result = await tool.invoke({"name": "mydoc", "resource": "../secret.txt"})
    finally:
        os.chdir(cwd)

    assert result.is_error
    assert result.error_type == "runtime_error"
