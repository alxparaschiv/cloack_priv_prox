"""/geelark_profile_open — Batch-open GeeLark cloud phones that mirror existing
GoLogin profiles (same name, same proxy) and install Instagram on each.

Conversation flow:
  /geelark_profile_open                  → prompt for first GoLogin profile name
  user replies "Caroline Goni 5"         → validate exists in GoLogin → add to batch → ask "another?"
  user replies "Caroline Goni 6"         → validate → add to batch → ask again
  user replies "no" / "done" / "finish"  → process the batch:
        for each name:
          1) fetch GoLogin profile (id + proxy block)
          2) create a GeeLark cloud phone with the same name + same proxy
          3) install com.instagram.android on it
        send a per-row status summary back

Why this exists: the FB account creation pipeline already lives behind a
matching GoLogin browser profile (per-account proxy, fingerprint). When we
want a corresponding IG phone for posting/managing, we need a GeeLark cloud
phone that uses the same proxy so IG sees one consistent network identity
across web (FB) + mobile (IG). Doing this by hand for N accounts is tedious.

Mirrors the /rambler + /sms conversation pattern. Reads from GoLogin via
the standard /browser/v2 + /browser/{id} REST. Talks to GeeLark via the
HMAC-signed /open/v1 API the way reel_bot.py does — same headers helper,
same /phone/addNew + /app/installable/list + /app/install endpoints.
"""
import os
import re
import uuid
import time
import hashlib
import logging

import requests
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

GOLOGIN_API_KEY = os.environ.get('GOLOGIN_API_KEY', '')
GEELARK_API_KEY = os.environ.get('GEELARK_API_KEY', '')
GEELARK_APP_ID  = os.environ.get('GEELARK_APP_ID', '')

GEELARK_OPENAPI_BASE = 'https://openapi.geelark.com/open/v1'

# Sentinels that end batch collection
DONE_WORDS = {'no', 'done', 'finish', 'finished', 'stop', 'end', 'cancel', 'that\'s it', "that's it"}


# ─── GoLogin ────────────────────────────────────────────────────────────────

def _gologin_find_profile_by_name(name):
    """Return (profile_id, error). Case-insensitive name match against /browser/v2."""
    if not GOLOGIN_API_KEY:
        return None, "GOLOGIN_API_KEY not set"
    try:
        r = requests.get(
            'https://api.gologin.com/browser/v2',
            headers={'Authorization': f'Bearer {GOLOGIN_API_KEY}'},
            params={'limit': 500}, timeout=30,
        )
        if r.status_code != 200:
            return None, f"GoLogin HTTP {r.status_code}"
        profiles = r.json().get('profiles') or []
        target = name.strip().lower()
        for p in profiles:
            if (p.get('name') or '').strip().lower() == target:
                return p.get('id'), None
        return None, f"no GoLogin profile named '{name}' (searched {len(profiles)})"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _gologin_get_proxy(profile_id):
    """Return (proxy_dict, error). proxy_dict is {host, port, username, password, protocol}."""
    if not GOLOGIN_API_KEY:
        return None, "GOLOGIN_API_KEY not set"
    try:
        r = requests.get(
            f'https://api.gologin.com/browser/{profile_id}',
            headers={'Authorization': f'Bearer {GOLOGIN_API_KEY}'},
            timeout=30,
        )
        if r.status_code != 200:
            return None, f"GoLogin profile GET HTTP {r.status_code}"
        prx = r.json().get('proxy') or {}
        host = prx.get('host')
        port = prx.get('port')
        if not host or not port:
            return None, f"profile {profile_id} has no proxy attached"
        # GoLogin's `mode` field is the proxy protocol — usually 'http' or 'socks5'.
        proto = (prx.get('mode') or 'http').lower()
        if proto not in ('http', 'https', 'socks4', 'socks5'):
            proto = 'http'
        return {
            'host': host,
            'port': int(port),
            'username': prx.get('username') or '',
            'password': prx.get('password') or '',
            'protocol': proto,
        }, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# ─── GeeLark ────────────────────────────────────────────────────────────────

def _geelark_headers():
    """HMAC-signed headers for GeeLark openapi. Same pattern as reel_bot.py."""
    if not (GEELARK_API_KEY and GEELARK_APP_ID):
        return None
    trace_id = str(uuid.uuid4())
    ts = str(int(time.time() * 1000))
    nonce = trace_id[:6]
    sign_input = GEELARK_APP_ID + trace_id + ts + nonce + GEELARK_API_KEY
    sign = hashlib.sha256(sign_input.encode()).hexdigest().upper()
    return {
        'appId': GEELARK_APP_ID,
        'traceId': trace_id,
        'ts': ts,
        'nonce': nonce,
        'sign': sign,
        'Content-Type': 'application/json',
    }


