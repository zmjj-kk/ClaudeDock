from __future__ import annotations

from pathlib import Path

import pytest

from kama_claude.core.skills.loader import Skill, SkillLoader


# 功能：内建 review skill 应能被 SkillLoader 查找到
# 设计：直接调用 resolve("review")，不依赖文件系统之外的任何状态
def test_builtin_skill_found() -> None:
    loader = SkillLoader()
    skill = loader.resolve("review")
    assert skill is not None
    assert skill.name == "review"
    assert "审查" in skill.description or "review" in skill.description.lower()
    assert skill.system_prompt_template != ""


# 功能：内建 init / summarize / orchestrate skill 均可找到
# 设计：列举所有内建 skill 名，断言均能解析
@pytest.mark.parametrize("name", ["init", "review", "summarize", "orchestrate"])
def test_all_builtin_skills_found(name: str) -> None:
    loader = SkillLoader()
    skill = loader.resolve(name)
    assert skill is not None, f"builtin skill '{name}' not found"


# 功能：不存在的 skill 名应返回 None
# 设计：查找一个不存在的名称，断言 resolve 返回 None 而非抛异常
def test_unknown_skill_returns_none() -> None:
    loader = SkillLoader()
    result = loader.resolve("nonexistent_skill_xyz")
    assert result is None


# 功能：render_prompt 应将 $ARGUMENTS 替换为传入的参数字符串
# 设计：构造含 $ARGUMENTS 的 skill，验证 render_prompt 结果不含 "$ARGUMENTS" 且含参数值
def test_arguments_substituted() -> None:
    loader = SkillLoader()
    skill = Skill(
        name="test",
        description="test skill",
        system_prompt_template="Review this: $ARGUMENTS\nPlease be thorough.",
        allowed_tools=[],
    )
    rendered = loader.render_prompt(skill, "src/foo.py")
    assert "$ARGUMENTS" not in rendered
    assert "src/foo.py" in rendered


# 功能：frontmatter 中的 allowed_tools 列表应被正确解析
# 设计：构造含 allowed_tools 的 Markdown 文件，通过 _parse_skill_file 解析并验证结果
def test_frontmatter_parsed(tmp_path: Path) -> None:
    from kama_claude.core.skills.loader import _parse_skill_file

    content = """\
---
name: custom
description: 自定义 skill 测试
allowed_tools:
  - read_file
  - bash
---
你是一个测试助手，目标：$ARGUMENTS
"""
    p = tmp_path / "custom.md"
    p.write_text(content, encoding="utf-8")
    skill = _parse_skill_file(p)
    assert skill.name == "custom"
    assert skill.description == "自定义 skill 测试"
    assert "read_file" in skill.allowed_tools
    assert "bash" in skill.allowed_tools
    assert "$ARGUMENTS" in skill.system_prompt_template


# 功能：无 frontmatter 的 Markdown 文件仍可加载，allowed_tools 为空列表
# 设计：写入纯正文 Markdown，断言解析成功且 allowed_tools=[]
def test_no_frontmatter(tmp_path: Path) -> None:
    from kama_claude.core.skills.loader import _parse_skill_file

    content = "你是助手，请帮助用户完成任务：$ARGUMENTS\n"
    p = tmp_path / "plain.md"
    p.write_text(content, encoding="utf-8")
    skill = _parse_skill_file(p)
    assert skill.name == "plain"
    assert skill.allowed_tools == []
    assert "你是助手" in skill.system_prompt_template


# 功能：项目本地 skill 应覆盖内建同名 skill
# 设计：在 .kama/skills/ 中写入同名文件，用 monkeypatch 修改 cwd，断言加载到的是本地版本
def test_project_overrides_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local_skills = tmp_path / ".kama" / "skills"
    local_skills.mkdir(parents=True)
    (local_skills / "review.md").write_text(
        "---\nname: review\ndescription: local override\n---\nlocal system prompt $ARGUMENTS\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    loader = SkillLoader()
    skill = loader.resolve("review")
    assert skill is not None
    assert skill.description == "local override"
    assert "local system prompt" in skill.system_prompt_template


# 功能：skill 规范名应以文件名为准，而非 frontmatter 里可能不一致的 name 字段
# 设计：写入 realname.md（frontmatter name=different），断言 resolve 按文件名命中且索引用规范名
def test_skill_canonical_name_is_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / ".kama" / "skills"
    local.mkdir(parents=True)
    (local / "realname.md").write_text(
        "---\nname: different\ndescription: d\n---\nbody $ARGUMENTS\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
    (tmp_path / "empty_home").mkdir()
    monkeypatch.chdir(tmp_path)

    loader = SkillLoader()
    skill = loader.resolve("realname")
    assert skill is not None
    assert skill.name == "realname"
    assert loader.resolve("different") is None
    index = loader.skill_index()
    assert any(line.startswith("realname:") for line in index)
    assert not any(line.startswith("different:") for line in index)


# 功能：高优先级 skill 解析失败时应回退到内建配置，而非直接返回 None
# 设计：本地 review.md 写入非法 UTF-8 字节，断言 resolve 仍返回内建 review
def test_resolve_falls_back_on_parse_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / ".kama" / "skills"
    local.mkdir(parents=True)
    (local / "review.md").write_bytes(b"\xff\xfe\x00invalid utf8")
    monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))
    (tmp_path / "empty_home").mkdir()
    monkeypatch.chdir(tmp_path)

    loader = SkillLoader()
    skill = loader.resolve("review")
    assert skill is not None
    assert skill.name == "review"


# 功能：allowed_tools 内联列表 [a, b] 应被正确解析
# 设计：写入含内联列表的 skill，断言 _parse_skill_file 解析出完整列表
def test_inline_allowed_tools_parsed(tmp_path: Path) -> None:
    from kama_claude.core.skills.loader import _parse_skill_file

    content = "---\nname: t\ndescription: d\nallowed_tools: [read_file, bash]\n---\nbody\n"
    p = tmp_path / "t.md"
    p.write_text(content, encoding="utf-8")
    skill = _parse_skill_file(p)
    assert skill.allowed_tools == ["read_file", "bash"]
