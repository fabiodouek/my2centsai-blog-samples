"""07_noisy.py's hog settings, read from its source (07 imports common, which needs env.sh)."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def constants(path: Path) -> dict:
    tree = ast.parse(path.read_text())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                if isinstance(node.value, ast.BinOp) and isinstance(node.value.left, ast.Name):
                    out[node.targets[0].id] = (node.value.left.id, ast.literal_eval(node.value.right))
    return out


def test_a_hog_outlives_its_own_seconds_under_execd():
    """execd kills a background command when its timeout runs out: the hogs' must be longer than
    hog.py's own --seconds, which must be longer than any scenario (the longest ran about 130 s)."""
    found = constants(ROOT / "07_noisy.py")
    seconds = found["HOG_SECONDS"]
    base, extra = found["HOG_TIMEOUT_S"]
    assert base == "HOG_SECONDS" and extra > 0 and seconds >= 600


def test_every_background_command_passes_a_timeout():
    for path in sorted(ROOT.glob("[0-9][0-9]_*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "run_in":
                words = {k.arg: k.value for k in node.keywords}
                background = words.get("background")
                if isinstance(background, ast.Constant) and background.value is True:
                    assert "timeout" in words, f"{path.name}:{node.lineno}: a background run_in() without timeout="
