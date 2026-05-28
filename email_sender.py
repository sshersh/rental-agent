"""Resend transport for outreach emails.

Two public functions:
  send(to, subject, body, from_addr=None) -> message_id
  verify_webhook(headers, body) -> True   (raises ValueError on bad signature)

The webhook verifier implements Resend's Svix-compatible signing scheme:
HMAC-SHA256 over "{svix-id}.{svix-timestamp}.{body}", base64-encoded, compared
against any of the v1 signatures in the `svix-signature` header.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time


_DEFAULT_FROM_NAME = "Sam Shersher"


def _api_key() -> str:
    key = os.getenv("RESEND_API_KEY", "").strip()
    if not key:
        raise RuntimeError("RESEND_API_KEY is not set")
    return key


def _from_header(override: str | None) -> str:
    addr = (override or os.getenv("RESEND_FROM_ADDRESS", "")).strip()
    if not addr:
        raise RuntimeError("RESEND_FROM_ADDRESS is not set")
    name = os.getenv("RESEND_FROM_NAME", _DEFAULT_FROM_NAME).strip() or _DEFAULT_FROM_NAME
    return f"{name} <{addr}>"


def send(to: str, subject: str, body: str, from_addr: str | None = None) -> str:
    """Dispatch a single email via Resend. Returns the Resend message id."""
    import resend  # lazy: optional dep; only required when actually sending
    resend.api_key = _api_key()
    reply_to = os.getenv("RESEND_REPLY_TO", "").strip() or os.getenv("RESEND_FROM_ADDRESS", "").strip()
    params: dict = {
        "from": _from_header(from_addr),
        "to": [to],
        "subject": subject,
        "text": body,
    }
    if reply_to:
        params["reply_to"] = reply_to
    resp = resend.Emails.send(params)
    msg_id = resp.get("id") if isinstance(resp, dict) else getattr(resp, "id", None)
    if not msg_id:
        raise RuntimeError(f"Resend returned no message id: {resp!r}")
    return msg_id


# ── Webhook signature verification (Svix scheme) ────────────────────────


_WEBHOOK_TOLERANCE_SECONDS = 5 * 60


def _decode_secret(secret: str) -> bytes:
    s = secret.strip()
    if s.startswith("whsec_"):
        s = s[len("whsec_"):]
    # Base64 padding is sometimes stripped; add it back.
    pad = "=" * (-len(s) % 4)
    return base64.b64decode(s + pad)


def verify_webhook(headers, body: bytes) -> None:
    """Raise ValueError if the request is not a valid Resend webhook.

    `headers` should be a case-insensitive mapping (Flask's `request.headers`
    qualifies). `body` must be the raw request bytes.
    """
    secret = os.getenv("RESEND_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise ValueError("RESEND_WEBHOOK_SECRET is not set")
    svix_id = headers.get("svix-id") or headers.get("webhook-id")
    svix_ts = headers.get("svix-timestamp") or headers.get("webhook-timestamp")
    svix_sig = headers.get("svix-signature") or headers.get("webhook-signature")
    if not (svix_id and svix_ts and svix_sig):
        raise ValueError("missing svix-* headers")
    try:
        ts_int = int(svix_ts)
    except ValueError as e:
        raise ValueError("bad svix-timestamp") from e
    if abs(int(time.time()) - ts_int) > _WEBHOOK_TOLERANCE_SECONDS:
        raise ValueError("webhook timestamp outside tolerance")
    signed = f"{svix_id}.{svix_ts}.{body.decode('utf-8')}".encode("utf-8")
    expected = base64.b64encode(
        hmac.new(_decode_secret(secret), signed, hashlib.sha256).digest()
    ).decode("ascii")
    for part in svix_sig.split():
        # Each entry looks like "v1,<base64>".
        if "," not in part:
            continue
        version, sig = part.split(",", 1)
        if version != "v1":
            continue
        if hmac.compare_digest(sig, expected):
            return
    raise ValueError("no matching v1 signature")
