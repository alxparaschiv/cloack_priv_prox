"""/banner_gen — Generate a wide-banner-format image of a model from a reference photo.

Conversation flow:
  /banner_gen                  → prompt for a reference photo
  user uploads photo           → bot calls nano-banana-pro/edit with a
                                  "lying-on-bed banner pose" prompt,
                                  returns the generated image in TG

The reference image carries the model's identity (face, hair, features);
the prompt specifies the new pose, wardrobe, and setting. Aspect ratio
is 21:9 first (cinematic banner / social header format), falling back to
16:9 if WaveSpeed rejects 21:9.

Env deps:
  - WAVESPEED_API_KEY        — same key used by /artistic_bg
"""
import os
import io
import base64
import time
import asyncio
import logging

import requests
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

WAVESPEED_API_BASE = 'https://api.wavespeed.ai/api/v3'
WAVESPEED_API_KEY = os.environ.get('WAVESPEED_API_KEY', '')
ENGINE = 'nano-banana-pro'  # Gemini 3 Pro Image; ~$0.07-0.20/img

# Prompt describing the TARGET pose/style. The reference image carries
# the identity; this text describes everything ELSE about the output.
BANNER_PROMPT = (
    "Generate a wide-format cinematic banner image of the SAME person from "
    "the reference image. Preserve her face, hair color and texture, eye "
    "color, complexion, and overall identity EXACTLY — do not change her "
    "facial features or proportions.\n"
    "\n"
    "NEW POSE: lying horizontally on a bed, propped up on one elbow, "
    "head and shoulders in the foreground filling roughly the right "
    "half of the frame, body extending into the background to the left. "
    "Direct eye contact with the camera. Natural relaxed expression — "
    "slightly intimate, slightly sultry, but tasteful.\n"
    "\n"
    "WARDROBE: elegant black dress or black top with subtle beaded or "
    "pearl detail at the waist or neckline. Tasteful, not revealing.\n"
    "\n"
    "SETTING: minimalist modern bedroom, white bedding, off-white pillows, "
    "soft natural daylight from the left side of the frame. Clean, "
    "uncluttered background.\n"
    "\n"
    "COMPOSITION: cinematic ultrawide banner — the subject fills most of "
    "the frame, head in the right third, body extending into the rest. "
    "Suitable as a social-media header / cover image."
)


