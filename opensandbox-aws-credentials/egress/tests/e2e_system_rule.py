"""Test-only stand-in for OpenSandbox system.py, used by test_mitmdump_e2e.py only when the real
system.py is not available (no egress image, no ../../opensandbox-eks/.src checkout).

It copies the one system.py rule the addon must survive: system.py's responseheaders hook, which
runs before the addon's, streams every chunked or event-stream response
(OpenSandbox release-1.2.0, components/egress/mitmscripts/system.py:1910-1917).
"""


def responseheaders(flow):
    if flow.response is None:
        return
    content_type = flow.response.headers.get("content-type", "").lower()
    transfer_encoding = flow.response.headers.get("transfer-encoding", "").lower()
    if "text/event-stream" in content_type or "chunked" in transfer_encoding:
        flow.response.stream = True
