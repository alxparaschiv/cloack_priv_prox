"""
meta_dev.py — Autonomous Meta-for-Developers account setup, Shard 1.

User flow (this shard):
  1. /meta_dev_setup
  2. Bot lists every GoLogin 'Validated Profile N' as a button → user taps one
  3. Bot prompts: paste the FB account blob (same format as /blob)
  4. User pastes blob (one or more messages) → bot parses on hitting cookies
  5. Bot opens the picked GoLogin Cloud Browser via CDP, injects the parsed
     cookies, navigates to facebook.com, verifies the session is alive
  6. Bot DMs: "✅ logged in as <name>" or "❌ cookies stale, re-blob"

Future shards (queued):
  - Shard 2: navigate to developers.facebook.com/apps, walk signup, fill form
  - Shard 3: phone verify via TextVerified
  - Shard 4: email confirmation via Rambler IMAP + capsolver
  - Shard 5: polish (per-step DMs, /meta_dev_status, Drive audit log)
"""
import os
import re
import logging
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import cookies as _cookies_mod
import proxy as _proxy_mod

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# GoLogin profile listing (reads from the proxy.py pipeline's API key)
# ──────────────────────────────────────────────────────────────────────
def _list_validated_profiles():
    """Return [{name, id}] for every 'Validated Profile N' on the account,
    sorted by N ascending. Empty list on API error."""
    import requests
    key = _proxy_mod.GOLOGIN_API_KEY
    if not key:
        logger.warning("[meta_dev] GOLOGIN_API_KEY not set")
        return []
    try:
        r = requests.get(
            'https://api.gologin.com/browser/v2',
            headers={'Authorization': f'Bearer {key}'},
            params={'limit': 200, 'query': 'Validated Profile'},
            timeout=20)
        profs = [
            {'name': p.get('name', '?'), 'id': p.get('id', '?')}
            for p in r.json().get('profiles', [])
            if (p.get('name') or '').startswith('Validated Profile')
        ]
        # Sort by trailing integer
        def _idx(p):
            m = re.search(r'(\d+)', p['name'])
            return int(m.group(1)) if m else 0
        profs.sort(key=_idx)
        return profs
    except Exception as e:
        logger.warning(f"[meta_dev] list profiles failed: {e}")
        return []


def _profile_picker_kb(profiles):
    """Build a 2-column inline keyboard, one button per profile."""
    rows = []
    pair = []
    for p in profiles:
        # Short name in button; full ID in callback_data (under 64B cap)
        pair.append(InlineKeyboardButton(
            p['name'], callback_data=f"mdev:pick:{p['id']}"))
        if len(pair) == 2:
            rows.append(pair); pair = []
    if pair: rows.append(pair)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="mdev:cancel")])
    return InlineKeyboardMarkup(rows)


# ──────────────────────────────────────────────────────────────────────
# Cookie format conversion: Cookie-Editor JSON → Playwright add_cookies()
# ──────────────────────────────────────────────────────────────────────
def _convert_cookies_for_playwright(ce_cookies):
    """Cookie-Editor cookies use keys like 'expirationDate' and 'sameSite' in
    title-case; Playwright wants 'expires' and lowercase 'Strict|Lax|None'.
    Skips entries that lack name/value/domain.

    Cookie-Editor schema (from cookies.py): {name, value, domain, path,
    expirationDate?, httpOnly?, secure?, sameSite?, hostOnly?, session?, ...}
    Playwright schema: {name, value, domain, path, expires?, httpOnly?,
    secure?, sameSite?} where sameSite ∈ {'Strict','Lax','None'}.
    """
    out = []
    SS_MAP = {
        'no_restriction': 'None', 'none': 'None',
        'lax': 'Lax', 'strict': 'Strict',
        'unspecified': 'Lax',
    }
    for c in (ce_cookies or []):
        name = c.get('name'); value = c.get('value')
        domain = c.get('domain') or ''
        if not (name and value is not None and domain):
            continue
        pw = {
            'name': name,
            'value': str(value),
            'domain': domain,
            'path': c.get('path') or '/',
        }
        # expires: prefer expirationDate (float seconds); -1 / 0 = session
        exp = c.get('expirationDate')
        if exp and exp > 0:
            pw['expires'] = float(exp)
        if 'httpOnly' in c:
            pw['httpOnly'] = bool(c['httpOnly'])
        if 'secure' in c:
            pw['secure'] = bool(c['secure'])
        ss = (c.get('sameSite') or '').lower()
        if ss in SS_MAP:
            pw['sameSite'] = SS_MAP[ss]
        out.append(pw)
    return out


