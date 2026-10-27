from __future__ import annotations

import asyncio
import os
import shutil
import signal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from claudedock.core.security.capabilities import Capability
from claudedock.core.security.context import SecurityContext
from claudedock.core.tools.base import BaseTool, ToolResult
from claudedock.core.tools.paths import resolve_inside

_MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB
_DEFAULT_TIMEOUT = 60


class BashParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    command: str
    timeout: int = Field(default=_DEFAULT_TIMEOUT, ge=1, le=120)


# 从流中读取至多 cap 字节；若还有剩余数据则排空并标记截断，避免子进程因管道满而阻塞
async def _read_capped(stream: asyncio.StreamReader, cap: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    while total < cap:
        chunk = await stream.read(min(8192, cap - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)

    truncated = False
    if total >= cap:
        probe = await stream.read(1)
        if probe:
            truncated = True
            # 排空剩余输出，让子进程不被写满的管道卡住
            while True:
                rest = await stream.read(65536)
                if not rest:
                    break
    return b"".join(chunks), truncated


class BashTool(BaseTool):
    capabilities = frozenset({Capability.EXECUTE})

    # 默认使用操作系统沙箱；非隔离模式只用于注入测试
    def __init__(
        self,
        *,
        sandbox: bool = True,
        root: Path | None = None,
        writable_paths: tuple[str, ...] = (),
    ) -> None:
        self._writable_paths = writable_paths
        self._sandbox = sandbox
        self._root = (root or Path.cwd()).resolve()
        if self._root == Path(self._root.anchor):
            raise ValueError("filesystem root cannot be a shell workspace")

    # 宿主统一网关禁止未隔离执行，直接 invoke 仅保留为测试专用入口
    def check_permissions(self, context: SecurityContext, params: dict[str, object]) -> str | None:
        if not self._sandbox:
            return "shell_sandbox_disabled"
        return super().check_permissions(context, params)

    # 创建最小只读运行环境、工作目录挂载、隔离网络及资源上限
    def _argv(self, command: str, timeout: int) -> list[str]:
        if not self._sandbox:
            return ["/bin/bash", "-c", command]
        bubblewrap = shutil.which("bwrap")
        if bubblewrap is None or shutil.which("prlimit") is None:
            raise PermissionError("Shell sandbox unavailable: install bubblewrap and util-linux")
        argv = [
            bubblewrap,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--cap-drop",
            "ALL",
        ]
        for directory in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(directory).exists():
                argv.extend(["--ro-bind", directory, directory])
        argv.extend(["--ro-bind", str(self._root), "/workspace"])
        for writable_target in self._writable_paths:
            safe = resolve_inside(self._root, writable_target)
            if safe.path is None or safe.path == self._root or not safe.path.is_dir():
                raise PermissionError("Shell writable scope must be a bounded existing directory")
            # 禁止可写目录包含策略、凭据或账本；只读根挂载防止删除或替换这些目录
            if any(resolve_inside(self._root, str(p)).path is None for p in safe.path.rglob("*")):
                raise PermissionError("Shell writable scope contains protected paths")
            argv.extend(
                [
                    "--bind",
                    str(safe.path),
                    "/workspace/" + safe.path.relative_to(self._root).as_posix(),
                ]
            )
        # 对所有层级遮蔽保护路径与硬链接；不跟随软链接，不仅屏蔽仓库根的 .env
        entries = 0
        for directory, names, files in os.walk(self._root, followlinks=False):
            for name in list(names):
                target = Path(directory) / name
                entries += 1
                if resolve_inside(self._root, str(target)).path is None:
                    names.remove(name)
                    if not target.is_symlink():
                        argv.extend(
                            ["--tmpfs", "/workspace/" + target.relative_to(self._root).as_posix()]
                        )
            for name in files:
                target = Path(directory) / name
                entries += 1
                if resolve_inside(self._root, str(target)).path is None and not target.is_symlink():
                    argv.extend(
                        [
                            "--ro-bind",
                            "/dev/null",
                            "/workspace/" + target.relative_to(self._root).as_posix(),
                        ]
                    )
            if entries > 100000:
                raise PermissionError("Shell workspace scan limit exceeded")
        argv.extend(
            [
                "--tmpfs",
                "/scratch",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--chdir",
                "/workspace",
                "--setenv",
                "PATH",
                "/usr/bin:/bin",
                "--setenv",
                "HOME",
                "/scratch",
                "--setenv",
                "TMPDIR",
                "/scratch",
                "--",
                "/usr/bin/prlimit",
                f"--cpu={timeout}",
                "--as=1073741824",
                "--nproc=64",
                "--fsize=67108864",
                "--nofile=128",
                "--",
                "/bin/bash",
                "-c",
                command,
            ]
        )
        return argv

    params_model = BashParams
    risk_level = "execute"
    name = "bash"
    description = (
        "Execute a shell command and return its output (stdout + stderr combined). "
        "Non-interactive only — commands requiring user input will hang and time out. "
        "Prefer short, focused commands. Output is truncated at 64 KB."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Maximum seconds to wait (default {_DEFAULT_TIMEOUT}, max 120).",
            },
        },
        "required": ["command"],
    }

    # 终止子进程及其整个进程组（start_new_session 使其成为组长），回收 shell 派生的后代进程
    @staticmethod
    async def _kill_proc(proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await proc.wait()
        except Exception:
            pass

    # 在子进程中执行 shell 命令，流式读取并截断输出，超时或非零退出码时返回错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = BashParams.model_validate(params)
        command = p.command
        timeout = p.timeout

        try:
            argv = self._argv(command, timeout)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self._root,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            stdout = proc.stdout
            if stdout is None:
                await proc.wait()
                return ToolResult(content="[no output]")
            try:
                async with asyncio.timeout(timeout):
                    stdout_bytes, truncated = await _read_capped(stdout, _MAX_OUTPUT_BYTES)
                    await proc.wait()
            except asyncio.CancelledError:
                await self._kill_proc(proc)
                raise
            except TimeoutError:
                await self._kill_proc(proc)
                return ToolResult(
                    content=f"[timeout after {timeout}s]",
                    is_error=True,
                    error_type="timeout",
                )
        except PermissionError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="permission_denied")
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")

        await self._kill_proc(proc)
        output = stdout_bytes.decode("utf-8", errors="replace")
        if truncated:
            output += "\n[truncated]"

        returncode = proc.returncode or 0
        if returncode != 0:
            return ToolResult(
                content=f"[exit {returncode}]\n{output}",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=output or "[no output]")