def _geelark_post(path, body, timeout=60):
    """Generic signed POST to GeeLark. Returns (data, err)."""
    headers = _geelark_headers()
    if not headers:
        return None, "GEELARK_API_KEY and GEELARK_APP_ID must both be set"
    try:
        resp = requests.post(GEELARK_OPENAPI_BASE + path, json=body, headers=headers, timeout=timeout)
        try:
            data = resp.json()
        except Exception:
            return None, f"HTTP {resp.status_code}: {resp.text[:300]}"
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}: {data}"
        if data.get('code') not in (0, '0'):
            return None, f"GeeLark code={data.get('code')} msg={data.get('msg')}"
        return data.get('data'), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _geelark_create_phone(profile_name, proxy):
    """Create a GeeLark Android cloud phone bound to the given proxy.
    proxy = dict with host, port, username, password, protocol.
    Returns (phone_id, err).
    """
    # GeeLark wants proxyInformation as a URL string: scheme://user:pass@host:port
    scheme = proxy['protocol'] if proxy['protocol'] in ('http', 'https', 'socks4', 'socks5') else 'http'
    if proxy['username'] or proxy['password']:
        proxy_url = f"{scheme}://{proxy['username']}:{proxy['password']}@{proxy['host']}:{proxy['port']}"
    else:
        proxy_url = f"{scheme}://{proxy['host']}:{proxy['port']}"

    # Try a couple of language candidates — GeeLark periodically rejects
    # specific values with code 43025 (see reel_bot.py geelark_create_phone).
    for lang in ('default', 'en-US', 'zh-CN'):
        body = {
            'mobileType': 'Android 13',
            'chargeMode': 0,
            'region': 'us',
            'data': [{
                'profileName': profile_name,
                'proxyInformation': proxy_url,
                'proxyQueryChannel': 2,
                'mobileLanguage': lang,
                'profileTags': ['ig', 'cloak-priv-prox'],
            }],
        }
        data, err = _geelark_post('/phone/addNew', body)
        if not data:
            continue
        details = data.get('details') or data.get('successDetails') or []
        if isinstance(details, list) and details:
            first = details[0]
            phone_id = first.get('id') or first.get('phoneId') or first.get('envId')
            if phone_id:
                return phone_id, None
            code = first.get('code')
            err_msg = (first.get('msg') or '').lower()
            if code == 43025 or 'language' in err_msg:
                continue  # try next language
            return None, f"GeeLark error: code={code} msg={first.get('msg')}"
    return None, "all language candidates rejected"


def _geelark_install_instagram(phone_id, max_attempts=24, sleep_between=10):
    """Look up the Instagram app version on the freshly-booted phone and install it.
    Same logic as reel_bot.geelark_install_app — installable list only populates
    after the phone has fully booted, so we poll for up to 4 min by default.
    Returns (ok, msg).
    """
    package = 'com.instagram.android'
    search_name = 'Instagram'
    target = None
    last = None
    for attempt in range(max_attempts):
        data, err = _geelark_post('/app/installable/list', {
            'envId': phone_id, 'name': search_name,
            'getUploadApp': False, 'page': 1, 'pageSize': 50,
        })
        if data:
            items = data.get('items') or []
            target = next((a for a in items if a.get('packageName') == package), None)
            if target:
                break
            last = f"package {package} not in installable list (attempt {attempt+1}/{max_attempts}, {len(items)} apps)"
        else:
            last = f"installable list error (attempt {attempt+1}/{max_attempts}): {err}"
        time.sleep(sleep_between)
    if not target:
        return False, last or f"installable list empty after {max_attempts} attempts"
    versions = target.get('appVersionInfoList') or []
    if not versions:
        return False, f"no versions for {package}"
    version_id = versions[0].get('id')
    _, err = _geelark_post('/app/install', {
        'envId': phone_id, 'appVersionId': version_id,
    })
    if err:
        return False, err
    return True, "OK"


# ─── Telegram handlers ──────────────────────────────────────────────────────

