"""红线守卫 —— 危险命令拦截 + 工程目录污染防护。

对应内部版 AGENT_PROMPT §3 红线的开源内核实施（不只是写在文档里）：
  - 危险命令不裸跑：sudo / rm -rf / git reset --hard 等先拦截
  - 不污染用户工程目录：过程产物必须写进 data_dir，禁止写工程根
"""

from __future__ import annotations

import re
import shlex
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

KNOWN_GIT_COMMANDS = frozenset({
    "add", "apply", "branch", "cat-file", "checkout", "clean", "clone", "commit",
    "config", "describe", "diff", "fetch", "for-each-ref", "grep", "init",
    "log", "ls-files", "ls-remote", "ls-tree", "merge-base", "pull", "remote",
    "rebase", "reset", "restore", "rev-list", "rev-parse", "show",
    "show-ref", "status", "submodule", "switch", "tag", "worktree", "push",
})


class RedlineViolation(Exception):
    pass


def check_command(argv: Sequence[str]) -> tuple[bool, str]:
    """判断一条命令是否命中危险红线。返回 (是否安全, 说明)。"""
    tokens = [str(item) for item in argv]
    line = " ".join(tokens)
    semantic_tokens = tokens
    if tokens and Path(tokens[0]).name in {"bash", "sh", "zsh"}:
        command_index = next(
            (
                index + 1
                for index, token in enumerate(tokens[:-1])
                if (
                    token.startswith("-")
                    and not token.startswith("--")
                    and "c" in token.lstrip("-")
                )
            ),
            0,
        )
        if command_index:
            shell_command = tokens[command_index]
            lexer = shlex.shlex(
                shell_command,
                posix=True,
                punctuation_chars=";&|",
            )
            lexer.whitespace_split = True
            try:
                shell_tokens = list(lexer)
            except ValueError:
                shell_tokens = []
            if (
                any(token in {"&&", "||", ";", "|", "&"} for token in shell_tokens)
                or re.search(r"[\r\n]|`|\$\(", shell_command)
            ):
                return False, (
                    "复合 shell 命令不可由红线守卫安全判定，"
                    f"需拆分或显式确认: {line}"
                )
            try:
                semantic_tokens = shlex.split(shell_command)
            except ValueError:
                semantic_tokens = tokens

    executable = Path(semantic_tokens[0]).name if semantic_tokens else ""
    if executable == "rm":
        options = [token for token in semantic_tokens[1:] if token.startswith("-")]
        recursive = any(
            token in {"--recursive"} or (
                not token.startswith("--")
                and any(flag in token.lstrip("-") for flag in ("r", "R"))
            )
            for token in options
        )
        force = any(
            token in {"--force"} or (
                not token.startswith("--") and "f" in token.lstrip("-")
            )
            for token in options
        )
        if recursive and force:
            return False, f"危险命令（递归删除）不可裸跑，需封装或显式确认: {line}"

    if executable == "git":
        index = 1
        while index < len(semantic_tokens):
            token = semantic_tokens[index]
            if token == "-c":
                value = (
                    semantic_tokens[index + 1]
                    if index + 1 < len(semantic_tokens)
                    else ""
                )
                if value.lower().startswith("alias."):
                    return False, (
                        "危险命令（git alias 注入）不可裸跑，"
                        f"需封装或显式确认: {line}"
                    )
                index += 2
                continue
            if token.startswith("-calias."):
                return False, (
                    "危险命令（git alias 注入）不可裸跑，"
                    f"需封装或显式确认: {line}"
                )
            if token.startswith("--config-env=alias."):
                return False, (
                    "危险命令（git alias 注入）不可裸跑，"
                    f"需封装或显式确认: {line}"
                )
            if token in {"-C", "--git-dir", "--work-tree", "--namespace"}:
                index += 2
                continue
            if token.startswith(("--git-dir=", "--work-tree=", "--namespace=")):
                index += 1
                continue
            if token.startswith("-"):
                index += 1
                continue
            break
        command = semantic_tokens[index] if index < len(semantic_tokens) else ""
        command_args = semantic_tokens[index + 1:]
        if command and command not in KNOWN_GIT_COMMANDS:
            return False, (
                "未知 git 子命令可能是本地 alias，不可裸跑，"
                f"需封装或显式确认: {line}"
            )
        if command == "reset" and any(
            token in {"--hard", "--merge", "--keep"}
            for token in command_args
        ):
            return False, f"危险命令（git 重置工作树）不可裸跑，需封装或显式确认: {line}"
        if command == "restore":
            return False, f"危险命令（git 恢复工作树）不可裸跑，需封装或显式确认: {line}"
        has_force = any(
            token in {"--force", "--discard-changes"}
            or (
                token.startswith("-")
                and not token.startswith("--")
                and "f" in token.lstrip("-")
            )
            for token in command_args
        )
        if command == "checkout":
            positional = [
                token for token in command_args
                if not token.startswith("-")
            ]
            creates_branch = any(
                token in {"-b", "-B", "--orphan"} for token in command_args
            )
            destructive_checkout = (
                "--" in command_args
                or has_force
                or any(token in {".", ".."} for token in positional)
                or (len(positional) > 1 and not creates_branch)
            )
            if destructive_checkout:
                return False, (
                    "危险命令（git 丢弃改动）不可裸跑，"
                    f"需封装或显式确认: {line}"
                )
        if command == "switch" and has_force:
            return False, f"危险命令（git 丢弃改动）不可裸跑，需封装或显式确认: {line}"
        clean_dry_run = any(
            token == "--dry-run"
            or (
                token.startswith("-")
                and not token.startswith("--")
                and "n" in token.lstrip("-")
            )
            for token in command_args
        )
        if command == "clean" and not clean_dry_run:
            return False, f"危险命令（git 清理文件）不可裸跑，需封装或显式确认: {line}"
        if command == "push" and any(
            token == "-f"
            or token == "--force"
            or token.startswith("--force-with-lease")
            for token in command_args
        ):
            return False, f"危险命令（git 强推）不可裸跑，需封装或显式确认: {line}"
        if command == "rebase":
            return False, f"危险命令（git rebase）不可裸跑，需封装或显式确认: {line}"
        if command == "commit" and "--amend" in command_args:
            return False, f"危险命令（git amend）不可裸跑，需封装或显式确认: {line}"

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
