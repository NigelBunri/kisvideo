"""Tests for the outgoing webhook's HMAC signature (app/workers/transcode.py::
_send_webhook / _sign_webhook_body). Mocks urllib.request.urlopen so no real
network call happens — the thing under test is what gets sent, not whether
delivery succeeds (that's already covered by _send_webhook's own
best-effort try/except, unchanged by this feature)."""

import hashlib
import hmac
import json
from unittest.mock import patch

from app.config.settings import settings
from app.workers.transcode import _send_webhook, _sign_webhook_body

TEST_TOKEN = "test-internal-token"


class _FakeResponse:
    """Minimal stand-in for the object urllib.request.urlopen's context
    manager yields — just enough for _send_webhook's `resp.status` check."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_sign_webhook_body_matches_manual_hmac(monkeypatch):
    monkeypatch.setattr(settings, "internal_token", TEST_TOKEN)
    body = b'{"job_id": "abc123", "status": "ready"}'

    signature = _sign_webhook_body(body)

    expected = hmac.new(TEST_TOKEN.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert signature == expected


def test_sign_webhook_body_returns_none_when_token_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "internal_token", "")
    assert _sign_webhook_body(b"anything") is None


def test_sign_webhook_body_is_sensitive_to_payload_changes(monkeypatch):
    monkeypatch.setattr(settings, "internal_token", TEST_TOKEN)
    sig_a = _sign_webhook_body(b'{"status": "ready"}')
    sig_b = _sign_webhook_body(b'{"status": "failed"}')
    assert sig_a != sig_b


def test_send_webhook_includes_signature_header(monkeypatch):
    monkeypatch.setattr(settings, "internal_token", TEST_TOKEN)
    payload = {"job_id": "abc123", "status": "ready", "caller_reference": "ref-1"}

    captured_request = {}

    def _fake_urlopen(req, timeout=10):
        captured_request["headers"] = dict(req.header_items())
        captured_request["body"] = req.data
        return _FakeResponse()

    with patch("app.workers.transcode.urllib.request.urlopen", side_effect=_fake_urlopen):
        _send_webhook("https://example.test/callback", payload)

    # HTTP header names are case-insensitive, and urllib.request.Request
    # normalizes whatever case is passed in via str.capitalize() (confirmed
    # by actually running this test — it comes out "X-kisvideo-signature",
    # not "X-KisVideo-Signature" as the header dict literal in _send_webhook
    # reads) — so the real, meaningful assertion is a case-insensitive
    # lookup, not an exact-case match against the source-code spelling.
    headers_ci = {k.lower(): v for k, v in captured_request["headers"].items()}
    assert "x-kisvideo-signature" in headers_ci
    signature_header = headers_ci["x-kisvideo-signature"]
    assert signature_header.startswith("sha256=")

    expected_body = json.dumps(payload).encode("utf-8")
    expected_signature = hmac.new(TEST_TOKEN.encode("utf-8"), expected_body, hashlib.sha256).hexdigest()
    assert signature_header == f"sha256={expected_signature}"
    assert captured_request["body"] == expected_body


def test_send_webhook_sets_a_non_default_user_agent(monkeypatch):
    """Regression test for a 2026-09-07 production finding: urllib's
    default User-Agent ("Python-urllib/3.x") gets blocked outright by
    Cloudflare in front of the real Django host - a 403 at the edge,
    before the request ever reaches Django's own auth/signature checks.
    Confirmed directly against the real endpoint: the default UA gets
    403'd, a real browser/curl-like UA reaches Django and gets a genuine
    400 instead. Every real transcode-complete callback was silently
    dropped until this was set."""
    monkeypatch.setattr(settings, "internal_token", TEST_TOKEN)
    captured_request = {}

    def _fake_urlopen(req, timeout=10):
        captured_request["headers"] = dict(req.header_items())
        return _FakeResponse()

    with patch("app.workers.transcode.urllib.request.urlopen", side_effect=_fake_urlopen):
        _send_webhook("https://example.test/callback", {"job_id": "abc123", "status": "ready"})

    headers_ci = {k.lower(): v for k, v in captured_request["headers"].items()}
    assert "user-agent" in headers_ci
    assert "python-urllib" not in headers_ci["user-agent"].lower()


def test_send_webhook_omits_signature_and_warns_when_token_unconfigured(monkeypatch, caplog):
    monkeypatch.setattr(settings, "internal_token", "")
    payload = {"job_id": "abc123", "status": "ready"}

    captured_request = {}

    def _fake_urlopen(req, timeout=10):
        captured_request["headers"] = dict(req.header_items())
        return _FakeResponse()

    with patch("app.workers.transcode.urllib.request.urlopen", side_effect=_fake_urlopen):
        _send_webhook("https://example.test/callback", payload)

    assert "X-Kisvideo-Signature" not in captured_request["headers"]
    assert any("unsigned" in record.message for record in caplog.records)
