from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7437
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_LOG_FILE = "~/.kama/logs/core.log"
_DEFAULT_LOG_FORMAT = "text"
_DEFAULT_CONFIG_PATH = "~/.kama/config.toml"
_DEFAULT_MAX_STEPS = 20
_DEFAULT_MODEL = "claude-sonnet-4-6"
_DEFAULT_TRACE_FILE = "~/.kama/traces/daemon.jsonl"
_DEFAULT_RAG_KNOWLEDGE_DIR = "~/.kama/knowledge"
_DEFAULT_MEMORY_STORE_DIR = "~/.kama/memory"
_RETRIEVER_CHOICES = ("auto", "bm25", "embedding", "hybrid")


@dataclass
class LoggingConfig:
    level: str = _DEFAULT_LOG_LEVEL
    file: str = _DEFAULT_LOG_FILE
    format: str = _DEFAULT_LOG_FORMAT  # "text" | "json"


@dataclass
class AgentConfig:
    max_steps: int = _DEFAULT_MAX_STEPS


@dataclass
class LlmConfig:
    default_model: str = _DEFAULT_MODEL
    router: str = "static"  # "static" | "rule_based" (S4) | "cost_budget" (S6)


@dataclass
class TraceConfig:
    enabled: bool = True
    file: str = _DEFAULT_TRACE_FILE
    include_llm_payload: bool = True  # false 时 LLM 记录只保留摘要


@dataclass
class PermissionConfig:
    timeout_s: float = 60.0  # 审批超时秒数；0 表示不超时


@dataclass
class CompactionConfig:
    auto_threshold: float = 0.0  # context_pct 触发自动压缩的阈值（0 禁用，推荐手动 /compact）
    tool_result_limit: int = 8_000  # tool_result 截断触发字符数
    tool_result_keep: int = 4_000   # 截断后保留的前缀字符数


@dataclass
class McpServerConfig:
    name: str
    transport: str = "stdio"       # "stdio" | "tcp"
    command: str = ""              # stdio 专用：可执行文件路径
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    host: str = "localhost"        # tcp 专用
    port: int = 3000               # tcp 专用


@dataclass
class McpConfig:
    servers: list[McpServerConfig] = field(default_factory=list)


@dataclass
class RagConfig:
    enabled: bool = False              # 是否注册 rag_search 工具（false 时完全不加载知识库）
    knowledge_dir: str = _DEFAULT_RAG_KNOWLEDGE_DIR  # 知识库根目录，递归读取 .md/.txt
    top_k: int = 3                     # 每次检索返回的 chunk 数
    chunk_size: int = 800              # 语义切片的目标/上限字符数
    chunk_overlap: int = 100           # 超长单元硬切回退时的重叠字符数
    api_key: str = ""                  # 智谱 API key；留空则回退本地 TF-IDF
    embedding_model: str = "embedding-3"  # 智谱 embedding 模型名
    embedding_base_url: str = "https://open.bigmodel.cn/api/paas/v4"  # embedding API 根地址
    embedding_dimensions: int = 2048   # 输出向量维度（256/512/1024/2048）
    embedding_batch_size: int = 64     # 单次请求最大输入条数（≤64）
    index_cache: bool = True           # 是否将向量落盘缓存，避免重启重复计费
    retriever: str = "auto"            # 检索后端：auto/bm25/embedding/hybrid
    rerank_enabled: bool = False       # 是否在检索后加 rerank 精排（按量计费，默认关闭）
    rerank_model: str = "rerank"       # 智谱 rerank 模型代码（官方公共重排序模型为 "rerank"）
    rerank_depth: int = 50             # rerank 前取回的候选 chunk 数（受 API ≤64 上限约束）
    vector_db: str = ""                # 向量数据库后端；留空走内置 JSON 缓存，可选 "qdrant"
    qdrant_url: str = ""               # Qdrant 服务地址；留空走本地 local mode（路径持久化）
    qdrant_path: str = "~/.kama/qdrant"  # Qdrant local mode 的持久化目录
    query_enhance_enabled: bool = False  # 是否启用 LLM 查询改写+拓展（按量计费，默认关闭）
    query_expand_n: int = 3            # 查询拓展生成的子查询条数（≥1）
    query_depth: int = 10              # 多查询融合时每条子查询的召回候选数


