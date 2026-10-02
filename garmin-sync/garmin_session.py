"""
Garmin login for GitHub Actions without a password login on every run.

Garmin blocks fresh username/password logins from GitHub's servers (HTTP 429 /
Cloudflare 403). A session created once on a home/office connection keeps
working, so the sync restores a saved session instead of logging in:

1. garmin-sync/session.enc  - the latest session, encrypted, committed by the
                              previous run (Garmin may rotate the refresh
                              token, so the newest copy has to be kept).
2. GARMIN_TOKENS secret     - the session created by garmin_login.py on your
                              own computer. Also the key that encrypts (1).
3. GARMIN_EMAIL / GARMIN_PASSWORD - last resort only; normally blocked.

Nothing in this module prints a token.
"""
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

from garminconnect import Garmin

SESSION_FILE = Path(__file__).resolve().parent / "session.enc"

RELOGIN_HELP = (
    "Garmin session is missing or expired. On your own computer run "
    "garmin-sync/garmin_login.py, then paste the value it copies into the "
    "GitHub secret GARMIN_TOKENS (repo Settings -> Secrets and variables -> "
    "Actions)."
)


def _log(msg):
    print(f"[garmin-auth] {msg}", file=sys.stderr)


def _normalise(raw):
    """Return the token JSON string from a raw or base64-encoded secret."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.startswith("{"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8").strip()
        except Exception:
            return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("di_refresh_token"):
        return None
    return json.dumps(data, sort_keys=True)


def _fernet(seed):
    from cryptography.fernet import Fernet

    digest = hashlib.sha256(b"vtaper-garmin-session-v1:" + seed.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _load_saved(seed):
    if not seed or not SESSION_FILE.exists():
        return None
    try:
        plain = _fernet(seed).decrypt(SESSION_FILE.read_bytes().strip())
        return _normalise(plain.decode("utf-8"))
    except Exception:
        # Wrong key (GARMIN_TOKENS was replaced after a new login) or damaged.
        _log("saved session does not match the current GARMIN_TOKENS; ignoring it")
        return None


def _try_tokens(label, tokens):
    try:
        g = Garmin()
        g.login(tokens)
        _log(f"signed in with {label}")
        return g
    except Exception as e:  # noqa: BLE001 - any failure means try the next source
        _log(f"{label} was not accepted ({type(e).__name__})")
        return None


def login():
    """Return (client, seed). Raises SystemExit with a plain-English message."""
    seed = _normalise(os.environ.get("GARMIN_TOKENS"))
    if os.environ.get("GARMIN_TOKENS") and not seed:
        _log("GARMIN_TOKENS is set but is not a valid Garmin session")

    saved = _load_saved(seed)
    for label, tokens in (("saved session", saved), ("GARMIN_TOKENS", seed)):
        if tokens:
            g = _try_tokens(label, tokens)
            if g:
                return g, seed

    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if email and password:
        _log("no usable session; trying a password login (Garmin usually blocks this from GitHub)")
        try:
            g = Garmin(email, password)
            g.login()
            return g, seed
        except Exception as e:  # noqa: BLE001
            _log(f"password login failed: {e}")

    raise SystemExit(RELOGIN_HELP)


def save(g, seed):
    """Encrypt the current session next to this file if it has changed."""
    if not seed:
        return False
    current = _normalise(g.client.dumps())
    if not current:
        return False
    if current == _load_saved(seed):
        return False
    if current == seed and not SESSION_FILE.exists():
        return False  # nothing rotated yet; the secret is still the latest
    SESSION_FILE.write_bytes(_fernet(seed).encrypt(current.encode()) + b"\n")
    _log("saved the refreshed session (encrypted)")
    return True
