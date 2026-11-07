"""Sign in to the AWS console with a read-only federation token and take MASKED screenshots.

usage: console_login.py FED_JSON WORK_DIR SHOTS_DIR SESSION_NAME

FED_JSON is the output of `aws sts get-federation-token` (read from a file in a mode-700 temp directory that
capture.sh deletes on exit). Writes WORK_DIR/state.json (Playwright storage state, mode 600) for browse.py
and SHOTS_DIR/iam-role.png and SHOTS_DIR/cloudtrail-events.png.

Masking: before every screenshot, and again after every navigation, text nodes are rewritten in the DOM:
the account ID (and its dashed form) becomes 123456789012, the console's account alias becomes my-account
and the IAM user name becomes author. The script then checks that none of them is left in the page text.
It prints the final page title and the masking result, never a token or the sign-in URL.
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

fed_path, work, shots, session_name = sys.argv[1:5]
fed = json.load(open(fed_path))
creds = fed["Credentials"]
account = fed["FederatedUser"]["Arn"].split(":")[4]
caller = os.environ.get("OSB_CAPTURE_CALLER_NAME", "")  # the IAM user's name, to mask; set by capture.sh
alias = os.environ.get("OSB_CAPTURE_ALIAS", "")           # the account alias, to mask; set by capture.sh

session = json.dumps({"sessionId": creds["AccessKeyId"], "sessionKey": creds["SecretAccessKey"], "sessionToken": creds["SessionToken"]})
query = urllib.parse.urlencode({"Action": "getSigninToken", "Session": session})
with urllib.request.urlopen(f"https://signin.aws.amazon.com/federation?{query}", timeout=20) as reply:
    token = json.load(reply)["SigninToken"]
ROLE_URL = "https://us-east-1.console.aws.amazon.com/iam/home?region=us-east-1#/roles/details/osb-sandbox?section=permissions"
login = "https://signin.aws.amazon.com/federation?" + urllib.parse.urlencode(
    {"Action": "login", "Issuer": "my2cents.ai", "Destination": ROLE_URL, "SigninToken": token})
EVENTS_URL = "https://us-east-1.console.aws.amazon.com/cloudtrail/home?region=us-east-1#/events"

MASK_JS = r"""([pairs]) => {
  const walk = () => {
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n; while ((n = w.nextNode())) {
      let v = n.nodeValue, c = v;
      for (const [from, to] of pairs) if (from) c = c.split(from).join(to);
      // the console's top bar shows "<account name> (<account id>)": the name is not an IAM alias, so it
      // cannot be read from IAM; mask whatever precedes the (already masked) ID in the top bar
      if (n.parentElement && n.parentElement.getBoundingClientRect().top < 80)
        c = c.replace(/^(?!my-account)(\S[^()]*?) \((123456789012|1234-5678-9012)\)$/, 'my-account ($2)');
      // a truncated temporary access key ID in a table
      c = c.replace(/ASIA[0-9A-Z]{4,}/g, 'ASIA…');
      if (c !== v) n.nodeValue = c;
    }
  };
  walk();
  if (!window.__osbMask) { window.__osbMask = new MutationObserver(walk); window.__osbMask.observe(document.body, {subtree: true, childList: true, characterData: true}); }
}"""
PAIRS = [[account, "123456789012"], [re.sub(r"(\d{4})(\d{4})(\d{4})", r"\1-\2-\3", account), "1234-5678-9012"], [alias, "my-account"], [caller, "author"]]
def decline_cookies(page) -> None:
    """Choose Decline on the cookie banner (and close any popover), or remove the banner if the button is not there."""
    try:
        page.get_by_role("button", name="Decline").first.click(timeout=4000)
    except Exception:
        page.evaluate("() => document.querySelectorAll('#awsccc-cb-c, #awsccc-sb-ux-c').forEach(e => e.remove())")
    page.wait_for_timeout(500)


def mask(page) -> bool:
    page.evaluate(MASK_JS, [PAIRS])
    page.wait_for_timeout(500)
    text = page.inner_text("body")
    return not any(src and src in text for src, _ in PAIRS)


with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    page = ctx.new_page()
    page.goto(login, wait_until="load", timeout=60000)
    page.wait_for_timeout(10000)
    decline_cookies(page)
    ok_role = mask(page)
    page.screenshot(path=os.path.join(shots, "iam-role.png"))
    print(f"iam role page: {page.title()[:60]!r}; masked clean: {ok_role}")

    page.goto(EVENTS_URL, wait_until="load", timeout=60000)
    page.wait_for_timeout(10000)
    decline_cookies(page)
    # Event history: pick the "User name" lookup attribute, type the session name, press Enter.
    try:
        page.get_by_text("Read-only", exact=True).first.click(timeout=8000)
        page.get_by_text("User name", exact=True).first.click(timeout=8000)
        box = page.get_by_role("searchbox").first if page.get_by_role("searchbox").count() else page.locator("input[type=text]").first
        if os.environ.get("OSB_CAPTURE_DEBUG"):
            print("filter field:", box.evaluate("e => e.outerHTML.replace(/value=\"[^\"]*\"/, '').slice(0, 300)"))
            print("trigger:", page.get_by_text("User name", exact=True).first.evaluate("e => e.parentElement.outerHTML.slice(0, 200)"))
        box.fill(session_name, timeout=8000)
        page.keyboard.press("Enter")
        page.wait_for_timeout(8000)
        filtered = session_name in page.inner_text("body")
    except Exception as error:  # the console UI changes; the script then shows the unfiltered list and says so
        print(f"could not filter event history by user name: {type(error).__name__}")
        filtered = False
    ok_events = mask(page)
    page.screenshot(path=os.path.join(shots, "cloudtrail-events.png"))
    print(f"cloudtrail page: {page.title()[:60]!r}; filtered by session name: {filtered}; masked clean: {ok_events}")

    ctx.storage_state(path=os.path.join(work, "state.json"))
    os.chmod(os.path.join(work, "state.json"), 0o600)
    browser.close()
sys.exit(0 if (ok_role and ok_events and filtered) else 1)
