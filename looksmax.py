"""looksmax.py — face "looksmax" glow-up generator (2026-06-21).

Telegram: /looksmax [model] [hair=<colour>]  → send ONE reference photo of a
model → the bot generates a STRONG, VISIBLE attractiveness glow-up of the same
woman in 3 styles (natural / goth / max), saves them to a fresh Google Drive
folder and DMs you the link (the original is saved as 00_original for A/B).

What it does (validated against the user's anchors): a BIG, clearly-visible
upgrade — not a subtle nudge — toward an idealized pale e-girl look:
  • skin → dramatically paler, flawless porcelain-white
  • eyes → noticeably larger, rounder, more OPEN (defined crease, more Western /
    less-monolid shape), brighter light iris, big lashes
  • lips → fuller
  • figure → slim, snatched waist
  • glam → soft (natural) / dramatic goth / maxed
Identity is loosened on purpose (recognizably HER, but levelled up). HAIR colour
is KEPT by default; pass `hair=blonde` (etc.) to recolour it for a run.

Hard-won notes baked in:
  • Strong language + looser identity = the change actually shows (timid
    "subtle/slightly" prompts produced ~no change).
  • DO NOT modify bust/breasts — nano-banana-pro's safety filter flags it. The
    figure is done via "slim waist" only.

Engine: WaveSpeed `google/nano-banana-pro/edit` — reuses artistic_bg_gen's
WaveSpeed + Drive helpers (same engine + Drive auth as /artistic_bg, /banner_gen).
Per-model MODEL_PROFILES override the dials per model.
"""
import os
import base64
import time
import logging
import asyncio

import requests
import artistic_bg_gen as _ag
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

OUTPUT_ROOT_NAME = 'Looksmax variations'
ASPECT_RATIO = '3:4'


def _base_prompt(hair=None):
    """The consolidated face/skin/eyes/lips glow-up. KEEPS hair unless `hair`
    is given (then recolours to it). No bust language (safety filter)."""
    if hair:
        hair_clause = f"change her hair to {hair}; "
        hair_keep = ""
    else:
        hair_clause = ""
        hair_keep = "SAME hair colour and style, "
    return (
        "Generate a glamour-upgraded, looksmaxed version of the SAME woman. Keep "
        "her recognizable (same face identity, " + hair_keep + "same outfit, pose "
        "and setting), but VISIBLY transform her looks — the changes must be "
        "CLEARLY VISIBLE, not subtle. MANDATORY enhancements: " + hair_clause +
        "make her skin DRAMATICALLY paler and flawless — luminous porcelain-white, "
        "even and glowing; make her eyes noticeably LARGER, ROUNDER and more OPEN "
        "with a defined upper-eyelid crease and a more Western, less-monolid shape, "
        "a brighter light iris and big lashes; fuller lips; a slim, snatched waist. "
        "Photorealistic, real skin texture, natural smartphone photo, NO bokeh, not "
        "plastic, not AI-looking. Makeup style: ")


# (label, makeup-style tail) — appended to the base. 3 produced every run.
_STYLES = [
    ("natural", "soft natural 'clean girl' glam — fresh, glowing, pretty and "
                "believable, like a real stunning girl."),
    ("goth", "dramatic GOTH e-girl glam — bold smokey/black eye makeup with sharp "
             "winged liner, dark or black lipstick, edgy styling."),
    ("max", "push every enhancement to the MAX while staying photorealistic — the "
            "most striking, flawless, magazine-cover version of this look."),
]

# Per-model overrides: model name (lowercase) -> list of (label, FULL prompt).
# Add a model here once you've tuned its look (e.g. force blonde, specific glam).
MODEL_PROFILES = {}


def _dials_for(model=None, hair=None):
    key = (model or '').strip().lower()
    if key and key in MODEL_PROFILES:
        return MODEL_PROFILES[key]
    base = _base_prompt(hair)
    return [(lbl, base + tail) for lbl, tail in _STYLES]


def _parse_args(args):
    """`/looksmax [model] [hair=<colour>]` → (model, hair)."""
    model, hair = '', None
    for a in (args or []):
        if a.lower().startswith('hair='):
            hair = a.split('=', 1)[1].strip() or None
        elif not model:
            model = a.strip()
    return model, hair


def _generate_one(data_uri, prompt):
    rid = _ag._wavespeed_submit_multi_ref(
        [data_uri], prompt=prompt, aspect_ratio=ASPECT_RATIO)
    out_url = _ag._wavespeed_wait(rid)
    r = requests.get(out_url, timeout=60)
    r.raise_for_status()
    return r.content