# ──────────────────────────────────────────────────────────────────────
# Inject cookies → open Cloud Browser → verify logged in
# ──────────────────────────────────────────────────────────────────────
def _persist_cookies_to_gologin_profile(profile_id, cookies_ce):
    """Persist cookies into the GoLogin profile's STORAGE (not the runtime
    cookie jar of a Cloud Browser session). Cookies set this way are loaded
    on every subsequent open of the profile — whether via Desktop GoLogin or
    Cloud Browser CDP. Verified 2026-05-28 via the official cookies endpoint:
      POST /browser/{id}/cookies  body=<cookies array>  → 204
    """
    import requests
    if not _proxy_mod.GOLOGIN_API_KEY:
        return False, "GOLOGIN_API_KEY not set"
    try:
        r = requests.post(
            f'https://api.gologin.com/browser/{profile_id}/cookies',
            json=cookies_ce,
            headers={'Authorization': f'Bearer {_proxy_mod.GOLOGIN_API_KEY}',
                     'Content-Type': 'application/json'},
            timeout=30)
        if r.status_code in (200, 201, 204):
            return True, f"HTTP {r.status_code}"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def _login_fb_in_gologin_browser(profile_id, profile_name,
                                       parsed_blob, send_update):
    """Persist the parsed FB cookies into the GoLogin profile's storage,
    then open a Cloud Browser CDP session as a sanity check that FB
    recognizes the session.

    Returns dict: {ok:bool, name:str|None, screenshot:bytes|None, err:str|None}.
    Always stops the Cloud Browser session in `finally` so we don't leak
    metered minutes.

    Why this two-step (persist + verify) instead of just runtime add_cookies:
    Playwright's context.add_cookies() only writes to the Cloud Browser's
    runtime cookie jar, which dies when the session ends. So when the user
    later opens the same profile in Desktop GoLogin, no cookies are loaded
    and they see the FB login page. POST /browser/{id}/cookies writes them
    to persistent storage — Desktop AND Cloud Browser load them on every
    fresh open. (Confirmed 2026-05-28: returns 204.)
    """
    pipeline = _proxy_mod._pipeline()
    out = {'ok': False, 'name': None, 'screenshot': None, 'err': None}

    cookies_ce = parsed_blob.get('cookies') or []
    profile_id_from_blob = parsed_blob.get('profile_id') or ''
    if not cookies_ce:
        out['err'] = 'no cookies found in blob (parse returned empty list)'
        return out

    await send_update(
        f"   🍪 parsed <b>{len(cookies_ce)}</b> cookies from blob "
        f"(c_user=<code>{profile_id_from_blob}</code>)")

    # Step A: PERSIST cookies into the GoLogin profile's storage. This is
    # the load-bearing step — without it, the Desktop browser won't see
    # any of the cookies on next open.
    persist_ok, persist_msg = _persist_cookies_to_gologin_profile(
        profile_id, cookies_ce)
    if not persist_ok:
        out['err'] = f'persist cookies failed: {persist_msg}'
        return out
    await send_update(
        f"   💾 persisted <b>{len(cookies_ce)}</b> cookies into "
        f"<b>{profile_name}</b>'s profile storage "
        f"(<code>{persist_msg}</code>) — they'll load on every open")

    # Step B: open the Cloud Browser as a sanity check (does FB recognize
    # the session?). The persistent cookies are read by the Cloud Browser
    # on session start, so we don't need to call add_cookies() at runtime.
    cdp_url = pipeline.orbita_cloud_cdp_url(profile_id)
    if not cdp_url:
        out['err'] = 'GOLOGIN_API_KEY not set on bot'
        return out
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        out['err'] = f'playwright import failed: {e}'
        return out

    try:
        async with async_playwright() as p:
            await send_update(
                f"   🌐 opening Cloud Browser CDP for "
                f"<b>{profile_name}</b> (sanity check — cold start ~5-30s)…")
            browser = await p.chromium.connect_over_cdp(cdp_url, timeout=120000)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            await send_update(f"   🚀 navigating to facebook.com (cookies loaded from "
                              f"persistent storage)…")
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await page.goto('https://www.facebook.com/',
                                wait_until='domcontentloaded', timeout=45000)
                await page.wait_for_timeout(3000)
            except Exception as e:
                out['err'] = f'facebook.com nav failed: {str(e)[:200]}'
                try:
                    out['screenshot'] = await page.screenshot(type='png', full_page=False)
                except Exception: pass
                return out
            url = page.url or ''
            out['screenshot'] = await page.screenshot(type='png', full_page=False)
            # Detect logged-in vs login-wall
            # Logged-in heuristics: URL contains '/home' or doesn't have '/login'
            # AND page has a logged-in-only element like the composer or profile icon.
            if '/login' in url or '/checkpoint' in url:
                out['err'] = f'FB redirected to {url[:100]} — cookies stale/invalid'
                return out
            # Try to find a logged-in marker — composer entry or profile menu
            try:
                marker = await page.query_selector(
                    "div[aria-label='Create a post'], "
                    "div[role='banner'] [aria-label='Your profile'], "
                    "div[role='banner'] svg[aria-label='Your profile'], "
                    "a[href*='/me/'], "
                    "[data-testid='royal_login_form']")
                if marker is None:
                    # Best-effort: scan title for "Facebook" + any logged-in
                    # navigation. If the login form isn't present, treat as ok.
                    body = (await page.content() or '')[:5000].lower()
                    if 'log in to facebook' in body or 'forgot password' in body:
                        out['err'] = 'FB login form visible — not logged in'
                        return out
            except Exception:
                pass
            # Try to extract the visible profile name (best-effort)
            try:
                name_el = await page.query_selector(
                    "div[role='banner'] [aria-label*='profile' i] span, "
                    "div[role='banner'] [aria-label*='account' i]")
                if name_el:
                    txt = await name_el.text_content()
                    if txt: out['name'] = txt.strip()[:80]
            except Exception:
                pass
            out['ok'] = True
            return out
    except Exception as e:
        out['err'] = f'{type(e).__name__}: {str(e)[:300]}'
        return out
    finally:
        try:
            pipeline.stop_gologin_cloud_browser(profile_id)
        except Exception: pass


