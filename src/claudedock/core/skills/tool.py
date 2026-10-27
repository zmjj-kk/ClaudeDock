from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from claudedock.core.security.capabilities import Capability
from claudedock.core.skills.loader import SkillLoader
from claudedock.core.tools.base import BaseTool, ToolResult


class UseSkillParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    arguments: str = ""


# 让 Agent 按相关性主动加载 skill 全文；返回指令正文、资源清单与工具建议
class UseSkillTool(BaseTool):
    capabilities = frozenset({Capability.READ})
    params_model = UseSkillParams
    name = "use_skill"
    description = (
        "Load a skill's full instructions by name from the Available Skills list. "
        "Call this when a listed skill matches the current task, before doing the work. "
        "The returned text is the skill's operating procedure — follow it. "
        "Reference docs and templates are loaded separately with read_skill_resource."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Skill name, exactly as listed under Available Skills.",
            },
            "arguments": {
                "type": "string",
                "description": "Task or goal to substitute for $ARGUMENTS in the skill body.",
            },
        },
        "required": ["name"],
    }

    # 持有 SkillLoader，供 invoke 解析与渲染
    def __init__(self, loader: SkillLoader) -> None:
        self._loader = loader

    # 解析并渲染 skill 正文；附加资源清单与 allowed_tools 建议（A/B 策略：仅提示不强制）
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = UseSkillParams.model_validate(params)
        skill = self._loader.resolve(p.name)
        if skill is None:
            return ToolResult(
                content=f"unknown skill: {p.name}",
                is_error=True,
                error_type="runtime_error",
            )
        body = self._loader.render_prompt(skill, p.arguments)
        if skill.allowed_tools:
            body += (
                "\n\n### Recommended tools\n"
                "This skill is designed around: "
                + ", ".join(skill.allowed_tools)
                + ". Prefer these; avoid unrelated tools."
            )
        if skill.resources:
            body += (
                "\n\n### Skill resources (load with read_skill_resource)\n"
                + "\n".join(f"- {r}" for r in skill.resources)
            )
        return ToolResult(content=body)


class ReadSkillResourceParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    resource: str


# 渐进式读取 skill 目录内的资源文件（references/templates），并禁止越出目录
class ReadSkillResourceTool(BaseTool):
    capabilities = frozenset({Capability.READ})
    params_model = ReadSkillResourceParams
    # 只读工具：runtime_error（如瞬时 IO 失败）可安全重试，不会产生副作用
    retryable_errors = frozenset({"rate_limited", "runtime_error"})
    name = "read_skill_resource"
    description = (
        "Read a resource file (reference doc, template, script) inside an activated "
        "skill's directory. `resource` must be one of the paths listed by use_skill "
        "for that skill."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name."},
            "resource": {
                "type": "string",
                "description": "Resource path relative to the skill directory.",
            },
        },
        "required": ["name", "resource"],
    }

    # 持有 SkillLoader，供 invoke 解析资源路径
    def __init__(self, loader: SkillLoader) -> None:
        self._loader = loader

    # 在 skill 目录内安全解析并读取资源文件；未知 skill / 路径穿越 / 未声明资源返回错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = ReadSkillResourceParams.model_validate(params)
        skill = self._loader.resolve(p.name)
        if skill is None or skill.directory is None:
            return ToolResult(
                content=f"unknown skill or no resources: {p.name}",
                is_error=True,
                error_type="runtime_error",
            )
        base = skill.directory.resolve()
        target = (skill.directory / p.resource).resolve()
        if not target.is_relative_to(base):
            return ToolResult(
                content="resource path escapes skill directory",
                is_error=True,
                error_type="runtime_error",
            )
        if p.resource not in skill.resources:
            return ToolResult(
                content=f"resource not declared by skill '{p.name}': {p.resource}",
                is_error=True,
                error_type="runtime_error",
            )
        try:
            return ToolResult(
                content=target.read_text(encoding="utf-8", errors="replace")
            )
        except FileNotFoundError:
            return ToolResult(
                content=f"no such resource: {p.resource}",
                is_error=True,
                error_type="runtime_error",
            )