@dataclass
class MemoryConfig:
    enabled: bool = False             # 是否注册 memory_* 工具并启用长期记忆层
    store_dir: str = _DEFAULT_MEMORY_STORE_DIR  # sqlite 库与向量缓存的根目录
    inject_top_n: int = 5             # 主动注入 System Prompt 的记忆条数
    search_top_k: int = 5             # memory_search 默认返回条数
    max_structured_len: int = 500     # 超过此长度的内容改走向量层
    dedup_threshold: float = 0.90     # vector 层语义去重阈值
    half_life_days: float = 30.0      # recency 衰减半衰期（天）
    max_entries: int = 5000           # 超过此阈值触发自动整合


@dataclass
class KamaConfig:
    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    trace: TraceConfig = field(default_factory=TraceConfig)
    permission: PermissionConfig = field(default_factory=PermissionConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    rag: RagConfig = field(default_factory=RagConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)


# 构建并返回运行时配置：默认值 → 全局 TOML → 项目本地 TOML → .env → 系统环境变量（后者优先级最高）
def get_config() -> KamaConfig:
    config = KamaConfig()

    # .env 必须在读取 KAMA_CONFIG 之前加载，以便 .env 中的 KAMA_CONFIG 能影响 TOML 路径
    load_dotenv(".env", override=False)

    # 若显式指定 KAMA_CONFIG，只读该文件；否则按优先级叠加：全局 → 项目本地
    explicit = os.environ.get("KAMA_CONFIG")
    if explicit:
        config_paths = [Path(explicit).expanduser()]
    else:
        config_paths = [
            Path(_DEFAULT_CONFIG_PATH).expanduser(),
            Path(".kama/config.toml"),
        ]

    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, "rb") as f:
                    data = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise SystemExit(f"Config parse error ({config_path}): {e}") from e
            _apply_toml(config, data)

    _apply_env(config)
    return config


