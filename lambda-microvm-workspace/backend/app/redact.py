"""Server-side redaction for the admin Config view (GET /api/policy/config), which shows iron-proxy.yaml and
aws-scope.json as they are in the control bucket.

Neither file should ever hold secret material: iron-proxy reads its secrets from Secrets Manager (`secret_id` is an
ARN), its management token from an environment variable (`api_key_env` names it) and its CA key from a file
(`ca_key` is a path). The placeholders under `proxy_value` are what sandboxes already have in their environment.
This is the backstop for a hand-edited file: a value under a key that names a secret (token, password, value,
private_key...), a PEM private key, and strings shaped like well-known tokens are replaced before the text leaves
the backend. Known placeholders are exempt, so the view still shows which placeholder each secret replaces."""

from __future__ import annotations

import json
import re

MARK = "[redacted]"

# A YAML/JSON key whose value would be secret material. `proxy_value`, `secret_id`, `api_key_env` and `ca_key` don't
# match: the pattern is anchored on the whole key.
SECRET_KEY_RE = re.compile(
    r"^(value|secret|secret_value|secret_string|token|access_token|refresh_token|id_token|session_token|bearer|"
    r"password|passwd|passphrase|api_key|apikey|private_key|client_secret|secret_access_key|aws_secret_access_key|"
    r"aws_session_token|authorization|credentials?)$", re.I)

TOKEN_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("a PEM private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
                                     re.S)),
    ("an Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("an AWS access key ID", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("a bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
]

YAML_KEY_RE = re.compile(r"^(\s*(?:-\s+)?)([A-Za-z_][\w.-]*|\"[^\"]+\"|'[^']+')(\s*:)(\s*)(.*)$")


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _shield(text: str, placeholders) -> tuple[str, dict[str, str]]:
    """Swap known placeholders for inert markers so the token patterns leave them alone."""
    keep: dict[str, str] = {}
    for i, ph in enumerate(sorted({p for p in placeholders if p and len(p) >= 4}, key=len, reverse=True)):
        marker = f"\x00ph{i}\x00"
        if ph in text:
            keep[marker] = ph
            text = text.replace(ph, marker)
    return text, keep


def _unshield(text: str, keep: dict[str, str]) -> str:
    for marker, ph in keep.items():
        text = text.replace(marker, ph)
    return text


def _tokens(text: str, found: list[dict]) -> str:
    for what, rx in TOKEN_PATTERNS:
        def sub(m: re.Match, what=what, text=text) -> str:
            if "placeholder" in m.group(0).lower() and "PRIVATE KEY" not in m.group(0):
                return m.group(0)   # shaped like a token, but a placeholder (part 2's are)
            found.append({"line": _line_of(text, m.start()), "what": what})
            return MARK + "\n" * m.group(0).count("\n")   # keep the line count, so later line numbers hold
        text = rx.sub(sub, text)
    return text


def redact_yaml(text: str, placeholders=()) -> tuple[str, list[dict]]:
    """iron-proxy.yaml with secret material replaced. Returns (text, [{line, what}])."""
    found: list[dict] = []
    text, keep = _shield(text, placeholders)
    out: list[str] = []
    block_indent: int | None = None   # inside a redacted block scalar (`token: |`)
    for n, line in enumerate(text.split("\n"), 1):
        if block_indent is not None:
            indent = len(line) - len(line.lstrip(" "))
            if not line.strip() or indent > block_indent:
                out.append(line if not line.strip() else " " * (block_indent + 2) + MARK)
                continue
            block_indent = None
        m = YAML_KEY_RE.match(line)
        if m and not line.lstrip().startswith("#") and SECRET_KEY_RE.match(m.group(2).strip("\"'")):
            value = m.group(5)
            bare = value.split(" #", 1)[0].strip()
            if bare in ("|", ">", "|-", ">-", "|+", ">+"):
                found.append({"line": n, "what": f"a value under {m.group(2)}"})
                block_indent = len(m.group(1))   # the key's column; the block's lines are indented further
                out.append(line)
                continue
            if bare and bare not in ('""', "''", "null", "~") and not any(k in bare for k in keep):
                found.append({"line": n, "what": f"a value under {m.group(2)}"})
                out.append(f"{m.group(1)}{m.group(2)}{m.group(3)} \"{MARK}\"")
                continue
        out.append(line)
    text = _tokens("\n".join(out), found)
    found.sort(key=lambda f: f["line"])
    return _unshield(text, keep), found


def redact_json(doc, placeholders=()) -> tuple[str, list[dict]]:
    """A JSON document (aws-scope.json) pretty-printed with secret material replaced."""
    found: list[dict] = []

    def walk(v, path: str):
        if isinstance(v, dict):
            out = {}
            for k, x in v.items():
                if SECRET_KEY_RE.match(str(k)) and x not in (None, "", [], {}):
                    found.append({"line": 0, "what": f"a value under {path}{k}"})
                    out[k] = MARK
                else:
                    out[k] = walk(x, f"{path}{k}.")
            return out
        if isinstance(v, list):
            return [walk(x, path) for x in v]
        return v

    text = json.dumps(walk(doc, ""), indent=2, ensure_ascii=False)
    text, keep = _shield(text, placeholders)
    text = _tokens(text, found)
    return _unshield(text, keep), found
