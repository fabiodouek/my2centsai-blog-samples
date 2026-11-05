"""Extra environment variables a user sets on a new sandbox. They travel in the run-hook payload and the in-VM agent
writes them to /etc/sandbox.env as `export NAME="value"` lines, which every shell and /exec command sources.

Not for secrets: the values are visible to anything in the sandbox and stored with the sandbox's record. Credentials
go through the Proxy policy page, where the sandbox only ever gets a placeholder.

Names follow `^[A-Za-z_][A-Za-z0-9_]*$` and may not override what the payload already sets (the placeholders, the
AWS variables, the CA variables, SANDBOX_NAME...), a custom credential's variable, or the shell's own. Values are
printable ASCII without `$`, a backquote, `"` or a backslash: /etc/sandbox.env is a shell script, and inside its
double quotes those would expand, run or escape."""

from __future__ import annotations

import re

from .microvm import build_run_payload
from .policy import RESERVED_ENV

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NAME_MAX = 128
VALUE_MAX = 2048
MAX_VARS = 32
FORBIDDEN_CHARS = set('$`"\\')
RESERVED_PREFIXES = ("AWS_", "LD_", "BASH_FUNC_")
# The shell's own and what the image sets (bashrc, profile)
SHELL_ENV = {"BASH_ENV", "PS2", "PS4", "IS_SANDBOX", "COLORTERM", "OLDPWD", "SHLVL", "MAIL", "LOGNAME", "EDITOR"}


class EnvError(ValueError):
    pass


def reserved_names(placeholders: dict, region: str, custom_env: dict | None = None) -> set[str]:
    """Every name a user may not set: the payload's own variables (computed, so a new one is covered), the custom
    credentials' variables, and the shell's."""
    base = build_run_payload(placeholders, region, {"SANDBOX_NAME": "x"})["env"]
    return set(base) | set(custom_env or {}) | RESERVED_ENV | SHELL_ENV | {"SANDBOX_NAME", "AWS_SDK_UA_APP_ID"}


def check(env: dict | None, reserved: set[str]) -> dict[str, str]:
    """The validated variables, in the order given. Raises EnvError with every problem (values never quoted)."""
    env = env or {}
    if not isinstance(env, dict):
        raise EnvError("env must be an object of NAME: value")
    if len(env) > MAX_VARS:
        raise EnvError(f"at most {MAX_VARS} variables")
    problems: list[str] = []
    out: dict[str, str] = {}
    for name, value in env.items():
        name = str(name)
        shown = name if len(name) <= 40 else name[:40] + "…"
        if not NAME_RE.match(name) or len(name) > NAME_MAX:
            problems.append(f"{shown!r} isn't a valid name (letters, digits and _, not starting with a digit)")
            continue
        if name in reserved or name.upper() in reserved or name.upper().startswith(RESERVED_PREFIXES):
            problems.append(f"{name} is reserved: the sandbox sets it itself")
            continue
        if not isinstance(value, str):
            problems.append(f"{name}: the value must be a string")
            continue
        if len(value) > VALUE_MAX:
            problems.append(f"{name}: the value is over {VALUE_MAX} characters")
            continue
        if any(not (32 <= ord(c) <= 126) for c in value):
            problems.append(f"{name}: printable ASCII only (no newlines, tabs or other characters)")
            continue
        if FORBIDDEN_CHARS & set(value):
            problems.append(f"{name}: no $, backquote, \" or backslash (the value goes in a shell file)")
            continue
        out[name] = value
    if problems:
        raise EnvError("; ".join(problems))
    return out


def entry_bytes(name: str, value: str) -> int:
    """What one variable adds to the compact JSON payload: "NAME":"value", (values need no escaping here)."""
    return len(name) + len(value) + 6