# 将已解析的 TOML 根表写入 config；未知小节或类型错误时退出进程
def _apply_toml(config: KamaConfig, data: dict[str, Any]) -> None:
    unknown = set(data.keys()) - {
        "core", "logging", "agent", "llm", "trace", "permission", "compaction", "mcp",
        "rag", "memory",
    }
    if unknown:
        raise SystemExit(f"Unknown top-level config keys: {', '.join(sorted(unknown))}")

    if "core" in data:
        core = data["core"]
        if not isinstance(core, dict):
            raise SystemExit("Config error: [core] must be a table")
        unknown_core: set[str] = set(core.keys()) - {"host", "port"}
        if unknown_core:
            raise SystemExit(f"Unknown [core] keys: {', '.join(sorted(unknown_core))}")
        if "host" in core:
            val = core["host"]
            if not isinstance(val, str):
                raise SystemExit("Config error: core.host must be a string")
            config.host = val
        if "port" in core:
            val = core["port"]
            if not isinstance(val, int):
                raise SystemExit("Config error: core.port must be an integer")
            config.port = val

    if "logging" in data:
        log = data["logging"]
        if not isinstance(log, dict):
            raise SystemExit("Config error: [logging] must be a table")
        unknown_log: set[str] = set(log.keys()) - {"level", "file", "format"}
        if unknown_log:
            raise SystemExit(f"Unknown [logging] keys: {', '.join(sorted(unknown_log))}")
        for key in ("level", "file", "format"):
            if key in log:
                val = log[key]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: logging.{key} must be a string")
                setattr(config.logging, key, val)

    if "agent" in data:
        agent = data["agent"]
        if not isinstance(agent, dict):
            raise SystemExit("Config error: [agent] must be a table")
        unknown_agent: set[str] = set(agent.keys()) - {"max_steps"}
        if unknown_agent:
            raise SystemExit(f"Unknown [agent] keys: {', '.join(sorted(unknown_agent))}")
        if "max_steps" in agent:
            val = agent["max_steps"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit("Config error: agent.max_steps must be a positive integer")
            config.agent.max_steps = val

    if "llm" in data:
        llm = data["llm"]
        if not isinstance(llm, dict):
            raise SystemExit("Config error: [llm] must be a table")
        unknown_llm: set[str] = set(llm.keys()) - {"default_model", "router"}
        if unknown_llm:
            raise SystemExit(f"Unknown [llm] keys: {', '.join(sorted(unknown_llm))}")
        if "default_model" in llm:
            val = llm["default_model"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.default_model must be a string")
            config.llm.default_model = val
        if "router" in llm:
            val = llm["router"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.router must be a string")
            config.llm.router = val

    if "trace" in data:
        trace = data["trace"]
        if not isinstance(trace, dict):
            raise SystemExit("Config error: [trace] must be a table")
        unknown_trace: set[str] = set(trace.keys()) - {"enabled", "file", "include_llm_payload"}
        if unknown_trace:
            raise SystemExit(f"Unknown [trace] keys: {', '.join(sorted(unknown_trace))}")
        if "enabled" in trace:
            val = trace["enabled"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: trace.enabled must be a boolean")
            config.trace.enabled = val
        if "file" in trace:
            val = trace["file"]
            if not isinstance(val, str):
                raise SystemExit("Config error: trace.file must be a string")
            config.trace.file = val
        if "include_llm_payload" in trace:
            val = trace["include_llm_payload"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: trace.include_llm_payload must be a boolean")
            config.trace.include_llm_payload = val

    if "permission" in data:
        perm = data["permission"]
        if not isinstance(perm, dict):
            raise SystemExit("Config error: [permission] must be a table")
        unknown_perm: set[str] = set(perm.keys()) - {"timeout_s"}
        if unknown_perm:
            raise SystemExit(f"Unknown [permission] keys: {', '.join(sorted(unknown_perm))}")
        if "timeout_s" in perm:
            val = perm["timeout_s"]
            if not isinstance(val, (int, float)) or val < 0:
                raise SystemExit("Config error: permission.timeout_s must be a non-negative number")
            config.permission.timeout_s = float(val)

    if "compaction" in data:
        comp = data["compaction"]
        if not isinstance(comp, dict):
            raise SystemExit("Config error: [compaction] must be a table")
        unknown_comp: set[str] = set(comp.keys()) - {
            "auto_threshold", "tool_result_limit", "tool_result_keep"
        }
        if unknown_comp:
            raise SystemExit(f"Unknown [compaction] keys: {', '.join(sorted(unknown_comp))}")
        if "auto_threshold" in comp:
            val = comp["auto_threshold"]
            if not isinstance(val, (int, float)) or not (0.0 <= val <= 1.0):
                raise SystemExit("Config error: compaction.auto_threshold must be between 0 and 1")
            config.compaction.auto_threshold = float(val)
        if "tool_result_limit" in comp:
            val = comp["tool_result_limit"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit(
                    "Config error: compaction.tool_result_limit must be a positive integer"
                )
            config.compaction.tool_result_limit = val
        if "tool_result_keep" in comp:
            val = comp["tool_result_keep"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit(
                    "Config error: compaction.tool_result_keep must be a positive integer"
                )
            config.compaction.tool_result_keep = val

    if "mcp" in data:
        mcp = data["mcp"]
        if not isinstance(mcp, dict):
            raise SystemExit("Config error: [mcp] must be a table")
        unknown_mcp: set[str] = set(mcp.keys()) - {"servers"}
        if unknown_mcp:
            raise SystemExit(f"Unknown [mcp] keys: {', '.join(sorted(unknown_mcp))}")
        servers_raw = mcp.get("servers", [])
        if not isinstance(servers_raw, list):
            raise SystemExit("Config error: mcp.servers must be an array of tables")
        for i, srv in enumerate(servers_raw):
            if not isinstance(srv, dict):
                raise SystemExit(f"Config error: mcp.servers[{i}] must be a table")
            name = srv.get("name")
            if not isinstance(name, str) or not name:
                raise SystemExit(f"Config error: mcp.servers[{i}].name must be a non-empty string")
            transport = srv.get("transport", "stdio")
            if transport not in ("stdio", "tcp"):
                raise SystemExit(
                    f"Config error: mcp.servers[{i}].transport must be 'stdio' or 'tcp'"
                )
            s = McpServerConfig(name=name, transport=transport)
            if "command" in srv:
                val = srv["command"]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: mcp.servers[{i}].command must be a string")
                s.command = val
            if "args" in srv:
                val = srv["args"]
                if not isinstance(val, list):
                    raise SystemExit(f"Config error: mcp.servers[{i}].args must be an array")
                s.args = [str(a) for a in val]
            if "env" in srv:
                val = srv["env"]
                if not isinstance(val, dict):
                    raise SystemExit(f"Config error: mcp.servers[{i}].env must be a table")
                s.env = {str(k): str(v) for k, v in val.items()}
            if "host" in srv:
                val = srv["host"]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: mcp.servers[{i}].host must be a string")
                s.host = val
            if "port" in srv:
                val = srv["port"]
                if not isinstance(val, int):
                    raise SystemExit(f"Config error: mcp.servers[{i}].port must be an integer")
                s.port = val
            config.mcp.servers.append(s)

    if "rag" in data:
        rag = data["rag"]
        if not isinstance(rag, dict):
            raise SystemExit("Config error: [rag] must be a table")
        unknown_rag: set[str] = set(rag.keys()) - {
            "enabled", "knowledge_dir", "top_k", "chunk_size", "chunk_overlap",
            "api_key", "embedding_model", "embedding_base_url", "embedding_dimensions",
            "embedding_batch_size", "index_cache", "retriever",
            "rerank_enabled", "rerank_model", "rerank_depth",
            "vector_db", "qdrant_url", "qdrant_path",
            "query_enhance_enabled", "query_expand_n", "query_depth",
        }
        if unknown_rag:
            raise SystemExit(f"Unknown [rag] keys: {', '.join(sorted(unknown_rag))}")
        if "enabled" in rag:
            val = rag["enabled"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: rag.enabled must be a boolean")
            config.rag.enabled = val
        if "knowledge_dir" in rag:
            val = rag["knowledge_dir"]
            if not isinstance(val, str):
                raise SystemExit("Config error: rag.knowledge_dir must be a string")
            config.rag.knowledge_dir = val
        if "top_k" in rag:
            val = rag["top_k"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit("Config error: rag.top_k must be a positive integer")
            config.rag.top_k = val
        if "chunk_size" in rag:
            val = rag["chunk_size"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit("Config error: rag.chunk_size must be a positive integer")
            config.rag.chunk_size = val
        if "chunk_overlap" in rag:
            val = rag["chunk_overlap"]
            if not isinstance(val, int) or val < 0:
                raise SystemExit("Config error: rag.chunk_overlap must be a non-negative integer")
            config.rag.chunk_overlap = val
        if "api_key" in rag:
            val = rag["api_key"]
            if not isinstance(val, str):
                raise SystemExit("Config error: rag.api_key must be a string")
            config.rag.api_key = val
        if "embedding_model" in rag:
            val = rag["embedding_model"]
            if not isinstance(val, str):
                raise SystemExit("Config error: rag.embedding_model must be a string")
            config.rag.embedding_model = val
        if "embedding_base_url" in rag:
            val = rag["embedding_base_url"]
            if not isinstance(val, str):
                raise SystemExit("Config error: rag.embedding_base_url must be a string")
            config.rag.embedding_base_url = val
        if "embedding_dimensions" in rag:
            val = rag["embedding_dimensions"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit(
                    "Config error: rag.embedding_dimensions must be a positive integer"
                )
            config.rag.embedding_dimensions = val
        if "embedding_batch_size" in rag:
            val = rag["embedding_batch_size"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit(
                    "Config error: rag.embedding_batch_size must be a positive integer"
                )
            config.rag.embedding_batch_size = val
        if "index_cache" in rag:
            val = rag["index_cache"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: rag.index_cache must be a boolean")
            config.rag.index_cache = val
        if "retriever" in rag:
            val = rag["retriever"]
            if not isinstance(val, str) or val not in _RETRIEVER_CHOICES:
                raise SystemExit(
                    "Config error: rag.retriever must be one of "
                    f"{list(_RETRIEVER_CHOICES)}, got: {val!r}"
                )
            config.rag.retriever = val
        if "rerank_enabled" in rag:
            val = rag["rerank_enabled"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: rag.rerank_enabled must be a boolean")
            config.rag.rerank_enabled = val
        if "rerank_model" in rag:
            val = rag["rerank_model"]
            if not isinstance(val, str):
                raise SystemExit("Config error: rag.rerank_model must be a string")
            config.rag.rerank_model = val
        if "rerank_depth" in rag:
            val = rag["rerank_depth"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit("Config error: rag.rerank_depth must be a positive integer")
            config.rag.rerank_depth = val
        if "vector_db" in rag:
            val = rag["vector_db"]
            if not isinstance(val, str):
                raise SystemExit("Config error: rag.vector_db must be a string")
            config.rag.vector_db = val
        if "qdrant_url" in rag:
            val = rag["qdrant_url"]
            if not isinstance(val, str):
                raise SystemExit("Config error: rag.qdrant_url must be a string")
            config.rag.qdrant_url = val
        if "qdrant_path" in rag:
            val = rag["qdrant_path"]
            if not isinstance(val, str):
                raise SystemExit("Config error: rag.qdrant_path must be a string")
            config.rag.qdrant_path = val
        if "query_enhance_enabled" in rag:
            val = rag["query_enhance_enabled"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: rag.query_enhance_enabled must be a boolean")
            config.rag.query_enhance_enabled = val
        if "query_expand_n" in rag:
            val = rag["query_expand_n"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit(
                    "Config error: rag.query_expand_n must be a positive integer"
                )
            config.rag.query_expand_n = val
        if "query_depth" in rag:
            val = rag["query_depth"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit(
                    "Config error: rag.query_depth must be a positive integer"
                )
            config.rag.query_depth = val

    if "memory" in data:
        mem = data["memory"]
        if not isinstance(mem, dict):
            raise SystemExit("Config error: [memory] must be a table")
        unknown_mem: set[str] = set(mem.keys()) - {
            "enabled", "store_dir", "inject_top_n", "search_top_k",
            "max_structured_len", "dedup_threshold", "half_life_days", "max_entries",
        }
        if unknown_mem:
            raise SystemExit(f"Unknown [memory] keys: {', '.join(sorted(unknown_mem))}")
        if "enabled" in mem:
            val = mem["enabled"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: memory.enabled must be a boolean")
            config.memory.enabled = val
        if "store_dir" in mem:
            val = mem["store_dir"]
            if not isinstance(val, str):
                raise SystemExit("Config error: memory.store_dir must be a string")
            config.memory.store_dir = val
        if "inject_top_n" in mem:
            val = mem["inject_top_n"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit("Config error: memory.inject_top_n must be a positive integer")
            config.memory.inject_top_n = val
        if "search_top_k" in mem:
            val = mem["search_top_k"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit("Config error: memory.search_top_k must be a positive integer")
            config.memory.search_top_k = val
        if "max_structured_len" in mem:
            val = mem["max_structured_len"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit(
                    "Config error: memory.max_structured_len must be a positive integer"
                )
            config.memory.max_structured_len = val
        if "dedup_threshold" in mem:
            val = mem["dedup_threshold"]
            if not isinstance(val, (int, float)) or not (0.0 < val <= 1.0):
                raise SystemExit(
                    "Config error: memory.dedup_threshold must be between 0 and 1 (exclusive)"
                )
            config.memory.dedup_threshold = float(val)
        if "half_life_days" in mem:
            val = mem["half_life_days"]
            if not isinstance(val, (int, float)) or val <= 0:
                raise SystemExit(
                    "Config error: memory.half_life_days must be a positive number"
                )
            config.memory.half_life_days = float(val)
        if "max_entries" in mem:
            val = mem["max_entries"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit("Config error: memory.max_entries must be a positive integer")
            config.memory.max_entries = val


# 用 KAMA_* 环境变量覆盖 config 中对应字段（若变量已设置）
def _apply_env(config: KamaConfig) -> None:
    host = os.environ.get("KAMA_HOST")
    if host is not None:
        config.host = host

    port_str = os.environ.get("KAMA_PORT")
    if port_str is not None:
        try:
            config.port = int(port_str)
        except ValueError:
            raise SystemExit(f"Config error: KAMA_PORT must be an integer, got: {port_str!r}")

    log_level = os.environ.get("KAMA_LOG_LEVEL")
    if log_level is not None:
        config.logging.level = log_level

    log_file = os.environ.get("KAMA_LOG_FILE")
    if log_file is not None:
        config.logging.file = log_file

    log_format = os.environ.get("KAMA_LOG_FORMAT")
    if log_format is not None:
        config.logging.format = log_format

    max_steps_str = os.environ.get("KAMA_MAX_STEPS")
    if max_steps_str is not None:
        try:
            val = int(max_steps_str)
            if val <= 0:
                raise SystemExit(
                    "Config error: KAMA_MAX_STEPS must be a positive integer,"
                    f" got: {max_steps_str!r}"
                )
            config.agent.max_steps = val
        except ValueError:
            raise SystemExit(
                f"Config error: KAMA_MAX_STEPS must be an integer, got: {max_steps_str!r}"
            )

    default_model = os.environ.get("KAMA_LLM_DEFAULT_MODEL")
    if default_model is not None:
        config.llm.default_model = default_model

    trace_enabled = os.environ.get("KAMA_TRACE_ENABLED")
    if trace_enabled is not None:
        config.trace.enabled = trace_enabled.lower() not in ("0", "false", "no")

    trace_file = os.environ.get("KAMA_TRACE_FILE")
    if trace_file is not None:
        config.trace.file = trace_file

    trace_payload = os.environ.get("KAMA_TRACE_INCLUDE_LLM_PAYLOAD")
    if trace_payload is not None:
        config.trace.include_llm_payload = trace_payload.lower() not in ("0", "false", "no")

    perm_timeout = os.environ.get("KAMA_PERMISSION_TIMEOUT_S")
    if perm_timeout is not None:
        try:
            perm_timeout_val = float(perm_timeout)
            if perm_timeout_val < 0:
                raise SystemExit(
                    f"Config error: KAMA_PERMISSION_TIMEOUT_S must be >= 0, got: {perm_timeout!r}"
                )
            config.permission.timeout_s = perm_timeout_val
        except ValueError:
            raise SystemExit(
                f"Config error: KAMA_PERMISSION_TIMEOUT_S must be a number, got: {perm_timeout!r}"
            )

    compact_threshold = os.environ.get("KAMA_COMPACT_THRESHOLD")
    if compact_threshold is not None:
        try:
            compact_threshold_val = float(compact_threshold)
            if not (0.0 <= compact_threshold_val <= 1.0):
                raise SystemExit(
                    "Config error: KAMA_COMPACT_THRESHOLD must be between 0 and 1, "
                    f"got: {compact_threshold!r}"
                )
            config.compaction.auto_threshold = compact_threshold_val
        except ValueError:
            raise SystemExit(
                f"Config error: KAMA_COMPACT_THRESHOLD must be a number, got: {compact_threshold!r}"
            )

    compact_tool_limit = os.environ.get("KAMA_COMPACT_TOOL_LIMIT")
    if compact_tool_limit is not None:
        try:
            compact_tool_limit_val = int(compact_tool_limit)
            if compact_tool_limit_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_COMPACT_TOOL_LIMIT must be a positive integer, "
                    f"got: {compact_tool_limit!r}"
                )
            config.compaction.tool_result_limit = compact_tool_limit_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_COMPACT_TOOL_LIMIT must be an integer, "
                f"got: {compact_tool_limit!r}"
            )

    compact_tool_keep = os.environ.get("KAMA_COMPACT_TOOL_KEEP")
    if compact_tool_keep is not None:
        try:
            compact_tool_keep_val = int(compact_tool_keep)
            if compact_tool_keep_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_COMPACT_TOOL_KEEP must be a positive integer, "
                    f"got: {compact_tool_keep!r}"
                )
            config.compaction.tool_result_keep = compact_tool_keep_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_COMPACT_TOOL_KEEP must be an integer, "
                f"got: {compact_tool_keep!r}"
            )

    rag_enabled = os.environ.get("KAMA_RAG_ENABLED")
    if rag_enabled is not None:
        config.rag.enabled = rag_enabled.lower() not in ("0", "false", "no")

    rag_knowledge_dir = os.environ.get("KAMA_RAG_KNOWLEDGE_DIR")
    if rag_knowledge_dir is not None:
        config.rag.knowledge_dir = rag_knowledge_dir

    rag_top_k = os.environ.get("KAMA_RAG_TOP_K")
    if rag_top_k is not None:
        try:
            rag_top_k_val = int(rag_top_k)
            if rag_top_k_val <= 0:
                raise SystemExit(
                    f"Config error: KAMA_RAG_TOP_K must be a positive integer, got: {rag_top_k!r}"
                )
            config.rag.top_k = rag_top_k_val
        except ValueError:
            raise SystemExit(
                f"Config error: KAMA_RAG_TOP_K must be an integer, got: {rag_top_k!r}"
            )

    rag_chunk_size = os.environ.get("KAMA_RAG_CHUNK_SIZE")
    if rag_chunk_size is not None:
        try:
            rag_chunk_size_val = int(rag_chunk_size)
            if rag_chunk_size_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_RAG_CHUNK_SIZE must be a positive integer, "
                    f"got: {rag_chunk_size!r}"
                )
            config.rag.chunk_size = rag_chunk_size_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_RAG_CHUNK_SIZE must be an integer, "
                f"got: {rag_chunk_size!r}"
            )

    rag_chunk_overlap = os.environ.get("KAMA_RAG_CHUNK_OVERLAP")
    if rag_chunk_overlap is not None:
        try:
            rag_chunk_overlap_val = int(rag_chunk_overlap)
            if rag_chunk_overlap_val < 0:
                raise SystemExit(
                    "Config error: KAMA_RAG_CHUNK_OVERLAP must be a non-negative integer, "
                    f"got: {rag_chunk_overlap!r}"
                )
            config.rag.chunk_overlap = rag_chunk_overlap_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_RAG_CHUNK_OVERLAP must be an integer, "
                f"got: {rag_chunk_overlap!r}"
            )

    rag_api_key = os.environ.get("KAMA_RAG_API_KEY") or os.environ.get("ZHIPU_API_KEY")
    if rag_api_key is not None:
        config.rag.api_key = rag_api_key

    rag_embedding_model = os.environ.get("KAMA_RAG_EMBEDDING_MODEL")
    if rag_embedding_model is not None:
        config.rag.embedding_model = rag_embedding_model

    rag_embedding_base_url = os.environ.get("KAMA_RAG_EMBEDDING_BASE_URL")
    if rag_embedding_base_url is not None:
        config.rag.embedding_base_url = rag_embedding_base_url

    rag_embedding_dimensions = os.environ.get("KAMA_RAG_EMBEDDING_DIMENSIONS")
    if rag_embedding_dimensions is not None:
        try:
            rag_embedding_dimensions_val = int(rag_embedding_dimensions)
            if rag_embedding_dimensions_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_RAG_EMBEDDING_DIMENSIONS must be a positive integer, "
                    f"got: {rag_embedding_dimensions!r}"
                )
            config.rag.embedding_dimensions = rag_embedding_dimensions_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_RAG_EMBEDDING_DIMENSIONS must be an integer, "
                f"got: {rag_embedding_dimensions!r}"
            )

    rag_embedding_batch_size = os.environ.get("KAMA_RAG_EMBEDDING_BATCH_SIZE")
    if rag_embedding_batch_size is not None:
        try:
            rag_embedding_batch_size_val = int(rag_embedding_batch_size)
            if rag_embedding_batch_size_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_RAG_EMBEDDING_BATCH_SIZE must be a positive integer, "
                    f"got: {rag_embedding_batch_size!r}"
                )
            config.rag.embedding_batch_size = rag_embedding_batch_size_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_RAG_EMBEDDING_BATCH_SIZE must be an integer, "
                f"got: {rag_embedding_batch_size!r}"
            )

    rag_index_cache = os.environ.get("KAMA_RAG_INDEX_CACHE")
    if rag_index_cache is not None:
        config.rag.index_cache = rag_index_cache.lower() not in ("0", "false", "no")

    rag_retriever = os.environ.get("KAMA_RAG_RETRIEVER")
    if rag_retriever is not None:
        if rag_retriever not in _RETRIEVER_CHOICES:
            raise SystemExit(
                "Config error: KAMA_RAG_RETRIEVER must be one of "
                f"{list(_RETRIEVER_CHOICES)}, got: {rag_retriever!r}"
            )
        config.rag.retriever = rag_retriever

    rag_rerank_enabled = os.environ.get("KAMA_RAG_RERANK_ENABLED")
    if rag_rerank_enabled is not None:
        config.rag.rerank_enabled = rag_rerank_enabled.lower() not in ("0", "false", "no")

    rag_rerank_model = os.environ.get("KAMA_RAG_RERANK_MODEL")
    if rag_rerank_model is not None:
        config.rag.rerank_model = rag_rerank_model

    rag_rerank_depth = os.environ.get("KAMA_RAG_RERANK_DEPTH")
    if rag_rerank_depth is not None:
        try:
            rag_rerank_depth_val = int(rag_rerank_depth)
            if rag_rerank_depth_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_RAG_RERANK_DEPTH must be a positive integer, "
                    f"got: {rag_rerank_depth!r}"
                )
            config.rag.rerank_depth = rag_rerank_depth_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_RAG_RERANK_DEPTH must be an integer, "
                f"got: {rag_rerank_depth!r}"
            )

    rag_vector_db = os.environ.get("KAMA_RAG_VECTOR_DB")
    if rag_vector_db is not None:
        config.rag.vector_db = rag_vector_db

    rag_qdrant_url = os.environ.get("KAMA_RAG_QDRANT_URL")
    if rag_qdrant_url is not None:
        config.rag.qdrant_url = rag_qdrant_url

    rag_qdrant_path = os.environ.get("KAMA_RAG_QDRANT_PATH")
    if rag_qdrant_path is not None:
        config.rag.qdrant_path = rag_qdrant_path

    rag_query_enhance = os.environ.get("KAMA_RAG_QUERY_ENHANCE")
    if rag_query_enhance is not None:
        config.rag.query_enhance_enabled = rag_query_enhance.lower() not in ("0", "false", "no")

    rag_query_expand_n = os.environ.get("KAMA_RAG_QUERY_EXPAND_N")
    if rag_query_expand_n is not None:
        try:
            rag_query_expand_n_val = int(rag_query_expand_n)
            if rag_query_expand_n_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_RAG_QUERY_EXPAND_N must be a positive integer, "
                    f"got: {rag_query_expand_n!r}"
                )
            config.rag.query_expand_n = rag_query_expand_n_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_RAG_QUERY_EXPAND_N must be an integer, "
                f"got: {rag_query_expand_n!r}"
            )

    rag_query_depth = os.environ.get("KAMA_RAG_QUERY_DEPTH")
    if rag_query_depth is not None:
        try:
            rag_query_depth_val = int(rag_query_depth)
            if rag_query_depth_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_RAG_QUERY_DEPTH must be a positive integer, "
                    f"got: {rag_query_depth!r}"
                )
            config.rag.query_depth = rag_query_depth_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_RAG_QUERY_DEPTH must be an integer, "
                f"got: {rag_query_depth!r}"
            )

    mem_enabled = os.environ.get("KAMA_MEMORY_ENABLED")
    if mem_enabled is not None:
        config.memory.enabled = mem_enabled.lower() not in ("0", "false", "no")

    mem_store_dir = os.environ.get("KAMA_MEMORY_STORE_DIR")
    if mem_store_dir is not None:
        config.memory.store_dir = mem_store_dir

    mem_inject_top_n = os.environ.get("KAMA_MEMORY_INJECT_TOP_N")
    if mem_inject_top_n is not None:
        try:
            mem_inject_top_n_val = int(mem_inject_top_n)
            if mem_inject_top_n_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_MEMORY_INJECT_TOP_N must be a positive integer, "
                    f"got: {mem_inject_top_n!r}"
                )
            config.memory.inject_top_n = mem_inject_top_n_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_MEMORY_INJECT_TOP_N must be an integer, "
                f"got: {mem_inject_top_n!r}"
            )

    mem_search_top_k = os.environ.get("KAMA_MEMORY_SEARCH_TOP_K")
    if mem_search_top_k is not None:
        try:
            mem_search_top_k_val = int(mem_search_top_k)
            if mem_search_top_k_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_MEMORY_SEARCH_TOP_K must be a positive integer, "
                    f"got: {mem_search_top_k!r}"
                )
            config.memory.search_top_k = mem_search_top_k_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_MEMORY_SEARCH_TOP_K must be an integer, "
                f"got: {mem_search_top_k!r}"
            )

    mem_max_structured = os.environ.get("KAMA_MEMORY_MAX_STRUCTURED_LEN")
    if mem_max_structured is not None:
        try:
            mem_max_structured_val = int(mem_max_structured)
            if mem_max_structured_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_MEMORY_MAX_STRUCTURED_LEN must be a positive integer, "
                    f"got: {mem_max_structured!r}"
                )
            config.memory.max_structured_len = mem_max_structured_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_MEMORY_MAX_STRUCTURED_LEN must be an integer, "
                f"got: {mem_max_structured!r}"
            )

    mem_dedup = os.environ.get("KAMA_MEMORY_DEDUP_THRESHOLD")
    if mem_dedup is not None:
        try:
            mem_dedup_val = float(mem_dedup)
            if not (0.0 < mem_dedup_val <= 1.0):
                raise SystemExit(
                    "Config error: KAMA_MEMORY_DEDUP_THRESHOLD must be between 0 and 1, "
                    f"got: {mem_dedup!r}"
                )
            config.memory.dedup_threshold = mem_dedup_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_MEMORY_DEDUP_THRESHOLD must be a number, "
                f"got: {mem_dedup!r}"
            )

    mem_half_life = os.environ.get("KAMA_MEMORY_HALF_LIFE_DAYS")
    if mem_half_life is not None:
        try:
            mem_half_life_val = float(mem_half_life)
            if mem_half_life_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_MEMORY_HALF_LIFE_DAYS must be positive, "
                    f"got: {mem_half_life!r}"
                )
            config.memory.half_life_days = mem_half_life_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_MEMORY_HALF_LIFE_DAYS must be a number, "
                f"got: {mem_half_life!r}"
            )

    mem_max_entries = os.environ.get("KAMA_MEMORY_MAX_ENTRIES")
    if mem_max_entries is not None:
        try:
            mem_max_entries_val = int(mem_max_entries)
            if mem_max_entries_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_MEMORY_MAX_ENTRIES must be a positive integer, "
                    f"got: {mem_max_entries!r}"
                )
            config.memory.max_entries = mem_max_entries_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_MEMORY_MAX_ENTRIES must be an integer, "
                f"got: {mem_max_entries!r}"
            )
