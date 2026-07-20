"""Thin direct-HTTP client for textverified.com REST API v2.

Auth flow: POST /api/pub/v2/auth with the API key → returns a Bearer token →
attach as `Authorization: Bearer <token>` to all subsequent requests.

Public methods used by the IG orchestrator:
- balance()                       → float, account credit
- create_verification(service)    → returns {id, number} for the acquired number
- poll_sms(verification_id, ...)  → returns the SMS code (string of digits) or None
- cancel(verification_id)         → release the number when done

The bearer token has a short lifetime (typically ~30 min); we re-fetch it on
401 and on every successful auth call we cache its expiry.
"""
from __future__ import annotations
import os
import re
import time
import logging
from typing import Optional, Dict, Any, List
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://www.textverified.com"
TOKEN_PATH = "/api/pub/v2/auth"  # POST — generates bearer token
ACCOUNT_ME = "/api/pub/v2/account/me"
SERVICE_LIST = "/api/pub/v2/services"
VERIFICATIONS = "/api/pub/v2/verifications"

# IG-specific service id — looked up at runtime from /services. Cached.
_SERVICE_ID_CACHE: Dict[str, str] = {}


class TextVerifiedError(RuntimeError):
    pass


class TextVerifiedClient:
    def __init__(self, api_key: Optional[str] = None, api_username: Optional[str] = None,
                 bearer_token: Optional[str] = None):
        # textverified API v2 auth: account-email + API key → POST /api/pub/v2/auth
        # → Bearer token. We support providing a pre-generated bearer directly
        # (TEXTVERIFIED_BEARER_TOKEN env var) — useful when minting one in the
        # dashboard rather than via /auth.
        self.api_key = api_key or os.environ.get("TEXTVERIFIED_API_KEY")
        self.api_username = api_username or os.environ.get("TEXTVERIFIED_API_USERNAME")
        self._token = bearer_token or os.environ.get("TEXTVERIFIED_BEARER_TOKEN") or None
        self._token_exp: float = float('inf') if self._token else 0.0
        if not self._token and not self.api_key:
            raise TextVerifiedError(
                "Need TEXTVERIFIED_BEARER_TOKEN, OR (TEXTVERIFIED_API_KEY + TEXTVERIFIED_API_USERNAME)")
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            # Cloudflare in front of textverified.com 403's bot-shaped requests
            "User-Agent": "Mozilla/5.0 (compatible; reel-bot/1.0)",
        })
        if self._token:
            self.session.headers["Authorization"] = f"Bearer {self._token}"

    # ── Auth ─────────────────────────────────────────────────────────
    def _fetch_token(self) -> str:
        """Get a fresh Bearer token from POST /api/pub/v2/auth.
        Confirmed shape from textverified.com/docs/api/v2#post-/api/pub/v2/auth:
            Headers: X-API-USERNAME (your email), X-API-KEY (your primary key)
            Body:    empty
        Tokens expire — caller (_ensure_token) handles periodic re-minting.
        """
        if not self.api_key or not self.api_username:
            raise TextVerifiedError(
                "to mint a bearer, both TEXTVERIFIED_API_KEY and "
                "TEXTVERIFIED_API_USERNAME (your account email) must be set")
        url = BASE_URL + TOKEN_PATH
        headers = {
            "X-API-KEY": self.api_key,
            "X-API-USERNAME": self.api_username,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; reel-bot/1.0)",
        }
        r = requests.post(url, headers=headers, timeout=30)
        if r.status_code != 200:
            raise TextVerifiedError(f"auth failed: HTTP {r.status_code} {r.text[:200]}")
        return self._extract_token(r.json())

    @staticmethod
    def _extract_token(body: Dict[str, Any]) -> str:
        for k in ("token", "bearerToken", "access_token", "accessToken"):
            if isinstance(body.get(k), str) and body[k]:
                return body[k]
        # Sometimes the token is nested
        data = body.get("data") if isinstance(body.get("data"), dict) else None
        if data:
            for k in ("token", "bearerToken", "access_token", "accessToken"):
                if isinstance(data.get(k), str) and data[k]:
                    return data[k]
        raise TextVerifiedError(f"no token field in auth response: {body}")

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_exp - 30:
            return self._token
        self._token = self._fetch_token()
        # Bearer tokens last ~30 min by default in their docs; cache for 25.
        self._token_exp = time.time() + 25 * 60
        self.session.headers["Authorization"] = f"Bearer {self._token}"
        return self._token

    # ── Helpers ──────────────────────────────────────────────────────
    def _request(self, method: str, path: str, timeout: int = 30, **kwargs) -> Any:
        self._ensure_token()
        url = BASE_URL + path
        r = self.session.request(method, url, timeout=timeout, **kwargs)
        if r.status_code == 401:
            # Token expired mid-session — refresh once and retry
            self._token = None
            self._ensure_token()
            r = self.session.request(method, url, timeout=timeout, **kwargs)
        if r.status_code >= 400:
            raise TextVerifiedError(f"{method} {path} → HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except Exception:
            return r.text

    # ── Public API ───────────────────────────────────────────────────
    def balance(self) -> float:
        body = self._request("GET", ACCOUNT_ME)
        if isinstance(body, dict):
            for k in ("currentBalance", "current_balance", "balance"):
                v = body.get(k)
                if isinstance(v, (int, float)):
                    return float(v)
        raise TextVerifiedError(f"could not parse balance from {body}")

    def list_services(self, number_type: str = "mobile",
                      reservation_type: str = "verification") -> List[Dict[str, Any]]:
        params = {"numberType": number_type, "reservationType": reservation_type}
        body = self._request("GET", SERVICE_LIST, params=params)
        if isinstance(body, list):
            return body
        if isinstance(body, dict):
            for k in ("services", "data", "items"):
                v = body.get(k)
                if isinstance(v, list):
                    return v
        return []

    def find_service_id(self, name_substring: str = "facebook") -> Optional[str]:
        """Find the service id for a given service (default: Instagram)."""
        cache_key = name_substring.lower()
        if cache_key in _SERVICE_ID_CACHE:
            return _SERVICE_ID_CACHE[cache_key]
        services = self.list_services()
        for svc in services:
            name = (svc.get("serviceName") or svc.get("name") or "").lower()
            if name_substring.lower() in name:
                sid = svc.get("serviceId") or svc.get("id") or svc.get("serviceName") or svc.get("name")
                if sid:
                    _SERVICE_ID_CACHE[cache_key] = sid
                    return sid
        return None

    def create_verification(self, service: str = "facebook",
                            capability: str = "sms",
                            number_wait: int = 45,
                            number_poll: int = 3) -> Dict[str, Any]:
        """Acquire a phone number for the given service. Returns {id, number, ...}.

        The v2 create endpoint returns ONLY an href (201, ~1s) — no id, no number:
            {"method":"GET","href":".../verifications/lr_..."}
        The number is provisioned ASYNCHRONOUSLY, so we take the id from the href and
        POLL the verification detail until the number appears (up to number_wait s).
        The old code fetched the detail ONCE, got number=None, and the caller stalled —
        that was the '/fb_page_verify hangs on Getting a number' bug (2026-07-20)."""
        service_id = self.find_service_id(service) or service  # API may accept name directly
        # The create POST is usually ~1s but INTERMITTENTLY stalls >30s. A client-side
        # timeout there still leaves the verification CREATED server-side (a silent
        # $0.75 charge that never reaches the user) — so give it a generous timeout to
        # actually receive the href instead of abandoning a paid verification.
        body = self._request("POST", VERIFICATIONS, timeout=90, json={
            "serviceName": service_id,
            "capability": capability,
        })
        vid = None
        number = None
        if isinstance(body, dict):
            href = body.get("href") or (body.get("link") or {}).get("href", "")
            vid = (body.get("id") or body.get("verificationId")
                   or (href.rsplit("/", 1)[-1] if href else None))
            number = body.get("number") or body.get("phoneNumber") or body.get("to")
        if not vid:
            raise TextVerifiedError(f"unexpected create_verification response: {body}")
        # Poll the detail until the number is provisioned (it appears within a few s).
        end = time.time() + max(0, number_wait)
        while not number:
            detail = self.get_verification(vid)
            number = detail.get("number") or detail.get("phoneNumber")
            if number or time.time() >= end:
                break
            time.sleep(number_poll)
        return {"id": vid, "number": number, "raw": body}

    def get_verification(self, verification_id: str) -> Dict[str, Any]:
        body = self._request("GET", f"{VERIFICATIONS}/{verification_id}")
        if isinstance(body, dict):
            return body
        return {}

    def poll_sms(self, verification_id: str, timeout: int = 300,
                 poll_interval: int = 5) -> Optional[str]:
        """Wait up to `timeout` seconds for an SMS to arrive on this verification.
        Returns the 6-digit code (string) or None."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                detail = self.get_verification(verification_id)
            except Exception as e:
                logger.warning(f"[TV poll] {e}"); time.sleep(poll_interval); continue
            # Look for SMS body in several common fields
            sms_body = None
            for k in ("smsContent", "messageContent", "message", "smsBody", "content"):
                v = detail.get(k)
                if isinstance(v, str) and v:
                    sms_body = v; break
            # Some APIs put SMSes in a list
            for k in ("messages", "sms", "incomingMessages"):
                v = detail.get(k)
                if isinstance(v, list) and v:
                    last = v[-1]
                    if isinstance(last, dict):
                        for kk in ("body", "content", "text", "message"):
                            if isinstance(last.get(kk), str):
                                sms_body = last[kk]; break
                    if sms_body: break
            if sms_body:
                # Extract the first 4-8 digit run — most IG codes are 6 digits
                m = re.search(r"\b(\d{4,8})\b", sms_body)
                if m:
                    code = m.group(1)
                    logger.info(f"[TV poll] got SMS '{sms_body[:60]}' → code {code}")
                    return code
            time.sleep(poll_interval)
        return None

    def cancel(self, verification_id: str) -> bool:
        """Cancel/release the verification (refund if no SMS yet)."""
        try:
            self._request("POST", f"{VERIFICATIONS}/{verification_id}/cancel")
            return True
        except TextVerifiedError as e:
            logger.warning(f"[TV cancel] {verification_id}: {e}")
            return False


# ── Module-level convenience (for quick scripts) ─────────────────────
_default_client: Optional[TextVerifiedClient] = None
def _client() -> TextVerifiedClient:
    global _default_client
    if _default_client is None:
        _default_client = TextVerifiedClient()
    return _default_client

def acquire_fb_number() -> Dict[str, Any]:
    """One-shot helper: returns {id, number} for an Instagram verification."""
    return _client().create_verification(service="instagram")

def poll_fb_sms(verification_id: str, timeout: int = 300) -> Optional[str]:
    return _client().poll_sms(verification_id, timeout=timeout)

def release_fb_number(verification_id: str) -> bool:
    return _client().cancel(verification_id)


if __name__ == "__main__":
    # Quick CLI: `python3 textverified_client.py balance` to verify auth works.
    import sys
    cli = TextVerifiedClient()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "balance"
    if cmd == "balance":
        print(f"Balance: ${cli.balance():.2f}")
    elif cmd == "services":
        for s in cli.list_services()[:30]:
            print(f"  {s.get('serviceName') or s.get('name')}  ${s.get('price', '?')}")
    elif cmd == "ig-number":
        v = cli.create_verification("instagram")
        print(f"Got number: {v['number']}  id={v['id']}")
        print("Polling for SMS (5 min timeout)…")
        code = cli.poll_sms(v["id"], timeout=300)
        print(f"SMS code: {code}")
        cli.cancel(v["id"])
    else:
        print(f"Unknown command: {cmd}. Try: balance | services | ig-number")