# ──────────────────────────────────────────────────────────────────────
# Telegram handlers
# ──────────────────────────────────────────────────────────────────────
async def meta_dev_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/meta_dev_setup — entry point. Lists Validated Profile N entries
    and prompts the user to pick one."""
    profiles = await asyncio.to_thread(_list_validated_profiles)
    if not profiles:
        await update.message.reply_text(
            "🟡 No <b>Validated Profile</b> entries found on your GoLogin "
            "account. Run /proxy first to create at least one.",
            parse_mode='HTML')
        return
    await update.message.reply_text(
        f"🛠 <b>Autonomous Meta-for-Developers account setup</b>\n\n"
        f"Found <b>{len(profiles)}</b> Validated Profile entries. "
        f"Pick one to use:\n\n"
        f"<i>Next step: I'll ask you to paste the FB account blob, then I'll "
        f"inject the cookies into the picked GoLogin browser and verify "
        f"facebook.com sees you as logged in.</i>",
        parse_mode='HTML',
        reply_markup=_profile_picker_kb(profiles))


async def meta_dev_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes mdev:<action> callbacks."""
    query = update.callback_query
    try: await query.answer()
    except Exception: pass
    parts = query.data.split(':', 2)
    action = parts[1] if len(parts) > 1 else ''
    arg = parts[2] if len(parts) > 2 else ''
    if action == 'cancel':
        try: await query.edit_message_text("❌ Cancelled.")
        except Exception: pass
        context.user_data.pop('meta_dev_profile_id', None)
        context.user_data.pop('meta_dev_profile_name', None)
        context.user_data.pop('expecting_meta_dev_blob', None)
        return
    if action == 'pick':
        # Look up the profile name from arg (profile_id)
        profiles = await asyncio.to_thread(_list_validated_profiles)
        profile = next((p for p in profiles if p['id'] == arg), None)
        if not profile:
            await query.edit_message_text(
                "❌ Profile not found (deleted?). Re-run /meta_dev_setup.")
            return
        context.user_data['meta_dev_profile_id'] = profile['id']
        context.user_data['meta_dev_profile_name'] = profile['name']
        context.user_data['expecting_meta_dev_blob'] = True
        # Also clear any partial blob buffer from a prior attempt
        context.user_data['meta_dev_blob_buf'] = ''
        await query.edit_message_text(
            f"✅ Picked <b>{profile['name']}</b>.\n\n"
            f"Now <b>paste the Facebook account blob</b> (same format as /blob).\n"
            f"Multi-line is fine; I'll keep accumulating until the message "
            f"contains a cookie block, then auto-parse and continue.\n\n"
            f"<i>Send /cancel any time to abort.</i>",
            parse_mode='HTML')
        return


