"""/bio_gen — standalone 2-step bio-tagline generator.

Compact version of the bio step from the full /cloak wizard (which is
the 11-step cloak setup in reel-bot-Carolina, step 9 of which is the
bio generator). Same OpenAI backend (cloak_suggestions.suggest_bios),
same niche list (NICHES in cloak.py) — but split off into a fast
2-tap wizard for when the user only needs bio inspiration and not the
full slug/overlay/display setup.

Flow:
  /bio_gen                → Step 1/2 niche picker
  pick niche              → Step 2/2 model picker (or "default")
  pick model              → 8 AI bios + 🔄 Refresh + ⬅ Back + ✖ Cancel
  pick a bio              → copy-paste-friendly preformatted block

State lives in context.user_data['biogen']; callbacks use prefix
`biogen:` so they don't collide with /cloak's `cloak:` namespace.
"""
import html as _h
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import cloak                  # NICHES + _known_models
import cloak_suggestions      # suggest_bios

logger = logging.getLogger(__name__)


# ─── Step 1 — niche picker ─────────────────────────────────────────────────

def _niche_kb():
    """All NICHES as a 2-per-row grid + Cancel."""
    rows, row = [], []
    for i, n in enumerate(cloak.NICHES):
        row.append(InlineKeyboardButton(f"📁 {n}",
                                         callback_data=f"biogen:nch:{i}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✖ Cancel", callback_data="biogen:cancel")])
    return InlineKeyboardMarkup(rows)


# ─── Step 2 — model / handle picker ────────────────────────────────────────

def _model_kb():
    """Auto-discovered models (caro / kira / …) + a "default" handle option
    + back/cancel. Same source of truth as the full /cloak wizard."""
    rows = []
    models = []
    try:
        models = cloak._known_models() or []
    except Exception as e:
        logger.warning(f"[biogen] _known_models failed: {e}")
    for m in models:
        rows.append([InlineKeyboardButton(f"👤 {m}",
                                            callback_data=f"biogen:mod:{m}")])
    # Always offer a niche-only "default" handle — bios still get the niche
    # flavor, just without a specific model name in the prompt context.
    rows.append([InlineKeyboardButton("🎲 default (niche-only)",
                                        callback_data="biogen:mod:default")])
    rows.append([
        InlineKeyboardButton("⬅ Back", callback_data="biogen:back_nch"),
        InlineKeyboardButton("✖ Cancel", callback_data="biogen:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


# ─── Result — bios + refresh ───────────────────────────────────────────────

def _result_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh (8 new)",
                              callback_data="biogen:refresh")],
        [InlineKeyboardButton("⬅ Back to model picker",
                              callback_data="biogen:back_mod")],
        [InlineKeyboardButton("✖ Cancel", callback_data="biogen:cancel")],
    ])


def _render_bios_block(niche, model, bios):
    """Numbered, monospace bio list. Each line wrapped in <code> so the
    user can tap-to-copy on Telegram mobile."""
    if not bios:
        return ("⚠️ no bios — OPENAI_API_KEY may be unset or the API "
                "is down. Try Refresh, or set the env var on Railway.")
    lines = [
        f"📁 <b>Niche</b>: {_h.escape(str(niche))}",
        f"👤 <b>Model</b>: {_h.escape(str(model))}",
        "",
        f"<b>8 AI bios</b> (tap to copy):",
        "",
    ]
    for i, b in enumerate(bios, 1):
        lines.append(f"<b>{i}.</b> <code>{_h.escape(str(b))}</code>")
    lines.append("")
    lines.append("🔄 Refresh for 8 new ones (each call ~$0.001).")
    return '\n'.join(lines)


async def _fetch_bios(niche, model, force_refresh=False):
    """Async wrapper around the blocking OpenAI call. Returns list of strings."""
    import asyncio
    try:
        return await asyncio.to_thread(
            cloak_suggestions.suggest_bios,
            niche, model, 8, force_refresh)
    except Exception as e:
        logger.warning(f"[biogen] suggest_bios failed: {e}")
        return []


# ─── Command + callback ────────────────────────────────────────────────────

