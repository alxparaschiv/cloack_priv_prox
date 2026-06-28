"""/portrait_gen — Resize any image to a 3:4 portrait at 2K quality.

Two steps, NO AI recreation (the subject/content is never changed):
  1. Deterministic crop to 3:4 (fills the frame, no distortion, no bars).
     The vertical crop is biased toward the top so the face/upper body is
     kept and more of the lower frame is dropped.
  2. AI super-resolution upscale to 2K via WaveSpeed's image-upscaler — adds
     real detail and sharpness instead of the quality loss a plain downscale
     would cause.

Conversation flow:
  /portrait_gen                → prompt for an image
  user sends photo OR file     → crop to 3:4 → upscale to 2K → return the file

Env deps:
  - WAVESPEED_API_KEY        — same key as /banner_gen and /artistic_bg
"""
import os
import io
import base64
import time
import asyncio
import logging

import requests
from PIL import Image, ImageOps
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

WAVESPEED_API_BASE = 'https://api.wavespeed.ai/api/v3'
WAVESPEED_API_KEY = os.environ.get('WAVESPEED_API_KEY', '')
UPSCALER = 'wavespeed-ai/image-upscaler'  # $0.01/run, ~18s, 2k/4k/8k
TARGET_RESOLUTION = '2k'

TARGET_W, TARGET_H = 3, 4          # aspect ratio
# When trimming height (input taller than 3:4), take this fraction of the
# excess off the TOP and the rest off the bottom. 0.25 → keep the head/upper
# body, drop more of the lower frame (legs/feet). Center for width crops.
TOP_BIAS = 0.25


def crop_to_34(src_path, dst_path):
    """Crop `src_path` to a 3:4 portrait, lossless (no downscale), save PNG.
    Returns (w, h). Vertical crops biased toward the top (TOP_BIAS)."""
    with Image.open(src_path) as im:
        im = ImageOps.exif_transpose(im)        # respect phone orientation
        if im.mode not in ('RGB',):
            im = im.convert('RGB')
        w, h = im.size
        if w * TARGET_H >= h * TARGET_W:
            # Wider than 3:4 → full height, crop width (centered).
            crop_h = h
            crop_w = round(h * TARGET_W / TARGET_H)
        else:
            # Taller than 3:4 → full width, crop height (top-biased).
            crop_w = w
            crop_h = round(w * TARGET_H / TARGET_W)
        left = (w - crop_w) // 2
        top = round((h - crop_h) * TOP_BIAS)
        im = im.crop((left, top, left + crop_w, top + crop_h))
        im.save(dst_path, 'PNG')                # lossless before upscaling
        return im.size


def _upscale_2k(src_path, dst_path):
    """AI super-resolution upscale to 2K via WaveSpeed image-upscaler.
    Returns (out_w, out_h, err). On error returns (0, 0, message)."""
    if not WAVESPEED_API_KEY:
        return 0, 0, "WAVESPEED_API_KEY not set"
    try:
        with open(src_path, 'rb') as f:
            b = f.read()
    except Exception as e:
        return 0, 0, f"can't read crop: {e}"
    data_uri = f"data:image/png;base64,{base64.b64encode(b).decode('ascii')}"

    submit_url = f"{WAVESPEED_API_BASE}/{UPSCALER}"
    body = {
        'image': data_uri,
        'target_resolution': TARGET_RESOLUTION,
        'output_format': 'jpeg',
        'enable_base64_output': False,
        'enable_sync_mode': False,
    }
    try:
        r = requests.post(submit_url, json=body, timeout=60,
                          headers={'Authorization': f'Bearer {WAVESPEED_API_KEY}',
                                   'Content-Type': 'application/json'})
        if r.status_code != 200:
            return 0, 0, f"submit HTTP {r.status_code}: {r.text[:200]}"
        rid = (r.json().get('data') or {}).get('id') or r.json().get('id')
    except Exception as e:
        return 0, 0, f"submit err: {type(e).__name__}: {e}"
    if not rid:
        return 0, 0, "no prediction id returned"

    deadline = time.time() + 180
    out_url = None
    while time.time() < deadline:
        try:
            pr = requests.get(f"{WAVESPEED_API_BASE}/predictions/{rid}/result",
                              headers={'Authorization': f'Bearer {WAVESPEED_API_KEY}'},
                              timeout=30)
            pr.raise_for_status()
            d = pr.json().get('data') or pr.json()
            status = d.get('status') or ''
            if status in ('completed', 'succeeded'):
                outs = d.get('outputs') or []
                if outs:
                    out_url = outs[0] if isinstance(outs[0], str) else outs[0].get('url')
                break
            if status == 'failed':
                return 0, 0, f"upscale failed: {str(d)[:200]}"
        except Exception as e:
            logger.warning(f"[portrait_gen] upscale poll err: {e}")
        time.sleep(3)
    if not out_url:
        return 0, 0, f"upscale timed out (rid={rid})"

    try:
        ir = requests.get(out_url, timeout=60); ir.raise_for_status()
        with open(dst_path, 'wb') as f:
            f.write(ir.content)
        with Image.open(dst_path) as im:
            return im.width, im.height, None
    except Exception as e:
        return 0, 0, f"download err: {e}"