async def meta_dev_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches the FB blob paste set by mdev:pick. Accumulates across
    multiple messages, parses on each receive, fires the cookie injection
    flow once parse_fb_account_blob returns a non-empty cookie list."""
    text = (update.message.text or '').strip()
    if text.startswith('/'):
        # User typed a slash command — abort the blob flow
        context.user_data.pop('expecting_meta_dev_blob', None)
        context.user_data.pop('meta_dev_blob_buf', None)
        return
    profile_id = context.user_data.get('meta_dev_profile_id')
    profile_name = context.user_data.get('meta_dev_profile_name')
    if not profile_id:
        context.user_data.pop('expecting_meta_dev_blob', None)
        await update.message.reply_text(
            "Session expired — re-run /meta_dev_setup.")
        return
    buf = context.user_data.get('meta_dev_blob_buf', '')
    if buf: buf += '\n'
    buf += text
    context.user_data['meta_dev_blob_buf'] = buf
    # Try to parse — only proceed if we got cookies
    parsed = _cookies_mod.parse_fb_account_blob(buf)
    cookies = parsed.get('cookies') or []
    if not cookies:
        await update.message.reply_text(
            f"⏳ Got <b>{len(buf)}</b> chars so far, no cookies parsed yet "
            f"— send the rest of the blob.", parse_mode='HTML')
        return
    # Got cookies — consume the buffer + run the login flow
    context.user_data.pop('expecting_meta_dev_blob', None)
    context.user_data.pop('meta_dev_blob_buf', None)
    chat_id = update.effective_chat.id
    bot = context.bot

    async def _say(text):
        try:
            await bot.send_message(chat_id, text, parse_mode='HTML',
                                   disable_web_page_preview=True)
        except Exception as e:
            logger.warning(f"[meta_dev send] {e}")

    await _say(
        f"🛠 <b>Starting Meta-Dev setup</b> on <b>{profile_name}</b>\n\n"
        f"<b>Step 1 / 4</b>: log into Facebook via cookie injection")
    res = await _login_fb_in_gologin_browser(
        profile_id, profile_name, parsed, _say)
    # Send screenshot if we have one (helps diagnose either result)
    shot = res.get('screenshot')
    if shot:
        try:
            from io import BytesIO
            tag = '✅' if res.get('ok') else '🚫'
            await bot.send_photo(
                chat_id, photo=BytesIO(shot),
                caption=f"{tag} FB after cookie injection (in {profile_name})")
        except Exception as e:
            logger.warning(f"[meta_dev photo] {e}")
    if res.get('ok'):
        nm = res.get('name') or '(name not extracted)'
        await _say(
            f"✅ <b>Step 1 done</b> — Facebook recognizes the session.\n"
            f"   profile name on FB: <b>{nm}</b>\n\n"
            f"<i>Shard 2 (Meta Dev signup form) not built yet. Tell me to "
            f"continue and I'll ship it next.</i>")
    else:
        await _say(
            f"❌ <b>Step 1 failed</b>: <code>{res.get('err', '?')[:300]}</code>\n\n"
            f"Most common cause: cookies expired. Re-pull the blob from "
            f"the source and re-run /meta_dev_setup.")
