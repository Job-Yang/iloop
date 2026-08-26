"""Host hook policy that prevents side-effect actions bypassing Runtime."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Iterable, Mapping, Optional, Union

from .authorization import AuthorizationGrant, AuthorizationVerifier


READ_ONLY_PROGRAMS = frozenset({
    "cat", "cut", "date", "du", "echo", "find", "grep", "head",
    "jq", "ls", "printf", "pwd", "readlink", "rg", "sort", "stat",
    "tail", "test", "tr", "true", "uname", "uniq", "wc", "which",
})
READ_ONLY_GIT = frozenset({
    "blame", "cat-file", "diff", "diff-tree",
    "for-each-ref", "log", "ls-files", "ls-remote", "merge-base",
    "rev-list", "rev-parse", "show", "show-ref", "status",
})
READ_ONLY_TOOL_NAMES = frozenset({
    "glob", "grep", "read", "read_file", "search", "view_image",
    "web_fetch", "web_search",
})
COMMAND_TOOL_NAMES = frozenset({
    "bash", "exec_command", "run_command", "shell", "terminal",
})
WRITE_TOOL_NAMES = frozenset({
    "apply_patch", "edit", "multiedit", "write", "write_file",
})
WRITE_WORDS = frozenset({
    "add", "apply", "approve", "close", "commit", "create", "delete",
    "deploy", "execute", "merge", "publish", "push", "release",
    "remove", "rename", "replace", "reply", "rollback", "run",
    "send", "set", "start", "submit", "trigger", "update", "upload",
})


def _git_command_kind(tokens: list[str], subcommand_index: int) -> str:
    subcommand = (
        tokens[subcommand_index]
        if subcommand_index < len(tokens) else ""
    )
    arguments = tokens[subcommand_index + 1:]
    if any(
        item == "--output" or item.startswith("--output=")
        for item in arguments
    ):
        return "write"
    if subcommand in READ_ONLY_GIT:
        return "read"
    if subcommand == "branch":
        return (
            "read"
            if not arguments
            or all(
                item in {
                    "--show-current", "--list", "-l", "--all", "-a",
                    "--remotes", "-r", "--verbose", "-v", "-vv",
                }
                for item in arguments
            )
            else "write"
        )
    if subcommand == "remote":
        if not arguments or all(
            item in {"-v", "--verbose"} for item in arguments
        ):
            return "read"
        operation = arguments[0]
        if operation in {"get-url", "show"}:
            return "read"
        return "write"
    return "write"


def _within(path: Path, roots: Iterable[Path]) -> bool:
    resolved = path.expanduser().resolve()
    return any(
        resolved == root or resolved.is_relative_to(root)
        for root in roots
    )


def _write_paths(tool_input: Mapping[str, object]) -> list[Path]:
    values = []
    for key in ("file_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value)
    patch = tool_input.get("patch") or tool_input.get("patch_text")
    if isinstance(patch, str):
        values.extend(
            match.group(1).strip()
            for match in re.finditer(
                r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+)$",
                patch,
                re.MULTILINE,
            )
        )
    return [Path(item) for item in values]


def _command_kind(command: str) -> str:
    if any(token in command for token in ("\n", "$(", "`")) or re.search(
        r"[;&|<>]", command
    ):
        return "blocked"
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "blocked"
    if not tokens:
        return "blocked" if tokens else "read"
    program = Path(tokens[0]).name
    if program == "git":
        index = 1
        options_with_values = {
            "-C", "-c", "--git-dir", "--work-tree",
            "--namespace", "--super-prefix",
        }
        while index < len(tokens):
            token = tokens[index]
            if token in options_with_values:
                index += 2
                continue
            if any(
                token.startswith(prefix + "=")
                for prefix in options_with_values
                if prefix.startswith("--")
            ):
                index += 1
                continue
            if token.startswith("-"):
                index += 1
                continue
            break
        return _git_command_kind(tokens, index)
    if program in READ_ONLY_PROGRAMS:
        if program == "find" and any(
            token in {
                "-delete", "-exec", "-execdir", "-ok", "-okdir",
                "-fprint", "-fprint0", "-fprintf", "-fls",
            }
            for token in tokens[1:]
        ):
            return "write"
        if program == "sort" and any(
            token == "-o"
            or token.startswith("-o")
            or token == "--output"
            or token.startswith("--output=")
            for token in tokens[1:]
        ):
            return "write"
        return "read"
    words = {
        word
        for token in tokens[1:]
        for word in re.split(r"[^a-z0-9]+", token.casefold())
        if word
    }
    if words & WRITE_WORDS:
        return "write"
    return "blocked"


def authorize_tool_use(
    payload: Mapping[str, object],
    *,
    action_id: str,
    task_id: str,
    case_id: str,
    diagnosis_revision: int,
    policy_digest: str,
    grant: Optional[AuthorizationGrant],
    verifier: Optional[AuthorizationVerifier],
    writable_roots: Iterable[Union[str, Path]] = (),
    cwd: Union[str, Path] = ".",
) -> tuple[bool, str]:
    """Evaluate a host tool call using the same grant as its Action."""
    name = str(
        payload.get("tool_name") or payload.get("toolName") or ""
    ).casefold()
    raw_input = payload.get("tool_input") or payload.get("toolInput") or {}
    tool_input = raw_input if isinstance(raw_input, Mapping) else {}
    roots = tuple(Path(item).expanduser().resolve() for item in writable_roots)
    write_required = name in WRITE_TOOL_NAMES
    if write_required:
        paths = _write_paths(tool_input)
        if not paths:
            return False, "write tool has no verifiable target path"
        root = Path(cwd).expanduser().resolve()
        if not all(
            _within(path if path.is_absolute() else root / path, roots)
            for path in paths
        ):
            return False, "write target is outside authorized roots"
    command = tool_input.get("command") or tool_input.get("cmd")
    if isinstance(command, str):
        if name not in COMMAND_TOOL_NAMES:
            return False, "unknown tool cannot delegate a shell command"
        kind = _command_kind(command)
        if kind == "blocked":
            return False, "runtime command is not on the safe allowlist"
        write_required = write_required or kind == "write"
    elif name not in READ_ONLY_TOOL_NAMES | WRITE_TOOL_NAMES:
        return False, "unknown tool is not on the safe allowlist"
    if not write_required:
        return True, ""
    if grant is None or verifier is None:
        return False, "side-effect tool use requires authorization"
    if not verifier.verify(
        grant,
        action_id=action_id,
        task_id=task_id,
        case_id=case_id,
        diagnosis_revision=diagnosis_revision,
        policy_digest=policy_digest,
    ):
        return False, "authorization grant does not cover this tool use"
    return True, ""