def make_2k_portrait(src_path):
    """Crop to 3:4 then upscale to 2K. Returns (out_path, info_str, err)."""
    ts = time.strftime('%Y%m%d_%H%M%S')
    crop_path = f"/tmp/portrait_crop_{ts}.png"
    out_path = f"/tmp/portrait_{ts}.jpg"
    cw, ch = crop_to_34(src_path, crop_path)
    ow, oh, err = _upscale_2k(crop_path, out_path)
    if err:
        return None, None, err
    return out_path, f"{cw}×{ch} → {ow}×{oh}", None


async def portrait_gen_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['expecting_portrait_photo'] = True
    await update.message.reply_text(
        "🖼 *3:4 resize → 2K*\n\n"
        "Send an image (as a photo OR as a file for max quality) and I'll "
        "resize it to a 3:4 vertical portrait at 2K.\n\n"
        "Crop fills the frame — no distortion, no bars; the top is kept "
        "(face/upper body), more of the bottom is trimmed. Then AI "
        "super-resolution upscales it to 2K (content unchanged, just "
        "sharper). ~$0.01, ~20s.",
        parse_mode='Markdown')


def _image_source(message):
    """(file_id, unique_id) for an image sent as a photo OR an image file."""
    if message.photo:
        p = message.photo[-1]
        return p.file_id, p.file_unique_id
    doc = message.document
    if doc:
        mt = (doc.mime_type or '').lower()
        name = (doc.file_name or '').lower()
        if mt.startswith('image/') or name.endswith(
                ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif')):
            return doc.file_id, doc.file_unique_id
    return None


async def portrait_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Photo/document-router entry. Returns True if handled; False otherwise.
    Accepts the image as a compressed photo OR an uncompressed file."""
    if not context.user_data.get('expecting_portrait_photo'):
        return False
    src = _image_source(update.message)
    if not src:
        return False  # not an image — leave flag set for the next message
    context.user_data.pop('expecting_portrait_photo', None)
    file_id, unique_id = src

    try:
        f = await context.bot.get_file(file_id)
        local_ref = f"/tmp/portrait_ref_{unique_id}.jpg"
        await f.download_to_drive(local_ref)
    except Exception as e:
        await update.message.reply_text(f"❌ couldn't download image: `{e}`",
                                          parse_mode='Markdown')
        return True

    await update.message.reply_text("⏳ cropping to 3:4 + upscaling to 2K…")

    out_path, info, err = await asyncio.to_thread(make_2k_portrait, local_ref)
    if err:
        await update.message.reply_text(f"❌ resize failed: `{err}`",
                                          parse_mode='Markdown')
        return True

    try:
        ts = time.strftime('%Y%m%d_%H%M%S')
        with open(out_path, 'rb') as fh:
            await update.message.reply_document(
                document=fh, filename=f"portrait_2k_{ts}.jpg",
                caption=f"✅ 3:4 @ 2K — {info}")
    except Exception as e:
        await update.message.reply_text(f"⚠️ done but TG send failed: `{e}`",
                                          parse_mode='Markdown')
    return True
