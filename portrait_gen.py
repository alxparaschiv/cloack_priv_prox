"""/portrait_gen — Pure resize of any image to a 3:4 vertical portrait.

NOT an AI recreation — this is a plain, deterministic resize. Whatever image
you send, it center-crops to a 3:4 aspect ratio (fills the frame, no
distortion, no letterbox bars) and returns it. Instant and free.

Conversation flow:
  /portrait_gen                → prompt for an image
  user uploads photo           → bot center-crops to 3:4 and returns it

Crop behavior: keeps the center of the image and trims the overflowing
edges (sides if the input is wider than 3:4, top/bottom if taller). Output
is capped at 1080×1440 (downscale only — never upscales a small image).
"""
import os
import time
import asyncio
import logging

from PIL import Image, ImageOps
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

TARGET_W, TARGET_H = 3, 4          # aspect ratio
MAX_H = 1440                       # cap output height (1080×1440 = IG portrait)
JPEG_QUALITY = 92


def resize_to_34(src_path, dst_path):
    """Center-crop `src_path` to a 3:4 portrait and save to `dst_path`.
    Returns (out_w, out_h). No distortion, no bars; trims the long edge."""
    with Image.open(src_path) as im:
        # Respect EXIF orientation (phone photos) then drop alpha for JPEG.
        im = ImageOps.exif_transpose(im)
        if im.mode not in ('RGB',):
            im = im.convert('RGB')
        w, h = im.size

        # Largest 3:4 box that fits inside the image, centered.
        if w * TARGET_H >= h * TARGET_W:
            # Image is wider than 3:4 → full height, crop width.
            crop_h = h
            crop_w = round(h * TARGET_W / TARGET_H)
        else:
            # Image is taller than 3:4 → full width, crop height.
            crop_w = w
            crop_h = round(w * TARGET_H / TARGET_W)
        left = (w - crop_w) // 2
        top = (h - crop_h) // 2
        im = im.crop((left, top, left + crop_w, top + crop_h))

        # Downscale only — never upscale (would just blur a small input).
        if im.height > MAX_H:
            new_w = round(im.width * MAX_H / im.height)
            im = im.resize((new_w, MAX_H), Image.LANCZOS)

        im.save(dst_path, 'JPEG', quality=JPEG_QUALITY)
        return im.size


async def portrait_gen_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['expecting_portrait_photo'] = True
    await update.message.reply_text(
        "🖼 *3:4 resize*\n\n"
        "Send an image and I'll resize it to a 3:4 vertical portrait "
        "(IG feed / profile format).\n\n"
        "Plain center-crop — no AI, no distortion, no bars. The sides "
        "(or top/bottom) are trimmed so the center fills a 3:4 frame. "
        "Output up to 1080×1440.",
        parse_mode='Markdown')


async def portrait_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Photo-router entry. Returns True if we handled the upload; False if
    we're not expecting a portrait photo right now."""
    if not context.user_data.get('expecting_portrait_photo'):
        return False
    context.user_data.pop('expecting_portrait_photo', None)

    photo = (update.message.photo or [None])[-1]   # highest-res variant
    if not photo:
        await update.message.reply_text("❌ no photo in that message. Run /portrait_gen again.")
        return True

    try:
        f = await context.bot.get_file(photo.file_id)
        local_ref = f"/tmp/portrait_ref_{photo.file_unique_id}.jpg"
        await f.download_to_drive(local_ref)
    except Exception as e:
        await update.message.reply_text(f"❌ couldn't download photo: `{e}`",
                                          parse_mode='Markdown')
        return True

    ts = time.strftime('%Y%m%d_%H%M%S')
    local_out = f"/tmp/portrait_{ts}.jpg"
    try:
        out_w, out_h = await asyncio.to_thread(resize_to_34, local_ref, local_out)
    except Exception as e:
        logger.warning(f"[portrait_gen] resize err: {e}")
        await update.message.reply_text(f"❌ resize failed: `{e}`",
                                          parse_mode='Markdown')
        return True

    try:
        # Send as a document to avoid Telegram re-compressing/re-cropping the
        # photo, so the user gets the exact 3:4 file.
        with open(local_out, 'rb') as fh:
            await update.message.reply_document(
                document=fh, filename=f"portrait_{ts}.jpg",
                caption=f"✅ resized to 3:4 — {out_w}×{out_h}")
    except Exception as e:
        await update.message.reply_text(f"⚠️ resized but TG send failed: `{e}`",
                                          parse_mode='Markdown')
    return True
