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
    # Resolve GoLogin profile by name. The user's previously-working flow was
    # a single-shot GET /browser/v2?limit=500 — keep that as the dependable
    # fallback. Layer on a server-side ?search= filter first so we don't
    # have to walk a huge roster when the name is unique-ish.
    if not GOLOGIN_API_KEY:
        return None, "GOLOGIN_API_KEY not set"
    target = name.strip().lower()
    headers = {'Authorization': f'Bearer {GOLOGIN_API_KEY}'}
    debug_counts = []

    # ── Strategy 1: server-side ?search=
    # GoLogin's /browser/v2 supports ?search=substring against the profile name.
    # If supported, it returns a tight result set without us walking the roster.
    for kw in ('search', 'q', 'name'):
        try:
            r = requests.get(
                'https://api.gologin.com/browser/v2',
                headers=headers,
                params={'limit': 100, kw: name.strip()},
                timeout=30,
            )
            if r.status_code == 200:
                body = r.json()
                profiles = body.get('profiles') or body.get('browser') or []
                debug_counts.append(f"{kw}={len(profiles)}")
                for p in profiles:
                    if (p.get('name') or '').strip().lower() == target:
                        return p.get('id'), None
        except Exception as e:
            logger.warning(f"[gologin_find] {kw}-mode err: {e}")

    # ── Strategy 2: dependable single-shot limit=500
    # This was the originally-working code before my paginated-walk attempt
    # regressed it. Keep it last so server-side search wins when supported.
    try:
        r = requests.get(
            'https://api.gologin.com/browser/v2',
            headers=headers,
            params={'limit': 500}, timeout=30,
        )
        if r.status_code != 200:
            return None, f"GoLogin HTTP {r.status_code} ({'/'.join(debug_counts) or 'no search hits'})"
        body = r.json()
        profiles = body.get('profiles') or body.get('browser') or []
        debug_counts.append(f"bulk={len(profiles)}")
        sample = [(p.get('name') or '').strip() for p in profiles[:3]]
        for p in profiles:
            if (p.get('name') or '').strip().lower() == target:
                return p.get('id'), None
        return None, (f"no GoLogin profile named '{name}'. Counts: {', '.join(debug_counts)}. "
                      f"Sample names returned: {sample}")
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
    # GeeLark caps profileName at 64 characters. Anything longer is rejected
    # with "app name too long maximum 64 characters". Per user spec
    # 2026-06-08: just take the first 64 chars of the GoLogin name and use
    # those — no marker, no transformation, just a hard slice.
    GEELARK_NAME_CAP = 64
    if len(profile_name) > GEELARK_NAME_CAP:
        original_len = len(profile_name)
        profile_name = profile_name[:GEELARK_NAME_CAP]
        logger.warning(
            f"[geelark_create_phone] profile name was {original_len} chars; "
            f"truncated to first {GEELARK_NAME_CAP} chars: {profile_name!r}")

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


def _geelark_boot_wait(phone_id, sleep_s=60):
    """Trust /phone/start's success + sleep for boot. Mirrors reel_bot.py which
    has been running this exact pattern in production for months.

    We previously tried to poll /phone/list status field, but the field lags /
    reports stale data for freshly-started phones (a phone visibly running in
    the UI was still status=0 via API). Polling led to false-negative timeouts
    on phones that had actually booted fine. Simple sleep + retry-on-install
    is more reliable.
    """
    time.sleep(sleep_s)
    return True, f"slept {sleep_s}s for boot", None


def _geelark_install_instagram(phone_id, list_max_attempts=24, list_sleep=10,
                                install_max_attempts=6, install_sleep=10):
    """Look up the Instagram app version on the running phone and install it.

    Two retry loops:
      - installable/list — phone may take a while to populate the catalog
      - /app/install — even on a "running" phone, the first install POST can
        hit 42002 transiently; retry a few times before giving up.

    Returns (ok, msg).
    """
    package = 'com.instagram.android'
    search_name = 'Instagram'

    # ── Step 1: find the package in the installable catalog
    target = None
    last = None
    for attempt in range(list_max_attempts):
        data, err = _geelark_post('/app/installable/list', {
            'envId': phone_id, 'name': search_name,
            'getUploadApp': False, 'page': 1, 'pageSize': 50,
        })
        if data:
            items = data.get('items') or []
            target = next((a for a in items if a.get('packageName') == package), None)
            if target:
                break
            last = f"package {package} not in installable list (attempt {attempt+1}/{list_max_attempts}, {len(items)} apps)"
        else:
            last = f"installable/list err (attempt {attempt+1}/{list_max_attempts}): {err}"
        time.sleep(list_sleep)
    if not target:
        return False, last or f"installable list empty after {list_max_attempts} attempts"
    versions = target.get('appVersionInfoList') or []
    if not versions:
        return False, f"no versions for {package}"
    version_id = versions[0].get('id')

    # ── Step 2: actually install (with retry for the 42002 transient)
    last_err = None
    for attempt in range(install_max_attempts):
        _, err = _geelark_post('/app/install', {
            'envId': phone_id, 'appVersionId': version_id,
        })
        if not err:
            return True, "OK"
        last_err = f"install err (attempt {attempt+1}/{install_max_attempts}): {err}"
        # 42002 = env not running — phone state may flap right after boot. Sleep and retry.
        time.sleep(install_sleep)
    return False, last_err or "install failed after all retries"


