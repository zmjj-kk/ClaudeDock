from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from rich.markdown import Markdown
from rich.markup import escape
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, Static, TextArea

from kama_claude.core.config import KamaConfig
from kama_claude.core.skills.loader import SkillLoader
from kama_claude.core.transport.socket_client import IpcError, SocketClient

log = logging.getLogger(__name__)


def _preview(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s


def _params_str(params: dict[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False, indent=2)


# 从工具参数中提取最适合摘要展示的关键字段
def _param_summary(tool_name: str, params: dict[str, Any], max_len: int = 72) -> str:
    keys_by_tool = {
        "read_file": ("path",),
        "write_file": ("path",),
        "list_dir": ("path", "max_depth"),
        "bash": ("command",),
        "note_save": ("content",),
    }
    keys = keys_by_tool.get(tool_name, ())
    parts = [f"{key}={params[key]!r}" for key in keys if key in params]
    if not parts:
        parts = [f"{key}={value!r}" for key, value in list(params.items())[:2]]
    return _preview(", ".join(parts), max_len)


class LLMStreamBlock(Static):
    """在同一个 Static widget 中累积 LLM 流式 token。"""

    DEFAULT_CSS = "LLMStreamBlock { padding: 0 2; color: $text; }"

    # 初始化为空文本块；流式 token 是纯文本，禁用 markup 避免 [ ] 被当样式标签吞掉
    def __init__(self) -> None:
        super().__init__("", markup=False)
        self._text = ""
        self._finalized = False

    # 追加一个 token 并刷新显示
    def append_token(self, token: str) -> None:
        if self._finalized:
            return
        self._text += token
        self.update(self._text)

    # 将累积文本渲染为 Markdown，供流式块结束后显示
    def finalize_markdown(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self._text.strip():
            self.update(Markdown(self._text, code_theme="monokai"))


class ToolCallBlock(Widget):
    """可折叠的工具调用块：折叠时显示摘要，点击后展开完整 params 和 output。"""

    DEFAULT_CSS = """
    ToolCallBlock { height: auto; padding: 0 2; color: $text-muted; }
    ToolCallBlock > .summary { color: $text-muted; }
    ToolCallBlock > .detail { display: none; padding: 0 2 0 4; color: $text-muted; }
    ToolCallBlock.expanded > .detail { display: block; }
    """

    # 初始化工具调用信息
    def __init__(self, tool_name: str, params: dict[str, Any]) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._params = params
        self._params_full = _params_str(params)
        self._output = ""
        self._elapsed_ms = 0
        self._is_error = False
        self._finished = False

    def compose(self) -> ComposeResult:
        yield Static(self._summary(), classes="summary")
        yield Static("", classes="detail")

    # 生成摘要行文本
    def _summary(self) -> str:
        if self._tool_name == "note_save" and self._finished and not self._is_error:
            return f"  [green]remembered[/green]  [dim]{self._elapsed_ms}ms[/dim]"

        params_pre = _param_summary(self._tool_name, self._params)
        line = f"  [dim]tool[/dim] [bold]{escape(self._tool_name)}[/bold]"
        if params_pre:
            line += f"  [dim]{escape(params_pre)}[/dim]"
        if self._finished:
            color = "red" if self._is_error else "green"
            status = "failed" if self._is_error else "done"
            hint = "  [dim](click to expand)[/dim]" if self._output else ""
            line += f"  [{color}]{status}[/{color}]  [dim]{self._elapsed_ms}ms[/dim]{hint}"
        return line

    # 工具调用完成时更新结果并刷新摘要（widget 未挂载时跳过 DOM 更新）
    def set_result(self, output: str, elapsed_ms: int, *, is_error: bool = False) -> None:
        self._output = output
        self._elapsed_ms = elapsed_ms
        self._is_error = is_error
        self._finished = True
        if self.children:
            self.query_one(".summary", Static).update(self._summary())

    # 点击时切换展开/折叠状态
    def on_click(self) -> None:
        if not self._finished:
            return
        if "expanded" in self.classes:
            self.remove_class("expanded")
        else:
            detail = self.query_one(".detail", Static)
            detail.update(
                f"[dim]params[/dim]\n{escape(self._params_full)}\n\n"
                f"[dim]output[/dim]\n{escape(self._output)}\n\n"
                f"[dim]elapsed:[/dim] {self._elapsed_ms}ms"
            )
            self.add_class("expanded")


class PermissionSelect(Static):
    """内联权限选择控件：挂载在日志流中，键盘焦点无需 ModalScreen。"""

    can_focus = True

    DEFAULT_CSS = """
    PermissionSelect {
        height: auto;
        padding: 0 2;
        margin-bottom: 1;
    }
    """

    _CHOICES: tuple[tuple[str, str, str], ...] = (
        ("allow_once", "Allow once", "y / 1"),
        ("always_allow", "Allow this input for session", "a / 2"),
        ("deny_once", "Deny", "n / 3"),
        ("always_deny", "Deny this input for session", "d / 4"),
    )
    _KEY_MAP: dict[str, str] = {
        "y": "allow_once",
        "1": "allow_once",
        "a": "always_allow",
        "2": "always_allow",
        "n": "deny_once",
        "3": "deny_once",
        "d": "always_deny",
        "4": "always_deny",
    }

    # 用户作出权限决策时发布，携带工具 ID 和决策字符串
    class Decided(Message):
        # 初始化决策消息，存储控件引用、工具 ID 和决策
        def __init__(self, widget: PermissionSelect, tool_use_id: str, decision: str) -> None:
            self.widget = widget
            self.tool_use_id = tool_use_id
            self.decision = decision
            super().__init__()

    # 初始化控件，存储工具 ID（用于 IPC 回复）
    def __init__(self, tool_use_id: str) -> None:
        super().__init__("")
        self._tool_use_id = tool_use_id
        self._cursor = 0

    def on_mount(self) -> None:
        self.update(self._render_ui())
        self.focus()

    # 生成带光标高亮的选项列表文本
    def _render_ui(self) -> str:
        lines: list[str] = []
        for i, (_, label, key_hint) in enumerate(self._CHOICES):
            if i == self._cursor:
                lines.append(f"  [bold cyan]❯ {label}[/bold cyan]  [dim]{key_hint}[/dim]")
            else:
                lines.append(f"    {label}  [dim]{key_hint}[/dim]")
        lines.append("[dim]  ↑↓ navigate   enter confirm[/dim]")
        return "\n".join(lines)

    # 方向键导航；快捷键直接选择；enter 确认光标位置
    def on_key(self, event: events.Key) -> None:
        key = event.key
        if key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key == "enter":
            event.stop()
            self._pick(self._CHOICES[self._cursor][0])
        else:
            decision = self._KEY_MAP.get(key)
            if decision is not None:
                event.stop()
                self._pick(decision)

    # 发布决策消息，由宿主 App 负责 IPC 回复和控件清理
    def _pick(self, decision: str) -> None:
        self.post_message(self.Decided(self, self._tool_use_id, decision))


class PermissionBlock(Static):
    """日志里的权限审批摘要"""

    _LABEL_MAP: dict[str, str] = {
        "allow_once": "allowed (once)",
        "always_allow": "allowed for this input/session",
        "deny_once": "denied",
        "always_deny": "denied for this input/session",
        "timeout": "⏱ timed out",
    }
    LABEL_MAP = _LABEL_MAP

    # 初始化审批块，记录工具 ID、名称和参数预览
    def __init__(self, tool_use_id: str, tool_name: str, param_preview: str) -> None:
        self._tool_use_id = tool_use_id
        self._tool_name = tool_name
        self._param_preview = param_preview
        self.approval_token = ""
        self._resolved = False
        super().__init__(self._pending_text(), classes="log-line")

    def _pending_text(self) -> str:
        preview = f"  [dim]{escape(self._param_preview)}[/dim]" if self._param_preview else ""
        return f"[bold red]? permission[/bold red]  [bold]{escape(self._tool_name)}[/bold]{preview}"

    # 将块收缩为单行摘要，展示决策结果
    def _resolve(self, decision: str) -> None:
        if self._resolved:
            return
        self._resolved = True
        allowed = decision in ("allow_once", "always_allow")
        icon = "[bold green]✓[/bold green]" if allowed else "[bold red]✗[/bold red]"
        label = self._LABEL_MAP.get(decision, decision)
        preview = f"  [dim]{escape(self._param_preview)}[/dim]" if self._param_preview else ""
        self.update(
            f"{icon} permission  [bold]{escape(self._tool_name)}[/bold]{preview}  "
            f"[dim]{escape(label)}[/dim]"
        )


class SlashCompleteWidget(Static):
    """斜杠命令自动补全弹出框：输入 / 时显示可用 skill 列表并支持键盘筛选与选择。"""

    can_focus = False

    DEFAULT_CSS = """
    SlashCompleteWidget {
        height: auto;
        padding: 0 1;
        margin: 0 2;
        background: $surface;
        border: round $surface-lighten-2;
    }
    """

    # 用户选中某条命令时发布
    class Selected(Message):
        # 初始化，携带被选中的 skill 名称
        def __init__(self, skill_name: str) -> None:
            self.skill_name = skill_name
            super().__init__()

    # 初始化，接收全量 (name, description) 列表
    def __init__(self, items: list[tuple[str, str]]) -> None:
        super().__init__("")
        self._all_items = items
        self._filtered: list[tuple[str, str]] = list(items)
        self._cursor = 0

    # 根据查询字符串筛选列表，重置光标并重新渲染
    def set_query(self, query: str) -> None:
        q = query.lower()
        self._filtered = [(n, d) for n, d in self._all_items if not q or q in n.lower()]
        self._cursor = min(self._cursor, max(0, len(self._filtered) - 1))
        if self.is_attached:
            self._redraw()

    # 向上移动光标并重新渲染
    def move_up(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor - 1) % len(self._filtered)
            self._redraw()

    # 向下移动光标并重新渲染
    def move_down(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor + 1) % len(self._filtered)
            self._redraw()

    # 选中当前光标项并发布 Selected 消息
    def select_current(self) -> None:
        if self._filtered:
            self.post_message(self.Selected(self._filtered[self._cursor][0]))

    # 返回当前是否有可选项
    def has_selection(self) -> bool:
        return len(self._filtered) > 0

    def on_mount(self) -> None:
        self._redraw()

    # 渲染筛选后的命令列表，高亮当前光标项
    def _redraw(self) -> None:
        if not self._filtered:
            self.update("[dim]  no matching commands[/dim]")
            return
        lines: list[str] = []
        for i, (name, desc) in enumerate(self._filtered):
            desc_part = f"  [dim]{escape(desc)}[/dim]" if desc else ""
            if i == self._cursor:
                lines.append(f"  [bold cyan]❯ /{escape(name)}[/bold cyan]{desc_part}")
            else:
                lines.append(f"    [cyan]/{escape(name)}[/cyan]{desc_part}")
        lines.append("[dim]  ↑↓ navigate   tab/enter select   esc dismiss[/dim]")
        self.update("\n".join(lines))


class SessionChoiceWidget(Static):
    """启动时列出可续接的旧会话，供用户选择续接或新建。"""

    can_focus = True

    DEFAULT_CSS = """
    SessionChoiceWidget {
        height: auto;
        padding: 0 2;
        margin-bottom: 1;
        background: $surface;
        border: round $surface-lighten-2;
    }
    """

    # 用户选定会话时发布；session_id 为 None 表示新建会话
    class Selected(Message):
        def __init__(self, session_id: str | None) -> None:
            self.session_id = session_id
            super().__init__()

    # 初始化候选列表：首项为新建，其余为可续接的旧会话
    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        super().__init__("")
        self._items: list[tuple[str | None, str]] = [(None, "＋ 新建会话")] + [
            (s.get("id", ""), self._label(s)) for s in sessions
        ]
        self._cursor = 0

    # 生成单条会话摘要：标题 + id + 状态
    def _label(self, s: dict[str, Any]) -> str:
        title = str(s.get("title") or "(无标题)")
        sid = str(s.get("id", ""))
        status = str(s.get("status", ""))
        return f"{title}  [dim]{sid}[/dim]  [{status}]"

    def on_mount(self) -> None:
        self._redraw()
        self.focus()

    # 渲染候选列表并高亮当前光标项
    def _redraw(self) -> None:
        lines = ["[bold]选择要续接的会话[/bold]"]
        for i, (_sid, label) in enumerate(self._items):
            prefix = "[bold cyan]❯[/bold cyan]" if i == self._cursor else "  "
            lines.append(f"{prefix} {label}")
        lines.append("[dim]  ↑↓ 选择  enter 确认[/dim]")
        self.update("\n".join(lines))

    # 方向键导航；enter 确认当前项并发布 Selected 消息
    def on_key(self, event: events.Key) -> None:
        key = event.key
        if key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._items)
            self._redraw()
        elif key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._items)
            self._redraw()
        elif key == "enter":
            event.stop()
            sid = self._items[self._cursor][0]
            self.remove()
            self.post_message(self.Selected(sid))


class ChatTextArea(TextArea):
    """支持 Enter 提交、Cmd/Shift/Alt+Enter 换行的多行聊天输入框。"""

    DEFAULT_CSS = """
    ChatTextArea {
        height: auto;
        min-height: 3;
        max-height: 12;
        border: round $surface-lighten-2;
        background: $background;
        padding: 0 1;
        margin: 1 2;
        scrollbar-size-vertical: 1;
    }
    ChatTextArea:focus {
        border: round $accent;
        background: $background;
    }
    """

    # 子类自定义的提交消息，供宿主 App 监听
    class Submitted(Message):
        def __init__(self, area: ChatTextArea) -> None:
            self.text_area = area
            self.value = area.text
            super().__init__()

    # 输入内容以 / 开头且无空格时发布，query 为 / 之后的字符串（可为空串）；None 表示收起弹窗
    class SlashChanged(Message):
        def __init__(self, query: str | None) -> None:
            self.query = query
            super().__init__()

    # 文本变化时检测 / 前缀，通知宿主 App 更新自动补全弹窗
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = self.text
        if text.startswith("/") and " " not in text:
            self.post_message(ChatTextArea.SlashChanged(query=text[1:]))
        else:
            self.post_message(ChatTextArea.SlashChanged(query=None))

    # Enter 提交；↑↓/Tab/Esc 路由到自动补全弹窗；Cmd/Shift/Alt+Enter 插入换行；其余键交回 TextArea
    async def _on_key(self, event: events.Key) -> None:
        key = event.key

        popup: SlashCompleteWidget | None = None
        try:
            popup = self.app.query_one(SlashCompleteWidget)
        except NoMatches:
            popup = None

        if key == "enter":
            event.stop()
            event.prevent_default()
            if popup is not None and popup.has_selection():
                popup.select_current()
                return
            if self.text.strip():
                self.post_message(self.Submitted(self))
            return
        if key in ("alt+enter", "shift+enter", "ctrl+j", "super+enter"):
            event.stop()
            event.prevent_default()
            if not self.read_only:
                self.insert("\n")
            return
        if popup is not None:
            if key == "up":
                event.stop()
                event.prevent_default()
                popup.move_up()
                return
            elif key == "down":
                event.stop()
                event.prevent_default()
                popup.move_down()
                return
            elif key == "tab":
                event.stop()
                event.prevent_default()
                popup.select_current()
                return
            elif key == "escape":
                event.stop()
                event.prevent_default()
                self.post_message(ChatTextArea.SlashChanged(query=None))
                return
        await super()._on_key(event)


class KamaTuiApp(App[None]):
    """KamaClaude TUI：终端滚屏风格，实时展示 agent 执行过程。"""

    TITLE = "KamaClaude"
    BINDINGS = [
        Binding("ctrl+q", "quit", "quit"),
        Binding("ctrl+c", "interrupt", "stop/quit", priority=True),
    ]
    CSS = """
    Screen { background: $background; }
    #header {
        height: 1;
        background: $surface;
        color: $text;
        padding: 0 1;
    }
    #log-view {
        height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }
    #banner { padding: 1 2 0 2; }
    Static.user-turn { color: $text; padding: 1 2 0 2; }
    Static.run-header { color: $text-muted; padding: 1 2 0 2; }
    Static.step-divider { color: $text-muted; padding: 0 2; }
    Static.run-ok { color: green; padding: 0 2 1 2; }
    Static.run-err { color: red; padding: 0 2 1 2; }
    Static.usage { padding: 0 2; }
    Static.log-line { padding: 0 2; }
    """

    _BANNER = (
        "[bold cyan]██╗  ██╗ █████╗ ███╗   ███╗ █████╗  ██████╗██╗      █████╗ ██╗   ██╗██████╗ ███████╗[/bold cyan]\n"
        "[bold cyan]██║ ██╔╝██╔══██╗████╗ ████║██╔══██╗██╔════╝██║     ██╔══██╗██║   ██║██╔══██╗██╔════╝[/bold cyan]\n"
        "[bold cyan]█████╔╝ ███████║██╔████╔██║███████║██║     ██║     ███████║██║   ██║██║  ██║█████╗  [/bold cyan]\n"
        "[bold cyan]██╔═██╗ ██╔══██║██║╚██╔╝██║██╔══██║██║     ██║     ██╔══██║██║   ██║██║  ██║██╔══╝  [/bold cyan]\n"
        "[bold cyan]██║  ██╗██║  ██║██║ ╚═╝ ██║██║  ██║╚██████╗███████╗██║  ██║╚██████╔╝██████╔╝███████╗[/bold cyan]\n"
        "[bold cyan]╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝╚═╝  ╚═╝ ╚═════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝[/bold cyan]\n"
        "[dim]  输入消息开始对话  ·  键入 / 触发 skill  ·  Ctrl+C 停止任务 / 空闲时退出[/dim]"
    )

    # 初始化连接参数和 TUI 内部状态
    def __init__(self, host: str, port: int, replay_run_id: str | None = None) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._event_cursors: dict[str, int] = {}
        self._resume_run_id: str | None = None
        self._resume_session_id: str | None = None
        self._active_run_id: str | None = None
        self._replay_run_id = replay_run_id
        self._client: SocketClient | None = None
        self._current_llm: LLMStreamBlock | None = None
        self._pending_tool_blocks: dict[str, ToolCallBlock] = {}
        self._pending_permission_blocks: dict[str, PermissionBlock] = {}
        self._session_id: str | None = None
        self._busy = False
        self._last_context_pct: float = 0.0
        self._last_context_window: int | None = None
        self._slash_items: list[tuple[str, str]] = []
        self._subagent_run_ids: dict[str, str] = {}  # child run_id -> description
        self._subagent_start_times: dict[str, float] = {}  # child run_id -> start time
        self._session_choice_event: asyncio.Event | None = None
        self._session_choice_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Label("[bold]KamaClaude[/bold]  [dim]connecting...[/dim]", id="header")
        yield VerticalScroll(id="log-view")
        yield ChatTextArea(id="prompt", show_line_numbers=False)

    def on_mount(self) -> None:
        self._slash_items = self._build_slash_items()
        self._append(Static(self._BANNER, id="banner"))
        self.run_worker(self._socket_loop(), exclusive=True, name="socket")
        prompt = self.query_one("#prompt", ChatTextArea)
        prompt.disabled = True
        prompt.border_title = "connecting..."

    # 构建斜杠命令候选列表：内建命令 + 所有已注册 skill
    def _build_slash_items(self) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = [
            ("compact", "compress context window"),
            ("context", "inspect context sources and session notes"),
            ("notes", "inspect structured session memory"),
        ]
        try:
            loader = SkillLoader()
            for skill in loader.list_all_skills():
                desc = skill.description.splitlines()[0] if skill.description else ""
                if len(desc) > 60:
                    desc = desc[:57] + "..."
                items.append((skill.name, desc))
        except Exception:
            # 单个 skill 加载失败不应拖垮斜杠命令列表，降级为内建命令
            log.exception("failed to load skill list for slash completion")
        return items

    # 根据 / 前缀查询字符串挂载、更新或移除自动补全弹窗
    def on_chat_text_area_slash_changed(self, event: ChatTextArea.SlashChanged) -> None:
        query = event.query
        if query is None:
            try:
                self.query_one(SlashCompleteWidget).remove()
            except NoMatches:
                pass
            return
        try:
            popup = self.query_one(SlashCompleteWidget)
            popup.set_query(query)
        except NoMatches:
            popup = SlashCompleteWidget(self._slash_items)
            self.mount(popup, before="#prompt")
            popup.set_query(query)

    # 用户选中自动补全项后将 /{name} 填入输入框并移除弹窗
    def on_slash_complete_widget_selected(self, event: SlashCompleteWidget.Selected) -> None:
        prompt = self._prompt()
        if prompt is not None:
            prompt.text = f"/{event.skill_name} "
            prompt.move_cursor(prompt.document.end)
        try:
            self.query_one(SlashCompleteWidget).remove()
        except NoMatches:
            pass

    # PermissionSelect 失去焦点后作为兜底处理权限快捷键
    def on_key(self, event: events.Key) -> None:
        if not self._pending_permission_blocks:
            return
        try:
            select = self.query_one(PermissionSelect)
        except NoMatches:
            return
        if select.has_focus:
            return  # PermissionSelect 有焦点时自行处理，事件不会冒泡到这里
        key = event.key
        decision = PermissionSelect._KEY_MAP.get(key)
        if decision:
            event.stop()
            select._pick(decision)
        elif key in ("up", "k"):
            event.stop()
            select._cursor = (select._cursor - 1) % len(PermissionSelect._CHOICES)
            select.update(select._render_ui())
        elif key in ("down", "j"):
            event.stop()
            select._cursor = (select._cursor + 1) % len(PermissionSelect._CHOICES)
            select.update(select._render_ui())
        elif key == "enter":
            event.stop()
            select._pick(PermissionSelect._CHOICES[select._cursor][0])

    # 正在运行时取消任务，空闲时保持原退出操作
    async def action_interrupt(self) -> None:
        if self._active_run_id is not None and self._client is not None:
            try:
                await self._client.send_command("run.cancel", {"run_id": self._active_run_id})
                self._append(Static("[yellow]正在取消任务…[/yellow]"))
            except (IpcError, RuntimeError, OSError):
                self._append(Static("[red]取消请求失败，请检查连接[/red]"))
            return
        await self.action_quit()

    # 退出前尽力关闭当前 session，失败也不阻塞 TUI 退出
    async def action_quit(self) -> None:
        if self._client is not None and self._session_id is not None:
            try:
                await self._client.send_command("session.close", {"session_id": self._session_id})
            except (IpcError, RuntimeError, OSError):
                self._append(Static("[yellow]warning: failed to close session[/yellow]"))
        self.exit()

    # 将输入框提交内容发送给当前 chat session；用 worker 发送，避免 await 阻塞 App 消息泵
    async def on_chat_text_area_submitted(self, event: ChatTextArea.Submitted) -> None:
        content = event.value.strip()
        if not content:
            return
        if content in {"/context", "/notes"}:
            event.text_area.text = ""
            self.run_worker(self._do_context(), name="context-view", exclusive=False)
            return
        # 检测 /compact 指令
        if content == "/compact" or content.startswith("/compact "):
            event.text_area.text = ""
            if self._client is None or self._session_id is None:
                self._append(Static("[yellow]not connected[/yellow]", classes="log-line"))
            elif self._busy:
                self._append(
                    Static(
                        "[yellow]cannot compact while agent is running[/yellow]", classes="log-line"
                    )
                )
            else:
                self.run_worker(
                    self._do_compact(content.removeprefix("/compact").strip()),
                    name="compact",
                    exclusive=False,
                )
            return
        if self._client is None or self._session_id is None or self._busy:
            self._append(Static("[yellow]agent busy or disconnected[/yellow]", classes="log-line"))
            return
        self._busy = True
        prompt = event.text_area
        prompt.text = ""
        prompt.disabled = True
        prompt.border_title = "agent is working..."
        self._append(Static(f"[bold]>[/bold] {escape(content)}", classes="user-turn"))
        self._update_header("running")
        self.run_worker(self._do_send_message(content), name="send_message", exclusive=False)

    # 通过认证 IPC 展示来源与脱敏笔记，禁止把正文解释为终端标记
    async def _do_context(self) -> None:
        if self._client is None or self._session_id is None:
            self._append(Static("[yellow]not connected[/yellow]", classes="log-line"))
            return
        try:
            result = await self._client.send_command(
                "session.get_context",
                {"session_id": self._session_id},
            )
            lines = ["Context sources and session memory"]
            for source in result.get("sources", []):
                lines.append(
                    f"{source.get('scope')}: {source.get('source_path')} sha256={source.get('source_hash')}"
                )
            lines.append(str(result.get("notes", "")))
            lines.append(f"Projection: {result.get('projection', {})}")
            self._append(Static(escape("\n".join(lines)), classes="log-line"))
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]context view error: {escape(str(exc))}[/red]"))

    # 在 worker 中执行手动压缩命令，完成后显示结果横幅
    async def _do_compact(self, focus: str = "") -> None:
        if self._client is None or self._session_id is None:
            return
        self._append(Static("[dim]⚡ compacting context...[/dim]", classes="log-line"))
        try:
            result = await self._client.send_command(
                "session.compact",
                {"session_id": self._session_id, "focus": focus},
            )
            summary_tokens = result.get("summary_tokens", 0)
            saved_tokens = result.get("saved_tokens", 0)
            after = result.get("projection_tokens") or summary_tokens
            self._last_context_pct = (
                float(after) / self._last_context_window if self._last_context_window else 0.0
            )
            self._append(
                Static(
                    f"[bold cyan]⚡ Context compacted[/bold cyan]"
                    f"  [dim]summary={summary_tokens} tokens  saved≈{saved_tokens} tokens[/dim]",
                    classes="log-line",
                )
            )
        except (IpcError, RuntimeError, OSError) as e:
            self._append(Static(f"[red]compact error: {escape(str(e))}[/red]", classes="log-line"))

    # 在 worker 中执行 IPC 发送，使 App 消息泵在 agent 运行期间仍能处理键盘/焦点等消息
    async def _do_send_message(self, content: str) -> None:
        if self._client is None:
            return
        try:
            await self._client.send_command(
                "session.send_message",
                {"session_id": self._session_id, "content": content},
            )
        except (IpcError, RuntimeError, OSError) as e:
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
            self._update_header("ready")
            self._append(Static(f"[red]send error: {escape(str(e))}[/red]", classes="log-line"))

    # 处理内联审批控件的用户决策：发送 IPC 响应并恢复输入框
    async def on_permission_select_decided(self, msg: PermissionSelect.Decided) -> None:
        tool_use_id = msg.tool_use_id
        decision = msg.decision
        log.info("permission decided tool_use_id=%s decision=%s", tool_use_id, decision)
        try:
            msg.widget.remove()
            perm_block = self._pending_permission_blocks.pop(tool_use_id, None)
            if perm_block is not None:
                perm_block._resolve(decision)
            if self._client is not None:
                try:
                    await self._client.send_command(
                        "permission.respond",
                        {
                            "tool_use_id": tool_use_id,
                            "decision": decision,
                            "approval_token": perm_block.approval_token if perm_block else "",
                        },
                    )
                except (IpcError, RuntimeError, OSError):
                    # 发送失败时 daemon 侧的 Future 会挂起至超时；记录以便排查而非静默吞掉
                    log.exception(
                        "permission.respond failed tool_use_id=%s decision=%s",
                        tool_use_id,
                        decision,
                    )
            # agent 循环在权限决策后仍继续运行，输入恢复统一交给 session.waiting_for_input
        except Exception:
            log.exception("on_permission_select_decided failed tool_use_id=%s", tool_use_id)

    # 向日志视图追加一个 widget 并滚动到底部
    def _append(self, widget: Widget) -> None:
        log_view = self.query_one("#log-view", VerticalScroll)
        log_view.mount(widget)
        log_view.scroll_end(animate=False)

    # 结束当前 LLM 流式块（下一个 token 将开启新块）
    def _break_llm(self) -> None:
        if self._current_llm is not None:
            self._current_llm.finalize_markdown()
        self._current_llm = None

    # 将选择控件挂载到 Screen 顶层（#prompt 之前），避免 VerticalScroll 争抢焦点
    def _mount_permission_select(self, select: PermissionSelect) -> None:
        self.mount(select, before="#prompt")

    # 安全获取输入框；组件测试中未挂载时返回 None，调用方据此跳过 UI 操作
    def _prompt(self) -> ChatTextArea | None:
        try:
            return self.query_one("#prompt", ChatTextArea)
        except NoMatches:
            return None

    # 生成 context 占用率的彩色进度条字符串
    def _render_ctx_bar(self, pct: float) -> str:
        # 钳制到 [0,1]，防止 context_pct 超过 1 时进度条溢出 20 格
        pct = min(max(pct, 0.0), 1.0)
        filled = int(pct * 20)
        bar = "█" * filled + "░" * (20 - filled)
        label = f"ctx:{pct * 100:.1f}%"
        if pct >= 0.85:
            color = "bold red"
        elif pct >= 0.70:
            color = "yellow"
        else:
            color = "dim"
        return f"[{color}]{label} {bar}[/{color}]"

    # 根据连接和运行状态刷新顶部标题
    def _update_header(self, state: str) -> None:
        try:
            header = self.query_one("#header", Label)
        except NoMatches:
            return
        session = f"  [dim]{self._session_id}[/dim]" if self._session_id else ""
        color = {
            "ready": "green",
            "running": "yellow",
            "disconnected": "red",
            "connecting": "dim",
        }.get(state, "dim")
        header.update(
            f"[bold]KamaClaude[/bold]  [dim]{self._host}:{self._port}[/dim]"
            f"{session}  [{color}]{state}[/{color}]"
        )

    # 列出旧会话供用户选择续接；无旧会话或选新建时创建新 session，返回 session_id
    async def _pick_or_create_session(self, client: SocketClient) -> str:
        try:
            listed = await client.send_command("session.list", {})
        except (IpcError, RuntimeError, OSError):
            listed = {}
        sessions = listed.get("sessions", []) if isinstance(listed, dict) else []
        if not sessions:
            created = await client.send_command("session.create", {"mode": "chat"})
            return str(created["session_id"])

        self._session_choice_event = asyncio.Event()
        self._session_choice_id = None
        self._mount_session_choice(sessions)
        await self._session_choice_event.wait()

        choice = self._session_choice_id
        if choice is None:
            created = await client.send_command("session.create", {"mode": "chat"})
            return str(created["session_id"])

        try:
            history = await client.send_command("session.get_history", {"session_id": choice})
        except (IpcError, RuntimeError, OSError):
            history = {}
        self._render_history(history.get("messages", []) if isinstance(history, dict) else [])
        return choice

    # 挂载会话选择控件到输入框之前
    def _mount_session_choice(self, sessions: list[dict[str, Any]]) -> None:
        widget = SessionChoiceWidget(sessions)
        try:
            self.mount(widget, before="#prompt")
        except Exception:
            self._append(widget)

    # 用户从会话选择控件选定后，记录选择并唤醒 _pick_or_create_session 的等待
    def on_session_choice_widget_selected(self, msg: SessionChoiceWidget.Selected) -> None:
        self._session_choice_id = msg.session_id
        if self._session_choice_event is not None:
            self._session_choice_event.set()

    # 将恢复的历史 messages 渲染为静态块，便于用户回顾续接的上下文
    def _render_history(self, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        self._append(Static("[dim]── 已恢复的历史会话 ──[/dim]", classes="run-header"))
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    btype = block.get("type", "")
                    if btype == "text":
                        parts.append(str(block.get("text", "")))
                    elif btype == "tool_use":
                        parts.append(f"[tool_use {block.get('name', '')}]")
                    elif btype == "tool_result":
                        parts.append(
                            f"[tool_result] {_preview(str(block.get('content', '')), 120)}"
                        )
                text = "\n".join(parts)
            else:
                text = str(content)
            if not text.strip():
                continue
            if role == "user":
                self._append(Static(f"[bold]>[/bold] {escape(text)}", classes="user-turn"))
            else:
                self._append(Static(Markdown(text), classes="log-line"))
        self._append(Static("[dim]── 历史结束，继续输入即可 ──[/dim]", classes="run-header"))

    # 管理 SocketClient 生命周期：连接、订阅事件、断线重连
    async def _socket_loop(self) -> None:
        header = self.query_one("#header", Label)

        while True:
            client = SocketClient(self._host, self._port, cursors=self._event_cursors)
            self._client = None
            try:
                await client.connect()
            except (ConnectionRefusedError, OSError):
                log.warning("connection refused %s:%s, retrying", self._host, self._port)
                self._update_header("disconnected")
                await asyncio.sleep(2)
                continue

            log.info("connected to %s:%s", self._host, self._port)
            self._client = client
            self._update_header("connecting")
            loop_task = asyncio.create_task(client.run_event_loop())

            async def on_event(event: dict[str, Any]) -> None:
                self._handle_event(event)

            client.on_event(on_event)

            try:
                loop_task.add_done_callback(
                    lambda t: (
                        log.error("loop_task failed: %s", t.exception())
                        if not t.cancelled() and t.exception() is not None
                        else None
                    )
                )
                if self._resume_session_id is not None:
                    listed = await client.send_command("session.list", {})
                    if any(s["id"] == self._resume_session_id for s in listed.get("sessions", [])):
                        self._session_id = self._resume_session_id
                    else:
                        self._resume_session_id = None
                if self._session_id is None:
                    self._session_id = await self._pick_or_create_session(client)
                self._resume_session_id = self._session_id
                params: dict[str, Any] = {
                    "topics": [
                        "session.*",
                        "run.*",
                        "step.*",
                        "tool.*",
                        "llm.token",
                        "llm.usage",
                        "log.*",
                        "permission.*",
                        "context.*",
                        "subagent.*",
                        "skill.*",
                    ],
                    "scope": f"session:{self._session_id}",
                }
                replay_id = self._replay_run_id or self._resume_run_id
                if replay_id is not None:
                    params["replay_from_run"] = replay_id
                    params["after_sequence"] = self._event_cursors.get(replay_id, 0)
                await client.send_command("event.subscribe", params)
                log.info("session ready session_id=%s", self._session_id)
                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = False
                    prompt.read_only = False
                    prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                    prompt.focus()
                self._update_header("ready")
                await loop_task
            except IpcError as e:
                header.update(f"[bold]KamaClaude[/bold]  [red]subscribe error: {e}[/red]")
            finally:
                if not loop_task.done():
                    loop_task.cancel()
                self._client = None
                self._session_id = None
                # 断连后这些 id 不会再收到完成事件，清空免留死块；busy 复位避免输入永久锁死
                self._busy = False
                self._active_run_id = None
                self._pending_tool_blocks.clear()
                self._pending_permission_blocks.clear()
                self._subagent_run_ids.clear()
                self._subagent_start_times.clear()
                for select in list(self.query(PermissionSelect)):
                    select.remove()
                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = True
                    prompt.read_only = False
                    prompt.border_title = "disconnected, retrying..."
                self._break_llm()
                await client.close()

            self._update_header("disconnected")
            await asyncio.sleep(2)

    # 根据事件 type 路由到对应渲染逻辑；捕获异常防止 socket loop 因单个事件崩溃
    def _handle_event(self, event: dict[str, Any]) -> None:
        try:
            self._handle_event_inner(event)
        except Exception:
            log.exception("_handle_event crashed  event_type=%s", event.get("type", "?"))

    # 实际的事件路由逻辑
    def _handle_event_inner(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")

        if t == "llm.token":
            run_id = event.get("run_id", "")
            # 后台 subagent 的 token 与父 run 并发，不并入同一流式块，避免输出串扰
            if run_id in self._subagent_run_ids:
                return
            token = event.get("token", "")
            if self._current_llm is None:
                llm_block = LLMStreamBlock()
                self._append(llm_block)
                self._current_llm = llm_block
            self._current_llm.append_token(token)
            return

        self._break_llm()

        if t == "session.waiting_for_input":
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                prompt.focus()
            self._update_header("ready")

        elif t == "session.closed":
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.read_only = False
                prompt.border_title = "session closed"
            self._update_header("disconnected")

        elif t == "run.started":
            run_id = event.get("run_id", "")
            self._active_run_id = str(run_id)
            self._resume_run_id = str(run_id)
            goal = event.get("goal", "")
            self._append(
                Static(
                    f"[dim]run[/dim]  [cyan]{escape(run_id)}[/cyan]  "
                    f"[dim]{escape(_preview(goal, 96))}[/dim]",
                    classes="run-header",
                )
            )

        elif t == "skill.invoked":
            skill_name = event.get("skill_name", "")
            arguments = event.get("arguments", "")
            args_preview = _preview(arguments, 80) if arguments else ""
            args_part = f"  [dim]{escape(args_preview)}[/dim]" if args_preview else ""
            self._append(
                Static(
                    f"[bold cyan]/{escape(skill_name)}[/bold cyan]{args_part}",
                    classes="log-line",
                )
            )

        elif t == "subagent.started":
            run_id = event.get("run_id", "")
            description = event.get("description", "")
            self._subagent_run_ids[run_id] = description
            self._subagent_start_times[run_id] = time.monotonic()
            short_id = run_id[:8] if len(run_id) >= 8 else run_id
            self._append(
                Static(
                    f"[dim]┌─[/dim] [cyan]{escape(_preview(description, 72))}[/cyan]  "
                    f"[dim]{escape(short_id)}[/dim]",
                    classes="log-line",
                )
            )

        elif t == "subagent.finished":
            run_id = event.get("run_id", "")
            status = event.get("status", "")
            description = self._subagent_run_ids.pop(run_id, event.get("description", ""))
            start = self._subagent_start_times.pop(run_id, None)
            elapsed = f"  [dim]{time.monotonic() - start:.1f}s[/dim]" if start is not None else ""
            desc_part = f"[cyan]{escape(_preview(description, 72))}[/cyan]{elapsed}"
            if status == "success":
                self._append(
                    Static(
                        f"[dim]└─[/dim] [bold green]✓[/bold green] {desc_part}",
                        classes="log-line",
                    )
                )
            else:
                self._append(
                    Static(
                        f"[dim]└─[/dim] [bold red]✗[/bold red] {desc_part}",
                        classes="log-line",
                    )
                )

        elif t == "step.started":
            run_id = event.get("run_id", "")
            if run_id in self._subagent_run_ids:
                return
            step = event.get("step", "")
            self._append(
                Static(
                    f"[dim]step {step}[/dim]",
                    classes="step-divider",
                )
            )

        elif t == "tool.call_started":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            params = event.get("params") or {}
            run_id = event.get("run_id", "")
            tc_block = ToolCallBlock(tool_name, params)
            if run_id in self._subagent_run_ids:
                tc_block.styles.padding = (0, 2, 0, 6)
            self._pending_tool_blocks[tool_use_id] = tc_block
            self._append(tc_block)

        elif t == "tool.call_finished":
            tool_use_id = str(event.get("tool_use_id", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            output = str(event.get("output") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(output, elapsed_ms)

        elif t == "tool.call_failed":
            tool_use_id = str(event.get("tool_use_id", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            error_msg = str(event.get("error_message") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(error_msg, elapsed_ms, is_error=True)

        elif t == "run.finished":
            if event.get("run_id") == self._active_run_id:
                self._active_run_id = None
            status = event.get("status", "")
            steps = event.get("steps", 0)
            reason = event.get("reason") or ""
            if status == "success":
                self._append(
                    Static(
                        f"[bold green]✓ completed[/bold green]  [dim]{steps} steps[/dim]",
                        classes="run-ok",
                    )
                )
            else:
                detail = f"  [dim]{reason}[/dim]" if reason else ""
                self._append(
                    Static(
                        f"[bold red]✗ failed[/bold red]{detail}  [dim]{steps} steps[/dim]",
                        classes="run-err",
                    )
                )

        elif t == "llm.usage":
            run_id = event.get("run_id", "")
            if run_id in self._subagent_run_ids:
                return
            pct = float(event.get("context_pct") or 0.0)
            if "context_window" in event:
                self._last_context_window = event.get("context_window")
            self._last_context_pct = pct
            ctx_bar = (
                "[dim]context window unknown[/dim]"
                if "context_window" in event and event["context_window"] is None
                else self._render_ctx_bar(pct)
            )
            self._append(
                Static(
                    f"[dim]  tokens  "
                    f"in={event.get('input_tokens')} "
                    f"out={event.get('output_tokens')} "
                    f"cache={event.get('cache_read_input_tokens')}[/dim]"
                    f"  {ctx_bar}",
                    classes="usage",
                )
            )

        elif t == "context.sources_loaded":
            self._append(
                Static(
                    f"[dim]Context rules: {len(event.get('sources', []))} sources; "
                    f"{len(event.get('diagnostics', []))} diagnostics. Use /context to inspect.[/dim]",
                    classes="log-line",
                )
            )

        elif t == "context.memory_updated":
            self._append(
                Static(
                    f"[dim]Session memory revision={event.get('revision')} "
                    f"evidence={event.get('source_count')}  /notes[/dim]",
                    classes="log-line",
                )
            )

        elif t == "context.cache_usage":
            self._append(
                Static(
                    f"[dim]Prompt cache read={event.get('cache_read')} "
                    f"created={event.get('cache_create')}[/dim]",
                    classes="usage",
                )
            )

        elif t == "context.budget_checked":
            if event.get("run_id") in self._subagent_run_ids:
                return
            window = event.get("window")
            self._last_context_window = int(window) if window else None
            tokens = int(event.get("input_tokens", 0))
            reserve = int(event.get("reserved_output", 0))
            status = (
                f"input≈{tokens} / {window}  output reserve={reserve}"
                if window
                else "context window unknown"
            )
            self._append(
                Static(
                    f"[dim]  Context budget ({escape(str(event.get('mode', 'shadow')))}) "
                    f"{escape(status)}[/dim]",
                    classes="usage",
                )
            )

        elif t == "context.pruned":
            self._append(
                Static(
                    f"[cyan]Context pruned[/cyan]  results={event.get('count', 0)} "
                    f"saved≈{event.get('saved_tokens', 0)} tokens",
                    classes="log-line",
                )
            )

        elif t == "context.compaction_started":
            self._append(
                Static(
                    f"[cyan]Compacting context[/cyan] revision={event.get('revision')} "
                    f"input≈{event.get('pre_tokens')} target={event.get('target_low_watermark')}",
                    classes="log-line",
                )
            )

        elif t == "context.compaction_failed":
            reason = "circuit open" if event.get("circuit_open") else "original context retained"
            self._append(
                Static(
                    f"[yellow]Compaction failed[/yellow]  {reason} "
                    f"failures={event.get('consecutive_failures', 0)}",
                    classes="log-line",
                )
            )

        elif t == "context.compacted":
            if event.get("run_id") in self._subagent_run_ids:
                self._append(
                    Static(
                        f"[dim]Subagent context compacted: after≈"
                        f"{event.get('projection_tokens') or event.get('summary_tokens', 0)} tokens[/dim]",
                        classes="log-line",
                    )
                )
                return
            orig = event.get("original_tokens", 0)
            summary = event.get("summary_tokens", 0)
            after = event.get("projection_tokens") or summary
            self._last_context_pct = (
                float(after) / self._last_context_window if self._last_context_window else 0.0
            )
            self._append(
                Static(
                    f"[bold cyan]⚡ Context compacted[/bold cyan]"
                    f"  [dim]input≈{orig} → after≈{after} tokens; summary≈{summary}; "
                    f"recent={event.get('retained_messages', 0)} messages "
                    f"revision={event.get('revision', 0)}[/dim]",
                    classes="log-line",
                )
            )

        elif t == "permission.requested":
            if event.get("_replay"):
                return
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            from kama_claude.core.security.redaction import redact

            param_preview = (
                json.dumps(
                    redact(event.get("params", {})),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\nScope: "
                + str(event.get("approval_scope", ""))
            )
            if tool_use_id in self._pending_permission_blocks:
                return
            perm_block = PermissionBlock(tool_use_id, tool_name, param_preview)
            perm_block.approval_token = str(event.get("approval_token", ""))
            self._pending_permission_blocks[tool_use_id] = perm_block
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.border_title = "permission required"
            self._append(perm_block)
            select = PermissionSelect(tool_use_id)
            self._mount_permission_select(select)

        elif t == "permission.denied":
            # 处理超时/断连等非用户交互触发的 deny（用户主动 deny 已由
            # on_permission_select_decided 处理）
            tool_use_id = str(event.get("tool_use_id", ""))
            decision = str(event.get("decision", "denied"))
            if tool_use_id in self._pending_permission_blocks:
                perm_block = self._pending_permission_blocks.pop(tool_use_id)
                perm_block._resolve(decision)
                try:
                    select = self.query_one(PermissionSelect)
                    select.remove()
                except NoMatches:
                    pass
                # 输入恢复统一交给 session.waiting_for_input，deny 后 run 仍在继续

        elif t == "log.line":
            level = event.get("level", "INFO")
            color = "bold red" if level == "ERROR" else ("yellow" if level == "WARNING" else "dim")
            self._append(
                Static(
                    f"[{color}]{escape(str(level))}[/{color}]  "
                    f"[dim]{escape(str(event.get('source', '')))}[/dim]  "
                    f"{escape(str(event.get('message', '')))}",
                    classes="log-line",
                )
            )


# TUI 入口：读取配置并启动 KamaTuiApp
def run(config: KamaConfig, replay_run_id: str | None = None) -> None:
    app = KamaTuiApp(config.host, config.port, replay_run_id=replay_run_id)
    app.run()
