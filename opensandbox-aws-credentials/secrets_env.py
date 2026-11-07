"""Read one value from the secrets file, without running the file.

The secrets file is a KEY=value file outside this repository: OSB_SECRETS_ENV, default
~/.config/opensandbox-eks/secrets.env. It is parsed here (KEY=value lines; `export ` and matching quotes
allowed; nothing is expanded or executed), never sourced, and only the keys in ALLOWED can be
read. Part 5 needs one: AWS_TEST_BUCKET. The Bedrock model is the pinned Terraform variable,
not the file's BEDROCK_MODEL_ID, and no other key in the file is read.

    bucket="$(python3 -I secrets_env.py AWS_TEST_BUCKET)"

The value goes to stdout only when stdout is not a terminal (a pipe, a command substitution),
so running it by hand doesn't put the value on screen. Error messages never contain values.
Standard library only, so `python3 -I` runs it with no project environment.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

DEFAULT_PATH = "~/.config/opensandbox-eks/secrets.env"
ALLOWED = ("AWS_TEST_BUCKET",)
_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def _given_path() -> str:
    """The path as given (OSB_SECRETS_ENV or the default), for messages: not expanded, so a
    message shows ~/... and not the home directory."""
    return os.environ.get("OSB_SECRETS_ENV") or DEFAULT_PATH


def secrets_path() -> Path:
    return Path(_given_path()).expanduser()


def _unquote(raw: str) -> str:
    """A quoted value ends at its closing quote (a comment may follow); an unquoted one at " #"."""
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        if end > 0:
            return raw[1:end]
    return raw.split(" #", 1)[0].strip()


def read_secret(name: str, path: Path | None = None) -> str:
    """The value of name in the secrets file. The last assignment wins, as in a shell.

    Raises KeyError if name is not in ALLOWED or the file doesn't set it, and FileNotFoundError
    if there is no file.
    """
    if name not in ALLOWED:
        raise KeyError(f"{name} is not a key part 5 reads from the secrets file (only {', '.join(ALLOWED)})")
    path = path or secrets_path()
    shown = str(path) if path != secrets_path() else _given_path()
    if not path.is_file():
        raise FileNotFoundError(f"secrets file not found: {shown} (set OSB_SECRETS_ENV to its path)")
    value = ""
    for line in path.read_text().splitlines():
        match = _ASSIGNMENT.match(line)
        if match and match.group(1) == name:
            value = _unquote(match.group(2))
    if not value:
        raise KeyError(f"{shown} does not set {name}")
    return value


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: python3 -I {Path(argv[0]).name} NAME   (NAME: {', '.join(ALLOWED)})", file=sys.stderr)
        return 2
    if sys.stdout.isatty():
        print(f'not printing {argv[1]} to a terminal; capture it: value="$(python3 -I secrets_env.py {argv[1]})"',
              file=sys.stderr)
        return 2
    try:
        value = read_secret(argv[1])
    except (KeyError, FileNotFoundError) as error:
        print(error.args[0], file=sys.stderr)
        return 1
    sys.stdout.write(value)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