async def looksmax_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/looksmax [model] [hair=<colour>] — arm the photo prompt."""
    model, hair = _parse_args(context.args)
    context.user_data['expecting_looksmax_photo'] = True
    context.user_data['looksmax_model'] = model
    context.user_data['looksmax_hair'] = hair
    dials = _dials_for(model, hair)
    await update.message.reply_text(
        "🧬 <b>Looksmax glow-up</b>"
        + (f" — <b>{model}</b>" if model else "")
        + (f" · hair → <b>{hair}</b>" if hair else " · keeping hair")
        + "\n\nSend me <b>one reference photo</b> of the model now. I'll generate "
        f"<b>{len(dials)}</b> styles — <i>{', '.join(l for l, _ in dials)}</i> — a "
        "strong, visible glow-up (paler porcelain skin, bigger rounder eyes, fuller "
        "lips, glam), save them to a fresh Drive folder (with the original for A/B) "
        "and send the link.\n\n"
        "<i>Tip: add <code>hair=blonde</code> to recolour the hair.</i>\n"
        "Send /cancel to abort.",
        parse_mode='HTML')


async def looksmax_photo_received(update: Update,
                                  context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Photo-router hook. Returns True if it handled the photo."""
    if not context.user_data.get('expecting_looksmax_photo'):
        return False
    context.user_data.pop('expecting_looksmax_photo', None)
    model = (context.user_data.pop('looksmax_model', '') or '').strip()
    hair = context.user_data.pop('looksmax_hair', None)

    photos = getattr(update.message, 'photo', None)
    if not photos:
        await update.message.reply_text("❌ No photo in that message. Run /looksmax again.")
        return True

    try:
        f = await context.bot.get_file(photos[-1].file_id)
        local_ref = f"/tmp/looksmax_ref_{int(time.time())}.jpg"
        await f.download_to_drive(local_ref)
        with open(local_ref, 'rb') as fh:
            ref_bytes = fh.read()
    except Exception as e:
        await update.message.reply_text(f"❌ couldn't download photo: {type(e).__name__}: {e}")
        return True
    data_uri = "data:image/jpeg;base64," + base64.b64encode(ref_bytes).decode('ascii')

    dials = _dials_for(model, hair)
    await update.message.reply_text(
        f"🧬 Generating <b>{len(dials)}</b> looksmax styles"
        + (f" for <b>{model}</b>" if model else "")
        + (f" (hair → {hair})" if hair else "")
        + " — nano-banana-pro, ~1-2 min each…", parse_mode='HTML')

    try:
        svc = await asyncio.to_thread(_ag._drive_service)
        root_id = await asyncio.to_thread(_ag._ensure_folder, svc, OUTPUT_ROOT_NAME)
        batch_name = (f"looksmax_{(model + '_') if model else ''}"
                      f"{time.strftime('%Y%m%d_%H%M%S')}")
        batch_id = await asyncio.to_thread(_ag._ensure_folder, svc, batch_name, root_id)
        batch_url = _ag._folder_drive_url(batch_id)
    except Exception as e:
        await update.message.reply_text(f"❌ Drive folder setup failed: {type(e).__name__}: {e}")
        return True

    try:
        await asyncio.to_thread(_ag._upload_bytes_to_drive, svc, batch_id,
                                "00_original.jpg", ref_bytes)
    except Exception as e:
        logger.warning(f"[looksmax] original upload failed: {e}")

    ok, fails = 0, []
    for i, (label, prompt) in enumerate(dials, start=1):
        try:
            img = await asyncio.to_thread(_generate_one, data_uri, prompt)
            await asyncio.to_thread(_ag._upload_bytes_to_drive, svc, batch_id,
                                    f"{i:02d}_{label}.jpg", img)
            ok += 1
            await update.message.reply_text(f"  ✅ {i}/{len(dials)} <b>{label}</b>",
                                            parse_mode='HTML')
        except Exception as e:
            fails.append(label)
            logger.warning(f"[looksmax] {label} failed: {e}")
            await update.message.reply_text(
                f"  ⚠️ {i}/{len(dials)} {label} failed: {type(e).__name__}: {str(e)[:120]}")

    msg = (f"🎉 <b>Done</b> — {ok}/{len(dials)} styles"
           + (f" for <b>{model}</b>" if model else "") + ".\n"
           f"📂 <a href=\"{batch_url}\">Open the Drive folder</a>\n"
           f"<code>{batch_url}</code>")
    if fails:
        msg += "\n⚠️ failed: " + ", ".join(fails)
    await update.message.reply_text(msg, parse_mode='HTML', disable_web_page_preview=True)
    return True
