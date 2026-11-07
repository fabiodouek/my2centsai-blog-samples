"""Offline tests for the log helpers in common.py (no cluster, no AWS)."""

import common

WRAPPED = ('{"level":"warn","ts":"2026-11-01T09:20:49.229Z","logger":"opensandbox.egress","msg":"[mitmproxy] credential proxy: '
           '[sigv4] re-signed POST sts.us-east-1.amazonaws.com/ service=sts region=us-east-1 key=ASIA... payload=sha256 '
           'hook=request","sandbox_id":"00820223-0000"}')
PLAIN = "[06:53:57.061] credential proxy: [sigv4] killed POST dynamodb.us-east-1.amazonaws.com/ reason=host-sni-mismatch"


def test_show_log_prints_the_message_from_credential_proxy_to_the_closing_quote(capsys):
    common.show_log([WRAPPED])
    out = capsys.readouterr().out
    assert out.strip() == ("log: credential proxy: [sigv4] re-signed POST sts.us-east-1.amazonaws.com/ service=sts "
                           "region=us-east-1 key=ASIA... payload=sha256 hook=request")
    assert "{" not in out and "sandbox_id" not in out


def test_show_log_handles_an_unwrapped_line(capsys):
    common.show_log([PLAIN])
    assert capsys.readouterr().out.strip().startswith("log: credential proxy: [sigv4] killed POST")


def test_sigv4_events_matches_wrapped_and_plain_lines_by_substring():
    assert common.sigv4_events([WRAPPED, PLAIN, "credential proxy: applied binding=aws-sigv4"]) == [WRAPPED, PLAIN]
