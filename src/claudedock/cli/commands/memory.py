from __future__ import annotations

import asyncio
import sys

from claudedock.core.config import ClaudeDockConfig
from claudedock.core.transport.socket_client import IpcError, SocketClient


# 异步核心：连接 daemon，发送 memory.consolidate 请求并打印合并/退休条数
async def _consolidate(config: ClaudeDockConfig) -> int:
    client = SocketClient(config.host, config.port)
    try:
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1

    loop_task = asyncio.create_task(client.run_event_loop())
    try:
        result = await client.send_command("memory.consolidate", {})
        print(
            f"merged={result.get('merged', 0)}  retired={result.get('retired', 0)}"
        )
    except IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        await client.close()
    return 0


# 执行 claudedock memory consolidate 命令
def cmd_memory_consolidate(config: ClaudeDockConfig) -> None:
    try:
        exit_code = asyncio.run(_consolidate(config))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)
