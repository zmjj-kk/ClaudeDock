from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import ExitStack
from pathlib import Path


# 验证备份树哈希并拒绝链接与特殊文件，避免恢复时逃逸目录
def verify(root: Path) -> None:
    manifest = json.loads((root / "backup-manifest.json").read_text())
    actual = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("backup contains a symlink")
        if path.is_file() and path.name != "backup-manifest.json":
            actual[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not path.is_file() and not path.is_dir():
            raise ValueError("backup contains a special file")
    if actual != manifest["files"] or manifest["schema_version"] != 1:
        raise ValueError("backup checksum mismatch")


# 复制离线状态并使用 SQLite backup API 生成一致数据库快照
def backup(source: Path, target: Path) -> None:
    if target.exists():
        raise ValueError("backup destination already exists")
    source = source.resolve(strict=True)
    target = target.resolve()
    if source == Path(source.anchor) or source in target.parents or target in source.parents:
        raise ValueError("state and backup paths must be separate non-root directories")
    with ExitStack() as stack:
        for lock_path in sorted(source.rglob(".daemon.lock")):
            fd = os.open(lock_path, os.O_WRONLY | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".claudedock-backup-", dir=target.parent))
        stack.callback(shutil.rmtree, staging, True)
        files = {}
        for path in source.rglob("*"):
            if path.is_symlink():
                raise ValueError("state contains a symlink")
            relative = path.relative_to(source)
            dest = staging / relative
            if path.is_dir():
                dest.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            if not path.is_file():
                raise ValueError("state contains a special file")
            if path.name in {".daemon.lock", "backup-manifest.json"} or path.name.endswith(
                ("-wal", "-shm")
            ):
                continue
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with path.open("rb") as stream:
                sqlite = stream.read(16) == b"SQLite format 3\x00"
            if sqlite:
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as incoming:
                    with sqlite3.connect(dest) as outgoing:
                        incoming.backup(outgoing)
            else:
                shutil.copyfile(path, dest)
            dest.chmod(0o600)
            files[str(relative)] = hashlib.sha256(dest.read_bytes()).hexdigest()
        manifest = staging / "backup-manifest.json"
        manifest.write_text(json.dumps({"schema_version": 1, "files": files}, indent=2))
        manifest.chmod(0o600)
        verify(staging)
        os.rename(staging, target)


# 只恢复到尚不存在的新目录，保留旧状态供人工切换与回滚
def restore(source: Path, target: Path) -> None:
    verify(source)
    if target.exists():
        raise ValueError("restore destination must not exist")
    backup(source, target)
    (target / "backup-manifest.json").unlink()


# 为停止服务后的备份、校验及恢复提供明确命令入口
def main() -> None:
    parser = argparse.ArgumentParser(description="Offline state backup; stop every writer first")
    parser.add_argument("action", choices=("backup", "verify", "restore"))
    parser.add_argument("source", type=Path)
    parser.add_argument("target", nargs="?", type=Path)
    args = parser.parse_args()
    if args.action == "verify":
        verify(args.source)
    elif args.target is None:
        parser.error("target required")
    else:
        {"backup": backup, "restore": restore}[args.action](args.source, args.target)
    print(f"{args.action}: verified")


if __name__ == "__main__":
    main()
