#!/usr/bin/env python3
"""验证 Python 源文件的中文注释格式规范。

规则：
1. 普通函数：必须有一行中文注释紧接在 def 之前
2. 测试函数：必须有两行中文注释（功能：和 设计：）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def check_function_comments(file_path: Path) -> list[str]:
    """检查单个文件的函数注释格式，返回错误信息列表"""
    errors: list[str] = []
    lines = file_path.read_text(encoding="utf-8").splitlines()

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # 匹配函数定义（包括装饰器）
        if line.startswith("def ") or (line.startswith("async ") and "def " in line):
            func_name = re.search(r"def\s+(\w+)", line)
            if not func_name:
                i += 1
                continue

            name = func_name.group(1)
            is_test = name.startswith("test_")

            # 向上查找注释（跳过装饰器）
            j = i - 1
            while j >= 0 and (lines[j].strip().startswith("@") or not lines[j].strip()):
                j -= 1

            if j < 0 or not lines[j].strip().startswith("#"):
                errors.append(f"{file_path}:{i+1}: 函数 {name} 缺少中文注释")
                i += 1
                continue

            # 测试函数需要两行注释
            if is_test:
                comment2 = lines[j].strip() if j >= 0 else ""
                comment1 = lines[j - 1].strip() if j >= 1 else ""

                if not (
                    comment1.startswith("# 功能：")
                    and comment2.startswith("# 设计：")
                ):
                    errors.append(
                        f"{file_path}:{i+1}: 测试函数 {name} 需要两行注释（功能：和 设计：）"
                    )

        i += 1

    return errors


def main() -> int:
    """主入口：检查所有传入的文件"""
    all_errors: list[str] = []

    for file_arg in sys.argv[1:]:
        file_path = Path(file_arg)
        if file_path.suffix == ".py" and file_path.exists():
            errors = check_function_comments(file_path)
            all_errors.extend(errors)

    if all_errors:
        print("\n".join(all_errors), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
