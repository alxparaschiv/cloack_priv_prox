"""looksmax.py — face "looksmax" variation generator (2026-06-20).

Telegram: /looksmax  → send ONE reference photo of a model → the bot generates
a set of CONTROLLED, research-grounded attractiveness variations of the SAME
woman (each a single subtle "dial": skin glow, femininity, lips/eyes, brighter,
glam), identity strictly preserved, then saves them to a fresh Google Drive
folder and DMs you the link. The original is saved alongside for A/B comparison.

Idea: each model already looks different, so this lets you spin off small,
labelled variations and let HER traffic decide which look pulls the most
eyeballs — instead of guessing.

Research basis (female facial attractiveness): the most ROBUST drivers are skin
clarity / evenness / health ("glass skin"), averageness + femininity (softer
jaw, larger eyes, smaller nose, fuller cheeks/lips), and clear striking eyes.
Skin TONE preference is audience-dependent → it's offered as ONE test dial, not
assumed. Sources: Nature Sci-Reports 2025 (averageness+femininity > symmetry),
the classic symmetry/averageness/feature-size literature.

Engine: WaveSpeed `google/nano-banana-pro/edit` (Gemini 3 Pro Image) — reuses
the Drive + WaveSpeed helpers already in artistic_bg_gen.py. Per-model PROFILES
let you customise the dials per model (default profile used otherwise).
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

# Separate Drive root so looksmax sets don't mix with the account-setup images.
OUTPUT_ROOT_NAME = 'Looksmax variations'

# Portrait ratio — faces read best taller-than-wide. nano-banana-pro accepts it.
ASPECT_RATIO = '3:4'

# ── Identity lock (prepended to every dial) ────────────────────────────────
# The output MUST still clearly be HER — only the named dial changes. Mirrors
# the proven recreation/banner identity language used elsewhere in the fleet.
_IDENTITY = (
    "This is a reference photo of a woman. Generate a NEW photo of the SAME "
    "woman — keep her EXACT identity, face shape, bone structure, eye colour, "
    "hair colour and length, body and overall likeness so she is unmistakably "
    "the same person. Keep the same outfit, pose, framing and setting. "
    "Photorealistic, natural amateur smartphone photo with real skin texture "
    "and natural lighting; NO bokeh, not airbrushed, not plastic, not "
    "AI-looking. Apply ONLY this one subtle enhancement: "
)

# ── Default research-grounded looksmax dials (label, delta-prompt) ─────────
# Each is a SINGLE controlled change so any CTR difference is attributable.
_DEFAULT_DIALS = [
    ("glassskin", _IDENTITY +
     "give her flawless, healthy, even-toned skin with a soft dewy luminous "
     "'glass-skin' glow and a clear, radiant complexion — the single strongest "
     "attractiveness signal; gently even out blemishes and discoloration while "
     "KEEPING natural pores and skin texture (no plastic, no over-smoothing)."),
    ("femininity", _IDENTITY +
     "make her proportions very subtly more feminine and 'average-beautiful': "
     "slightly soften the jawline, open the eyes a touch larger, make the nose "
     "a little smaller and the cheeks slightly fuller and lifted — keep it "
     "natural and clearly still the same woman."),
    ("lipseyes", _IDENTITY +
     "give her slightly fuller, softly glossy lips and slightly larger, "
     "brighter eyes with a clearer iris and a subtle limbal ring — striking "
     "yet natural, never overdone."),
    ("brighter", _IDENTITY +
     "give her a slightly brighter, lighter and more even, luminous skin tone "
     "and a touch brighter platinum-blonde hair — fresh, radiant and glowing, "
     "still completely natural."),
    ("glam", _IDENTITY +
     "add fuller but tasteful glam makeup that pops on camera — long defined "
     "lashes, soft cheekbone contour and highlight, clean groomed brows and a "
     "polished lip. Model-quality, not heavy."),
]

# ── Per-model overrides (extend as you tune each model) ────────────────────
# Key on a lowercase model name passed as `/looksmax <model>`. Falls back to
# the default dials when the model isn't listed.
MODEL_PROFILES = {
    # 'kira': [ ("...", _IDENTITY + "..."), ... ],
}


def _dials_for(model):
    return MODEL_PROFILES.get((model or '').strip().lower(), _DEFAULT_DIALS)


def _generate_one(data_uri, prompt):
    """Submit the ref + a dial prompt, wait, fetch the output bytes."""
    rid = _ag._wavespeed_submit_multi_ref(
        [data_uri], prompt=prompt, aspect_ratio=ASPECT_RATIO)
    out_url = _ag._wavespeed_wait(rid)
    r = requests.get(out_url, timeout=60)
    r.raise_for_status()
    return r.content


async def looksmax_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/looksmax [model] — arm the photo prompt."""
    args = context.args or []
    model = args[0].strip() if args else ''
    context.user_data['expecting_looksmax_photo'] = True
    context.user_data['looksmax_model'] = model
    dials = _dials_for(model)
    _names = ", ".join(lbl for lbl, _ in dials)
    await update.message.reply_text(
        "🧬 <b>Looksmax variations</b>"
        + (f" — <b>{model}</b>" if model else "")
        + "\n\nSend me <b>one reference photo</b> of the model now. I'll generate "
        f"<b>{len(dials)}</b> controlled variations — <i>{_names}</i> — each a "
        "single subtle dial, identity preserved, save them to a fresh Drive "
        "folder (with the original for A/B), and send you the link.\n\n"
        "Send /cancel to abort.",
        parse_mode='HTML')


