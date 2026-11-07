"""Test-only mitmproxy addon for test_mitmdump_e2e.py; loaded after sigv4_resign.py.

Stands in for DNS: the addon pins the upstream to the AWS hostname, and this sends that
connection to the local fake endpoint instead. It changes only where mitmproxy connects
(request.data.host/port), not the Host header the request was signed with, and logs the
upstream the addon chose so the test can check the pin.
"""

import logging
import os

PORT = int(os.environ["OSB_E2E_UPSTREAM_PORT"])


def requestheaders(flow):
    if flow.request.host.endswith(".amazonaws.com"):
        logging.getLogger().warning("[e2e] upstream %s:%d", flow.request.host, flow.request.port)
        flow.request.data.host = "127.0.0.1"
        flow.request.data.port = PORT
