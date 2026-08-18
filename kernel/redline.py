"""红线守卫 —— 危险命令拦截 + 工程目录污染防护。

对应内部版 AGENT_PROMPT §3 红线的开源内核实施（不只是写在文档里）：
  - 危险命令不裸跑：sudo / rm -rf / git reset --hard 等先拦截
  - 不污染用户工程目录：过程产物必须写进 data_dir，禁止写工程根
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

# 危险命令模式：命中即拒绝裸跑，要求显式确认或封装
DANGEROUS_PATTERNS = [
    (r"\bsudo\b", "sudo 提权"),
    (r"\brm\s+-rf?\b", "递归删除"),
    (r"\bgit\s+reset\s+--hard\b", "git 硬重置"),
    (r"\bgit\s+checkout\s+--\b", "git 丢弃改动"),
    (r"\bgit\s+push\s+.*(-f|--force)\b", "git 强推"),
    (r"\bgit\s+rebase\b", "git rebase"),
    (r"\bgit\s+commit\s+.*--amend\b", "git amend"),
    (r"\bxcode-select\b", "切换全局 developer dir"),
    (r"\bkill(all)?\b", "杀进程"),
    (r"\bmkfs\b|\bdd\s+if=", "磁盘操作"),
]


class RedlineViolation(Exception):
    pass


def check_command(argv: Sequence[str]) -> tuple[bool, str]:
    """判断一条命令是否命中危险红线。返回 (是否安全, 说明)。"""
    line = " ".join(str(a) for a in argv)
    for pat, why in DANGEROUS_PATTERNS:
        if re.search(pat, line):
            return False, f"危险命令（{why}）不可裸跑，需封装或显式确认: {line}"
    return True, ""


def guard_command(argv: Sequence[str], *, allow: bool = False) -> None:
    """守卫：命中危险命令且未显式 allow 时抛异常。"""
    safe, why = check_command(argv)
    if not safe and not allow:
        raise RedlineViolation(why)


def guard_write_path(target: str | Path, *, project_root: str | Path, data_dir: str | Path) -> None:
    """守卫：过程产物只能写 data_dir，禁止写用户工程根（污染红线）。"""
    target = Path(target).resolve()
    proj = Path(project_root).resolve()
    data = Path(data_dir).resolve()
    # 允许写 data_dir 内
    try:
        target.relative_to(data)
        return
    except ValueError:
        pass
    # 落在工程根内且不在 data_dir → 污染
    try:
        target.relative_to(proj)
        raise RedlineViolation(f"禁止把过程产物写入用户工程根: {target}（应写 {data}）")
    except ValueError:
        return  # 工程根外、data_dir 外，放行（如 /tmp）