async def geelark_profile_open_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point. Initialises the batch selection."""
    context.user_data['geelark_batch'] = []
    context.user_data['expecting_geelark_name'] = True
    await update.message.reply_text(
        "📱 *GeeLark profile opener*\n\n"
        "Send the *GoLogin profile name* for the first GeeLark phone "
        "(e.g. `Caroline Goni 5`).\n\n"
        "I'll validate the name against GoLogin first. After each one, "
        "you can add more — or type `done` to start the batch.",
        parse_mode='Markdown')


async def geelark_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the user's reply when expecting a GoLogin profile name."""
    if not context.user_data.get('expecting_geelark_name'):
        return
    text = (update.message.text or '').strip()
    text_lower = text.lower()

    # ── Done sentinel — process the batch
    if text_lower in DONE_WORDS:
        batch = context.user_data.get('geelark_batch') or []
        context.user_data.pop('expecting_geelark_name', None)
        if not batch:
            await update.message.reply_text("⚠️ batch is empty — nothing to do. Run /geelark_profile_open again.")
            return
        await _run_geelark_batch(update, context, batch)
        context.user_data.pop('geelark_batch', None)
        return

    # ── Name — validate it exists in GoLogin
    await update.message.reply_text(f"🔍 Looking up `{text}` in GoLogin…", parse_mode='Markdown')
    profile_id, err = _gologin_find_profile_by_name(text)
    if err or not profile_id:
        await update.message.reply_text(
            f"❌ {err or 'profile not found'}\n\nTry another name, or type `done` to stop.",
            parse_mode='Markdown')
        return

    batch = context.user_data.setdefault('geelark_batch', [])
    # Reject duplicates within the same batch
    if any(b['name'].lower() == text.lower() for b in batch):
        await update.message.reply_text(
            f"⚠️ `{text}` already in this batch — skipping. "
            f"Send another name or `done`.",
            parse_mode='Markdown')
        return
    batch.append({'name': text, 'gologin_id': profile_id})
    queue = '\n'.join(f"  {i+1}. `{b['name']}`" for i, b in enumerate(batch))
    await update.message.reply_text(
        f"✅ added `{text}` ({profile_id}). Batch so far ({len(batch)}):\n{queue}\n\n"
        f"Send another GoLogin name to add more, or type `done` to start.",
        parse_mode='Markdown')


async def _run_geelark_batch(update, context, batch):
    """Process the batch: create a GeeLark phone per entry, install IG on each."""
    await update.message.reply_text(
        f"🚀 starting batch of {len(batch)} GeeLark phone(s)…",
        parse_mode='Markdown')

    results = []
    for i, entry in enumerate(batch):
        name = entry['name']
        gid = entry['gologin_id']
        await update.message.reply_text(
            f"⏳ [{i+1}/{len(batch)}] `{name}`: fetching GoLogin proxy…",
            parse_mode='Markdown')
        proxy, err = _gologin_get_proxy(gid)
        if err or not proxy:
            results.append({'name': name, 'ok': False, 'stage': 'gologin_proxy', 'err': err})
            await update.message.reply_text(f"❌ `{name}`: {err}", parse_mode='Markdown')
            continue
        await update.message.reply_text(
            f"   proxy: `{proxy['protocol']}://{proxy['host']}:{proxy['port']}` — creating GeeLark phone…",
            parse_mode='Markdown')
        phone_id, err = _geelark_create_phone(name, proxy)
        if err or not phone_id:
            results.append({'name': name, 'ok': False, 'stage': 'geelark_create', 'err': err})
            await update.message.reply_text(f"❌ `{name}`: phone create failed — {err}", parse_mode='Markdown')
            continue
        await update.message.reply_text(
            f"   ✅ phone created ({phone_id}). Installing Instagram (this can take ~2-4 min)…",
            parse_mode='Markdown')
        # Start the phone before install (installable list only populates after boot)
        _geelark_post('/phone/start', {'ids': [phone_id]})
        ok, msg = _geelark_install_instagram(phone_id)
        results.append({
            'name': name, 'phone_id': phone_id,
            'ok': ok, 'stage': 'install_ig' if not ok else 'done',
            'err': None if ok else msg,
        })
        if ok:
            await update.message.reply_text(f"   ✅ `{name}`: Instagram installed.", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"   ⚠️ `{name}`: phone exists but IG install failed — {msg}", parse_mode='Markdown')

    # ── Final green-light summary ──────────────────────────────────────────
    # User asked explicitly for a clear "all done — go ahead and log in" signal
    # at the end of the batch (not just a quiet per-row trickle).
    okay = sum(1 for r in results if r['ok'])
    fail = len(results) - okay
    ready = [r for r in results if r['ok']]
    failed = [r for r in results if not r['ok']]

    if okay == len(results):
        header = (f"🟢 *ALL DONE — {okay}/{len(results)} GeeLark phones ready.*\n"
                  f"Every phone has Instagram installed. You can now open them "
                  f"in the GeeLark app and sign into IG yourself.")
    elif okay > 0:
        header = (f"🟡 *Batch finished — {okay}/{len(results)} ready, {fail} failed.*\n"
                  f"The successful ones are ready for you to sign into IG; "
                  f"the failures are listed below.")
    else:
        header = (f"🔴 *Batch finished — 0/{len(results)} ready.*\n"
                  f"All entries failed — see the errors below.")

    lines = [header, ""]
    if ready:
        lines.append("*✅ Ready to use:*")
        for r in ready:
            lines.append(f"  • `{r['name']}` → phone id `{r['phone_id']}`")
    if failed:
        if ready: lines.append("")
        lines.append("*❌ Failed:*")
        for r in failed:
            lines.append(f"  • `{r['name']}` — stage `{r['stage']}` — {r['err']}")

    await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')
