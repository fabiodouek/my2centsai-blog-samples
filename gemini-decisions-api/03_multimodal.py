"""Multimodal input: the ticket plus a screenshot, a voice note, a screen
recording and a PDF statement.

Part 1 asks questions that only the media can answer.
Part 2 sends each file alone at every resolution setting and prints what the
API billed for it, because the per-modality token rates are not documented for
this model.
Part 3 sends the video through the Files API instead of inline base64.
"""

import json
import time

from common import ASSETS, MODEL, TICKET, b64, banner, record_sdk_usage, save, sdk_client, usd

client = sdk_client()
MEDIA = {
    "image": ("screenshot.png", "image/png"),
    "audio": ("voice_note.mp3", "audio/mp3"),
    "video": ("screen_recording.mp4", "video/mp4"),
    "document": ("statement.pdf", "application/pdf"),
}


def block(kind: str, resolution: str | None = None) -> dict:
    name, mime = MEDIA[kind]
    b = {"type": kind, "data": b64(ASSETS / name), "mime_type": mime}
    if resolution:
        b["resolution"] = resolution
    return b


banner("Part 1: one ticket, four media files, questions only the media can answer")
QUESTIONS = {
    "screenshot_shows_error": {
        "type": "noul",
        "instructions": "Does the app screenshot show a payment error message?",
    },
    "error_code_is_pay_4012": {
        "type": "noul",
        "instructions": "Is the error code visible in the screenshot PAY-4012?",
    },
    "statement_shows_duplicate": {
        "type": "noul",
        "instructions": "Does the PDF account statement list two charges of the same amount on the same day?",
    },
    "statement_contradicts_app": {
        "type": "noul",
        "instructions": "The app says the payment failed. Does the PDF statement show that the payment actually went through?",
    },
    "caller_tone": {
        "type": "choice",
        "instructions": "What is the tone of the speaker in the voice note?",
        "criteria": {"calm": "", "annoyed": "", "furious": "", "cheerful": ""},
    },
    "voice_deadline": {
        "type": "choice",
        "instructions": "Which day does the speaker in the voice note give as a deadline?",
        "criteria": {"monday": "", "wednesday": "", "friday": "", "no_deadline": "No day is mentioned."},
    },
    "video_error_appears": {
        "type": "noul",
        "instructions": "In the screen recording, does an error message appear partway through that was not there at the start?",
    },
    "refund_amount": {
        "type": "choice",
        "instructions": "How much should be refunded to fix the duplicate charge?",
        "criteria": {"eur_89": "89 euros", "eur_178": "178 euros", "eur_0": "Nothing, there is no duplicate"},
    },
}
shared = [{"type": "text", "text": json.dumps(TICKET) + "\n"},
          block("image"), block("audio"), block("video"), block("document")]
t0 = time.perf_counter()
result = client.decisions.create(model=MODEL, input=shared, questions=QUESTIONS)
ms = (time.perf_counter() - t0) * 1000
record_sdk_usage(result)
for key, ans in result.answers.items():
    d = ans.model_dump(exclude_none=True)
    top = d.get("noul", d.get("choice"))
    top = f"{top:.4f}" if isinstance(top, float) else top
    print(f"{key:<28} {top:<10} confidence={d["confidence"]:.4f}")
u = result.usage.model_dump(exclude_none=True)
by = ", ".join(f"{m['modality']}={m['tokens']:,}" for m in u["input_tokens_by_modality"])
print(f"\n{ms:.0f} ms, input tokens={u['total_input_tokens']:,} ({by}), output tokens={u['total_output_tokens']}")
print(f"cost: {usd(u['total_input_tokens'])}")
out = {"combined": {"ms": round(ms), "result": result.model_dump(exclude_none=True)}, "per_file": []}

banner("Part 2: what each file costs alone, by resolution")
PROBE = {"q": {"type": "noul", "instructions": "Is there a payment problem?"}}
baseline = client.decisions.create(model=MODEL, input="x", questions=PROBE)
record_sdk_usage(baseline)
overhead = baseline.usage.total_input_tokens
print(f"(a one-character text input with the same question bills {overhead} tokens)")
print(f"{'file':<22} {'resolution':<11} {'tokens':>7}  by modality")
for kind in MEDIA:
    settings = [None, "low", "medium", "high"] if kind in ("image", "video") else [None]
    if kind == "image":
        settings.append("ultra_high")
    for res in settings:
        r = client.decisions.create(model=MODEL, input=[block(kind, res)], questions=PROBE)
        record_sdk_usage(r)
        mods = {m.modality: m.tokens for m in r.usage.input_tokens_by_modality}
        print(f"{MEDIA[kind][0]:<22} {res or '(default)':<11} {r.usage.total_input_tokens:>7}  {mods}")
        out["per_file"].append({"file": MEDIA[kind][0], "resolution": res, "total": r.usage.total_input_tokens, "by_modality": mods})
out["question_overhead_tokens"] = overhead

banner("Part 3: the same video through the Files API")
f = client.files.upload(file=ASSETS / "screen_recording.mp4")
while f.state and f.state.name == "PROCESSING":
    time.sleep(2)
    f = client.files.get(name=f.name)
print(f"uploaded {f.name}, state={f.state.name if f.state else None}")
r = client.decisions.create(
    model=MODEL,
    input=[{"type": "video", "uri": f.uri, "mime_type": "video/mp4"}],
    questions={"video_error_appears": QUESTIONS["video_error_appears"]},
)
record_sdk_usage(r)
print(f"P(yes)={r.answers['video_error_appears'].noul:.4f} tokens={r.usage.total_input_tokens}")
print("The file stays in the Files API until it expires or 09_cleanup.py deletes it.")
out["files_api"] = {"tokens": r.usage.total_input_tokens, "noul": r.answers["video_error_appears"].noul}
save("03_multimodal", out)
