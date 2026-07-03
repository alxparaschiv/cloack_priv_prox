"""/bio_gen_v2 — intimate "girlfriend-brand" bio generator.

Same 2-step flow as /bio_gen (niche → model → 8 bios + refresh), but a
different tone: warm, flirty, parasocial "your #1 favorite <niche> girlfriend"
bonding bios instead of the toxic/accidentally-funny style of /bio_gen.
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

import cloak                  # NICHES + _known_models
import cloak_suggestions      # suggest_bios_v2

logger = logging.getLogger(__name__)


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


def _model_kb():
    rows = []
    try:
        models = cloak._known_models() or []
    except Exception as e:
        logger.warning(f"[biogen2] _known_models failed: {e}")
        models = []
    for m in models:
        rows.append([InlineKeyboardButton(f"👤 {m}",
                                          callback_data=f"biogen2:mod:{m}")])
    rows.append([InlineKeyboardButton("🎲 default (niche-only)",
                                      callback_data="biogen2:mod:default")])
    rows.append([
        InlineKeyboardButton("⬅ Back", callback_data="biogen2:back_nch"),
        InlineKeyboardButton("✖ Cancel", callback_data="biogen2:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _result_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh (8 new)", callback_data="biogen2:refresh")],
        [InlineKeyboardButton("⬅ Back to model picker", callback_data="biogen2:back_mod")],
        [InlineKeyboardButton("✖ Cancel", callback_data="biogen2:cancel")],
    ])


def _render_bios_block(niche, model, bios):
    if not bios:
        return ("⚠️ no bios — OPENAI_API_KEY may be unset or the API "
                "is down. Try Refresh, or set the env var on Railway.")
    lines = [
        f"💞 <b>Girlfriend-brand bios</b>",
        f"📁 <b>Niche</b>: {_h.escape(str(niche))}",
        f"👤 <b>Model</b>: {_h.escape(str(model))}",
        "",
        f"<b>8 bios</b> (tap to copy):",
        "",
    ]
    for i, b in enumerate(bios, 1):
        lines.append(f"<b>{i}.</b> <code>{_h.escape(str(b))}</code>")
    lines.append("")
    lines.append("🔄 Refresh for 8 new ones (each call ~$0.001).")
    return '\n'.join(lines)


async def _fetch_bios(niche, model, force_refresh=False):
    try:
        return await asyncio.to_thread(
            cloak_suggestions.suggest_bios_v2,
            niche, model, 8, force_refresh)
    except Exception as e:
        logger.warning(f"[biogen2] suggest_bios_v2 failed: {e}")
        return []


async def bio_gen_v2_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['biogen2'] = {'state': 'pick_niche'}
    await update.message.reply_text(
        "💞 <b>Bio generator V2 — girlfriend-brand</b> (gpt-4o-mini)\n\n"
        "Warmer, flirty, bonding bios — the \"your #1 favorite &lt;niche&gt; "
        "girlfriend\" angle (vs /bio_gen's toxic-funny style).\n\n"
        "<b>Step 1/2 — Pick a niche</b>",
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
            "💞 <b>Bio generator V2</b>\n\n<b>Step 1/2 — Pick a niche</b>",
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
        context.user_data['biogen2'] = {'state': 'pick_model', 'niche': niche}
        await q.edit_message_text(
            f"✅ Niche: <b>{_h.escape(niche)}</b>\n\n"
            f"<b>Step 2/2 — Pick a model</b> (flavors the bios)",
            parse_mode='HTML', reply_markup=_model_kb())
        return

    if data == 'biogen2:back_mod':
        niche = wiz.get('niche')
        if not niche:
            await q.edit_message_text("⚠️ session expired — run /bio_gen_v2 again.")
            return
        wiz['state'] = 'pick_model'
        wiz.pop('bios', None)
        context.user_data['biogen2'] = wiz
        await q.edit_message_text(
            f"✅ Niche: <b>{_h.escape(niche)}</b>\n\n"
            f"<b>Step 2/2 — Pick a model</b>",
            parse_mode='HTML', reply_markup=_model_kb())
        return

    if data.startswith('biogen2:mod:'):
        model = data.split(':', 2)[2]
        niche = wiz.get('niche')
        if not niche:
            await q.edit_message_text("⚠️ session expired — run /bio_gen_v2 again.")
            return
        wiz.update({'state': 'show_bios', 'model': model})
        context.user_data['biogen2'] = wiz
        await q.edit_message_text(
            f"💞 generating 8 girlfriend-brand bios for <b>{_h.escape(niche)}</b> / "
            f"<b>{_h.escape(model)}</b>…",
            parse_mode='HTML')
        bios = await _fetch_bios(niche, model)
        wiz['bios'] = bios
        context.user_data['biogen2'] = wiz
        await q.edit_message_text(_render_bios_block(niche, model, bios),
                                  parse_mode='HTML', reply_markup=_result_kb())
        return

    if data == 'biogen2:refresh':
        niche = wiz.get('niche')
        model = wiz.get('model')
        if not (niche and model):
            await q.edit_message_text("⚠️ session expired — run /bio_gen_v2 again.")
            return
        await q.edit_message_text(
            f"🔄 refreshing 8 bios for <b>{_h.escape(niche)}</b> / "
            f"<b>{_h.escape(model)}</b>…",
            parse_mode='HTML')
        bios = await _fetch_bios(niche, model, force_refresh=True)
        wiz['bios'] = bios
        context.user_data['biogen2'] = wiz
        await q.edit_message_text(_render_bios_block(niche, model, bios),
                                  parse_mode='HTML', reply_markup=_result_kb())
        return
