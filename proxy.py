"""
proxy.py — batch proxy validate + GoLogin profile creator.

Ported from reel-bot-Carolina (2026-05-28) for the acc-setup-bot
(cloack_priv_prox) repo so Carolina is freed to focus on content
generation. Full Carolina pipeline:

  1. Pull fresh IPRoyal mobile proxy
  2. Pre-gates (cheap, ~5-15s): IPQS reputation + AbuseIPDB + ip-api +
     DNSBL + latency p95 + multi-destination + exit-IP
  3. Browser gates (slow, ~30-60s): Google "hello" + Facebook login
     attempt + reCAPTCHA v3 score (Playwright + Camoufox)
  4. If all gates pass, create a GoLogin profile with the proxy
     pre-attached, named "<prefix> N" (continues past existing).
  5. Loop until N profiles created.

Required env vars on Railway:
  GOLOGIN_API_KEY                  (api.gologin.com — get from gologin web UI)
  IPROYAL_API_KEY                  (or IPROYAL_USERNAME + IPROYAL_PASSWORD)
  IPQS_API_KEY                     (ipqualityscore.com)
  ABUSEIPDB_API_KEY                (abuseipdb.com)
  FB_PROXY_TEST_PHONE              (the dummy FB account used for the login probe)
  FB_PROXY_TEST_PASSWORD
  GOLOGIN_TEST_PROFILE_NAME        (default "TEST ACC FOR PROXY")
  (optional) GEELARK_API_KEY, GEELARK_APP_ID — not used in this v1 port
  (optional) GOLOGIN_LINK_CHECK_PROFILE_NAME

Proxy history is persisted to ./_proxy_history.json (local file). On Railway
this is ephemeral per deploy — history is wiped on redeploy. That's fine:
within a single run, the IPRoyal API rotates IPs so duplicates are unlikely.
"""

