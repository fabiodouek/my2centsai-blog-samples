"""End-to-end smoke test of the workspace sandbox image, the way the backend will drive it:

  run (HTTP + SHELL ingress, part 2's sandbox connector, transparent interception: no proxy settings)
  -> GET /stats -> shell WebSocket -> `tmux new -A -s main` -> `claude` (TUI must start with no prompts)
  -> send a prompt, see the reply -> GET /claude-usage (non-zero tokens)
  -> suspend -> resume -> new shell WebSocket -> reattach -> the same Claude screen is still there
  -> suspend again and connect the shell to the SUSPENDED MicroVM (does the endpoint auto-resume it?)
  -> terminate (always).

One MicroVM at a time. Output goes to stdout and, with the account ID redacted, to results/smoke_sandbox.log.

  .venv/bin/python scripts/smoke_sandbox.py
"""

import asyncio
import json
import random
import re
import sys
import time

from mvm import ROOT, Shell, image_arn, mvm, redact, run_microvm

LOG = ROOT / "results" / "smoke_sandbox.log"
ANSI = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>78DEHMc]")
# Any of these on screen means Claude Code stopped at a dialog instead of the input box
PROMPTS = ["Choose the text style", "Do you want to use this API key", "Quick safety check", "Yes, I trust this folder",
           "Bypass Permissions mode", "Make auto mode your default", "Select login method", "Not logged in",
           "Press Enter to continue", "Security notes", "Enter to confirm"]
READY = "bypass permissions on"
SANDBOX_NAME = "smoke-test"
# Claude's reply lines start with a bullet: "●" on Linux ("⏺" on macOS)
REPLY_BULLETS = ("●", "⏺")


def is_reply(line: str) -> bool:
    return line.lstrip().startswith(REPLY_BULLETS)

# Proves it's the same session and process after resume: tmux session creation time, pane shell pid, claude pid
IDS = "tmux display -p -t main '#{session_created} #{pane_pid}'; ps -eo pid=,comm= | awk '$2 ~ /^claude/ {print $1; exit}'"

lines: list[str] = []
timings: dict[str, float] = {}


def log(*parts) -> None:
    text = redact(" ".join(str(p) for p in parts))
    print(text, flush=True)
    lines.append(text)


def banner(title: str) -> None:
    log(f"\n=== {title} " + "=" * max(0, 64 - len(title)))


def timed(label: str, seconds: float) -> None:
    timings[label] = round(seconds, 2)
    log(f"[{seconds:6.2f}s] {label}")


def plain(s: str) -> str:
    return ANSI.sub("", s)


def show(screen: str, last: int = 18) -> None:
    rows = [r.rstrip() for r in screen.splitlines()]
    while rows and not rows[-1]:
        rows.pop()
    for r in [r for r in rows if r][-last:]:
        log("  | " + r[:150])


def poll_screen(vm, until, timeout: float, what: str) -> tuple[str, float]:
    t0 = time.perf_counter()
    while True:
        screen = vm.screen()
        bad = [p for p in PROMPTS if p in screen]
        if bad:
            show(screen, 30)
            raise SystemExit(f"FAIL: Claude Code is showing a prompt: {bad}")
        if until(screen):
            return screen, time.perf_counter() - t0
        if time.perf_counter() - t0 > timeout:
            show(screen, 30)
            raise SystemExit(f"FAIL: {what} not seen within {timeout}s")
        time.sleep(0.5)


def usage_summary(u: dict) -> str:
    t = u["totals"]
    return (f"sessions={t['sessions']} turns={t['turns']} api_calls={t['api_calls']} input={t['input_tokens']} "
            f"output={t['output_tokens']} cache_read={t['cache_read_tokens']} cache_creation={t['cache_creation_tokens']} "
            f"models={sorted(t['models'])} skipped_lines={u['skipped_lines']}")