def _generate_banner(local_ref_path):
    """Submit a /edit task to nano-banana-pro with the reference image +
    banner prompt, poll for result, download. Returns (local_path, err).
    """
    if not WAVESPEED_API_KEY:
        return None, "WAVESPEED_API_KEY not set"

    # Base64 the reference image as a data: URI (same pattern artistic_bg uses)
    try:
        with open(local_ref_path, 'rb') as f:
            b = f.read()
    except Exception as e:
        return None, f"can't read reference: {e}"
    ext = os.path.splitext(local_ref_path)[1].lstrip('.').lower() or 'jpg'
    mime_by_ext = {'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png',
                   'webp':'image/webp','heic':'image/heic','heif':'image/heif'}
    mt = mime_by_ext.get(ext, 'image/jpeg')
    data_uri = f"data:{mt};base64,{base64.b64encode(b).decode('ascii')}"

    # Submit. Try 21:9 first (cinematic banner), fall back to 16:9 if
    # WaveSpeed rejects the ratio.
    submit_url = f"{WAVESPEED_API_BASE}/google/{ENGINE}/edit"
    rid = None
    used_ratio = None
    for aspect in ('21:9', '16:9'):
        body = {
            'prompt': BANNER_PROMPT,
            'images': [data_uri],
            'aspect_ratio': aspect,
            'resolution': '2k',
            'output_format': 'jpeg',
            'enable_sync_mode': False,
            'enable_base64_output': False,
        }
        try:
            r = requests.post(submit_url, json=body, timeout=60,
                              headers={'Authorization': f'Bearer {WAVESPEED_API_KEY}',
                                       'Content-Type': 'application/json'})
            if r.status_code != 200:
                logger.warning(f"[banner_gen] aspect={aspect} got HTTP {r.status_code}: {r.text[:200]}")
                continue
            j = r.json()
            rid = (j.get('data') or {}).get('id') or j.get('id')
            if rid:
                used_ratio = aspect
                logger.info(f"[banner_gen] submitted aspect={aspect}, rid={rid}")
                break
        except Exception as e:
            logger.warning(f"[banner_gen] submit err aspect={aspect}: {e}")
            continue
    if not rid:
        return None, "all aspect-ratio variants rejected on submit"

    # Poll for result — same pattern as artistic_bg, 5min budget
    deadline = time.time() + 300
    out_url = None
    while time.time() < deadline:
        try:
            r = requests.get(f"{WAVESPEED_API_BASE}/predictions/{rid}/result",
                             headers={'Authorization': f'Bearer {WAVESPEED_API_KEY}'},
                             timeout=30)
            r.raise_for_status()
            d = r.json().get('data') or r.json()
            status = d.get('status') or ''
            if status in ('completed', 'succeeded'):
                outputs = d.get('outputs') or []
                if outputs:
                    first = outputs[0]
                    out_url = first if isinstance(first, str) else first.get('url')
                    break
                return None, "completed but no outputs"
            if status == 'failed':
                return None, f"task failed: {d}"
        except Exception as e:
            logger.warning(f"[banner_gen] poll err: {e}")
        time.sleep(3)
    if not out_url:
        return None, f"timed out after 300s (rid={rid})"

    # Download
    try:
        img_r = requests.get(out_url, timeout=60); img_r.raise_for_status()
    except Exception as e:
        return None, f"output download err: {e}"
    ts = time.strftime('%Y%m%d_%H%M%S')
    local = f"/tmp/banner_{ts}.jpg"
    with open(local, 'wb') as f: f.write(img_r.content)
    logger.info(f"[banner_gen] saved {local} (aspect={used_ratio})")
    return local, None


async def banner_gen_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['expecting_banner_photo'] = True
    await update.message.reply_text(
        "🖼 *Banner generator*\n\n"
        "Send a reference photo of your model (just one image — any "
        "shot showing her face/identity).\n\n"
        "I'll generate a wide-banner-format image of the same person in "
        "a specific pose: lying on a bed, propped on one elbow, looking at "
        "the camera, wearing a black dress with subtle beaded waist detail.\n\n"
        "Engine: `nano-banana-pro` (Gemini 3 Pro Image)\n"
        "Aspect ratio: 21:9 (cinematic banner / social header)\n"
        "Cost: ~$0.10",
        parse_mode='Markdown')


async def banner_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Photo-router entry. Returns True if we handled the upload (so other
    photo handlers in bot.py don't also fire); False if we're not expecting
    a banner photo right now."""
    if not context.user_data.get('expecting_banner_photo'):
        return False
    context.user_data.pop('expecting_banner_photo', None)

    photo = (update.message.photo or [None])[-1]   # highest-res variant
    if not photo:
        await update.message.reply_text("❌ no photo in that message. Run /banner_gen again.")
        return True

    # Download to /tmp
    try:
        f = await context.bot.get_file(photo.file_id)
        local_ref = f"/tmp/banner_ref_{photo.file_unique_id}.jpg"
        await f.download_to_drive(local_ref)
    except Exception as e:
        await update.message.reply_text(f"❌ couldn't download photo: `{e}`",
                                          parse_mode='Markdown')
        return True

    await update.message.reply_text(
        f"🤖 generating banner from your reference (~60-120s)…",
        parse_mode='Markdown')

    local_out, err = await asyncio.to_thread(_generate_banner, local_ref)
    if err:
        await update.message.reply_text(f"❌ `{err}`", parse_mode='Markdown')
        return True

    try:
        with open(local_out, 'rb') as fh:
            await update.message.reply_photo(
                photo=fh,
                caption=(f"✅ banner generated\n"
                         f"Engine: `{ENGINE}` (nano-banana-pro)\n"
                         f"Saved locally: `{local_out}`"),
                parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"⚠️ generated but TG send failed: `{e}`",
                                          parse_mode='Markdown')
    return True