async def looksmax_photo_received(update: Update,
                                  context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Photo-router hook. Returns True if it handled the photo."""
    if not context.user_data.get('expecting_looksmax_photo'):
        return False
    context.user_data.pop('expecting_looksmax_photo', None)
    model = (context.user_data.pop('looksmax_model', '') or '').strip()

    photos = getattr(update.message, 'photo', None)
    if not photos:
        await update.message.reply_text(
            "❌ No photo in that message. Run /looksmax again.")
        return True

    # Download the highest-resolution version.
    try:
        f = await context.bot.get_file(photos[-1].file_id)
        local_ref = f"/tmp/looksmax_ref_{int(time.time())}.jpg"
        await f.download_to_drive(local_ref)
        with open(local_ref, 'rb') as fh:
            ref_bytes = fh.read()
    except Exception as e:
        await update.message.reply_text(
            f"❌ couldn't download photo: {type(e).__name__}: {e}")
        return True
    data_uri = "data:image/jpeg;base64," + base64.b64encode(ref_bytes).decode('ascii')

    dials = _dials_for(model)
    await update.message.reply_text(
        f"🧬 Generating <b>{len(dials)}</b> looksmax variations"
        + (f" for <b>{model}</b>" if model else "")
        + " — this runs on nano-banana-pro, ~1-2 min each…", parse_mode='HTML')

    # Fresh Drive folder for this run.
    try:
        svc = await asyncio.to_thread(_ag._drive_service)
        root_id = await asyncio.to_thread(_ag._ensure_folder, svc, OUTPUT_ROOT_NAME)
        batch_name = (f"looksmax_{(model + '_') if model else ''}"
                      f"{time.strftime('%Y%m%d_%H%M%S')}")
        batch_id = await asyncio.to_thread(_ag._ensure_folder, svc, batch_name, root_id)
        batch_url = _ag._folder_drive_url(batch_id)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Drive folder setup failed: {type(e).__name__}: {e}")
        return True

    # Save the original first (for side-by-side comparison).
    try:
        await asyncio.to_thread(
            _ag._upload_bytes_to_drive, svc, batch_id, "00_original.jpg", ref_bytes)
    except Exception as e:
        logger.warning(f"[looksmax] original upload failed: {e}")

    ok, fails = 0, []
    for i, (label, prompt) in enumerate(dials, start=1):
        try:
            img = await asyncio.to_thread(_generate_one, data_uri, prompt)
            await asyncio.to_thread(
                _ag._upload_bytes_to_drive, svc, batch_id,
                f"{i:02d}_{label}.jpg", img)
            ok += 1
            await update.message.reply_text(f"  ✅ {i}/{len(dials)} <b>{label}</b>",
                                            parse_mode='HTML')
        except Exception as e:
            fails.append(label)
            logger.warning(f"[looksmax] {label} failed: {e}")
            await update.message.reply_text(
                f"  ⚠️ {i}/{len(dials)} {label} failed: {type(e).__name__}: {str(e)[:120]}")

    msg = (f"🎉 <b>Done</b> — {ok}/{len(dials)} variations"
           + (f" for <b>{model}</b>" if model else "") + ".\n"
           f"📂 <a href=\"{batch_url}\">Open the Drive folder</a>\n"
           f"<code>{batch_url}</code>")
    if fails:
        msg += "\n⚠️ failed: " + ", ".join(fails)
    await update.message.reply_text(
        msg, parse_mode='HTML', disable_web_page_preview=True)
    return True