# ─── Telegram handlers ──────────────────────────────────────────────────────

async def geelark_profile_open_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point. Shows a mode-select first:
      - 🆕 Create + add images  (current create-batch flow, image step lands in
        a follow-up commit)
      - 🖼 Add images to existing GeeLark profiles  (image-only flow in
        geelark_image_wizard)
    Original create-batch behavior is preserved verbatim — only the entry
    prompt is wrapped by the mode-select.
    """
    import geelark_image_wizard
    await geelark_image_wizard.show_mode_select(update.message)


async def geelark_profile_open_command_legacy_entry(msg, context):
    """The original /geelark_profile_open prompt — kicks off the create-batch
    name-collection flow. Called by the mode-select callback when the user
    picks 'Create + add images'. Kept as a named function so the wizard can
    hand off to it without duplicating the prompt text.
    """
    context.user_data['geelark_batch'] = []
    context.user_data['expecting_geelark_name'] = True
    await msg.reply_text(
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
            f"   ✅ phone created ({phone_id}). Starting phone + waiting ~60s for boot…",
            parse_mode='Markdown')
        # /phone/start kicks off boot. We trust the API response + sleep — the
        # phone-status API field lags and isn't reliable on fresh starts.
        # reel_bot.py uses this exact pattern in production.
        _, start_err = _geelark_post('/phone/start', {'ids': [phone_id]})
        if start_err:
            results.append({'name': name, 'ok': False, 'stage': 'phone_start', 'err': start_err, 'phone_id': phone_id})
            await update.message.reply_text(f"❌ `{name}`: /phone/start failed — {start_err}", parse_mode='Markdown')
            continue
        _geelark_boot_wait(phone_id, sleep_s=60)
        await update.message.reply_text(
            f"   ✅ booted. Installing Instagram (~2-4 min)…",
            parse_mode='Markdown')
        ok, msg = _geelark_install_instagram(phone_id)
        # RULE: stop the phone after install regardless of outcome.
        # User explicitly requested this so the GeeLark UI shows the phone
        # as stopped — they'll start it manually when they're ready to log
        # into IG. Leaving phones running idle burns minutes + adds detection
        # signal. /phone/stop is idempotent and safe even if start never
        # actually transitioned the phone to running.
        _, stop_err = _geelark_post('/phone/stop', {'ids': [phone_id]})
        if stop_err:
            logger.warning(f"[geelark_open] post-install stop failed for {phone_id}: {stop_err}")
        results.append({
            'name': name, 'phone_id': phone_id,
            'ok': ok, 'stage': 'install_ig' if not ok else 'done',
            'err': None if ok else msg,
        })
        if ok:
            await update.message.reply_text(f"   ✅ `{name}`: Instagram installed + phone stopped.", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"   ⚠️ `{name}`: phone exists but IG install failed — {msg} (phone stopped)", parse_mode='Markdown')

    # ── Final green-light summary ──────────────────────────────────────────
    # User asked explicitly for a clear "all done — go ahead and log in" signal
    # at the end of the batch (not just a quiet per-row trickle).
    okay = sum(1 for r in results if r['ok'])
    fail = len(results) - okay
    ready = [r for r in results if r['ok']]
    failed = [r for r in results if not r['ok']]

    if okay == len(results):
        header = (f"🟢 *ALL DONE — {okay}/{len(results)} GeeLark phones ready.*\n"
                  f"Every phone has Instagram installed and has been *stopped* — "
                  f"start each one from the GeeLark app when you're ready to "
                  f"sign into IG.")
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


# ─── /geelark_stop_phone — batch stop phones once IG setup is done ──────────
# User rule: after the manual IG account setup on a GeeLark phone is done,
# the phone must be STOPPED (logs the device session out, frees GeeLark
# billing minutes, reduces the risk of leaving a phone running idle). Same
# batch-conversation pattern as the open command.

async def geelark_stop_phone_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['geelark_stop_batch'] = []
    context.user_data['expecting_geelark_stop_name'] = True
    await update.message.reply_text(
        "🛑 *GeeLark phone stopper*\n\n"
        "Send the *GoLogin profile name* of the GeeLark phone to stop "
        "(e.g. `Caroline Goni 5`). I'll look up the corresponding GeeLark "
        "phone by profile-name match and queue it for stop.\n\n"
        "After each one you can add more — or type `done` to stop them all.",
        parse_mode='Markdown')


def _geelark_find_phone_by_name(profile_name):
    """Search the GeeLark phone list for a phone whose name matches.

    GOTCHA — name field: GeeLark's /phone/list payload populates `serialName`
    (e.g. 'caro motorcycle') and leaves `profileName=null`. Looking up by
    `profileName` first would always miss. We check the FULL chain
    serialName → profileName → name and dedupe via .lower() compare.

    On miss, returns a diagnostic that includes the top 3 substring-similar
    phone names actually found, so the user can see whether the issue is a
    typo / hidden whitespace / pagination cap.
    """
    # Mirror the 64-char truncation applied at create time. If the original
    # GoLogin name was >64 chars, the GeeLark phone's serialName is just the
    # first 64 chars. The user is allowed to type the full GoLogin name and
    # we still need to match the truncated GeeLark name.
    target_full = profile_name.strip()
    target_truncated = target_full[:64]
    target = target_truncated.lower()
    target_first_word = target.split(' ', 1)[0]
    page = 1
    total_seen = 0
    similar_samples = []  # phones whose name shares a word with the target
    while True:
        data, err = _geelark_post('/phone/list', {'page': page, 'pageSize': 100})
        if err:
            return None, err
        items = data.get('items') or data.get('list') or []
        if not items:
            break
        for it in items:
            total_seen += 1
            raw = (it.get('serialName') or it.get('profileName')
                   or it.get('name') or '').strip()
            name_lc = raw.lower()
            if name_lc == target:
                return (it.get('id') or it.get('phoneId') or it.get('envId')), None
            # collect samples that share the first word — useful diagnostic
            if target_first_word and target_first_word in name_lc and len(similar_samples) < 5:
                similar_samples.append(raw)
        total = data.get('total', 0)
        if total and len(items) < 100:
            break
        if total and (page * 100) >= int(total):
            break
        page += 1
    diag = f" (walked {total_seen} phones"
    if similar_samples:
        diag += f"; similar names: {similar_samples[:3]}"
    diag += ")"
    return None, f"no GeeLark phone named '{profile_name}'{diag}"


async def geelark_stop_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('expecting_geelark_stop_name'):
        return
    text = (update.message.text or '').strip()
    text_lower = text.lower()

    if text_lower in DONE_WORDS:
        batch = context.user_data.get('geelark_stop_batch') or []
        context.user_data.pop('expecting_geelark_stop_name', None)
        if not batch:
            await update.message.reply_text("⚠️ no phones queued — nothing to stop.")
            return
        await _run_geelark_stop_batch(update, context, batch)
        context.user_data.pop('geelark_stop_batch', None)
        return

    await update.message.reply_text(f"🔍 Finding GeeLark phone for `{text}`…", parse_mode='Markdown')
    phone_id, err = _geelark_find_phone_by_name(text)
    if err or not phone_id:
        await update.message.reply_text(
            f"❌ {err or 'no match'}\n\nTry another name or type `done` to stop the queued ones.",
            parse_mode='Markdown')
        return
    batch = context.user_data.setdefault('geelark_stop_batch', [])
    if any(b['name'].lower() == text.lower() for b in batch):
        await update.message.reply_text(
            f"⚠️ `{text}` already queued — skipping.", parse_mode='Markdown')
        return
    batch.append({'name': text, 'phone_id': phone_id})
    queue = '\n'.join(f"  {i+1}. `{b['name']}` ({b['phone_id']})" for i, b in enumerate(batch))
    await update.message.reply_text(
        f"✅ queued `{text}` ({phone_id}). Stop queue ({len(batch)}):\n{queue}\n\n"
        f"Send another name to add, or `done` to stop them.",
        parse_mode='Markdown')


async def _run_geelark_stop_batch(update, context, batch):
    await update.message.reply_text(
        f"🛑 stopping {len(batch)} GeeLark phone(s)…", parse_mode='Markdown')
    ok_count = 0
    lines = []
    for i, entry in enumerate(batch):
        name = entry['name']
        pid = entry['phone_id']
        _, err = _geelark_post('/phone/stop', {'ids': [pid]})
        if err:
            lines.append(f"  ❌ `{name}` ({pid}) — {err}")
        else:
            lines.append(f"  ✅ `{name}` ({pid}) stopped")
            ok_count += 1
    if ok_count == len(batch):
        header = f"🟢 *All {ok_count} phones stopped.* You're clear of GeeLark billing for these."
    elif ok_count > 0:
        header = f"🟡 *{ok_count}/{len(batch)} stopped.* See per-row results below."
    else:
        header = f"🔴 *0/{len(batch)} stopped.* All failed."
    await update.message.reply_text(header + "\n" + '\n'.join(lines), parse_mode='Markdown')
