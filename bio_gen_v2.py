"""/bio_gen_v2 — intimate "girlfriend-brand" bio generator.

One-step flow: /bio_gen_v2 → pick a niche → 10 fresh bios + refresh. Bios are tied
to the NICHE only (not to any specific model), so there is no model-picker
step — warm, flirty, parasocial "your #1 favorite <niche> girlfriend" bonding
bios instead of the toxic/accidentally-funny style of /bio_gen.
Backed by cloak_suggestions.suggest_bios_v2 (its own _SYS_BIO_V2 prompt +
separate cache namespace). /bio_gen is left completely untouched.

Callbacks use prefix `biogen2:` and state lives in user_data['biogen2'] so
this never collides with /bio_gen's `biogen:` namespace.
"""
import html as _h
import asyncio
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import cloak                  # NICHES
import cloak_suggestions      # suggest_bios_v2

logger = logging.getLogger(__name__)

# Bios are niche-only — no model handle. This fixed value is passed as the
# `handle` arg so the prompt/cache key stay stable across all models.
_HANDLE = 'default'


def _niche_kb():
    rows, row = [], []
    for i, n in enumerate(cloak.NICHES):
        row.append(InlineKeyboardButton(f"📁 {n}",
                                         callback_data=f"biogen2:nch:{i}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✖ Cancel", callback_data="biogen2:cancel")])
    return InlineKeyboardMarkup(rows)


def _result_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh (10 new)", callback_data="biogen2:refresh")],
        [InlineKeyboardButton("⬅ Back to niches", callback_data="biogen2:back_nch")],
        [InlineKeyboardButton("✖ Cancel", callback_data="biogen2:cancel")],
    ])


def _render_bios_block(niche, bios):
    if not bios:
        return ("⚠️ no bios — OPENAI_API_KEY may be unset or the API "
                "is down. Try Refresh, or set the env var on Railway.")
    lines = [
        f"💞 <b>Girlfriend-brand bios</b>",
        f"📁 <b>Niche</b>: {_h.escape(str(niche))}",
        "",
        f"<b>{len(bios)} bios</b> — mixed lengths (tap to copy):",
        "",
    ]
    for i, b in enumerate(bios, 1):
        lines.append(f"<b>{i}.</b> <code>{_h.escape(str(b))}</code>")
    lines.append("")
    lines.append("🔄 Refresh for a fresh set (each call ~$0.002).")
    return '\n'.join(lines)


N_BIOS = 10


async def _fetch_bios(niche, force_refresh=True):
    # ALWAYS force_refresh=True → bypass the cache so every generation calls the
    # LLM fresh and returns a brand-new, non-repeating set (the user never wants
    # the same "your favorite goth gf" line twice).
    try:
        return await asyncio.to_thread(
            cloak_suggestions.suggest_bios_v2,
            niche, _HANDLE, N_BIOS, True)
    except Exception as e:
        logger.warning(f"[biogen2] suggest_bios_v2 failed: {e}")
        return []


async def bio_gen_v2_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['biogen2'] = {'state': 'pick_niche'}
    await update.message.reply_text(
        "💞 <b>Bio generator V2 — girlfriend-brand</b> (gpt-4o-mini)\n\n"
        "Warm, flirty, playful girlfriend-energy bios — confident (not clingy), "
        "with mixed lengths from ultra-short to scenario lines.\n\n"
        "<b>Pick a niche</b> → you'll get 10 mixed-length bios you can refresh.",
        parse_mode='HTML',
        reply_markup=_niche_kb())


async def bio_gen_v2_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ''
    wiz = context.user_data.get('biogen2') or {}

    if data == 'biogen2:cancel':
        context.user_data.pop('biogen2', None)
        await q.edit_message_text("✖ cancelled.")
        return

    if data == 'biogen2:back_nch':
        context.user_data['biogen2'] = {'state': 'pick_niche'}
        await q.edit_message_text(
            "💞 <b>Bio generator V2</b>\n\n<b>Pick a niche</b>",
            parse_mode='HTML', reply_markup=_niche_kb())
        return

    if data.startswith('biogen2:nch:'):
        try:
            idx = int(data.split(':', 2)[2])
        except ValueError:
            await q.edit_message_text("⚠️ bad niche index.")
            return
        if idx < 0 or idx >= len(cloak.NICHES):
            await q.edit_message_text("⚠️ stale niche — run /bio_gen_v2 again.")
            return
        niche = cloak.NICHES[idx]
        wiz = {'state': 'show_bios', 'niche': niche}
        context.user_data['biogen2'] = wiz
        await q.edit_message_text(
            f"💞 generating 10 fresh girlfriend-brand bios for "
            f"<b>{_h.escape(niche)}</b>…",
            parse_mode='HTML')
        bios = await _fetch_bios(niche)
        wiz['bios'] = bios
        context.user_data['biogen2'] = wiz
        await q.edit_message_text(_render_bios_block(niche, bios),
                                  parse_mode='HTML', reply_markup=_result_kb())
        return

    if data == 'biogen2:refresh':
        niche = wiz.get('niche')
        if not niche:
            await q.edit_message_text("⚠️ session expired — run /bio_gen_v2 again.")
            return
        await q.edit_message_text(
            f"🔄 refreshing 10 bios for <b>{_h.escape(niche)}</b>…",
            parse_mode='HTML')
        bios = await _fetch_bios(niche, force_refresh=True)
        wiz['bios'] = bios
        context.user_data['biogen2'] = wiz
        await q.edit_message_text(_render_bios_block(niche, bios),
                                  parse_mode='HTML', reply_markup=_result_kb())
        return