async def bio_gen_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point — open the niche picker."""
    context.user_data['biogen'] = {'state': 'pick_niche'}
    await update.message.reply_text(
        "🤖 <b>AI bio generator</b> (gpt-4o-mini)\n\n"
        "<b>Step 1/2 — Pick a niche</b>\n\n"
        "Same niche list as <code>/cloak</code>. After this pick a model "
        "(or default) and you'll get 8 bios you can refresh.",
        parse_mode='HTML',
        reply_markup=_niche_kb())


async def bio_gen_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle biogen:* inline buttons."""
    q = update.callback_query
    await q.answer()
    data = q.data or ''
    wiz = context.user_data.get('biogen') or {}

    if data == 'biogen:cancel':
        context.user_data.pop('biogen', None)
        await q.edit_message_text("✖ cancelled.")
        return

    if data == 'biogen:back_nch':
        context.user_data['biogen'] = {'state': 'pick_niche'}
        await q.edit_message_text(
            "🤖 <b>AI bio generator</b>\n\n<b>Step 1/2 — Pick a niche</b>",
            parse_mode='HTML',
            reply_markup=_niche_kb())
        return

    if data.startswith('biogen:nch:'):
        try:
            idx = int(data.split(':', 2)[2])
        except ValueError:
            await q.edit_message_text("⚠️ bad niche index.")
            return
        if idx < 0 or idx >= len(cloak.NICHES):
            await q.edit_message_text("⚠️ stale niche — run /bio_gen again.")
            return
        niche = cloak.NICHES[idx]
        context.user_data['biogen'] = {'state': 'pick_model', 'niche': niche}
        await q.edit_message_text(
            f"✅ Niche: <b>{_h.escape(niche)}</b>\n\n"
            f"<b>Step 2/2 — Pick a model</b> (flavors the bios)\n\n"
            f"Pick <i>default</i> for niche-only bios without a model handle.",
            parse_mode='HTML',
            reply_markup=_model_kb())
        return

    if data == 'biogen:back_mod':
        niche = wiz.get('niche')
        if not niche:
            await q.edit_message_text("⚠️ session expired — run /bio_gen again.")
            return
        wiz['state'] = 'pick_model'
        wiz.pop('bios', None)
        context.user_data['biogen'] = wiz
        await q.edit_message_text(
            f"✅ Niche: <b>{_h.escape(niche)}</b>\n\n"
            f"<b>Step 2/2 — Pick a model</b>",
            parse_mode='HTML',
            reply_markup=_model_kb())
        return

    if data.startswith('biogen:mod:'):
        model = data.split(':', 2)[2]
        niche = wiz.get('niche')
        if not niche:
            await q.edit_message_text("⚠️ session expired — run /bio_gen again.")
            return
        wiz.update({'state': 'show_bios', 'model': model})
        context.user_data['biogen'] = wiz
        # Edit to a "thinking" state, then patch with the bios when ready.
        await q.edit_message_text(
            f"🤖 generating 8 bios for <b>{_h.escape(niche)}</b> / "
            f"<b>{_h.escape(model)}</b>…",
            parse_mode='HTML')
        bios = await _fetch_bios(niche, model)
        wiz['bios'] = bios
        context.user_data['biogen'] = wiz
        await q.edit_message_text(_render_bios_block(niche, model, bios),
                                   parse_mode='HTML',
                                   reply_markup=_result_kb())
        return

    if data == 'biogen:refresh':
        niche = wiz.get('niche')
        model = wiz.get('model')
        if not (niche and model):
            await q.edit_message_text("⚠️ session expired — run /bio_gen again.")
            return
        await q.edit_message_text(
            f"🔄 refreshing 8 bios for <b>{_h.escape(niche)}</b> / "
            f"<b>{_h.escape(model)}</b>…",
            parse_mode='HTML')
        bios = await _fetch_bios(niche, model, force_refresh=True)
        wiz['bios'] = bios
        context.user_data['biogen'] = wiz
        await q.edit_message_text(_render_bios_block(niche, model, bios),
                                   parse_mode='HTML',
                                   reply_markup=_result_kb())
        return