import os
import json
import time
import random
import logging
import asyncio
import re
from io import BytesIO
from pathlib import Path

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Env-var constants (mirror of Carolina's reel_bot.py top-level block)
# ──────────────────────────────────────────────────────────────────────
GOLOGIN_API_KEY = os.getenv('GOLOGIN_API_KEY', '')
GEELARK_API_KEY = os.getenv('GEELARK_API_KEY', '')   # API Key (for key verification)
GEELARK_APP_ID  = os.getenv('GEELARK_APP_ID', '')    # Team APP ID (for key verification)
IPROYAL_API_KEY = os.getenv('IPROYAL_API_KEY', '')
IPROYAL_USERNAME = os.getenv('IPROYAL_USERNAME', '')
IPROYAL_PASSWORD = os.getenv('IPROYAL_PASSWORD', '')
FB_PROXY_TEST_PHONE = os.getenv('FB_PROXY_TEST_PHONE', '')
FB_PROXY_TEST_PASSWORD = os.getenv('FB_PROXY_TEST_PASSWORD', '')
# Set FB_PROXY_SKIP_GOOGLE_GATE=1 to bypass the Google "hello" check (it
# crashes Camoufox ~9/10 launches in practice, wasting ~40s per crashed
# launch). The FB login + reCAPTCHA gates still run, so validation quality
# stays high — you just stop burning time on a broken browser test.
FB_PROXY_SKIP_GOOGLE_GATE = os.getenv('FB_PROXY_SKIP_GOOGLE_GATE', '') == '1'

# 2026-05-28: API-based pre-gates burn quota fast and rate-limit. Defaulted
# to SKIPPED so the bot doesn't loop through 20+ proxies in 4 min. Set
# PROXY_SKIP_*=0 in Railway to re-enable any individual gate. The free,
# unlimited gates (exit-IP, mobile-ASN, latency, multi-dest) always run.
PROXY_SKIP_IPQS      = os.getenv('PROXY_SKIP_IPQS',      '1') == '1'  # ipqualityscore.com  (paid, ~5k/mo free)
PROXY_SKIP_ABUSEIPDB = os.getenv('PROXY_SKIP_ABUSEIPDB', '1') == '1'  # abuseipdb.com       (1k/day free)
PROXY_SKIP_IPAPI     = os.getenv('PROXY_SKIP_IPAPI',     '1') == '1'  # ip-api.com          (45/min hard)
PROXY_SKIP_DNSBL     = os.getenv('PROXY_SKIP_DNSBL',     '1') == '1'  # Spamhaus (often false-flags mobile)
GOLOGIN_TEST_PROFILE_NAME = os.getenv('GOLOGIN_TEST_PROFILE_NAME', 'TEST ACC FOR PROXY')
IPQS_API_KEY = os.getenv('IPQS_API_KEY', '')
ABUSEIPDB_API_KEY = os.getenv('ABUSEIPDB_API_KEY', '')
PROXY_AUTO_SOFT_WARN_AT = 20
PROXY_AUTO_EXIT_IP_HISTORY_DAYS = 90
PROXY_AUTO_LOCATION_COUNTRY = 'us'
PROXY_AUTO_LOCATION_CITY = 'newyorkcity'
PROXY_AUTO_LIFETIME = '168h'  # 7 days, matches user's IPRoyal mobile plan config
PROXY_HOST = 'geo.iproyal.com'
PROXY_PORT_HTTP = 12321
PROXY_EXIT_IP_PROBE_URL = 'https://ipinfo.io/json'
PROXY_EXIT_IP_PROBE_TIMEOUT = 15
MOBILE_CARRIER_HINTS = [
    't-mobile', 'tmobile', 't mobile',
    'verizon wireless', 'verizon business', 'verizon communications',
    'at&t mobility', 'att mobility', 'att wireless', 'cingular',
    'sprint', 'us cellular', 'uscellular',
    'cricket', 'metro pcs', 'metropcs',
    'boost mobile', 'tracfone',
    'cellco',  # Verizon Wireless dba name
]
IPQS_FRAUD_SCORE_MAX = 75
ABUSEIPDB_CONFIDENCE_MAX = 50
IPQS_API_URL_TPL = 'https://www.ipqualityscore.com/api/json/ip/{key}/{ip}'
ABUSEIPDB_API_URL = 'https://api.abuseipdb.com/api/v2/check'
PROXY_LATENCY_P95_MAX_MS = 2500
PROXY_LATENCY_JITTER_MAX_MS = 700
PROXY_LATENCY_SAMPLES = 3
PROXY_MULTI_DEST_URLS = [
    'https://www.facebook.com/',
    'https://www.instagram.com/',
    'https://www.google.com/',
]
PROXY_MULTI_DEST_TIMEOUT = 15
GOLOGIN_LINK_CHECK_PROFILE_NAME = os.getenv('GOLOGIN_LINK_CHECK_PROFILE_NAME', '')

# ──────────────────────────────────────────────────────────────────────
# Local proxy history (replaces Carolina's Drive-backed history). Path:
# ./_proxy_history.json — survives within a single Railway deploy.
# ──────────────────────────────────────────────────────────────────────
_PROXY_HISTORY_PATH = Path("_proxy_history.json")


class ProxyPipeline:
    """Standalone proxy validate + GoLogin creator (Carolina's AccountManager
    proxy/gologin methods, extracted and adapted for a bot without Drive
    auth). One instance per process, lives in _PROXY_PIPELINE_SINGLETON."""

    def __init__(self):
        # Mirrors AccountManager state used by the proxy methods.
        # All Drive-related state is replaced by local-file equivalents.
        self._proxy_history_cache = None        # in-memory cache of the history JSON
        self.gologin_api_key = GOLOGIN_API_KEY
        self.iproyal_api_key = IPROYAL_API_KEY
        # Camoufox/Playwright lazy-import (only when browser gate runs).
        self._camoufox_imported = False

    # ── proxy history (LOCAL FILE replacement for Carolina's Drive impl) ──

    def _proxy_history_load(self):
        if self._proxy_history_cache is not None:
            return self._proxy_history_cache
        try:
            if _PROXY_HISTORY_PATH.exists():
                self._proxy_history_cache = json.loads(_PROXY_HISTORY_PATH.read_text())
            else:
                self._proxy_history_cache = {"entries": []}
        except Exception as e:
            logger.warning(f"[proxy_history_load] {e} — starting fresh")
            self._proxy_history_cache = {"entries": []}
        return self._proxy_history_cache

    def _proxy_history_save(self, data):
        try:
            _PROXY_HISTORY_PATH.write_text(json.dumps(data, indent=2))
            self._proxy_history_cache = data
        except Exception as e:
            logger.warning(f"[proxy_history_save] {e}")

    def _proxy_history_seen_sets(self):
        data = self._proxy_history_load()
        seen_ips = {e.get("ip") for e in data.get("entries", []) if e.get("ip")}
        seen_strs = {e.get("proxy_str") for e in data.get("entries", []) if e.get("proxy_str")}
        return seen_ips, seen_strs

    def _proxy_history_record(self, proxy_str, ip, **kwargs):
        """Flexible kwargs to match Carolina's pipeline (fb_result, source,
        score, passed, reason, ...). Just records whatever you pass."""
        data = self._proxy_history_load()
        entry = {"ts": int(time.time()), "proxy_str": proxy_str, "ip": ip}
        entry.update(kwargs)
        data.setdefault("entries", []).append(entry)
        # Cap to last 2000 entries (avoid unbounded growth across restarts).
        data["entries"] = data["entries"][-2000:]
        self._proxy_history_save(data)

    def get_iproyal_proxy(self, country='US'):
        """Generate a sticky mobile proxy from IPRoyal."""
        if not IPROYAL_API_KEY:
            return None, "IPROYAL_API_KEY not set"
        
        url = 'https://resi-api.iproyal.com/v1/access/generate-proxy-list'
        headers = {
            'Authorization': f'Bearer {IPROYAL_API_KEY}',
            'Content-Type': 'application/json'
        }
        data = {
            'format': '{hostname}:{port}:{username}:{password}',
            'hostname': 'geo.iproyal.com',
            'port': 'http|https',
            'rotation': 'sticky',
            'sticky_session': '7d',
            'location': f'_country-{country.lower()}',
            'proxy_count': 1,
        }
        if IPROYAL_USERNAME:
            data['username'] = IPROYAL_USERNAME
        if IPROYAL_PASSWORD:
            data['password'] = IPROYAL_PASSWORD
        
        try:
            resp = requests.post(url, json=data, headers=headers, timeout=30)
            if resp.status_code == 200:
                proxy_str = resp.text.strip()
                logger.info(f"[IPRoyal] Generated proxy: {proxy_str[:30]}...")
                return proxy_str, "OK"
            return None, f"API error {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            return None, f"Error: {e}"

    def get_iproyal_proxy_nyc(self):
        """Construct a fresh IPRoyal MOBILE proxy credential string.

        IPRoyal Mobile doesn't expose a separate API for generating proxies
        — instead the credentials are constructed client-side by appending
        _country/_city/_session/_lifetime parameters to the base proxy
        password. Each call produces a new random 8-char session id so the
        string is unique by construction. The actual carrier IP that
        backs the session is assigned server-side at first use; we verify
        it via probe_proxy_exit_ip before trusting the proxy.

        Required env vars (NOT API keys — these are the actual proxy
        credentials from the IPRoyal dashboard 'Proxy username' and
        'Proxy password' fields):
          IPROYAL_USERNAME, IPROYAL_PASSWORD

        Returns (proxy_str, msg). proxy_str format: host:port:user:pass
        where pass includes the embedded _country/_city/_session/_lifetime.
        """
        if not IPROYAL_USERNAME or not IPROYAL_PASSWORD:
            return None, ("IPROYAL_USERNAME and IPROYAL_PASSWORD must be set "
                          "on Railway — these are the 'Proxy username' and "
                          "'Proxy password' fields from your IPRoyal mobile "
                          "dashboard, NOT an API key (mobile proxies don't "
                          "use one)")
        # Defensive: if the user pasted the FULL formatted password (which
        # already contains _country-/_city-/_session-/_lifetime- suffixes
        # from IPRoyal's dashboard "Formatted Proxy List" output), strip
        # those before we append fresh ones. Otherwise we'd build a
        # double-tagged password like:
        #   pass_country-us_city-X_session-A_lifetime-Y_country-us_city-X_session-B_lifetime-Y
        # which IPRoyal still parses but downstream tools like GoLogin
        # reject the whole proxy because of the extra crap. (Hit 2026-05-13.)
        base_password = IPROYAL_PASSWORD
        for marker in ('_country-', '_city-', '_session-', '_lifetime-'):
            idx = base_password.find(marker)
            if idx >= 0:
                base_password = base_password[:idx]
                break  # first marker found = truncation point
        import secrets as _secrets
        # IPRoyal session IDs MUST be exactly 8 ALPHANUMERIC chars per docs.
        # Previous version used token_urlsafe which can include _ and -, and
        # IPRoyal silently rejected those sessions (returning a default fallback
        # IP), causing every attempt to either dedupe-collide on the same
        # default IP or fail mobile-carrier validation. We use a SystemRandom
        # picker over the exact 62-char alphanumeric alphabet that the
        # dashboard produces (e.g. WWDZIRto, 9sTR5GaQ, U5Fcks2k).
        _ALNUM = ('ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                  'abcdefghijklmnopqrstuvwxyz'
                  '0123456789')
        _rng = _secrets.SystemRandom()
        session = ''.join(_rng.choice(_ALNUM) for _ in range(8))
        pw_with_params = (f"{base_password}"
                          f"_country-{PROXY_AUTO_LOCATION_COUNTRY}"
                          f"_city-{PROXY_AUTO_LOCATION_CITY}"
                          f"_session-{session}"
                          f"_lifetime-{PROXY_AUTO_LIFETIME}")
        proxy_str = (f"{PROXY_HOST}:{PROXY_PORT_HTTP}:"
                     f"{IPROYAL_USERNAME}:{pw_with_params}")
        logger.info(f"[IPRoyal/auto] constructed mobile proxy (session={session})")
        return proxy_str, "OK"

    def _is_mobile_carrier(self, isp_org):
        """True if ipinfo.io's `org` field indicates a mobile/cellular ISP.
        Case-insensitive substring match against MOBILE_CARRIER_HINTS."""
        if not isp_org:
            return False
        s = isp_org.lower()
        return any(hint in s for hint in MOBILE_CARRIER_HINTS)

    def lookup_ipqs_reputation(self, ip):
        """Query IPQualityScore for fraud reputation on an IP.

        Returns dict: {'available': bool, 'fraud_score': int, 'recent_abuse':
        bool, 'proxy': bool, 'vpn': bool, 'tor': bool, 'bot_status': bool,
        'isp': str, 'err': str}. If IPQS_API_KEY isn't set or the call
        fails, returns {'available': False, 'err': reason}.

        Graceful: a failed lookup never blocks the pipeline — the bot
        decides what to do based on `available`.
        """
        out = {'available': False, 'err': '',
               'fraud_score': None, 'recent_abuse': None,
               'proxy': None, 'vpn': None, 'tor': None,
               'bot_status': None, 'isp': None}
        if not IPQS_API_KEY:
            out['err'] = 'IPQS_API_KEY not set'
            return out
        if not ip:
            out['err'] = 'no IP'
            return out
        url = IPQS_API_URL_TPL.format(key=IPQS_API_KEY, ip=ip)
        try:
            # Mobile-tuned params: strictness=0 (least strict — mobile IPs
            # are inherently 'noisy' to IPQS so use the most permissive
            # scoring model), allow_public_access_points=true (mobile
            # carriers are essentially public-access), lighter_penalties=true
            # (penalize known proxy infra less since IPRoyal mobile IS one),
            # mobile=1 (tells IPQS this is a mobile-context lookup). Result
            # is more accurate but still ignored — we gate on booleans.
            resp = requests.get(url, timeout=12,
                                params={'strictness': 0,
                                        'allow_public_access_points': 'true',
                                        'lighter_penalties': 'true',
                                        'mobile': '1'})
            if resp.status_code != 200:
                out['err'] = f'HTTP {resp.status_code}: {resp.text[:120]}'
                return out
            data = resp.json() or {}
            if not data.get('success', True):
                out['err'] = f'IPQS error: {data.get("message", "unknown")}'
                return out
            out['available'] = True
            out['fraud_score'] = int(data.get('fraud_score') or 0)
            out['recent_abuse'] = bool(data.get('recent_abuse'))
            out['proxy'] = bool(data.get('proxy'))
            out['vpn'] = bool(data.get('vpn'))
            out['tor'] = bool(data.get('tor'))
            out['bot_status'] = bool(data.get('bot_status'))
            out['isp'] = data.get('ISP') or data.get('organization')
            return out
        except Exception as e:
            out['err'] = f'{type(e).__name__}: {e}'
            return out

    def lookup_abuseipdb_reputation(self, ip):
        """Query AbuseIPDB for abuse reports on an IP.

        Returns dict: {'available': bool, 'confidence_score': int (0-100),
        'total_reports': int, 'last_reported_at': str|None, 'err': str}.
        Graceful: failed lookups never block.
        """
        out = {'available': False, 'err': '',
               'confidence_score': None, 'total_reports': None,
               'last_reported_at': None}
        if not ABUSEIPDB_API_KEY:
            out['err'] = 'ABUSEIPDB_API_KEY not set'
            return out
        if not ip:
            out['err'] = 'no IP'
            return out
        try:
            resp = requests.get(
                ABUSEIPDB_API_URL,
                headers={'Key': ABUSEIPDB_API_KEY, 'Accept': 'application/json'},
                params={'ipAddress': ip, 'maxAgeInDays': 90},
                timeout=12,
            )
            if resp.status_code != 200:
                out['err'] = f'HTTP {resp.status_code}: {resp.text[:120]}'
                return out
            data = (resp.json() or {}).get('data') or {}
            out['available'] = True
            out['confidence_score'] = int(data.get('abuseConfidenceScore') or 0)
            out['total_reports'] = int(data.get('totalReports') or 0)
            out['last_reported_at'] = data.get('lastReportedAt')
            return out
        except Exception as e:
            out['err'] = f'{type(e).__name__}: {e}'
            return out

    def _proxy_raw_to_url(raw, scheme='http'):
        """Convert IPRoyal raw 'host:port:user:pass' to a valid proxy
        URL like 'http://user:pass@host:port'. Splits on ':' but joins
        anything past index 3 back into pass (since IPRoyal sticky
        sessions use ':' inside the password). Returns None on malform."""
        if not raw or not isinstance(raw, str):
            return None
        parts = raw.split(':')
        if len(parts) < 4:
            return None
        host, port = parts[0], parts[1]
        user = parts[2]
        # IPRoyal sticky-session passwords contain '_' but not ':'.
        # If extra ':' parts exist, rejoin (defensive).
        password = ':'.join(parts[3:])
        from urllib.parse import quote as _q
        return f"{scheme}://{_q(user, safe='')}:{_q(password, safe='')}@{host}:{port}"

    def lookup_ipapi_profile(self, ip, proxy_url=None, scheme='http'):
        """Query ip-api.com for a free second-opinion datacenter/proxy/mobile
        profile of an IP. Sourced from a friend's setup (2026-05-14) — the
        explicit `hosting` and `proxy` flags occasionally catch IPs IPQS
        misses (different vendor, different fingerprint database).

        If `proxy_url` is given, the request is routed THROUGH that proxy
        so we get the EXIT IP's profile (matches what FB/Google would see).
        Otherwise looks up the literal `ip` argument from our own egress.

        ip-api.com free tier: 45 req/min from a given source. Free tier is
        HTTP-only — HTTPS requires their pro tier.

        Returns {'available': bool, 'hosting': bool, 'proxy': bool,
                 'mobile': bool, 'query': str (echoed IP), 'err': str}.
        Graceful: failed lookups never block.
        """
        out = {'available': False, 'err': '',
               'hosting': None, 'proxy': None, 'mobile': None, 'query': None}
        url = 'http://ip-api.com/json/'
        if ip and not proxy_url:
            url += ip
        url += '?fields=mobile,proxy,hosting,query,status,message'
        try:
            kw = {'timeout': 10}
            if proxy_url:
                # Route the lookup through the proxy itself so we see the
                # EXIT IP's profile, not our local one. Caller may pass
                # either an IPRoyal raw string ('host:port:user:pass') or
                # an already-formatted URL ('http://user:pass@host:port').
                # Normalize via _proxy_raw_to_url so requests gets a real URL.
                if '://' in proxy_url and '@' in proxy_url:
                    formatted = proxy_url  # already a URL
                else:
                    formatted = self._proxy_raw_to_url(proxy_url, scheme)
                if not formatted:
                    out['err'] = f'unparseable proxy: {proxy_url[:80]}'
                    return out
                kw['proxies'] = {'http': formatted, 'https': formatted}
            resp = requests.get(url, **kw)
            if resp.status_code != 200:
                out['err'] = f'HTTP {resp.status_code}'
                return out
            data = resp.json() or {}
            if data.get('status') == 'fail':
                out['err'] = f"ip-api fail: {data.get('message', 'unknown')}"
                return out
            out['available'] = True
            out['hosting'] = bool(data.get('hosting'))
            out['proxy'] = bool(data.get('proxy'))
            out['mobile'] = bool(data.get('mobile'))
            out['query'] = data.get('query')
            return out
        except Exception as e:
            out['err'] = f'{type(e).__name__}: {e}'
            return out

    def check_dnsbl(self, ip):
        """Reverse-DNS-lookup an IP against four spam/abuse blocklists in
        parallel. Returns {'available': bool, 'listed_on': [str,...],
        'unknown': [str,...], 'err': str}. `listed_on` is the lists that
        flagged the IP as bad. `unknown` is lists that timed out / hit DNS
        rate limits — those are NOT counted as listings.

        IMPORTANT — Spamhaus PBL is intentionally treated as NEUTRAL, not
        bad. PBL = Policy Block List of IPs that shouldn't be sending
        email — i.e., residential and mobile IPs. That's exactly what we
        WANT for our mobile proxies. The friend's pipeline (which uses
        zen.spamhaus.org and rejects ANY 127.0.0.x) over-rejects clean
        mobile IPs because of this. We inspect Spamhaus return codes:
          127.0.0.2/3      = SBL, CSS  (real spammer)        → BAD
          127.0.0.4/5/6/7  = XBL       (compromised host)    → BAD
          127.0.0.10/11    = PBL       (residential/mobile)  → SKIP
        For the other three lists (SpamCop, SORBS, Barracuda), any
        127.0.0.x reply is a listing.
        """
        import socket as _socket
        out = {'available': True, 'listed_on': [], 'unknown': [], 'err': ''}
        if not ip:
            out['available'] = False
            out['err'] = 'no IP'
            return out
        try:
            octets = ip.split('.')
            if len(octets) != 4:
                out['available'] = False
                out['err'] = f'not an IPv4: {ip!r}'
                return out
            reversed_ip = '.'.join(reversed(octets))
        except Exception as e:
            out['available'] = False
            out['err'] = f'bad IP: {e}'
            return out

        def _query(zone, spamhaus_pbl_aware=False):
            host = f'{reversed_ip}.{zone}'
            try:
                ans = _socket.gethostbyname(host)
                # 127.0.0.x answer = listed. Spamhaus zen returns specific
                # codes per sublist — only flag SBL/CSS/XBL, ignore PBL.
                if ans.startswith('127.0.0.'):
                    if spamhaus_pbl_aware:
                        last = ans.rsplit('.', 1)[-1]
                        if last in ('10', '11'):
                            return 'pbl_skip'    # residential — neutral
                    return 'listed'
                return 'noise'                    # unexpected reply
            except _socket.gaierror:
                return 'clean'                    # NXDOMAIN = not listed
            except Exception:
                return 'unknown'                  # DNS failure / timeout

        zones = [
            ('zen.spamhaus.org',     True),  # PBL-aware
            ('bl.spamcop.net',       False),
            ('dnsbl.sorbs.net',      False),
            ('b.barracudacentral.org', False),
        ]
        for zone, pbl_aware in zones:
            r = _query(zone, spamhaus_pbl_aware=pbl_aware)
            if r == 'listed':
                out['listed_on'].append(zone)
            elif r == 'unknown':
                out['unknown'].append(zone)
            # 'clean' / 'pbl_skip' / 'noise' → no action
        return out

    def probe_proxy_latency(self, proxy_str, samples=None):
        """Measure round-trip latency through the proxy via N small HEAD
        requests to google.com (cheap, low-load, low-bandwidth). Returns
        {'available': bool, 'samples': [ms,...], 'p50_ms': int, 'p95_ms':
        int, 'jitter_ms': int, 'err': str}.

        Used by Tier 2 quality probe to reject proxies that would feel
        slow to a user (matches the "5-minute Facebook load times"
        botcheck-precursor pattern).
        """
        import time as _time
        out = {'available': False, 'err': '', 'samples': [],
               'p50_ms': None, 'p95_ms': None, 'jitter_ms': None}
        parts = (proxy_str or '').split(':')
        if len(parts) < 4:
            out['err'] = 'malformed proxy string'
            return out
        host, port, user, password = parts[0], parts[1], parts[2], ':'.join(parts[3:])
        proxy_url = f'http://{user}:{password}@{host}:{port}'
        proxies = {'http': proxy_url, 'https': proxy_url}
        n = samples or PROXY_LATENCY_SAMPLES
        samples_ms = []
        for _ in range(n):
            t0 = _time.time()
            try:
                # HEAD is cheaper than GET; google.com is reliable + fast.
                r = requests.head('https://www.google.com/', proxies=proxies,
                                  timeout=10, allow_redirects=False)
                samples_ms.append(int((_time.time() - t0) * 1000))
            except Exception as e:
                out['err'] = f'{type(e).__name__}: {e}'
                return out
        samples_ms.sort()
        out['available'] = True
        out['samples'] = samples_ms
        out['p50_ms'] = samples_ms[len(samples_ms) // 2]
        out['p95_ms'] = samples_ms[min(len(samples_ms) - 1,
                                        int(len(samples_ms) * 0.95))]
        out['jitter_ms'] = samples_ms[-1] - samples_ms[0]
        return out

    def probe_proxy_multi_destination(self, proxy_str, urls=None):
        """Hit each URL through the proxy and verify success. Detects
        selective throttling (proxy that loads google.com fine but
        rate-limits facebook.com — a known clustering tell).

        Returns {'available': bool, 'results': [{url, status, latency_ms,
        ok, err}], 'all_ok': bool, 'err': str}.
        """
        import time as _time
        out = {'available': False, 'err': '', 'results': [], 'all_ok': False}
        parts = (proxy_str or '').split(':')
        if len(parts) < 4:
            out['err'] = 'malformed proxy string'
            return out
        host, port, user, password = parts[0], parts[1], parts[2], ':'.join(parts[3:])
        proxy_url = f'http://{user}:{password}@{host}:{port}'
        proxies = {'http': proxy_url, 'https': proxy_url}
        target_urls = urls or PROXY_MULTI_DEST_URLS
        all_ok = True
        for u in target_urls:
            row = {'url': u, 'status': None, 'latency_ms': None,
                   'ok': False, 'err': ''}
            t0 = _time.time()
            try:
                r = requests.get(u, proxies=proxies,
                                 timeout=PROXY_MULTI_DEST_TIMEOUT,
                                 allow_redirects=True)
                row['status'] = r.status_code
                row['latency_ms'] = int((_time.time() - t0) * 1000)
                # 2xx-3xx all count as reachable; 4xx-5xx as throttled
                row['ok'] = 200 <= r.status_code < 400
                if not row['ok']:
                    all_ok = False
            except Exception as e:
                row['err'] = f'{type(e).__name__}: {e}'
                all_ok = False
            out['results'].append(row)
        out['available'] = True
        out['all_ok'] = all_ok
        return out

    def probe_proxy_exit_ip(self, proxy_str, timeout=None, scheme='http'):
        """Probe exit IP AND ISP through the proxy via ipinfo.io. Used by
        the auto-pipeline to (a) dedupe against previously-touched IPs
        before burning the heavyweight check, and (b) validate the proxy
        is mobile-carrier-backed (rejecting residential/data-center IPs
        that IPRoyal might occasionally return).

        proxy_str format: host:port:user:pass (IPRoyal-formatted).
        scheme: 'http' for HTTP proxies (IPRoyal default), 'socks5h' for
                SOCKS5 with DNS-via-proxy (fxdx.in, Proxy-Cheap, etc.).
                socks5h is critical for SOCKS5 — using 'http' silently
                fails since requests can't speak SOCKS5 via the http://
                scheme. See [[fxdx-private-proxy]] for the user's setup.
        Returns (ip_str, isp_org_str, err_str) — err is "" on success.
        """
        parts = (proxy_str or '').split(':')
        if len(parts) < 4:
            return None, None, "malformed proxy string"
        host, port, user, password = parts[0], parts[1], parts[2], ':'.join(parts[3:])
        proxy_url = f"{scheme}://{user}:{password}@{host}:{port}"
        timeout = timeout or PROXY_EXIT_IP_PROBE_TIMEOUT
        try:
            resp = requests.get(PROXY_EXIT_IP_PROBE_URL,
                                proxies={'http': proxy_url, 'https': proxy_url},
                                timeout=timeout)
            if resp.status_code == 200:
                data = resp.json() or {}
                ip = data.get('ip')
                org = data.get('org') or ''
                if ip:
                    return ip, org, ""
                return None, None, f"no 'ip' in response: {resp.text[:200]}"
            return None, None, f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            return None, None, f"{type(e).__name__}: {e}"

    def create_gologin_profile(self, name, proxy_str=None):
        """Create a GoLogin browser profile with optional proxy.

        Endpoint discovered via OpenAPI spec (api.gologin.com/docs-json,
        2026-05-13): POST /browser/custom with schema
        CreateCustomBrowserValidation. Top-level has no required fields;
        if navigator is included, userAgent + resolution + language +
        platform are all required, so we omit it entirely and let
        GoLogin auto-generate. Proxy.mode is the only required proxy
        field. browserType does NOT exist in this schema.
        """
        if not GOLOGIN_API_KEY:
            logger.warning("[GoLogin] create called but GOLOGIN_API_KEY not set")
            return None, "GOLOGIN_API_KEY not set"

        url = 'https://api.gologin.com/browser/custom'
        headers = {
            'Authorization': f'Bearer {GOLOGIN_API_KEY}',
            'Content-Type': 'application/json'
        }

        profile_data = {
            'name': name,
            'os': 'win',
            'autoLang': True,
        }

        proxy_summary = '<none>'
        if proxy_str:
            parts = proxy_str.split(':')
            if len(parts) >= 4:
                profile_data['proxy'] = {
                    'mode': 'http',
                    'host': parts[0],
                    'port': int(parts[1]) if parts[1].isdigit() else 12321,
                    'username': parts[2] if len(parts) > 2 else '',
                    'password': parts[3] if len(parts) > 3 else '',
                }
                proxy_summary = f"http://{parts[2][:8]}***@{parts[0]}:{parts[1]}"
            else:
                logger.warning(f"[GoLogin] proxy_str malformed (got {len(parts)} parts, need 4): "
                               f"{proxy_str[:60]}...")

        logger.info(f"[GoLogin] POST {url} name={name!r} proxy={proxy_summary}")
        try:
            resp = requests.post(url, json=profile_data, headers=headers, timeout=30)
            logger.info(f"[GoLogin] response status={resp.status_code} body={resp.text[:400]}")
            if resp.status_code in (200, 201):
                profile = resp.json()
                profile_id = profile.get('id')
                logger.info(f"[GoLogin] Created profile: {name} ({profile_id})")
                return profile_id, "OK"
            return None, f"API error {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            return None, f"Error: {e}"

    def delete_gologin_profile(self, profile_id):
        """Delete a GoLogin profile."""
        url = f'https://api.gologin.com/browser/{profile_id}'
        headers = {'Authorization': f'Bearer {GOLOGIN_API_KEY}'}
        try:
            resp = requests.delete(url, headers=headers, timeout=30)
            return resp.status_code in (200, 204)
        except:
            return False

    def get_gologin_profile_by_name(self, name):
        """Find a GoLogin profile ID by profile name."""
        if not GOLOGIN_API_KEY:
            return None, "GOLOGIN_API_KEY not set"
        headers = {'Authorization': f'Bearer {GOLOGIN_API_KEY}'}
        try:
            resp = requests.get(
                'https://api.gologin.com/browser/v2',
                headers=headers,
                params={'limit': 250},
                timeout=30
            )
            if resp.status_code != 200:
                return None, f"API error {resp.status_code}: {resp.text[:200]}"
            data = resp.json()
            profiles = data.get('profiles') or data.get('browser') or (data if isinstance(data, list) else [])
            name_lower = name.strip().lower()
            for p in profiles:
                if p.get('name', '').strip().lower() == name_lower:
                    return p.get('id'), "OK"
            return None, f"Profile '{name}' not found among {len(profiles)} profiles"
        except Exception as e:
            return None, f"Error: {e}"

    async def _launch_stealth_browser(self, proxy_cfg):
        """Launch Camoufox (patched Firefox) with anti-detection at the C++ level.

        2026-05-15: added os='macos' so Camoufox spoofs a macOS
        fingerprint instead of Linux. We're on Linux Railway hitting
        Google + FB through a US T-Mobile mobile proxy — a Linux
        fingerprint on a mobile carrier IP is a massive bot tell.
        Result before: 11/11 Google captcha hits in a single auto run.
        Also disable_coop=True for cross-origin captcha flows.
        2026-05-15 (later): pinned viewport to (1366, 900) so every
        screenshot taken during heavy proxy check (reCAPTCHA, Google,
        FB login) is a clean landscape frame instead of the default
        cropped/partial shape that left the user squinting at 60%
        of a screen."""
        from camoufox.async_api import AsyncCamoufox
        self._camoufox_ctx = AsyncCamoufox(
            headless="virtual",
            geoip=True,
            proxy=proxy_cfg,
            humanize=True,
            block_webrtc=True,
            os="macos",
            disable_coop=True,
            window=(1366, 900),
        )
        browser = await self._camoufox_ctx.__aenter__()
        page = await browser.new_page()
        logger.info("[Stealth] Camoufox launched (patched Firefox, virtual display, geoip)")
        return browser, page

    async def _simulate_human_behavior(self, page):
        """Simulate mouse movements and scrolling like a real user."""
        import random as _rand
        for _ in range(3):
            x = _rand.randint(100, 1200)
            y = _rand.randint(100, 600)
            await page.mouse.move(x, y, steps=_rand.randint(5, 15))
            await page.wait_for_timeout(_rand.randint(100, 400))
        await page.mouse.wheel(0, _rand.randint(50, 200))
        await page.wait_for_timeout(_rand.randint(300, 800))

    async def check_recaptcha_score(self, host, port, username, password, protocol='socks5'):
        """
        Step 1: Open antcpt.com/score_detector/ with proxy and read reCAPTCHA v3 score.
        Returns: (score_float_or_None, screenshot_bytes)
        """
        browser_scheme = 'http' if protocol in ('socks5', 'socks4') else protocol
        proxy_server = f"{browser_scheme}://{host}:{port}"
        proxy_cfg = {'server': proxy_server}
        if username:
            proxy_cfg['username'] = username
        if password:
            proxy_cfg['password'] = password

        logger.info(f"[RecaptchaScore] checking score via {proxy_server}")

        try:
            import random as _rand
            browser, page = await self._launch_stealth_browser(proxy_cfg)

            try:
                await page.goto('https://antcpt.com/score_detector/', wait_until='domcontentloaded', timeout=60000)
                logger.info(f"[RecaptchaScore] page loaded, waiting for score...")

                await self._simulate_human_behavior(page)

                score = None
                for attempt in range(20):
                    await page.wait_for_timeout(3000)
                    content = await page.content()
                    import re as _re
                    match = _re.search(r'Your score is:\s*([\d.]+)', content)
                    if match:
                        score = float(match.group(1))
                        logger.info(f"[RecaptchaScore] score detected: {score}")
                        break
                    logger.info(f"[RecaptchaScore] waiting for score... attempt {attempt+1}/20")

                screenshot = await page.screenshot(type='png')

                if score is None:
                    logger.warning("[RecaptchaScore] score never appeared on page")
                return score, screenshot
            finally:
                await self._camoufox_ctx.__aexit__(None, None, None)
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            logger.error(f"[RecaptchaScore] error: {err_msg}")
            if 'TIMED_OUT' in str(e) or 'Timeout' in type(e).__name__:
                return 'timeout', None
            return None, None

    async def _measure_proxy_latency(self, proxy_raw, samples=3,
                                       target='https://ipinfo.io/json',
                                       per_sample_timeout=8):
        """Measure proxy round-trip latency by hitting `target` `samples`
        times through the proxy. Returns dict:
          {ok, samples_ms, p95_ms, jitter_ms, err}
        - p95_ms: 95th-percentile of samples (with N=3, this is the max)
        - jitter_ms: max - min across samples
        - err: populated only if probe failed entirely

        Uses requests directly via socks5h (no browser, no DNS leak —
        DNS resolves through the proxy too)."""
        import time as _time
        import asyncio as _asyncio
        out = {'ok': False, 'samples_ms': [], 'p95_ms': None,
               'jitter_ms': None, 'err': None}
        try:
            parts = proxy_raw.split(':')
            if len(parts) < 4:
                out['err'] = 'malformed proxy'
                return out
            host, port, user, pw = parts[0], parts[1], parts[2], ':'.join(parts[3:])
            # socks5h:// = resolve DNS via the proxy (avoids DNS-leak skew
            # in latency measurements). requests handles this via PySocks.
            proxy_url = f"socks5h://{user}:{pw}@{host}:{port}"
            proxies = {'http': proxy_url, 'https': proxy_url}
        except Exception as e:
            out['err'] = f'proxy parse: {e}'
            return out

        for i in range(samples):
            t0 = _time.monotonic()
            try:
                r = await _asyncio.to_thread(
                    requests.get, target,
                    timeout=per_sample_timeout,
                    proxies=proxies)
                if r.status_code == 200:
                    out['samples_ms'].append(
                        int((_time.monotonic() - t0) * 1000))
                else:
                    out['samples_ms'].append(per_sample_timeout * 1000)
            except Exception:
                out['samples_ms'].append(per_sample_timeout * 1000)

        if not out['samples_ms']:
            out['err'] = 'no samples collected'
            return out
        sorted_ms = sorted(out['samples_ms'])
        # p95 with N=3 → take the max sample (conservative)
        out['p95_ms'] = sorted_ms[-1]
        out['jitter_ms'] = sorted_ms[-1] - sorted_ms[0]
        out['ok'] = True
        return out

    async def check_google_hello(self, host, port, username, password, protocol='socks5'):
        """Shard 3 (2026-05-14) — Google "hello" test. Open google.com via
        the proxy and submit a benign search ("hello"). If real results
        render, the proxy + browser fingerprint isn't flagged by Google.
        If Google shows the "Our systems have detected unusual traffic"
        captcha gate, the proxy/fingerprint combo is burned.

        Why this matters: Google's captcha gate is the *earliest* signal
        a proxy is on a known-bad list — Google sees almost every web
        request via search APIs, fonts, analytics. If Google blocks, the
        proxy is unreliable for any downstream automation too. Catching
        it BEFORE the (slower, FB-account-burning) FB login test saves
        time + a login attempt against the shared test account.

        Order in the pipeline:  reCAPTCHA → Google hello → FB login.
        reCAPTCHA is cheapest (just a score from antcpt.com). Google
        hello is mid-cost (one search page). FB login is the expensive
        final test.

        Returns (result, screenshot_bytes) where result is:
          'good'    — real search results page rendered
          'captcha' — Google's "I'm not a robot" gate appeared
          'timeout' — page never finished loading
          None      — unexpected error
        """
        import random as _rand
        browser_scheme = 'http' if protocol in ('socks5', 'socks4') else protocol
        proxy_server = f"{browser_scheme}://{host}:{port}"
        proxy_cfg = {'server': proxy_server}
        if username:
            proxy_cfg['username'] = username
        if password:
            proxy_cfg['password'] = password

        logger.info(f"[GoogleHello] launching Camoufox via {proxy_server}")
        try:
            logger.info("[GoogleHello] step 1/6 — launching browser")
            browser, page = await self._launch_stealth_browser(proxy_cfg)
            try:
                logger.info("[GoogleHello] step 2/6 — page.goto google search")
                await page.goto('https://www.google.com/search?q=hello&hl=en',
                                wait_until='domcontentloaded', timeout=30000)
                logger.info(f"[GoogleHello] step 3/6 — loaded, url={page.url}")
                await self._simulate_human_behavior(page)
                await page.wait_for_timeout(_rand.randint(800, 1500))

                # Two clear signals — check URL FIRST (most reliable):
                final_url = page.url or ''
                logger.info("[GoogleHello] step 4/6 — reading content")
                content = (await page.content()) or ''
                lower = content.lower()
                logger.info(f"[GoogleHello] step 5/6 — content {len(content)} bytes, classifying")

                # Google redirects to /sorry/index when it gates the request.
                if '/sorry/' in final_url or 'about this page' in lower or \
                   "i'm not a robot" in lower or \
                   'unusual traffic from your computer network' in lower:
                    logger.warning(f"[GoogleHello] CAPTCHA gate at {final_url}")
                    screenshot = await page.screenshot(type='png')
                    return 'captcha', screenshot

                # Positive markers: a real SERP has the search input
                # echo, the result divs, and the "About X results" line.
                positive = ('id="search"' in content
                            or 'search-results' in content
                            or 'results' in lower)
                if positive:
                    logger.info(f"[GoogleHello] real SERP rendered at {final_url}")
                    screenshot = await page.screenshot(type='png')
                    return 'good', screenshot

                # Ambiguous — neither captcha nor clear SERP. Treat as
                # captcha defensively (better to retry a proxy than to
                # ship one that might be silently degraded).
                logger.warning(f"[GoogleHello] ambiguous response at {final_url}, "
                               f"defensive-fail")
                screenshot = await page.screenshot(type='png')
                return 'captcha', screenshot
            finally:
                await self._camoufox_ctx.__aexit__(None, None, None)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            logger.error(f"[GoogleHello] error: {err}")
            if 'TIMED_OUT' in str(e) or 'Timeout' in type(e).__name__:
                return 'timeout', None
            return None, None

    async def test_proxy_on_facebook(self, host, port, username, password, protocol='socks5'):
        """
        Step 2: Launch Camoufox with proxy and test Facebook login.
        Returns: (result, screenshot_bytes, page_url)
        """
        browser_scheme = 'http' if protocol in ('socks5', 'socks4') else protocol
        proxy_server = f"{browser_scheme}://{host}:{port}"
        proxy_cfg = {'server': proxy_server}
        if username:
            proxy_cfg['username'] = username
        if password:
            proxy_cfg['password'] = password

        logger.info(f"[ProxyCheck] launching Camoufox with proxy {proxy_server}")

        try:
            import random as _rand
            browser, page = await self._launch_stealth_browser(proxy_cfg)

            try:
                logger.info("[ProxyCheck] navigating to facebook.com/login...")
                await page.goto('https://www.facebook.com/login', wait_until='domcontentloaded', timeout=40000)
                logger.info(f"[ProxyCheck] page loaded, url={page.url}")

                await self._simulate_human_behavior(page)
                await page.wait_for_timeout(_rand.randint(1000, 2000))

                for consent_sel in ['button[data-cookiebanner="accept_button"]',
                                    'button[data-testid="cookie-policy-dialog-accept-button"]',
                                    '[aria-label="Allow all cookies"]',
                                    'button:has-text("Accept All")',
                                    'button:has-text("Allow")']:
                    try:
                        btn = page.locator(consent_sel).first
                        if await btn.is_visible(timeout=1000):
                            await btn.click()
                            await page.wait_for_timeout(_rand.randint(1000, 2000))
                            break
                    except Exception:
                        pass

                email_field = page.locator('input[name="email"]')
                if not await email_field.is_visible(timeout=5000):
                    login_url = page.url
                    logger.error(f"[ProxyCheck] login form NOT found, url={login_url}")
                    screenshot = await page.screenshot(type='png')
                    return 'bad', screenshot, login_url

                logger.info(f"[ProxyCheck] typing credentials like a human...")
                await page.click('input[name="email"]')
                await page.wait_for_timeout(_rand.randint(300, 600))
                await page.type('input[name="email"]', FB_PROXY_TEST_PHONE, delay=_rand.randint(50, 150))
                await page.wait_for_timeout(_rand.randint(500, 1000))

                await page.click('input[name="pass"]')
                await page.wait_for_timeout(_rand.randint(200, 500))
                await page.type('input[name="pass"]', FB_PROXY_TEST_PASSWORD, delay=_rand.randint(50, 150))
                await page.wait_for_timeout(_rand.randint(500, 1500))

                await page.locator('input[name="pass"]').press('Enter')
                await page.wait_for_timeout(2000)

                if '/login' in page.url:
                    logger.info("[ProxyCheck] Enter didn't submit, clicking login button...")
                    for btn_sel in ['button[name="login"]', 'button[type="submit"]',
                                    'button:has-text("Log in")', 'button:has-text("Log In")']:
                        try:
                            btn = page.locator(btn_sel).first
                            if await btn.is_visible(timeout=2000):
                                await btn.click()
                                logger.info(f"[ProxyCheck] clicked {btn_sel}")
                                break
                        except Exception:
                            pass
                    await page.wait_for_timeout(2000)

                logger.info(f"[ProxyCheck] after submit, url={page.url}")
                await page.wait_for_timeout(8000)

                # 2026-05-15: explicit load-state wait before screenshot.
                # User reported white/empty screenshots on FB login —
                # the page navigates post-Enter and the previous 8s
                # blind sleep sometimes caught the in-between blank
                # frame. Now wait for load (with a 10s cap so we never
                # hang if FB keeps streaming) THEN screenshot.
                try:
                    await page.wait_for_load_state('load', timeout=10000)
                except Exception:
                    pass

                content = (await page.content()).lower()
                url = page.url
                title = await page.title()

                logger.info(f"[ProxyCheck] RESULT url={url}")
                logger.info(f"[ProxyCheck] RESULT title={title}")

                screenshot = await page.screenshot(type='png')

                import re as _re
                visible_text = _re.sub(r'<script[^>]*>.*?</script>', '', content, flags=_re.DOTALL)
                visible_text = _re.sub(r'<style[^>]*>.*?</style>', '', visible_text, flags=_re.DOTALL)
                visible_text = _re.sub(r'<[^>]+>', ' ', visible_text).lower()

                url_lower = url.lower()

                captcha_kws = ['recaptcha', "i'm not a robot", 'i am not a robot',
                               'captcha', 'security check', 'choose a picture',
                               'select all images', 'verify you are human',
                               'recaptcha enterprise']
                matched_captcha = [k for k in captcha_kws if k in visible_text]
                if matched_captcha:
                    logger.info(f"[ProxyCheck] -> BAD (captcha on page: {matched_captcha})")
                    return 'bad', screenshot, url

                if 'flow=pre_authentication' in url_lower:
                    logger.info(f"[ProxyCheck] -> BAD (URL flow=pre_authentication = captcha gate)")
                    return 'bad', screenshot, url
                if 'flow=two_factor_login' in url_lower:
                    logger.info(f"[ProxyCheck] -> GOOD (URL flow=two_factor_login = real 2FA)")
                    return 'good', screenshot, url

                good_kws = ['authentication app', '6-digit code', 'enter the code',
                            'authentication code', 'login code', 'authenticator',
                            'confirmation code', 'check your phone', 'enter code',
                            'verify your identity', 'security code', 'we sent a code',
                            'code generator', 'approve from another device',
                            'try another way']
                bad_kws  = ['unusual activity', "we'll walk you through",
                            'photo id', 'we need to verify',
                            'account temporarily', 'try again later', 'suspended',
                            'wrong password', 'incorrect password', 'password you entered']

                matched_good = [k for k in good_kws if k in visible_text]
                matched_bad = [k for k in bad_kws if k in visible_text]
                logger.info(f"[ProxyCheck] text matched_good={matched_good} matched_bad={matched_bad}")

                if matched_good:
                    logger.info(f"[ProxyCheck] -> GOOD (2FA in visible text)")
                    return 'good', screenshot, url
                if matched_bad:
                    logger.info(f"[ProxyCheck] -> BAD (block keywords in visible text)")
                    return 'bad', screenshot, url

                if 'two_step_verification' in url_lower and 'two_factor' in url_lower:
                    logger.info(f"[ProxyCheck] -> GOOD (URL has two_step + two_factor)")
                    return 'good', screenshot, url
                if 'facebook.com' in url_lower and '/login' not in url_lower and 'checkpoint' not in url_lower:
                    logger.info(f"[ProxyCheck] -> GOOD (redirected past login)")
                    return 'good', screenshot, url

                logger.info(f"[ProxyCheck] -> BAD (no clear signal)")
                return 'bad', screenshot, url
            finally:
                await self._camoufox_ctx.__aexit__(None, None, None)
        except Exception as e:
            logger.error(f"[ProxyCheck] Facebook test error: {type(e).__name__}: {e}")
            return f'error: {type(e).__name__}: {e}', None, None

    def _next_validated_profile_index(self, name_prefix='Validated Profile'):
        """Scan existing GoLogin profiles for names like '<prefix> N' and
        return the next free integer (max+1, or 1 if none/error). Lets a
        batch run keep numbering instead of colliding with a previous run's
        'Validated Profile 1..N'.

        Returns (next_index:int, total_profiles_seen:int, err:str|None).
        """
        if not GOLOGIN_API_KEY:
            return 1, 0, "GOLOGIN_API_KEY not set"
        import re as _re
        headers = {'Authorization': f'Bearer {GOLOGIN_API_KEY}'}
        try:
            resp = requests.get('https://api.gologin.com/browser/v2',
                                headers=headers, params={'limit': 250},
                                timeout=30)
            if resp.status_code != 200:
                return 1, 0, f"API {resp.status_code}: {resp.text[:120]}"
            data = resp.json()
            profiles = (data.get('profiles') or data.get('browser')
                        or (data if isinstance(data, list) else []))
            pat = _re.compile(rf'^{_re.escape(name_prefix)}\s+(\d+)$',
                              _re.IGNORECASE)
            mx = 0
            for p in profiles:
                m = pat.match((p.get('name') or '').strip())
                if m:
                    mx = max(mx, int(m.group(1)))
            return mx + 1, len(profiles), None
        except Exception as e:
            return 1, 0, f"{type(e).__name__}: {e}"

    async def run_batch_proxy_check_pipeline(self, send_update, send_photo,
                                              target_profiles=5,
                                              name_prefix='Validated Profile',
                                              max_total_attempts=None):
        """Batch sibling of run_proxy_check_auto_pipeline (2026-05-25).

        Pulls fresh IPRoyal mobile proxies in a loop and, for EVERY proxy
        that fully validates, creates a NEW GoLogin profile with that proxy
        already attached — named '<name_prefix> N' (auto-numbered, continues
        past any existing 'Validated Profile' profiles so re-runs don't
        collide). Keeps going until `target_profiles` profiles exist.

        Two differences vs the single-shot auto pipeline:
          1. BROWSER TEST ORDER is reordered to fail-fast by ASCENDING
             pass-rate (user's call): ① Google 'hello' (~10% pass — the
             killer) → ② Facebook login (~50%) → ③ reCAPTCHA v3 (~95%,
             almost always passes so it's pointless to run first). Rejecting
             on the least-likely gate first burns the fewest browser launches.
          2. It does NOT return on first success — it banks the profile and
             keeps looping until N are made.

        Cheap pre-gates (mobile-carrier ASN + IPQS + AbuseIPDB + ip-api +
        DNSBL + latency + multi-dest) are KEPT and run first — they reject
        junk proxies WITHOUT a browser launch, which is the big time-saver
        across a long batch. All blocking probes run in threads so the bot
        stays responsive (can take other commands) through the long run.

        Screenshots are buffered per attempt and only sent for proxies that
        FULLY pass (≈3 imgs × N) — a failure just gets a one-line reason, so
        a multi-hour batch doesn't spam hundreds of screenshots.

        Returns (created:list[dict], attempts:int, success:bool, message:str)
        where each dict = {name, id, proxy_socks, exit_ip, score, attempt}.
        """
        import html as _h, asyncio as _asyncio
        def _e(s): return _h.escape(str(s))
        async def _upd(text):
            await send_update(text)
            await _asyncio.sleep(0.4)

        target_profiles = max(1, int(target_profiles))
        if max_total_attempts is None:
            # Generous budget so a bad IPRoyal pool can't loop forever in
            # silence: ~80 proxy pulls per desired profile.
            max_total_attempts = target_profiles * 80

        # Where to start numbering — continue past existing profiles.
        start_idx, n_existing, idx_err = await _asyncio.to_thread(
            self._next_validated_profile_index, name_prefix)
        idx = start_idx

        seen_ips, seen_strs = self._proxy_history_seen_sets()
        await _upd(
            f"🧪 <b>Batch proxy validate + GoLogin creator</b>\n"
            f"🎯 Target: <b>{target_profiles}</b> validated profiles\n"
            f"🏷 Naming: <code>{_e(name_prefix)} {start_idx}</code> → "
            f"<code>{_e(name_prefix)} {start_idx + target_profiles - 1}</code>"
            + (f" (continuing — {n_existing} GoLogin profiles already exist)"
               if not idx_err and n_existing else "")
            + (f"\n⚠️ Couldn't read existing profiles ({_e(idx_err)}) — "
               f"starting at 1" if idx_err else "")
            + f"\n📋 History: <b>{len(seen_ips)}</b> exit IPs, "
            f"<b>{len(seen_strs)}</b> configs tried\n"
            f"🪜 Per proxy: pre-gates → ① Google → ② FB → ③ reCAPTCHA\n"
            f"🔁 Up to <b>{max_total_attempts}</b> proxy pulls. Send /cancel to stop."
        )

        created = []
        attempt = 0
        last_warn = 0
        while len(created) < target_profiles and attempt < max_total_attempts:
            attempt += 1
            remaining = target_profiles - len(created)
            # Periodic heartbeat so a long dry spell isn't silent.
            if attempt - last_warn >= 25:
                last_warn = attempt
                await _upd(
                    f"⏳ <b>{attempt}</b> proxies tried · "
                    f"<b>{len(created)}/{target_profiles}</b> profiles made · "
                    f"still hunting {remaining} more…")

            await _upd(f"🔄 <b>#{attempt}</b> "
                       f"({len(created)}/{target_profiles} made) — new proxy…")
            proxy_raw, msg = self.get_iproyal_proxy_nyc()
            if not proxy_raw:
                await _upd(f"❌ IPRoyal error: <code>{_e(msg)}</code>")
                continue
            parts = proxy_raw.split(':')
            if len(parts) < 4:
                await _upd("❌ Malformed proxy, skip")
                continue
            host, port = parts[0], parts[1]
            user, password = parts[2], ':'.join(parts[3:])
            proxy_socks_url = f"socks5://{user}:{password}@{host}:{port}"
            if proxy_socks_url in seen_strs:
                continue  # config collision (rare) — quietly skip

            # ── Step 1: Exit IP + ISP probe (ipinfo.io) ──
            try:
                exit_ip, isp_org, ip_err = await _asyncio.wait_for(
                    _asyncio.to_thread(self.probe_proxy_exit_ip, proxy_raw),
                    timeout=20.0)
            except _asyncio.TimeoutError:
                exit_ip, isp_org, ip_err = None, None, 'timeout'
            if not exit_ip:
                await _upd(f"🚫 #{attempt} <b>exit-IP probe</b> failed "
                           f"(<code>{_e(str(ip_err)[:40])}</code>) — skip")
                self._proxy_history_record(proxy_socks_url, None,
                    fb_result=f'batch_probe_err:{str(ip_err)[:30]}',
                    source='batch')
                seen_strs.add(proxy_socks_url)
                continue
            if exit_ip in seen_ips:
                await _upd(f"⏭ #{attempt} dedupe — IP <code>{exit_ip}</code> "
                           f"already tried, skipping")
                self._proxy_history_record(proxy_socks_url, exit_ip,
                    fb_result='batch_dedup_skip', source='batch')
                seen_strs.add(proxy_socks_url); seen_ips.add(exit_ip)
                continue
            await _upd(f"✅ #{attempt} <b>① exit-IP</b> (ipinfo.io) → "
                       f"<code>{exit_ip}</code> · "
                       f"<code>{_e((isp_org or '?')[:50])}</code>")

            # ── Step 2: Mobile-carrier ASN gate ──
            if not self._is_mobile_carrier(isp_org):
                await _upd(f"🚫 #{attempt} <b>② mobile-ASN</b> FAIL — "
                           f"not a mobile carrier "
                           f"(<code>{_e((isp_org or '?')[:40])}</code>)")
                self._proxy_history_record(proxy_socks_url, exit_ip,
                    fb_result=f'batch_not_mobile:{(isp_org or "")[:30]}',
                    source='batch')
                seen_strs.add(proxy_socks_url); seen_ips.add(exit_ip)
                continue
            await _upd(f"✅ #{attempt} <b>② mobile-ASN</b> → "
                       f"mobile carrier confirmed")

            # ── Step 3: IPQS reputation (ipqualityscore.com) ──
            if PROXY_SKIP_IPQS:
                await _upd(f"⏭ #{attempt} <b>③ IPQS</b> SKIPPED "
                           f"(<code>PROXY_SKIP_IPQS=1</code>)")
            else:
                ipqs = await _asyncio.to_thread(self.lookup_ipqs_reputation, exit_ip)
                ipqs_score = ipqs.get('fraud_score')
                ipqs_fail = (ipqs.get('available') and
                             ((ipqs_score is not None and ipqs_score > IPQS_FRAUD_SCORE_MAX)
                              or ipqs.get('recent_abuse')))
                if ipqs_fail:
                    tag = f"fraud_score={ipqs_score}"
                    if ipqs.get('recent_abuse'): tag += ", recent_abuse=1"
                    await _upd(f"🚫 #{attempt} <b>③ IPQS</b> FAIL "
                               f"(<code>{_e(tag)}</code>)")
                    self._proxy_history_record(proxy_socks_url, exit_ip,
                        fb_result=f'batch_ipqs:{tag[:40]}', source='batch')
                    seen_strs.add(proxy_socks_url); seen_ips.add(exit_ip)
                    continue
                await _upd(f"✅ #{attempt} <b>③ IPQS</b> (ipqualityscore.com) → "
                           f"fraud_score=<code>{ipqs_score}</code>")

            # ── Step 4: AbuseIPDB reputation ──
            if PROXY_SKIP_ABUSEIPDB:
                await _upd(f"⏭ #{attempt} <b>④ AbuseIPDB</b> SKIPPED "
                           f"(<code>PROXY_SKIP_ABUSEIPDB=1</code>)")
            else:
                abdb = await _asyncio.to_thread(self.lookup_abuseipdb_reputation, exit_ip)
                abdb_score = abdb.get('confidence_score')
                abdb_fail = (abdb.get('available') and abdb_score is not None
                             and abdb_score > ABUSEIPDB_CONFIDENCE_MAX)
                if abdb_fail:
                    await _upd(f"🚫 #{attempt} <b>④ AbuseIPDB</b> FAIL "
                               f"(<code>confidence={abdb_score}</code>)")
                    self._proxy_history_record(proxy_socks_url, exit_ip,
                        fb_result=f'batch_abuse:{abdb_score}', source='batch')
                    seen_strs.add(proxy_socks_url); seen_ips.add(exit_ip)
                    continue
                await _upd(f"✅ #{attempt} <b>④ AbuseIPDB</b> → "
                           f"confidence=<code>{abdb_score if abdb_score is not None else '?'}</code>")

            # ── Step 5: ip-api profile (ip-api.com) ──
            if PROXY_SKIP_IPAPI:
                await _upd(f"⏭ #{attempt} <b>⑤ ip-api</b> SKIPPED "
                           f"(<code>PROXY_SKIP_IPAPI=1</code>)")
            else:
                ipa = await _asyncio.to_thread(self.lookup_ipapi_profile,
                                               exit_ip, proxy_raw)
                ipa_fail = ipa.get('available') and (ipa.get('hosting')
                                                     or ipa.get('proxy'))
                if ipa_fail:
                    tag = 'hosting' if ipa.get('hosting') else 'proxy'
                    await _upd(f"🚫 #{attempt} <b>⑤ ip-api</b> FAIL — "
                               f"<code>{tag}</code> flag set")
                    self._proxy_history_record(proxy_socks_url, exit_ip,
                        fb_result=f'batch_ipapi:{tag}', source='batch')
                    seen_strs.add(proxy_socks_url); seen_ips.add(exit_ip)
                    continue
                await _upd(f"✅ #{attempt} <b>⑤ ip-api</b> (ip-api.com) → clean")

            # ── Step 6: DNSBL (Spamhaus + friends) ──
            if PROXY_SKIP_DNSBL:
                await _upd(f"⏭ #{attempt} <b>⑥ DNSBL</b> SKIPPED "
                           f"(<code>PROXY_SKIP_DNSBL=1</code>)")
            else:
                dnsbl = await _asyncio.to_thread(self.check_dnsbl, exit_ip)
                dnsbl_fail = dnsbl.get('available') and bool(dnsbl.get('listed_on'))
                if dnsbl_fail:
                    listed = ','.join(dnsbl['listed_on'])[:60]
                    await _upd(f"🚫 #{attempt} <b>⑥ DNSBL</b> FAIL — "
                               f"listed on <code>{_e(listed)}</code>")
                    self._proxy_history_record(proxy_socks_url, exit_ip,
                        fb_result=f'batch_dnsbl:{listed[:40]}', source='batch')
                    seen_strs.add(proxy_socks_url); seen_ips.add(exit_ip)
                    continue
                await _upd(f"✅ #{attempt} <b>⑥ DNSBL</b> → not listed")

            # ── Step 7: Latency probe ──
            lat = await _asyncio.to_thread(self.probe_proxy_latency, proxy_raw)
            if not lat.get('available'):
                await _upd(f"🚫 #{attempt} <b>⑦ latency</b> FAIL — probe error")
                self._proxy_history_record(proxy_socks_url, exit_ip,
                    fb_result='batch_latency_err', source='batch')
                seen_strs.add(proxy_socks_url); seen_ips.add(exit_ip)
                continue
            if (lat['p95_ms'] > PROXY_LATENCY_P95_MAX_MS
                    or lat['jitter_ms'] > PROXY_LATENCY_JITTER_MAX_MS):
                await _upd(f"🚫 #{attempt} <b>⑦ latency</b> FAIL — "
                           f"p95=<code>{lat['p95_ms']}ms</code> "
                           f"jitter=<code>{lat['jitter_ms']}ms</code> "
                           f"(max p95={PROXY_LATENCY_P95_MAX_MS})")
                self._proxy_history_record(proxy_socks_url, exit_ip,
                    fb_result=f"batch_latency_high:p95={lat['p95_ms']}",
                    source='batch')
                seen_strs.add(proxy_socks_url); seen_ips.add(exit_ip)
                continue
            await _upd(f"✅ #{attempt} <b>⑦ latency</b> → "
                       f"p95=<code>{lat['p95_ms']}ms</code> "
                       f"jitter=<code>{lat['jitter_ms']}ms</code>")

            # ── Step 8: Multi-destination reachability ──
            multi = await _asyncio.to_thread(
                self.probe_proxy_multi_destination, proxy_raw)
            if not multi.get('all_ok'):
                await _upd(f"🚫 #{attempt} <b>⑧ multi-dest</b> FAIL — "
                           f"selective throttling detected")
                self._proxy_history_record(proxy_socks_url, exit_ip,
                    fb_result='batch_multi_dest_fail', source='batch')
                seen_strs.add(proxy_socks_url); seen_ips.add(exit_ip)
                continue
            await _upd(f"✅ #{attempt} <b>⑧ multi-dest</b> → all reachable")

            await _upd(f"🎯 #{attempt} <b>all 8 pre-gates passed</b> — "
                       f"entering browser gauntlet…")

            # ════════ BROWSER GAUNTLET — ascending pass-rate ════════
            # ⑨ Google 'hello' — the ~10% gate, run FIRST to fail fast.
            # E2E run 2026-05-28 showed Camoufox crashes ~90% of Google
            # launches (not the proxy's fault). FB_PROXY_SKIP_GOOGLE_GATE=1
            # bypasses this so we don't waste ~40s per crashed launch.
            if FB_PROXY_SKIP_GOOGLE_GATE:
                await _upd(f"⏭ #{attempt} <b>⑨ Google 'hello'</b> SKIPPED "
                           f"(<code>FB_PROXY_SKIP_GOOGLE_GATE=1</code>)")
                g_result, g_shot = 'skipped', None
            else:
                await _upd(f"⏳ #{attempt} <b>⑨ Google 'hello'</b> (~30-60s)…")
                try:
                    g_result, g_shot = await _asyncio.wait_for(
                        self.check_google_hello(host, port, user, password, 'socks5'),
                        timeout=60.0)
                except _asyncio.TimeoutError:
                    g_result, g_shot = 'timeout', None
                    try:
                        if getattr(self, '_camoufox_ctx', None):
                            await self._camoufox_ctx.__aexit__(None, None, None)
                            self._camoufox_ctx = None
                    except Exception:
                        pass
            if g_result not in ('good', 'skipped'):
                tag = g_result if isinstance(g_result, str) else 'error'
                await _upd(f"🚫 #{attempt} <b>⑨ Google</b> FAIL "
                           f"(<code>{_e(tag)}</code>) — skip")
                self._proxy_history_record(proxy_socks_url, exit_ip,
                    fb_result=f'batch_google:{tag}', validated=False,
                    source='batch')
                seen_strs.add(proxy_socks_url); seen_ips.add(exit_ip)
                continue
            if g_result == 'good':
                await _upd(f"✅ #{attempt} <b>⑨ Google 'hello'</b> → real SERP rendered")

            # ⑩ Facebook login — the ~50% gate.
            await _upd(f"⏳ #{attempt} <b>⑩ Facebook</b> login probe (~60-80s)…")
            fb_result, fb_shot, fb_url = await self.test_proxy_on_facebook(
                host, port, user, password, 'socks5')
            if fb_result == 'error_no_playwright':
                return (created, attempt, len(created) >= target_profiles,
                        "Playwright/Chromium not available on server")
            if fb_result != 'good':
                tag = fb_result if isinstance(fb_result, str) else 'blocked'
                await _upd(f"🚫 #{attempt} <b>⑩ Facebook</b> FAIL "
                           f"(<code>{_e(tag)}</code>) — skip")
                self._proxy_history_record(proxy_socks_url, exit_ip,
                    fb_result=f'batch_fb:{tag}', validated=False,
                    source='batch')
                seen_strs.add(proxy_socks_url); seen_ips.add(exit_ip)
                continue
            await _upd(f"✅ #{attempt} <b>⑩ Facebook</b> → "
                       f"real 2FA gate (proxy looks legit to Meta)")

            # ⑪ reCAPTCHA v3 score — the ~95% gate, run LAST.
            await _upd(f"⏳ #{attempt} <b>⑪ reCAPTCHA v3</b> score (~30-50s)…")
            score, s_shot = await self.check_recaptcha_score(
                host, port, user, password, 'socks5')
            if score == 'error_no_playwright':
                return (created, attempt, len(created) >= target_profiles,
                        "Playwright/Chromium not available on server")
            score_ok = isinstance(score, float) and score >= 0.9
            score_txt = (str(score) if isinstance(score, float)
                         else ('timeout' if score == 'timeout' else 'N/A'))
            if not score_ok:
                await _upd(f"🚫 #{attempt} <b>⑪ reCAPTCHA</b> FAIL "
                           f"(score=<code>{_e(score_txt)}</code>, need ≥0.9) — skip")
                self._proxy_history_record(proxy_socks_url, exit_ip,
                    score=(score if isinstance(score, float) else None),
                    fb_result='batch_score_fail', validated=False,
                    source='batch')
                seen_strs.add(proxy_socks_url); seen_ips.add(exit_ip)
                continue
            await _upd(f"✅ #{attempt} <b>⑪ reCAPTCHA</b> → "
                       f"score=<code>{score_txt}</code> ≥ 0.9")

            # ════════ FULL PASS — bank proof + create the profile ════════
            self._proxy_history_record(proxy_socks_url, exit_ip,
                score=(score if isinstance(score, float) else None),
                fb_result='batch_pass', validated=True, source='batch')
            seen_strs.add(proxy_socks_url); seen_ips.add(exit_ip)

            if send_photo:
                for buf, cap in ((g_shot, f"#{attempt} ✅ Google SERP"),
                                 (fb_shot, f"#{attempt} ✅ FB 2FA"),
                                 (s_shot, f"#{attempt} ✅ reCAPTCHA {score_txt}")):
                    if buf:
                        try:
                            await send_photo(buf, cap)
                        except Exception as e:
                            logger.warning(f"[batch] photo failed: {e}")

            profile_name = f"{name_prefix} {idx}"
            await _upd(f"📘 #{attempt} <b>⑫ GoLogin</b> creating profile "
                       f"<b>{_e(profile_name)}</b> with proxy attached…")
            profile_id, cmsg = await _asyncio.to_thread(
                self.create_gologin_profile, profile_name, proxy_raw)
            if profile_id:
                created.append({
                    'name': profile_name, 'id': profile_id,
                    'proxy_socks': proxy_socks_url, 'exit_ip': exit_ip,
                    'score': score if isinstance(score, float) else None,
                    'attempt': attempt,
                })
                idx += 1
                await _upd(
                    f"✅ <b>{len(created)}/{target_profiles}</b> — "
                    f"<b>{_e(profile_name)}</b> created "
                    f"(<code>{_e(str(profile_id)[:12])}…</code>)\n"
                    f"🌍 IP <code>{exit_ip}</code> · "
                    f"📡 <code>{_e((isp_org or '?')[:32])}</code> · "
                    f"score {score_txt}")
            else:
                # Proxy is valid but GoLogin create failed — don't consume
                # the index; DM the proxy so it isn't wasted, keep looping.
                await _upd(
                    f"⚠️ #{attempt} proxy VALID but GoLogin create failed: "
                    f"<code>{_e(str(cmsg)[:160])}</code>\n"
                    f"<code>{_e(proxy_socks_url)}</code>\n"
                    f"<i>(saved to history — attach it manually if you want)</i>")

        # ── Batch finished ──
        success = len(created) >= target_profiles
        if created:
            lines = "\n".join(
                f"• <code>{_e(c['name'])}</code> — IP {c['exit_ip']} "
                f"(attempt {c['attempt']})" for c in created)
        else:
            lines = "<i>none</i>"
        if success:
            msg = (f"🏁 <b>Batch complete</b> — created all "
                   f"<b>{len(created)}/{target_profiles}</b> profiles "
                   f"in {attempt} proxy pulls.\n\n{lines}")
        elif attempt >= max_total_attempts:
            msg = (f"🟡 <b>Batch hit the {max_total_attempts}-pull budget</b> "
                   f"with only <b>{len(created)}/{target_profiles}</b> made.\n\n"
                   f"{lines}\n\n<i>IPRoyal pool may be degraded right now — "
                   f"re-run to make the rest; numbering will continue.</i>")
        else:
            msg = (f"🟡 <b>Batch stopped</b> — "
                   f"<b>{len(created)}/{target_profiles}</b> made.\n\n{lines}")
        return created, attempt, success, msg



# ──────────────────────────────────────────────────────────────────────
# Module-level singleton + Telegram handlers (the /proxy UI flow)
# ──────────────────────────────────────────────────────────────────────
_PROXY_PIPELINE_SINGLETON = None
_LAST_RUN_PATH = Path("_proxy_last_run.json")


def _save_last_run(target, created, attempts, success, msg):
    """Persist the last batch summary so /proxy_status can read it.
    Ephemeral on Railway (wiped on redeploy) — that's fine, status is
    only useful within the lifetime of a run anyway."""
    try:
        _LAST_RUN_PATH.write_text(json.dumps({
            'ts': int(time.time()),
            'target': target,
            'attempts': attempts,
            'success': success,
            'msg': msg,
            'created': [{'name': c.get('name'), 'id': c.get('id'),
                         'exit_ip': c.get('exit_ip'),
                         'score': c.get('score')} for c in (created or [])],
        }, indent=2))
    except Exception as e:
        logger.warning(f"[proxy save_last_run] {e}")

def _pipeline():
    global _PROXY_PIPELINE_SINGLETON
    if _PROXY_PIPELINE_SINGLETON is None:
        _PROXY_PIPELINE_SINGLETON = ProxyPipeline()
    return _PROXY_PIPELINE_SINGLETON


def _proxy_submenu_kb():
    """Mirror of Carolina's /accounts → 🌐 Proxy Tools submenu so the UI
    is familiar. acc-setup-bot implements only the Batch variant in this
    v1; the other buttons surface a 'use Carolina' alert until they're
    ported, so users don't lose their bearings."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 Batch Validate + GoLogin (×N)",
                              callback_data="proxy:batch")],
        [InlineKeyboardButton("🤖 Auto Proxy Check + Profile  (use Carolina)",
                              callback_data="proxy:not_impl")],
        [InlineKeyboardButton("🤖 Auto Proxy Check (no profile)  (use Carolina)",
                              callback_data="proxy:not_impl")],
        [InlineKeyboardButton("⚡ Quick Proxy Check  (use Carolina)",
                              callback_data="proxy:not_impl")],
        [InlineKeyboardButton("🎯 Strict Proxy Check  (use Carolina)",
                              callback_data="proxy:not_impl")],
        [InlineKeyboardButton("🔄 Rotate Private Proxy  (use Carolina)",
                              callback_data="proxy:not_impl")],
        [InlineKeyboardButton("📊 /proxy_status",
                              callback_data="proxy:status_hint")],
    ])


def _batch_picker_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("3",  callback_data="proxy:n:3"),
         InlineKeyboardButton("5",  callback_data="proxy:n:5"),
         InlineKeyboardButton("7",  callback_data="proxy:n:7")],
        [InlineKeyboardButton("10", callback_data="proxy:n:10"),
         InlineKeyboardButton("15", callback_data="proxy:n:15"),
         InlineKeyboardButton("20", callback_data="proxy:n:20")],
        [InlineKeyboardButton("✏️ Custom",  callback_data="proxy:cust"),
         InlineKeyboardButton("⬅️ Back",  callback_data="proxy:menu")],
    ])


async def proxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/proxy — Proxy Tools submenu (mirror of Carolina's UX)."""
    missing = [k for k, v in [
        ("GOLOGIN_API_KEY", GOLOGIN_API_KEY),
        ("IPROYAL_USERNAME/PASSWORD",
         IPROYAL_USERNAME and IPROYAL_PASSWORD),
        ("IPQS_API_KEY", IPQS_API_KEY),
        ("ABUSEIPDB_API_KEY", ABUSEIPDB_API_KEY),
        ("FB_PROXY_TEST_PHONE", FB_PROXY_TEST_PHONE),
        ("FB_PROXY_TEST_PASSWORD", FB_PROXY_TEST_PASSWORD),
    ] if not v]
    env_warn = ""
    if missing:
        env_warn = (
            "\n\n⚠️ <b>Missing env vars on Railway:</b>\n"
            + "\n".join(f"  • <code>{k}</code>" for k in missing)
        )
    skip = 'on' if FB_PROXY_SKIP_GOOGLE_GATE else 'off'
    await update.message.reply_text(
        "🌐 <b>Proxy Tools</b>\n\n"
        "<b>Batch Validate + GoLogin (×N)</b> — pulls fresh IPRoyal mobile "
        "proxies in a loop; for every proxy that passes pre-gates (IPQS + "
        "AbuseIPDB + ip-api + DNSBL + latency + multi-dest) AND browser gates "
        "(FB login + reCAPTCHA), creates a GoLogin profile with the proxy "
        "pre-attached. Loops until N profiles are made.\n\n"
        f"<i>Google-gate skip: <b>{skip}</b> · "
        f"set FB_PROXY_SKIP_GOOGLE_GATE in Railway to toggle</i>"
        + env_warn,
        parse_mode='HTML', reply_markup=_proxy_submenu_kb())


async def proxy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes proxy:<action> callbacks."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
    parts = query.data.split(':')
    action = parts[1] if len(parts) > 1 else ''
    arg = parts[2] if len(parts) > 2 else ''
    if action == 'menu':
        # Back to the proxy submenu (re-edit the message in place).
        skip = 'on' if FB_PROXY_SKIP_GOOGLE_GATE else 'off'
        try:
            await query.edit_message_text(
                "🌐 <b>Proxy Tools</b>\n\n"
                "<b>Batch Validate + GoLogin (×N)</b> — pulls fresh IPRoyal "
                "mobile proxies in a loop; creates a GoLogin profile for "
                "every one that passes all gates.\n\n"
                f"<i>Google-gate skip: <b>{skip}</b></i>",
                parse_mode='HTML', reply_markup=_proxy_submenu_kb())
        except Exception: pass
        return
    if action == 'batch':
        # Open the size picker.
        try:
            await query.edit_message_text(
                "🧪 <b>Batch Validate + GoLogin Profile Creator</b>\n\n"
                "Pick how many profiles to create — I'll loop until done. "
                "Per profile takes ~1-3 min depending on browser-gate speed.\n\n"
                "<i>Send /cancel any time to stop.</i>",
                parse_mode='HTML', reply_markup=_batch_picker_kb())
        except Exception: pass
        return
    if action == 'not_impl':
        await query.answer(
            "Use Carolina for this variant — only the Batch wizard is "
            "ported to acc-setup-bot for now.", show_alert=True)
        return
    if action == 'status_hint':
        await query.answer(
            "Type /proxy_status to see the last batch result.",
            show_alert=True)
        return
    if action == 'cust':
        context.user_data['expecting_proxy_count'] = True
        await query.edit_message_text(
            "✏️ Send the number of profiles to create (e.g. <code>25</code>).",
            parse_mode='HTML')
        return
    if action == 'n':
        try:
            n = max(1, int(arg))
        except Exception:
            n = 5
        await _launch_batch(update.effective_chat.id, context, n)
        try: await query.edit_message_text(
            f"🚀 Launching batch for <b>{n}</b> profiles… progress below.",
            parse_mode='HTML')
        except Exception: pass
        return


async def proxy_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the custom-count text input set by proxy:cust."""
    context.user_data.pop('expecting_proxy_count', None)
    text = (update.message.text or '').strip()
    if not text.isdigit() or int(text) <= 0 or int(text) > 200:
        await update.message.reply_text(
            "❌ Send a positive number 1-200 (e.g. <code>25</code>). "
            "Run /proxy again to retry.", parse_mode='HTML')
        return
    n = int(text)
    await update.message.reply_text(
        f"🚀 Launching batch for <b>{n}</b> profiles…", parse_mode='HTML')
    await _launch_batch(update.effective_chat.id, context, n)


async def _launch_batch(chat_id, context, n):
    """Kick off run_batch_proxy_check_pipeline on the pipeline singleton.
    Buffered send_update / send_photo callbacks DM the user as the run progresses."""
    bot = context.bot

    async def send_update(text):
        try:
            await bot.send_message(chat_id, text, parse_mode='HTML',
                                   disable_web_page_preview=True)
        except Exception as e:
            logger.warning(f"[proxy send_update] {e}")

    async def send_photo(photo_bytes, caption=''):
        try:
            await bot.send_photo(chat_id, photo=BytesIO(photo_bytes),
                                 caption=caption, parse_mode='HTML')
        except Exception as e:
            logger.warning(f"[proxy send_photo] {e}")

    try:
        created, attempts, success, msg = await _pipeline().run_batch_proxy_check_pipeline(
            send_update, send_photo,
            target_profiles=n, name_prefix='Validated Profile')
        _save_last_run(n, created, attempts, success, msg)
        summary = (f"✅ <b>Batch finished</b> — created <b>{len(created)}</b> "
                   f"profile(s) over <b>{attempts}</b> proxy attempts.\n\n")
        if created:
            summary += "Profiles:\n" + "\n".join(
                f"  • <code>{c.get('name','?')}</code> "
                f"(exit: {c.get('exit_ip','?')}, score: {c.get('score','?')})"
                for c in created)
        if not success and msg:
            summary += f"\n\n⚠️ {msg}"
        summary += "\n\n<i>Use /proxy_status to see this again.</i>"
        await send_update(summary)
    except Exception as e:
        logger.exception("[proxy batch] crashed")
        await send_update(f"❌ Batch crashed: <code>{type(e).__name__}: {e}</code>")


async def proxy_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/proxy_status — show the last completed /proxy batch result.
    Mirror of /fb_cleanup_status on the poster fleet. Ephemeral state
    (wiped on Railway redeploy)."""
    if not _LAST_RUN_PATH.exists():
        await update.message.reply_text(
            "🟢 No /proxy runs yet — start one with /proxy.\n\n"
            "<i>Tip: set <code>FB_PROXY_SKIP_GOOGLE_GATE=1</code> in Railway "
            "to bypass the flaky Google gate (Camoufox crashes ~9/10 launches).</i>",
            parse_mode='HTML')
        return
    try:
        d = json.loads(_LAST_RUN_PATH.read_text())
    except Exception as e:
        await update.message.reply_text(f"❌ Couldn't read last-run file: {e}")
        return
    age_s = int(time.time()) - int(d.get('ts', 0))
    if age_s < 120:
        age = f"{age_s}s ago"
    elif age_s < 3600:
        age = f"{age_s // 60}m ago"
    elif age_s < 86400:
        age = f"{age_s // 3600}h {(age_s % 3600) // 60}m ago"
    else:
        age = f"{age_s // 86400}d ago"
    lines = [
        f"📊 <b>Last /proxy run</b> — {age}",
        f"  target:   {d.get('target', '?')} profile(s)",
        f"  attempts: {d.get('attempts', '?')}",
        f"  created:  {len(d.get('created', []))}",
        f"  result:   {'✅ success' if d.get('success') else '⚠️ partial'}",
    ]
    skip_flag = '✓ on' if FB_PROXY_SKIP_GOOGLE_GATE else 'off'
    lines.append(f"  google-gate-skip: {skip_flag}")
    for c in (d.get('created') or [])[:20]:
        lines.append(f"    • <code>{(c.get('name') or '?')}</code> "
                     f"(exit {c.get('exit_ip', '?')}, "
                     f"score {c.get('score', '?')})")
    if d.get('msg'):
        import re
        plain = re.sub(r'<[^>]+>', '', d['msg'])
        if plain:
            lines.append(f"\n<i>{plain[:300]}</i>")
    await update.message.reply_text('\n'.join(lines), parse_mode='HTML')
