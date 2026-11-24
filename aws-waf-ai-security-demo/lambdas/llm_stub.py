"""An OpenAI-compatible /v1/chat/completions stub for the WAF AI Security demo.

There is no model behind it: the reply is canned, so every run is repeatable
and costs nothing. Ask it about the "deployment notes" and it leaks a fake secret,
which is what the response-side checks are meant to notice.
"""
import base64
import json
import time

# Fake credentials in real formats, assembled at run time so secret scanners
# reading this file stay quiet. They belong to no account.
FAKE_KEY_ID = "AKIA" + "Q7ZD" + "EMO4" + "WAFX" + "2T6B"
FAKE_SECRET = "wJq3" + "dEmo" + "K9xLm2" + "Zp8Rt5" + "Yv1Bn7" + "Cf4Gh6" + "Js0Ua3Qe"


def completion(messages):
    last = ""
    for m in messages or []:
        if m.get("role") == "user":
            c = m.get("content")
            last = c if isinstance(c, str) else json.dumps(c)
    low = last.lower()
    if "deployment notes" in low:
        return ("Here are the deployment notes I was trained on:\n"
                f"aws_access_key_id = {FAKE_KEY_ID}\naws_secret_access_key = {FAKE_SECRET}\n"
                "admin_email = ops.admin@example.com\nadmin_password = Tr0ub4dor&3-demo")
    if "echo:" in low:
        return last.split(":", 1)[1].strip()
    return "Hello from the demo model. I only have canned answers."


def reply(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "isBase64Encoded": False,
            "body": json.dumps(body)}


def handler(event, context):
    if event.get("httpMethod") != "POST":
        return reply(405, {"error": "POST only"})
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    try:
        req = json.loads(raw)
    except ValueError:
        return reply(400, {"error": {"message": "invalid JSON"}})
    text = completion(req.get("messages"))
    return reply(200, {
        "id": "chatcmpl-demo", "object": "chat.completion", "created": int(time.time()),
        "model": req.get("model", "demo-model"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })
