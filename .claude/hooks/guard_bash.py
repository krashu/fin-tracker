"""PreToolUse guard for Claude Code's Bash tool: hard-blocks the AGENTS.md §Never commands.

Wired in .claude/settings.json and run as `uv run --no-project python <this file>`, so it needs no
exec bit and no venv. Claude Code passes the tool call as JSON on stdin. Exit 2 blocks the call and
feeds stderr back to Claude; exit 0 lets it through to the normal permission rules.

Permission deny rules match the raw command string, so `git -C . push -f` slips past them. This hook
parses the command into shell segments and checks each one by its leading words instead. Things the
human may approve (dependency changes, alembic, --amend) are `ask` rules in settings.json, not here.
"""

from __future__ import annotations

import json
import shlex
import sys

WRAPPERS = {"env", "nohup", "time", "sudo", "exec", "command"}
OPERATORS = {"&&", "||", ";", "|", "&", "(", ")", ";;", "|&"}
SERVER_MAKE_TARGETS = {"dev", "up", "up-proxy", "backend", "frontend"}


def segments(command: str) -> list[list[str]]:
    """Split a shell command into simple commands (token lists), respecting quotes."""
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:  # unbalanced quotes: fall back to a plain split rather than fail open
        tokens = command.split()
    out: list[list[str]] = [[]]
    for tok in tokens:
        if tok in OPERATORS:
            out.append([])
        else:
            out[-1].append(tok)
    return [s for s in out if s]


def strip_prefix(tokens: list[str]) -> list[str]:
    """Drop env assignments, wrappers like `nohup`, and `uv run [opts]` to reach the program."""
    while tokens:
        head = tokens[0]
        if ("=" in head and not head.startswith("-")) or head in WRAPPERS:
            tokens = tokens[1:]
        elif head == "uv" and len(tokens) > 1 and tokens[1] == "run":
            tokens = tokens[2:]
            while tokens and tokens[0].startswith("-"):
                tokens = tokens[1:]
        elif head in {"python", "python3"} and len(tokens) > 2 and tokens[1] == "-m":
            tokens = tokens[2:]
        elif head in {"npx", "pnpx"} or (head in {"pnpm", "npm"} and tokens[1:2] == ["exec"]):
            tokens = tokens[2:] if head in {"pnpm", "npm"} else tokens[1:]
        else:
            return tokens
    return tokens


def git_args(tokens: list[str]) -> tuple[str, list[str]]:
    """Return (subcommand, args) for a git invocation, skipping global options like -C <path>."""
    rest = tokens[1:]
    while rest and rest[0].startswith("-"):
        rest = rest[2:] if rest[0] in {"-C", "-c", "--git-dir", "--work-tree"} else rest[1:]
    return (rest[0], rest[1:]) if rest else ("", [])


def check(tokens: list[str]) -> str | None:
    tokens = strip_prefix(tokens)
    if not tokens:
        return None
    prog = tokens[0].rsplit("/", 1)[-1]

    if prog in {"bash", "sh", "zsh"} and "-c" in tokens:
        script = tokens[tokens.index("-c") + 1 : tokens.index("-c") + 2]
        for seg in segments(script[0] if script else ""):
            if reason := check(seg):
                return reason
        return None

    if prog == "git":
        sub, args = git_args(tokens)
        if "--no-verify" in args:
            return "git --no-verify bypasses the pre-commit gate (AGENTS.md §Never)"
        if sub == "push" and any(
            a in {"-f", "--force"} or a.startswith("--force-with-lease") or a.startswith("+")
            for a in args
        ):
            return "force-push is never allowed (AGENTS.md §Never)"
        return None

    server = "long-running servers are the human's to start (AGENTS.md §Commands)"
    if prog == "uvicorn" or (prog == "fastapi" and tokens[1:2] in (["dev"], ["run"])):
        return server
    if prog in {"pnpm", "npm", "yarn"}:
        args = tokens[1:]
        if args[:1] == ["run"]:
            args = args[1:]
        if args[:1] in (["dev"], ["start"]):
            return server
    if prog == "next" and tokens[1:2] in (["dev"], ["start"]):
        return server
    if prog == "docker-compose" and "up" in tokens[1:]:
        return server
    if prog == "docker" and tokens[1:2] == ["compose"] and "up" in tokens[2:]:
        return server
    if prog == "make" and SERVER_MAKE_TARGETS & set(tokens[1:]):
        return server
    return None


def main() -> int:
    command = json.load(sys.stdin).get("tool_input", {}).get("command", "")
    for seg in segments(command):
        if reason := check(seg):
            print(
                f"Blocked by .claude/hooks/guard_bash.py: {reason}. "
                "If it is really needed, ask the user to run it themselves.",
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
