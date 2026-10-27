"""路径验证：防止路径遍历攻击和访问受限目录。"""

from __future__ import annotations

from pathlib import Path

from claudedock.core.tools.paths import resolve_inside


class PathValidator:
    """路径安全验证器：检测路径遍历、符号链接、受限目录访问"""

    # 默认禁止访问的目录（可通过配置覆盖）
    RESTRICTED_PATHS = frozenset(
        [
            "/etc",
            "/var",
            "/sys",
            "/proc",
            "/dev",
            "/root",
            "~/.ssh",
            "~/.aws",
            "~/.kube",
        ]
    )

    def __init__(self, allowed_base: Path | None = None) -> None:
        """初始化验证器

        Args:
            allowed_base: 允许访问的根目录（例如工作区目录）
        """
        self._allowed_base = allowed_base.resolve() if allowed_base else None

    # 验证路径是否安全
    def validate(self, path: str | Path) -> tuple[bool, str]:
        """验证路径安全性

        Returns:
            (is_safe, reason): 是否安全以及原因描述
        """
        if self._allowed_base is not None:
            safe = resolve_inside(self._allowed_base, str(path))
            if safe.path is None:
                return False, safe.error or "invalid path"
            return True, "OK"
        try:
            target = Path(path).expanduser().resolve()
        except (ValueError, RuntimeError) as e:
            return False, f"Invalid path: {e}"

        # 检查 1：路径遍历（../ 攻击）
        if self._allowed_base and not self._is_under(target, self._allowed_base):
            return False, f"Path outside allowed base: {target}"

        # 检查 2：符号链接（可能指向受限目录）
        if target.is_symlink():
            real_target = target.resolve()
            if self._allowed_base and not self._is_under(real_target, self._allowed_base):
                return False, f"Symlink points outside allowed base: {real_target}"

        # 检查 3：受限目录
        for restricted in self.RESTRICTED_PATHS:
            restricted_path = Path(restricted).expanduser().resolve()
            if self._is_under(target, restricted_path):
                return False, f"Access to restricted path: {restricted}"

        return True, "OK"

    @staticmethod
    def _is_under(path: Path, base: Path) -> bool:
        """检查 path 是否在 base 目录下"""
        try:
            path.relative_to(base)
            return True
        except ValueError:
            return False


# 便捷函数：快速验证路径安全性（使用当前工作目录作为 base）
def is_safe_path(path: str | Path, base: Path | None = None) -> bool:
    """检查路径是否安全（便捷函数）

    Args:
        path: 待验证的路径
        base: 允许访问的根目录（默认为当前工作目录）

    Returns:
        True 表示安全，False 表示存在风险
    """
    validator = PathValidator(allowed_base=base or Path.cwd())
    is_safe, _ = validator.validate(path)
    return is_safe
