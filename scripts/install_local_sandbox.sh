#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
    printf 'Run uv sync --frozen first\n' >&2
    exit 1
fi
command -v prlimit >/dev/null || { printf 'Install util-linux first\n' >&2; exit 1; }
mkdir -p .kama/local-sandbox
chmod 700 .kama/local-sandbox
cd .kama/local-sandbox
# 使用系统配置的 Ubuntu/Debian 源下载，不修改系统包数据库。
apt-get download bubblewrap
package=$(find . -maxdepth 1 -type f -name 'bubblewrap_*.deb' | sort | tail -1)
[ -n "$package" ]
dpkg-deb --extract "$package" extracted
install -m 755 extracted/usr/bin/bwrap ../../.venv/bin/bwrap
../../.venv/bin/bwrap --version
