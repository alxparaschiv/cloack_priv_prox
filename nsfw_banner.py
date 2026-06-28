"""/nsfw_banner — Spicier banner variant: Sophie-Rain-style OF banner framing.

Sibling of /banner_gen (which is left unchanged). Reuses banner_gen's WaveSpeed
submit/poll/retry plumbing; only the prompt differs. Recreates the model from a
reference photo in the classic OnlyFans-banner composition: face in the
foreground looking back at the camera, lingerie-clad hip/behind rising into the
background — with a soft, neutral-yet-innocent expression (the look the user
identified as the defining element of Sophie Rain's banner).

Scope guardrails baked into the prompt:
  • Subject is always a clearly ADULT woman (mid-to-late 20s) — never youthful.
  • Suggestive lingerie only — NOT explicit; no nudity, nothing uncovered.
nano-banana-pro's own safety filter enforces the hard line on top of this.

Env deps:
  - WAVESPEED_API_KEY        — same key as /banner_gen and /artistic_bg
"""
import os
import base64
import time
import asyncio
import logging

import requests
from telegram import Update
from telegram.ext import ContextTypes

# Reuse banner_gen's low-level WaveSpeed plumbing so we don't duplicate it
# and don't have to touch the existing /banner_gen.
from banner_gen import (
    _submit_and_poll_once, _looks_like_safety_block, MAX_ATTEMPTS,
    WAVESPEED_API_KEY, ENGINE,
)

logger = logging.getLogger(__name__)


# Expression wording — the defining element. Soft, calm, "neutral but
# innocent": understated, NOT a sultry pout or a big smile. Softened across
# retries to get past safety-filter edge cases without changing the look.
EXPRESSION_LEVELS = [
    "soft, calm, neutral-yet-innocent expression — relaxed face, lips softly "
    "together or barely parted, a gentle direct gaze straight into the camera. "
    "NOT a big smile, NOT a sultry pout, NOT an exaggerated 'sexy' face — "
    "understated, natural, a little innocent and disarming, like a candid "
    "look caught mid-moment",
    "soft calm expression — relaxed neutral face, gentle eyes looking at the "
    "camera, lips lightly together, natural and understated",
    "calm gentle expression — soft neutral face, kind eyes, relaxed and "
    "natural",
]


def _build_prompt(softening_level: int = 0) -> str:
    expression = EXPRESSION_LEVELS[min(softening_level, len(EXPRESSION_LEVELS) - 1)]
    return (
        "Generate a wide-format banner image of the SAME person from the "
        "reference image. Preserve her face, hair color and texture, eye "
        "color, complexion, and overall identity EXACTLY — do not change her "
        "facial features or proportions.\n"
        "\n"
        "AGE — CRITICAL: she is a mature ADULT woman in her mid-to-late "
        "twenties, clearly of legal age, with a grown adult woman's face and "
        "figure. Do NOT make her look youthful, teenage, schoolgirl, or "
        "childlike in any way.\n"
        "\n"
        "WARDROBE: render her in a tasteful matching lingerie set (bra and "
        "cheeky briefs), suggestive but NOT explicit. She is CLOTHED in the "
        "lingerie — this is NOT a nude image. Breasts and buttocks remain "
        "covered by the lingerie; no exposed nipples, no exposed genitals, "
        "nothing uncovered. Think a lingerie-brand promo photo, not "
        "pornography.\n"
        "\n"
        "POSE / FRAMING (this is the signature composition — read carefully): "
        "she lies on her front on a made bed, weight resting on her forearms. "
        "Her head, shoulders and face are in the FOREGROUND filling the LEFT "
        "portion of the frame, turned to look back over toward the camera. "
        "Her back gently arches so her hips and lingerie-covered behind rise "
        "into the UPPER-RIGHT of the frame, visible in the soft background "
        "behind her shoulder — present in the composition but not the sharp "
        "focal point. The behind is fully covered by the briefs: suggestive, "
        "implied, never explicit. " + expression + ".\n"
        "\n"
        "SETTING: a regular modern bedroom, white or off-white bedding, soft "
        "natural daylight from a window. Lived-in and casual, not a studio or "
        "hotel suite.\n"
        "\n"
        "CAMERA / PHOTOGRAPHY STYLE — CRITICAL (this is what makes it look "
        "REAL instead of a Photoshopped studio shot):\n"
        "  • Smartphone selfie/front-camera look ONLY — NOT professional "
        "photography, NOT a magazine shoot.\n"
        "  • ABSOLUTELY NO bokeh, NO depth-of-field blur, NO heavy background "
        "blur. Foreground and background at roughly the same sharpness.\n"
        "  • NO cinematic lighting, NO dramatic shadows, NO color grading, NO "
        "film filter, NO HDR, NO professional retouching, NO skin smoothing.\n"
        "  • Just plain ambient natural daylight from a window.\n"
        "  • Slight imperfections are GOOD: a touch of sensor noise, slightly "
        "imperfect framing, natural skin texture (visible pores, slight "
        "unevenness — NOT airbrushed), a stray hair or two.\n"
        "  • Photorealistic, candid, Instagram-feed feel — like a casual phone "
        "photo she took herself.\n"
        "\n"
        "COMPOSITION:\n"
        "  • Wide banner aspect ratio.\n"
        "  • Face fills the left third in sharp focus; hip/behind sits in the "
        "upper-right background. Frame fully occupied edge-to-edge — NO empty "
        "negative space, no blank walls or ceiling.\n"
        "  • No letterboxing, no borders, no margins."
    )


