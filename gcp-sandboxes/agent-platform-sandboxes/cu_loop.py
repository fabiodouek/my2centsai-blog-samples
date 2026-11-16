"""A Gemini Computer Use loop driving an Agent Platform Computer Use sandbox through CDP.

screenshot -> model -> action -> CDP command -> screenshot ... at most MAX_STEPS times.
Coordinates from the model are 0-999 in both axes; the page is 1280x720.
"""
import base64
from google import genai
from google.genai import types as gt
from sbx import *

MODEL = "gemini-3.8-flash"
MAX_STEPS = 6
W, H = 1280, 720
g = genai.Client(vertexai=True, project=PROJECT, location="global", credentials=CREDS)
CONFIG = gt.GenerateContentConfig(tools=[gt.Tool(computer_use=gt.ComputerUse(
    environment=gt.Environment.ENVIRONMENT_BROWSER))])


def shot(sb, tok):
    return base64.b64decode(cdp(sb, "Page.captureScreenshot", {"format": "png"}, tok=tok)["data"])


def url(sb, tok):
    return cdp(sb, "Runtime.evaluate", {"expression": "location.href", "returnByValue": True}, tok=tok)["result"]["value"]


def key(sb, tok, k, vk):
    for ev in ("keyDown", "keyUp"):
        cdp(sb, "Input.dispatchKeyEvent", {"type": ev, "key": k, "code": k, "windowsVirtualKeyCode": vk,
                                            **({"text": "\r"} if k == "Enter" and ev == "keyDown" else {})}, tok=tok)


def act(sb, tok, name, a):
    """Translate one model action into CDP commands. Returns an error string for actions we don't map."""
    px = lambda k, size: int(a[k] / 1000 * size)
    if name == "navigate":
        cdp(sb, "Page.navigate", {"url": a["url"]}, tok=tok)
    elif name == "search":
        cdp(sb, "Page.navigate", {"url": "https://www.google.com"}, tok=tok)
    elif name == "go_back":
        cdp(sb, "Runtime.evaluate", {"expression": "history.back()"}, tok=tok)
    elif name in ("click", "click_at", "type", "type_text_at", "hover", "hover_at"):
        if "x" in a:   # a "type" action without coordinates types into the focused element
            x, y = px("x", W), px("y", H)
            for ev in ("mouseMoved",) if name.startswith("hover") else ("mousePressed", "mouseReleased"):
                cdp(sb, "Input.dispatchMouseEvent", {"type": ev, "x": x, "y": y, "button": "left", "clickCount": 1}, tok=tok)
        if name in ("type", "type_text_at"):
            cdp(sb, "Input.insertText", {"text": a["text"]}, tok=tok)
            if a.get("press_enter", True):
                key(sb, tok, "Enter", 13)
    elif name.startswith("scroll"):
        dy = 600 if a.get("direction", "down") == "down" else -600
        cdp(sb, "Input.dispatchMouseEvent", {"type": "mouseWheel", "x": 640, "y": 360, "deltaX": 0, "deltaY": dy}, tok=tok)
    elif name in ("key", "key_combination", "press_key") and "Enter" in str(a):
        key(sb, tok, "Enter", 13)
    elif name.startswith("wait"):
        time.sleep(5)
    else:
        return f"unsupported action {name} {a}"
    time.sleep(2)


def loop(sb, task, max_steps=MAX_STEPS, save=None):
    tok = cu_token()
    contents = [gt.Content(role="user", parts=[
        gt.Part(text=task), gt.Part.from_bytes(data=shot(sb, tok), mime_type="image/png")])]
    steps, tokens = [], 0
    for step in range(1, max_steps + 1):
        t0 = time.perf_counter()
        resp = g.models.generate_content(model=MODEL, contents=contents, config=CONFIG)
        t_model = time.perf_counter() - t0
        tokens += resp.usage_metadata.total_token_count or 0
        content = resp.candidates[0].content
        contents.append(content)
        calls = [p.function_call for p in content.parts if p.function_call]
        if not calls:
            print(f"step {step}: model says: {resp.text}")
            return steps, tokens, resp.text
        parts = []
        for fc in calls:
            a = dict(fc.args or {})
            t1 = time.perf_counter()
            err = act(sb, tok, fc.name, a)
            print(f"step {step}: {fc.name} {({k: v for k, v in a.items() if k != 'safety_decision'})}  "
                  f"model {t_model:.1f}s, action {time.perf_counter()-t1:.1f}s" + (f"  [{err}]" if err else ""), flush=True)
            png = shot(sb, tok)
            if save:
                open(f"{save}-{step}.png", "wb").write(png)
            parts.append(gt.Part(function_response=gt.FunctionResponse(
                name=fc.name, response={"url": url(sb, tok), **({"error": err} if err else {})},
                parts=[gt.FunctionResponsePart(inline_data=gt.FunctionResponseBlob(mime_type="image/png", data=png))])))
            steps.append({"step": step, "action": fc.name, "args": a, "model_seconds": round(t_model, 1)})
        contents.append(gt.Content(role="user", parts=parts))
    return steps, tokens, None


if __name__ == "__main__":
    import sys
    sb, _ = create_from_template("computer-use", ttl="900s", display_name="cu-loop")
    try:
        loop(sb, sys.argv[1] if len(sys.argv) > 1 else
             "Search the Google Cloud docs for 'Agent Platform sandboxes' and open the first result.")
    finally:
        client.sandboxes.delete(name=sb.name)
