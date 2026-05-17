"""
/cloak — Cloaking link manager (Cloudflare KV).

Wizard creates a slug → model+OF mapping in Cloudflare KV. The actual
cloaking decision happens at the Cloudflare Worker layer (see
cloudflare_worker.js in this repo) — this script only writes config.

Commands:
  /cloak              — show menu (New / List / Delete)
  /cloak new          — start wizard
  /cloak list         — list all slugs
  /cloak delete <slug>— remove a slug

Wizard steps:
  1. Pick model (auto-discovered from OF_LINK_<NAME> env vars + KV model:* keys)
  2. Pick niche  (hardcoded NICHES list below — mirrors reel-bot's NICHE_SCENES)
  3. Number (auto or custom)
  4. Slug (auto-suggested + AI suggestions if OPENAI_API_KEY set)
  5. OF URL (text input)
  6. Overlay (with AI suggestions)
  7. Display name (with AI suggestions)
  8. Bio (with AI suggestions)
  9. IG URL (or skip)
 10. X URL (or skip)
 11. Save to KV

Env vars required:
  CLOAK_CF_ACCOUNT_ID    — Cloudflare account ID
  CLOAK_CF_API_TOKEN     — token with Workers KV:Edit + Workers Scripts:Edit
  CLOAK_CF_KV_NAMESPACE_ID — KV namespace where slugs live
  CLOAK_BASE_DOMAINS     — comma-separated base domains (e.g. domain1.link,domain2.link)
  OF_LINK_<MODEL>         — per-model OF URL, e.g. OF_LINK_CAROLINA=https://...
                            (the suffix becomes the model name, lowercased)
"""

import os
import re
import json
import logging
import random

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

try:
    import cloak_suggestions
    CLOAK_SUGG_AVAILABLE = True
except ImportError:
    CLOAK_SUGG_AVAILABLE = False
    cloak_suggestions = None

logger = logging.getLogger(__name__)

CF_ACCOUNT_ID = os.getenv('CLOAK_CF_ACCOUNT_ID', '')
CF_API_TOKEN = os.getenv('CLOAK_CF_API_TOKEN', '')
CF_KV_NAMESPACE_ID = os.getenv('CLOAK_CF_KV_NAMESPACE_ID', '')
BASE_DOMAINS = [d.strip() for d in os.getenv('CLOAK_BASE_DOMAINS', '').split(',')
                if d.strip()]
# Niches — hardcoded to mirror reel_bot.py's NICHE_SCENES top-level keys.
# Single source of truth: when reel_bot adds a niche to NICHE_SCENES, mirror
# it here in the same commit (see [[patch-both-repos-together]] rule).
NICHES = [
    'Construction', 'Police', 'Teacher', 'Goth', 'Goth Dwarf', 'Goth SFW',
    'Punk', 'Skater', 'Domina', 'Gamer', 'Asian Cosplay', 'Stewardess',
    'Stewardess Red', 'Military', 'NASA', 'Dwarf', 'Mommy', 'MAGA',
    'Redneck', 'Goth Mommy', 'Cute Girl', 'Cosplay', 'Surfer Girl', 'Prison',
    'Stuck With Strangers', 'Explorer', 'Fighter', 'Karate', 'Racer',
    'Goth Cosplay', 'Noir', 'Natural', 'Peasant Girl', 'Cyberpunk', 'Rave',
    'Medical Nurse', 'Ski Snow', 'DJ EDM', 'Halloween', 'Astronaut',
    'Welder', 'Geisha', 'Motorcycle', 'Color Hash', 'Japanese Schoolgirl',
    'Schoolgirl', 'Alternative',
]


def _known_models():
    """Auto-discover models. Mirrors reel_bot._cloak_known_models():
      1. OF_LINK_<NAME> env vars on Railway (skip numeric suffixes)
      2. model:<name> keys in Cloudflare KV
    Returns sorted, deduplicated lowercase list."""
    out = set()
    for k in os.environ:
        if k.startswith('OF_LINK_') and os.environ[k].strip():
            name = k[len('OF_LINK_'):].lower()
            if name.isdigit():
                continue
            out.add(name)
    if _cf_ready():
        try:
            keys, msg = cf_kv_list_keys()
            if msg == 'OK':
                for k in (keys or []):
                    if k.startswith('model:'):
                        out.add(k[len('model:'):].lower())
        except Exception:
            pass
    return sorted(out)