def _generate_nsfw_banner(local_ref_path, progress_cb=None):
    """Mirror of banner_gen._generate_banner but with the NSFW prompt builder.
    Returns (local_path, err, attempts_used, was_blocked)."""
    if not WAVESPEED_API_KEY:
        return None, "WAVESPEED_API_KEY not set", 0, False

    try:
        with open(local_ref_path, 'rb') as f:
            b = f.read()
    except Exception as e:
        return None, f"can't read reference: {e}", 0, False
    ext = os.path.splitext(local_ref_path)[1].lstrip('.').lower() or 'jpg'
    mime_by_ext = {'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png',
                   'webp':'image/webp','heic':'image/heic','heif':'image/heif'}
    mt = mime_by_ext.get(ext, 'image/jpeg')
    data_uri = f"data:{mt};base64,{base64.b64encode(b).decode('ascii')}"

    last_err = None
    was_blocked = False
    out_url = None
    used_ratio = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        soften = max(0, attempt - 2)
        prompt_text = _build_prompt(soften)
        if progress_cb:
            try: progress_cb(attempt, 'submitting' if attempt == 1 else
                              ('retry (soft prompt)' if soften else 'retry'))
            except Exception: pass
        url, ratio, err, blocked = _submit_and_poll_once(data_uri, prompt_text)
        if url:
            out_url = url; used_ratio = ratio
            logger.info(f"[nsfw_banner] succeeded on attempt {attempt}/{MAX_ATTEMPTS} (soften={soften})")
            break
        last_err = err
        was_blocked = was_blocked or blocked
        logger.warning(f"[nsfw_banner] attempt {attempt}/{MAX_ATTEMPTS} failed (blocked={blocked}): {err}")
        if not blocked:
            if attempt >= 2:
                break
            time.sleep(5)
            continue
        time.sleep(2)

    if not out_url:
        return None, last_err or 'unknown failure', attempt, was_blocked

    try:
        img_r = requests.get(out_url, timeout=60); img_r.raise_for_status()
    except Exception as e:
        return None, f"output download err: {e}", attempt, was_blocked
    ts = time.strftime('%Y%m%d_%H%M%S')
    local = f"/tmp/nsfw_banner_{ts}.jpg"
    with open(local, 'wb') as f: f.write(img_r.content)
    logger.info(f"[nsfw_banner] saved {local} (aspect={used_ratio}, attempt={attempt})")
    return local, None, attempt, was_blocked


async def nsfw_banner_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['expecting_nsfw_photo'] = True
    await update.message.reply_text(
        "🔥 *NSFW banner generator*\n\n"
        "Send a reference photo of your model (one image showing her "
        "face/identity).\n\n"
        "I'll recreate her as a wide OnlyFans-style banner: face in the "
        "foreground looking back at the camera, lingerie, hip/behind in the "
        "background — soft neutral-innocent expression. Suggestive, not "
        "explicit (the engine's safety filter enforces the hard line).\n\n"
        "Engine: `nano-banana-pro` · Aspect: 21:9 · Cost: ~$0.10",
        parse_mode='Markdown')


async def nsfw_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Photo-router entry. Returns True if handled; False otherwise."""
    if not context.user_data.get('expecting_nsfw_photo'):
        return False
    context.user_data.pop('expecting_nsfw_photo', None)

    photo = (update.message.photo or [None])[-1]
    if not photo:
        await update.message.reply_text("❌ no photo in that message. Run /nsfw_banner again.")
        return True

    try:
        f = await context.bot.get_file(photo.file_id)
        local_ref = f"/tmp/nsfw_ref_{photo.file_unique_id}.jpg"
        await f.download_to_drive(local_ref)
    except Exception as e:
        await update.message.reply_text(f"❌ couldn't download photo: `{e}`",
                                          parse_mode='Markdown')
        return True

    await update.message.reply_text(
        f"🤖 generating NSFW banner "
        f"(up to {MAX_ATTEMPTS} attempts × ~60-120s; safety-filter blocks "
        f"trigger an automatic softer-prompt retry)…",
        parse_mode='Markdown')

    loop = asyncio.get_running_loop()
    def _progress(attempt, status):
        try:
            asyncio.run_coroutine_threadsafe(
                update.message.reply_text(
                    f"   ⏳ attempt {attempt}/{MAX_ATTEMPTS} — {status}",
                    parse_mode='Markdown'),
                loop)
        except Exception as e:
            logger.warning(f"[nsfw_banner] progress msg err: {e}")

    local_out, err, attempts, was_blocked = await asyncio.to_thread(
        _generate_nsfw_banner, local_ref, _progress)

    if err:
        if was_blocked:
            await update.message.reply_text(
                f"🚫 *Blocked by nano-banana-pro's safety filter*\n"
                f"Tried {attempts}/{MAX_ATTEMPTS} times with progressively "
                f"softer prompts — all flagged. This prompt is closer to the "
                f"filter's line than /banner_gen, so blocks are more common.\n\n"
                f"• Try a less revealing reference photo\n"
                f"• Or retry — the filter is non-deterministic\n\n"
                f"Last error: `{err[:200]}`",
                parse_mode='Markdown')
        else:
            await update.message.reply_text(
                f"❌ generation failed after {attempts} attempt(s): `{err[:300]}`",
                parse_mode='Markdown')
        return True

    try:
        with open(local_out, 'rb') as fh:
            caption = (f"✅ NSFW banner generated"
                       f"{f' (took {attempts} attempts)' if attempts > 1 else ''}\n"
                       f"Engine: `{ENGINE}` · Saved: `{local_out}`")
            await update.message.reply_photo(photo=fh, caption=caption,
                                              parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"⚠️ generated but TG send failed: `{e}`",
                                          parse_mode='Markdown')
    return True