async def main() -> None:
    img = mvm.get_microvm_image(imageIdentifier=image_arn())
    banner("Run")
    log(f"image {image_arn()} version {img.get('latestActiveImageVersion')}")
    vm, info = run_microvm(extra_env={"SANDBOX_NAME": SANDBOX_NAME}, log=log)
    log(f"microvm {vm.id}  connector={info['connector']}  run-hook payload {info['payload_bytes']} bytes")
    try:
        vm.wait("RUNNING")
        timed("run_microvm -> RUNNING", time.perf_counter() - vm.t0)
        timed("agent /health answering (after RUNNING)", vm.ready())
        run_hook = [e for e in vm.get("/events")["events"] if e["hook"] == "run"]
        log("run hook:", json.dumps({k: run_hook[-1].get(k) for k in ("payload_len", "error")}) if run_hook else "not seen")
        log("sandbox.env keys:", vm.sh("sed 's/=.*//; s/export //' /etc/sandbox.env | tr '\\n' ' '"))
        log("egress check (api.anthropic.com via iron-proxy):",
            vm.sh("curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://api.anthropic.com/v1/models -H x-api-key:$ANTHROPIC_API_KEY -H anthropic-version:2023-06-01"))

        banner("GET /stats")
        log(json.dumps(vm.get("/stats")))

        banner("Shell WebSocket -> tmux -> claude")
        sh = Shell(vm)
        timed("shell token + WebSocket + session_init", await sh.connect())
        log("first frame:", json.dumps(sh.session_init))
        rtt = await sh.ping()
        log("WebSocket ping -> pong:", f"{rtt * 1000:.0f} ms" if rtt is not None else "no pong within 5 s")
        await sh.send("echo TERM=$TERM LANG=$LANG IS_SANDBOX=$IS_SANDBOX PWD=$PWD; tty\n")
        await sh.wait_for("IS_SANDBOX=1 PWD", 15)
        log("shell env:", [l for l in plain(sh.text()).splitlines() if l.startswith(("TERM=", "/dev/"))][-2:])
        t0 = time.perf_counter()
        await sh.send("tmux new -A -s main\n")
        while vm.exec("tmux has-session -t main")["rc"] != 0:
            await asyncio.sleep(0.2)
        timed("tmux session 'main' up", time.perf_counter() - t0)
        await sh.wait_for(f"{SANDBOX_NAME} · C-b d detaches", 20)
        log(f"tmux status line shows: '{SANDBOX_NAME} · C-b d detaches'")

        t0 = time.perf_counter()
        await sh.send("claude\r")
        screen, _ = poll_screen(vm, lambda s: READY in s and "❯" in s, 90, "Claude Code input box")
        timed("claude TUI ready, no prompts", time.perf_counter() - t0)
        show(screen, 12)

        a, b = random.randint(1000, 4999), random.randint(1000, 4999)
        answer = str(a + b)
        prompt = f"What is {a} + {b}? Reply with only the number."
        t0 = time.perf_counter()
        await sh.send(prompt)
        await asyncio.sleep(0.5)  # text and Enter in one frame looks like a paste to the TUI
        await sh.send("\r")
        screen, _ = poll_screen(vm, lambda s: any(answer in l and is_reply(l) for l in s.splitlines()), 180, f"reply {answer}")
        timed(f"prompt -> reply '{answer}' on screen", time.perf_counter() - t0)
        before = screen
        show(before, 14)
        ids = vm.sh(IDS)
        log("tmux session_created, pane pid, claude pid:", ids.split())

        banner("GET /claude-usage (after the exchange)")
        await asyncio.sleep(1)
        u = vm.get("/claude-usage")
        log(usage_summary(u))
        for s in u["sessions"]:
            log("  session", json.dumps({k: s[k] for k in ("session_id", "project", "model", "turns", "input_tokens", "output_tokens",
                                                       "cache_read_tokens", "cache_creation_tokens", "first_ts", "last_ts")}))
        if u["totals"]["input_tokens"] + u["totals"]["output_tokens"] + u["totals"]["cache_read_tokens"] <= 0:
            raise SystemExit("FAIL: /claude-usage shows no tokens")

        banner("Suspend (the shell WebSocket is left open)")
        t0 = time.perf_counter()
        mvm.suspend_microvm(microvmIdentifier=vm.id)
        vm.wait("SUSPENDED")
        timed("suspend_microvm -> SUSPENDED", time.perf_counter() - t0)
        await asyncio.sleep(2)
        closed = sh.ws.close_code, sh.ws.close_reason
        log(f"old shell WebSocket after suspend: state={sh.ws.state.name} close={closed}")

        banner("Resume, new shell, reattach")
        t0 = time.perf_counter()
        mvm.resume_microvm(microvmIdentifier=vm.id)
        vm.wait("RUNNING")
        timed("resume_microvm -> RUNNING", time.perf_counter() - t0)
        await asyncio.sleep(1)
        log(f"old shell WebSocket after resume: state={sh.ws.state.name} close={(sh.ws.close_code, sh.ws.close_reason)}")
        await sh.close()
        t0 = time.perf_counter()
        sh2 = Shell(vm)
        timed("new shell token + WebSocket + session_init", await sh2.connect())
        await sh2.send("tmux new -A -s main\n")
        t1 = time.perf_counter()
        while answer not in plain(sh2.text()):
            if time.perf_counter() - t1 > 30:
                raise SystemExit("FAIL: reattached tmux did not redraw the Claude screen")
            await asyncio.sleep(0.1)
        timed("reattach: WebSocket connect -> Claude screen redrawn on the shell", time.perf_counter() - t0)
        after = vm.screen()
        same = vm.sh(IDS)
        log("tmux session_created, pane pid, claude pid:", same.split(), "(same)" if same == ids else "(CHANGED)")
        reply_lines = lambda s: [l for l in s.splitlines() if answer in l and is_reply(l)]
        log("reply line before:", reply_lines(before), " after:", reply_lines(after))
        if same != ids or not reply_lines(after):
            raise SystemExit("FAIL: not the same Claude session after resume")
        show(after, 14)
        log("Claude still responsive after resume?")
        t0 = time.perf_counter()
        await sh2.send("Now reply with only the word resumed.")
        await asyncio.sleep(0.5)
        await sh2.send("\r")
        poll_screen(vm, lambda s: any("resumed" in l and is_reply(l) for l in s.splitlines()), 180, "second reply")
        timed("second prompt after resume -> reply", time.perf_counter() - t0)

        banner("GET /stats and /claude-usage after resume")
        log(json.dumps(vm.get("/stats")))
        log(usage_summary(vm.get("/claude-usage")))
        log("agent hook events:", [e["hook"] for e in vm.get("/events")["events"]])

        banner("Shell to a SUSPENDED MicroVM (auto-resume?)")
        await sh2.close()
        mvm.suspend_microvm(microvmIdentifier=vm.id)
        vm.wait("SUSPENDED")
        await asyncio.sleep(1)
        t0 = time.perf_counter()
        try:
            tok = vm.shell_token()
            log(f"shell token minted while SUSPENDED ({time.perf_counter() - t0:.2f}s)")
            sh3 = Shell(vm, tok)
            t0 = time.perf_counter()
            await sh3.connect()
            timed("WebSocket to SUSPENDED MicroVM -> session_init", time.perf_counter() - t0)
            log("state now:", vm.state())
            await sh3.send("tmux new -A -s main\n")
            await sh3.wait_for(answer, 30)
            log("reattached after auto-resume: Claude screen present")
            await sh3.close()
        except Exception as e:
            log(f"shell to SUSPENDED MicroVM: {type(e).__name__}: {str(e)[:200]}  state={vm.state()}")

        banner("Summary")
        for k, v in timings.items():
            log(f"  {v:7.2f}s  {k}")
        log("PASS")
    finally:
        try:
            vm.terminate()
            log(f"terminated {vm.id} ({vm.state()})")
        except Exception as e:
            log(f"terminate {vm.id}: {e}")
        LOG.parent.mkdir(exist_ok=True)
        LOG.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit as e:
        if e.code not in (0, None):
            print(e.code, file=sys.stderr)
            LOG.parent.mkdir(exist_ok=True)
            LOG.write_text("\n".join(lines + [redact(str(e.code))]) + "\n")
        raise