SLUG_RE = re.compile(r'^[a-z0-9_-]{2,40}$')

CF_API = 'https://api.cloudflare.com/client/v4'


def _cf_ready():
    return bool(CF_ACCOUNT_ID and CF_API_TOKEN and CF_KV_NAMESPACE_ID)


def cf_kv_get(key):
    """Read a KV value. Returns (value_text_or_None, error)."""
    if not _cf_ready():
        return None, 'cloudflare env vars not set'
    try:
        r = requests.get(
            f'{CF_API}/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/'
            f'{CF_KV_NAMESPACE_ID}/values/{key}',
            headers={'Authorization': f'Bearer {CF_API_TOKEN}'}, timeout=15)
        if r.status_code == 404:
            return None, None
        if r.status_code != 200:
            return None, f'CF {r.status_code}: {r.text[:200]}'
        return r.text, None
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'


def cf_kv_put(key, value):
    """Write a KV value. Returns (ok, error)."""
    if not _cf_ready():
        return False, 'cloudflare env vars not set'
    try:
        r = requests.put(
            f'{CF_API}/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/'
            f'{CF_KV_NAMESPACE_ID}/values/{key}',
            headers={'Authorization': f'Bearer {CF_API_TOKEN}',
                     'Content-Type': 'application/octet-stream'},
            data=value.encode('utf-8'), timeout=15)
        if r.status_code != 200:
            return False, f'CF {r.status_code}: {r.text[:200]}'
        return True, None
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def cf_kv_delete(key):
    if not _cf_ready():
        return False, 'cloudflare env vars not set'
    try:
        r = requests.delete(
            f'{CF_API}/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/'
            f'{CF_KV_NAMESPACE_ID}/values/{key}',
            headers={'Authorization': f'Bearer {CF_API_TOKEN}'}, timeout=15)
        if r.status_code not in (200, 404):
            return False, f'CF {r.status_code}: {r.text[:200]}'
        return True, None
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def cf_kv_list_keys():
    """Return list of all slug keys in the namespace."""
    if not _cf_ready():
        return [], 'cloudflare env vars not set'
    try:
        keys = []
        cursor = None
        while True:
            params = {'limit': 1000}
            if cursor:
                params['cursor'] = cursor
            r = requests.get(
                f'{CF_API}/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/'
                f'{CF_KV_NAMESPACE_ID}/keys',
                headers={'Authorization': f'Bearer {CF_API_TOKEN}'},
                params=params, timeout=15)
            if r.status_code != 200:
                return [], f'CF {r.status_code}: {r.text[:200]}'
            j = r.json()
            for k in j.get('result', []):
                keys.append(k['name'])
            cursor = (j.get('result_info') or {}).get('cursor')
            if not cursor:
                break
        return keys, None
    except Exception as e:
        return [], f'{type(e).__name__}: {e}'


def _pick_base_round_robin():
    if not BASE_DOMAINS:
        return None
    return random.choice(BASE_DOMAINS)


# ─── Suggestion keyboard helper ───────────────────────────────────────

