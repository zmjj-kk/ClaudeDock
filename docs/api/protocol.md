# KamaClaude API 参考

本文档描述 KamaClaude 的 JSON-RPC 2.0 协议定义。

## 概览

- **传输协议**: TCP (默认 `127.0.0.1:7437`)
- **编码格式**: NDJSON (每行一个 JSON 对象)
- **协议标准**: [JSON-RPC 2.0](https://www.jsonrpc.org/specification)

---

## 请求格式

```json
{
  "jsonrpc": "2.0",
  "method": "core.ping",
  "params": {
    "client": "kama-cli"
  },
  "id": 1
}
```

## 响应格式

### 成功响应

```json
{
  "jsonrpc": "2.0",
  "result": {
    "server_version": "0.0.1",
    "uptime_ms": 12345
  },
  "id": 1
}
```

### 错误响应

```json
{
  "jsonrpc": "2.0",
  "error": {
    "code": -32601,
    "message": "Method not found",
    "data": {"method": "unknown.method"}
  },
  "id": 1
}
```

---

## 核心命令

### `core.ping`

测试服务器连通性。

**请求参数**:
```json
{
  "client": "kama-cli"  // 客户端标识（可选）
}
```

**响应**:
```json
{
  "server_version": "0.0.1",
  "uptime_ms": 12345,
  "received_at": "2024-10-09T10:00:00Z"
}
```

---

## 会话管理

### `session.create`

创建新会话。

**请求参数**:
```json
{
  "session_id": "sess_abc123",  // 可选，不提供则自动生成
  "system_prompt": "You are a helpful assistant.",
  "initial_message": "Hello!"  // 可选
}
```

**响应**:
```json
{
  "session_id": "sess_abc123",
  "created_at": "2024-10-09T10:00:00Z"
}
```

### `session.send_message`

向会话发送消息。

**请求参数**:
```json
{
  "session_id": "sess_abc123",
  "content": "列出当前目录的文件"
}
```

**响应**:
```json
{
  "message_id": "msg_xyz789",
  "status": "processing"
}
```

### `session.get_history`

获取会话历史。

**请求参数**:
```json
{
  "session_id": "sess_abc123",
  "limit": 50,  // 可选，默认 50
  "offset": 0   // 可选，默认 0
}
```

**响应**:
```json
{
  "messages": [
    {
      "role": "user",
      "content": "Hello!",
      "timestamp": "2024-10-09T10:00:00Z"
    },
    {
      "role": "assistant",
      "content": "Hi! How can I help?",
      "timestamp": "2024-10-09T10:00:01Z"
    }
  ],
  "total": 2
}
```

### `session.close`

关闭会话。

**请求参数**:
```json
{
  "session_id": "sess_abc123"
}
```

**响应**:
```json
{
  "status": "closed"
}
```

### `session.compact`

压缩会话上下文。

**请求参数**:
```json
{
  "session_id": "sess_abc123",
  "strategy": "adaptive"  // "adaptive" | "aggressive" | "conservative"
}
```

**响应**:
```json
{
  "original_tokens": 150000,
  "compacted_tokens": 80000,
  "compression_ratio": 0.53,
  "removed_blocks": 45
}
```

---

## Agent 运行

### `agent.run`

执行一次完整的 agent 运行。

**请求参数**:
```json
{
  "session_id": "sess_abc123",  // 可选，不提供则创建临时会话
  "user_message": "分析这个项目的结构",
  "max_steps": 10,  // 可选，默认 50
  "tools": ["bash", "read_file", "list_dir"]  // 可选，限制可用工具
}
```

**响应**:
```json
{
  "run_id": "run_def456",
  "status": "started",
  "session_id": "sess_abc123"
}
```

---

## 事件订阅

### `event.subscribe`

订阅事件流（服务器推送）。

**请求参数**:
```json
{
  "event_types": ["token", "usage", "tool_call"],  // 可选，不提供则订阅所有
  "session_id": "sess_abc123"  // 可选，仅订阅特定会话的事件
}
```

**响应**:
```json
{
  "subscription_id": "sub_ghi789"
}
```

**服务器推送事件**:
```json
{
  "jsonrpc": "2.0",
  "method": "event.push",
  "params": {
    "subscription_id": "sub_ghi789",
    "event": {
      "type": "token",
      "run_id": "run_def456",
      "delta": "Hello"
    }
  }
}
```

---

## 权限管理

### `permission.respond`

响应权限请求。

**请求参数**:
```json
{
  "request_id": "req_jkl012",
  "decision": "allow",  // "allow" | "deny"
  "remember": "session"  // "never" | "session" | "always"
}
```

**响应**:
```json
{
  "status": "acknowledged"
}
```

---

## 内存管理

### `memory.consolidate`

触发记忆整合（合并相似记忆、清理过期数据）。

**请求参数**:
```json
{
  "session_id": "sess_abc123",
  "strategy": "merge_similar"  // "merge_similar" | "prune_old"
}
```

**响应**:
```json
{
  "merged_count": 5,
  "pruned_count": 12,
  "total_memories": 123
}
```

---

## 错误码

| 错误码 | 说明 |
|--------|------|
| `-32700` | Parse error (JSON 格式错误) |
| `-32600` | Invalid Request (缺少必需字段) |
| `-32601` | Method not found (方法不存在) |
| `-32602` | Invalid params (参数类型错误) |
| `-32603` | Internal error (服务器内部错误) |
| `1000` | Session not found |
| `1001` | Permission denied |
| `1002` | Tool execution failed |
| `1003` | LLM API error |

---

## 事件类型

### Token Event

流式输出的 token 增量。

```json
{
  "type": "token",
  "run_id": "run_def456",
  "step": 1,
  "delta": "Hello"
}
```

### Usage Event

LLM 调用的 token 使用量。

```json
{
  "type": "usage",
  "run_id": "run_def456",
  "input_tokens": 1234,
  "output_tokens": 567,
  "cache_read_tokens": 8900
}
```

### Tool Call Event

工具调用开始。

```json
{
  "type": "tool_call",
  "run_id": "run_def456",
  "tool_name": "bash",
  "tool_input": {"command": "ls -la"}
}
```

### Permission Request Event

权限请求（等待用户响应）。

```json
{
  "type": "permission_request",
  "request_id": "req_jkl012",
  "tool_name": "bash",
  "tool_input": {"command": "rm -rf /"},
  "reason": "outside_cwd"
}
```

### Run Finished Event

Agent 运行结束。

```json
{
  "type": "run_finished",
  "run_id": "run_def456",
  "status": "success",  // "success" | "failed" | "cancelled"
  "total_steps": 5,
  "duration_ms": 12345
}
```

---

## 完整协议定义

**自动生成的协议文档**: [WIRE_PROTOCOL.md](../../WIRE_PROTOCOL.md)

该文档由 `scripts/gen_protocol_doc.py` 自动生成，包含所有命令和事件的 TypeScript 类型定义。

---

## 客户端示例

### Python

```python
import asyncio
import json
import uuid

async def send_request(reader, writer, method, params):
    request = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": str(uuid.uuid4())
    }
    writer.write((json.dumps(request) + "\n").encode())
    await writer.drain()

    line = await reader.readline()
    return json.loads(line.decode())

async def main():
    reader, writer = await asyncio.open_connection("127.0.0.1", 7437)

    # Ping
    response = await send_request(reader, writer, "core.ping", {"client": "python"})
    print(response)

    writer.close()
    await writer.wait_closed()

asyncio.run(main())
```

### JavaScript (Node.js)

```javascript
const net = require('net');

const client = net.createConnection({ port: 7437 }, () => {
  const request = {
    jsonrpc: '2.0',
    method: 'core.ping',
    params: { client: 'node' },
    id: 1
  };
  client.write(JSON.stringify(request) + '\n');
});

client.on('data', (data) => {
  const response = JSON.parse(data.toString());
  console.log(response);
  client.end();
});
```

---

## 相关文档

- [WIRE_PROTOCOL.md](../../WIRE_PROTOCOL.md) - 自动生成的完整协议定义
- [架构文档](overview.md) - 系统架构说明
- [开发指南](../../CONTRIBUTING.md) - 如何扩展协议
