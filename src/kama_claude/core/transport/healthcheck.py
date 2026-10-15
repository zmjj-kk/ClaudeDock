from __future__ import annotations

import json
import os
import socket
import ssl
from contextlib import ExitStack

from kama_claude.core.transport.auth import read_token


# 通过带认证的真实 JSON-RPC ping 检查守护进程响应
def main() -> None:
    port = int(os.environ.get("KAMA_PORT", "7437"))
    request = {
        "jsonrpc": "2.0",
        "id": "health",
        "method": "core.ping",
        "params": {"_auth_token": read_token(port)},
    }
    with ExitStack() as stack:
        connection = stack.enter_context(socket.create_connection(("127.0.0.1", port), timeout=3))
        if os.environ.get("KAMA_TLS_CERT"):
            context = ssl.create_default_context(
                cafile=os.environ.get("KAMA_TLS_CA") or os.environ["KAMA_TLS_CERT"]
            )
            connection = stack.enter_context(
                context.wrap_socket(
                    connection, server_hostname=os.environ.get("KAMA_TLS_SERVER_NAME", "localhost")
                )
            )
        connection.sendall(json.dumps(request).encode() + b"\n")
        with connection.makefile("rb") as stream:
            response = json.loads(stream.readline(65536))
    if response.get("id") != "health" or "server_version" not in response.get("result", {}):
        raise SystemExit("Invalid JSON-RPC health response")


if __name__ == "__main__":
    main()