def _sugg_kb(step, suggestions):
    """Inline keyboard with up to 10 picks + Refresh + Cancel."""
    rows, row = [], []
    for i, s in enumerate((suggestions or [])[:10]):
        label = str(s) if len(str(s)) <= 28 else str(s)[:26] + '…'
        row.append(InlineKeyboardButton(
            label, callback_data=f"cloak:sugg:{step}:{i}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("🔄 Refresh",
                              callback_data=f"cloak:refresh:{step}"),
        InlineKeyboardButton("✖ Cancel", callback_data="cloak:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


async def _fetch_suggestions(step, wiz, force_refresh=False, n=8):
    if not CLOAK_SUGG_AVAILABLE:
        return []
    import asyncio
    niche = wiz.get('niche') or 'default'
    model = wiz.get('model') or 'caro'
    handle = wiz.get('slug') or wiz.get('display') or model
    try:
        if step == 'slug':
            out = await asyncio.to_thread(
                cloak_suggestions.suggest_slugs, niche, model, n, force_refresh)
        elif step == 'overlay':
            out = await asyncio.to_thread(
                cloak_suggestions.suggest_overlay_text, niche, model, n, force_refresh)
        elif step == 'display':
            out = await asyncio.to_thread(
                cloak_suggestions.suggest_display_names, niche, model, n, force_refresh)
        elif step == 'bio':
            out = await asyncio.to_thread(
                cloak_suggestions.suggest_bios, niche, handle, n, force_refresh)
        else:
            out = []
    except Exception as e:
        logger.warning(f'[cloak-sugg] fetch {step} failed: {e}')
        out = []
    wiz[f'_suggestions_{step}'] = out
    return out


# ─── Top-level command ────────────────────────────────────────────────

async def cloak_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cloak — show menu."""
    if not _cf_ready():
        await update.message.reply_text(
            "❌ Cloudflare not configured. Set on Railway:\n"
            "  <code>CLOAK_CF_ACCOUNT_ID</code>\n"
            "  <code>CLOAK_CF_API_TOKEN</code>\n"
            "  <code>CLOAK_CF_KV_NAMESPACE_ID</code>",
            parse_mode='HTML')
        return
    if not BASE_DOMAINS:
        await update.message.reply_text(
            "❌ No base domains. Set <code>CLOAK_BASE_DOMAINS</code> "
            "(comma-separated, e.g. <code>domain1.link,domain2.link</code>).",
            parse_mode='HTML')
        return
    models = _known_models()
    await update.message.reply_text(
        "🔗 <b>Cloak Manager</b>\n\n"
        f"<b>Base domains:</b> {', '.join(BASE_DOMAINS)}\n"
        f"<b>Models:</b> {', '.join(models) or '(none — set OF_LINK_&lt;NAME&gt; env vars)'}\n"
        f"<b>Niches:</b> {len(NICHES)} hardcoded ({', '.join(NICHES[:5])}…)",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✨ New cloaked link",
                                  callback_data="cloak:wiz:start")],
            [InlineKeyboardButton("📋 List slugs",
                                  callback_data="cloak:list")],
            [InlineKeyboardButton("🗑 Delete slug",
                                  callback_data="cloak:delete:list")],
        ]))


# ─── Wizard ──────────────────────────────────────────────────────────

async def cloak_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all cloak:* inline buttons."""
    import html as _h
    query = update.callback_query
    await query.answer()
    data = query.data or ''

    # Suggestion pick — re-dispatch into text handler with picked value
    if data.startswith('cloak:sugg:'):
        parts = data.split(':')
        if len(parts) < 4:
            return
        step, idx = parts[2], parts[3]
        wiz = context.user_data.get('cloak_wiz') or {}
        sug = wiz.get(f'_suggestions_{step}') or []
        try:
            picked = sug[int(idx)]
        except (ValueError, IndexError):
            await query.message.reply_text("❌ Suggestion expired.")
            return
        import types
        fake_update = types.SimpleNamespace(
            message=query.message, effective_user=update.effective_user,
            callback_query=None)
        await query.message.reply_text(
            f"✅ Picked: <code>{_h.escape(str(picked))}</code>",
            parse_mode='HTML')
        await cloak_text_received(fake_update, context, str(picked))
        return

    if data.startswith('cloak:refresh:'):
        step = data.split(':', 2)[2]
        wiz = context.user_data.get('cloak_wiz') or {}
        if not wiz:
            return
        await query.message.reply_text("🔄 Regenerating…")
        new_sugg = await _fetch_suggestions(step, wiz, force_refresh=True)
        context.user_data['cloak_wiz'] = wiz
        if not new_sugg:
            await query.message.reply_text(
                "❌ Couldn't fetch (OPENAI_API_KEY not set or API down). "
                "Type a custom value.")
            return
        await query.message.reply_text(
            f"💡 <b>Fresh suggestions for {step}:</b>",
            parse_mode='HTML', reply_markup=_sugg_kb(step, new_sugg))
        return

    if data == 'cloak:cancel':
        context.user_data.pop('cloak_wiz', None)
        context.user_data.pop('expecting_cloak_input', None)
        await query.edit_message_text("✖ Cancelled.")
        return

    if data == 'cloak:wiz:start':
        models = _known_models()
        if not models:
            await query.edit_message_text(
                "❌ No models detected. Set <code>OF_LINK_&lt;NAME&gt;</code> "
                "env vars (e.g. <code>OF_LINK_CAROLINA=https://onlyfans.com/...</code>).",
                parse_mode='HTML')
            return
        context.user_data['cloak_wiz'] = {'state': 'model'}
        kb = [[InlineKeyboardButton(m, callback_data=f"cloak:wiz:mod:{m}")]
              for m in models]
        kb.append([InlineKeyboardButton("✖ Cancel",
                                         callback_data="cloak:cancel")])
        await query.edit_message_text(
            "<b>Step 1/8 — Pick model</b>",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith('cloak:wiz:mod:'):
        model = data.split(':', 3)[3]
        wiz = context.user_data.setdefault('cloak_wiz', {})
        wiz['model'] = model
        wiz['state'] = 'niche'
        kb = []
        for i, n in enumerate(NICHES):
            kb.append([InlineKeyboardButton(f"📁 {n}",
                callback_data=f"cloak:wiz:nch:{i}")])
        kb.append([InlineKeyboardButton("✖ Cancel",
                                         callback_data="cloak:cancel")])
        wiz['_niche_snapshot'] = NICHES
        context.user_data['cloak_wiz'] = wiz
        await query.edit_message_text(
            f"✅ Model: <b>{_h.escape(model)}</b>\n\n"
            f"<b>Step 2/8 — Pick niche</b>",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith('cloak:wiz:nch:'):
        idx = int(data.split(':', 3)[3])
        wiz = context.user_data.setdefault('cloak_wiz', {})
        snap = wiz.get('_niche_snapshot') or NICHES
        if idx >= len(snap):
            await query.message.reply_text("❌ Bad niche index.")
            return
        wiz['niche'] = snap[idx]
        wiz['state'] = 'niche_n'
        # Auto-suggest next free number
        keys, _ = cf_kv_list_keys()
        prefix = f"{wiz['model']}_{wiz['niche']}_".lower()
        existing_n = []
        for k in keys:
            if k.lower().startswith(prefix):
                m = re.match(rf"{re.escape(prefix)}(\d+)", k.lower())
                if m:
                    existing_n.append(int(m.group(1)))
        suggested = max(existing_n) + 1 if existing_n else 1
        wiz['_suggested_n'] = suggested
        context.user_data['cloak_wiz'] = wiz
        context.user_data['expecting_cloak_input'] = True
        await query.edit_message_text(
            f"✅ Niche: <b>{_h.escape(wiz['niche'])}</b>\n\n"
            f"<b>Step 3/8 — Number</b>\n\n"
            f"Auto-suggested next free: <code>{suggested}</code>.\n"
            f"Send <code>auto</code> to accept, or type a positive integer.",
            parse_mode='HTML')
        return

    if data == 'cloak:list':
        keys, err = cf_kv_list_keys()
        if err:
            await query.edit_message_text(f"❌ List failed: {err}")
            return
        if not keys:
            await query.edit_message_text("📋 No slugs in KV yet.")
            return
        lines = [f"📋 <b>{len(keys)} slug(s)</b>\n"]
        for k in sorted(keys)[:50]:
            lines.append(f"  • <code>{_h.escape(k)}</code>")
        if len(keys) > 50:
            lines.append(f"  ... and {len(keys)-50} more")
        await query.edit_message_text('\n'.join(lines), parse_mode='HTML')
        return

    if data == 'cloak:delete:list':
        keys, err = cf_kv_list_keys()
        if err:
            await query.edit_message_text(f"❌ List failed: {err}")
            return
        if not keys:
            await query.edit_message_text("📋 No slugs to delete.")
            return
        kb = []
        for k in sorted(keys)[:20]:
            kb.append([InlineKeyboardButton(f"🗑 {k}",
                callback_data=f"cloak:delete:run:{k}")])
        kb.append([InlineKeyboardButton("✖ Cancel",
                                         callback_data="cloak:cancel")])
        await query.edit_message_text(
            f"🗑 <b>Pick a slug to delete</b> (showing first 20):",
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith('cloak:delete:run:'):
        slug = data.split(':', 3)[3]
        ok, err = cf_kv_delete(slug)
        if ok:
            await query.edit_message_text(
                f"✅ Deleted <code>{_h.escape(slug)}</code>",
                parse_mode='HTML')
        else:
            await query.edit_message_text(
                f"❌ Delete failed: {_h.escape(str(err))}",
                parse_mode='HTML')
        return


# ─── Text-input handler (called from bot.py's central router) ─────────

async def cloak_text_received(update, context, text):
    """Advance the wizard based on what the user typed (or picked via suggestion)."""
    import html as _h
    wiz = context.user_data.get('cloak_wiz') or {}
    state = wiz.get('state')
    text = (text or '').strip()
    if text.lower() in ('/cancel', 'cancel'):
        context.user_data.pop('cloak_wiz', None)
        context.user_data.pop('expecting_cloak_input', None)
        await update.message.reply_text("✖ Cancelled.")
        return

    if state == 'niche_n':
        t = text.lower()
        if t == 'auto':
            n = int(wiz.get('_suggested_n') or 1)
        else:
            try:
                n = int(t)
                if n < 1 or n > 9999:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(
                    "❌ Send a positive integer or <code>auto</code>.",
                    parse_mode='HTML')
                return
        wiz['niche_n'] = n
        wiz.pop('_suggested_n', None)
        wiz['state'] = 'slug'
        suggested_slug = f"{wiz['model']}_{wiz['niche'].replace(' ','_')}_{n}".lower()
        wiz['_suggested_slug'] = suggested_slug
        context.user_data['cloak_wiz'] = wiz
        # AI suggestions
        ai_sugg = await _fetch_suggestions('slug', wiz)
        context.user_data['cloak_wiz'] = wiz
        kb = _sugg_kb('slug', ai_sugg) if ai_sugg else None
        sugg_blurb = ("\n\n💡 <b>AI suggestions</b> (tap to pick or type custom):"
                      if ai_sugg else "")
        await update.message.reply_text(
            f"✅ Number: <b>{n}</b>\n\n"
            f"<b>Step 4/8 — URL slug</b>\n\n"
            f"Becomes the subdomain: <code>&lt;slug&gt;.&lt;base&gt;</code>\n"
            f"Auto-suggested: <code>{_h.escape(suggested_slug)}</code> "
            f"(send <code>auto</code>).\n"
            f"Or type a custom slug — 2-40 chars, lowercase letters/digits/_/-."
            f"{sugg_blurb}",
            parse_mode='HTML', reply_markup=kb)
        return

    if state == 'slug':
        if text.lower() == 'auto':
            slug = (wiz.get('_suggested_slug') or '').lower()
        else:
            slug = text.lower()
        if not SLUG_RE.match(slug):
            await update.message.reply_text(
                "❌ Slug must be 2-40 chars: lowercase letters, digits, _, -.")
            return
        existing, _err = cf_kv_get(slug)
        if existing:
            await update.message.reply_text(
                f"⚠️ Slug <code>{_h.escape(slug)}</code> already exists. "
                f"Pick another.", parse_mode='HTML')
            return
        wiz['slug'] = slug
        base = _pick_base_round_robin()
        wiz['base'] = base
        wiz['host'] = f'{slug}.{base}'
        wiz.pop('_suggested_slug', None)
        wiz['state'] = 'of'
        context.user_data['cloak_wiz'] = wiz
        await update.message.reply_text(
            f"✅ Slug: <code>{_h.escape(slug)}</code>\n"
            f"🌐 URL: <code>https://{_h.escape(wiz['host'])}</code>\n\n"
            f"<b>Step 5/8 — OF URL</b>\n\n"
            f"Paste the full OF URL (e.g. <code>https://onlyfans.com/handle</code>).",
            parse_mode='HTML')
        return

    if state == 'of':
        wiz['of'] = text
        wiz['state'] = 'overlay'
        context.user_data['cloak_wiz'] = wiz
        ai_sugg = await _fetch_suggestions('overlay', wiz)
        context.user_data['cloak_wiz'] = wiz
        kb = _sugg_kb('overlay', ai_sugg) if ai_sugg else None
        sugg_blurb = ("\n\n💡 <b>AI suggestions</b> (tap or type custom):"
                      if ai_sugg else "")
        await update.message.reply_text(
            f"<b>Step 6/8 — Overlay text</b>\n\n"
            f"Punchy text shown on the OF card image (1-4 words, emojis OK).\n"
            f"Send <code>default</code> for <code>OFF 💦😘</code>, "
            f"<code>skip</code> for none."
            f"{sugg_blurb}",
            parse_mode='HTML', reply_markup=kb)
        return

    if state == 'overlay':
        t = text.lower()
        if t == 'skip':
            wiz['of_overlay'] = ''
        elif t == 'default':
            wiz['of_overlay'] = 'OFF 💦😘'
        else:
            wiz['of_overlay'] = text[:40]
        wiz['state'] = 'display'
        context.user_data['cloak_wiz'] = wiz
        ai_sugg = await _fetch_suggestions('display', wiz)
        context.user_data['cloak_wiz'] = wiz
        kb = _sugg_kb('display', ai_sugg) if ai_sugg else None
        sugg_blurb = ("\n\n💡 <b>AI suggestions</b>:" if ai_sugg else "")
        await update.message.reply_text(
            f"<b>Step 7/8 — Display name</b>\n\n"
            f"The big name shown under the OF card.\n"
            f"Send <code>skip</code> for none."
            f"{sugg_blurb}",
            parse_mode='HTML', reply_markup=kb)
        return

    if state == 'display':
        wiz['display'] = '' if text.lower() == 'skip' else text[:60]
        wiz['state'] = 'bio'
        context.user_data['cloak_wiz'] = wiz
        ai_sugg = await _fetch_suggestions('bio', wiz)
        context.user_data['cloak_wiz'] = wiz
        kb = _sugg_kb('bio', ai_sugg) if ai_sugg else None
        sugg_blurb = ("\n\n💡 <b>AI suggestions</b>:" if ai_sugg else "")
        await update.message.reply_text(
            f"<b>Step 8/8 — Bio (tagline under the name)</b>\n\n"
            f"Send <code>skip</code> for none. After this we save to KV."
            f"{sugg_blurb}",
            parse_mode='HTML', reply_markup=kb)
        return

    if state == 'bio':
        wiz['bio'] = '' if text.lower() == 'skip' else text[:160]
        # SAVE TO KV
        config = {
            'model': wiz['model'],
            'host': wiz['host'],
            'niche': wiz['niche'],
            'niche_n': wiz['niche_n'],
            'of': wiz.get('of', ''),
        }
        if wiz.get('of_overlay'): config['of_overlay'] = wiz['of_overlay']
        if wiz.get('display'):    config['display'] = wiz['display']
        if wiz.get('bio'):        config['bio'] = wiz['bio']
        slug = wiz['slug']
        json_value = json.dumps(config, separators=(',', ':'))
        ok, err = cf_kv_put(slug, json_value)
        context.user_data.pop('cloak_wiz', None)
        context.user_data.pop('expecting_cloak_input', None)
        if not ok:
            await update.message.reply_text(
                f"❌ KV write failed: <code>{_h.escape(str(err))}</code>",
                parse_mode='HTML')
            return
        url = f'https://{wiz["host"]}'
        await update.message.reply_text(
            f"✅ <b>Cloaked link created</b>\n\n"
            f"🔗 <a href=\"{_h.escape(url)}\">{_h.escape(url)}</a>\n\n"
            f"<b>Model:</b> {_h.escape(wiz['model'])}\n"
            f"<b>Niche:</b> {_h.escape(wiz['niche'])}/#{wiz['niche_n']}\n"
            f"<b>OF:</b> {_h.escape(wiz.get('of',''))}\n\n"
            f"<i>KV propagates globally in ~30-60s. Open the URL above in "
            f"a phone browser to see the landing page.</i>",
            parse_mode='HTML', disable_web_page_preview=False)
