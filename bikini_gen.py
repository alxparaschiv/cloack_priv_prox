"""/bikini_gen — Batch GOTH bikini image generator for a reference model.

No image upload: you pick one of your existing models (carolina, kira, …) and
the bot pulls that model's REFERENCE face from her `reference <Model>` Drive
folder, generates a BATCH of goth-themed swimwear images (5/10/15/20) in
varied dark settings, uploads them to a fresh Drive folder, returns the link.

GOTH AESTHETIC (matches the accounts' niche — no bright-daylight bikini shots):
  - Dark swimwear only: black / charcoal / very dark grey, goth-styled.
  - Dark backgrounds: night, sunset, dim rooms.
  - Night phone-FLASH look: hard on-camera flash on her, darkness behind —
    like a flash selfie taken on a dark beach at night.

Every image targets the same expression: neutral → innocent → slightly
teasing — NOT smiling, NOT laughing, NOT sad. Soft, direct gaze.

Reliability: nano-banana-pro's safety filter is PROBABILISTIC — the same
prompt is flagged on some tries and passes on others. So each image retries a
few times with progressive softening. Successful 2k generations can take
>300s, so the poll timeout is generous (a too-short timeout was silently
discarding finished images). Images generate CONCURRENTLY so retries don't
blow up the wall-clock.

Env deps:
  - WAVESPEED_API_KEY            — nano-banana-pro
  - REEL_GOOGLE_TOKEN_PICKLE     — Drive holding the model reference folders
"""
import time
import base64
import random
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import cloak
import artistic_bg_gen as _ag

logger = logging.getLogger(__name__)

# Wan 2.7 Image Edit Pro — multi-image reference edit with a MUCH laxer safety
# filter than nano-banana-pro. nano flagged a real two-piece bikini on this
# reference ~100% of the time; Wan passes it first-try. ~$0.075/img.
ENGINE = 'alibaba/wan-2.7/image-edit-pro'
WAN_SIZE = '1080*1920'         # 9:16 vertical (w*h); within Wan's pixel limits
REFS_PER_CALL = 3              # identity refs per generation (face lock)
COUNT_OPTIONS = [5, 10, 15, 20]
BATCH_MAX = 20
MAX_ATTEMPTS = 3              # retries per image (Wan passes first-try; this is for transient errors)
WAVE_MAX_WAIT = 300          # s — Wan edits typically finish in 1-3 min
MAX_WORKERS = 6              # images generated concurrently (they're I/O-bound polls)


def _wan_submit(data_uris, prompt):
    """Submit a Wan 2.7 image-edit-pro job. Returns request_id or raises."""
    url = f"{_ag.WAVESPEED_API_BASE}/{ENGINE}"
    body = {'images': data_uris, 'prompt': prompt, 'size': WAN_SIZE, 'seed': -1}
    r = requests.post(url, json=body, timeout=60,
                      headers={'Authorization': f'Bearer {_ag.WAVESPEED_API_KEY}',
                               'Content-Type': 'application/json'})
    if r.status_code != 200:
        raise RuntimeError(f"Wan submit HTTP {r.status_code}: {r.text[:300]}")
    j = r.json()
    rid = (j.get('data') or {}).get('id') or j.get('id')
    if not rid:
        raise RuntimeError(f"Wan submit: no request id in {j}")
    return rid

# Dark / goth settings cycled across a batch — no bright daylight anywhere.
SETTINGS = [
    "on a dark beach at night, lit only by the harsh direct flash of her phone, "
    "black water and night sky behind her",
    "outdoors at sunset with a deep orange-to-dark sky, moody low light, dark "
    "silhouetted palms or dunes behind her",
    "a dim bedroom at night, lit mainly by her phone's flash, dark walls",
    "a dark bathroom mirror selfie at night, one dim light, hard phone flash",
    "on a balcony at night overlooking a dark sea, faint distant lights behind",
    "poolside at night in the dark, the pool glowing faintly, phone flash on her",
    "in a dark room lit by a single moody red or purple LED glow",
    "a night beach flash selfie, sand and darkness all around, strong on-camera flash",
]

# Expression — the defining element. Kept constant; softening below only
# changes wardrobe coverage / suggestiveness for safety retries.
EXPRESSION = (
    "a neutral, calm, innocent expression — soft relaxed face, lips softly "
    "together or barely parted, a calm direct gaze into the camera, relaxed "
    "eyebrows. Not smiling, not laughing, not sad — understated, natural and a "
    "little innocent, with a soft confident look in her eyes"
)

# The swimwear is ALWAYS a real TWO-PIECE bikini on EVERY attempt — retries do
# NOT soften toward more coverage. Two hard-won lessons:
#   1. Sexualizing words ("sexy/full bust/curvy/suggestive") spike the safety
#      filter to ~100% rejection, so the language is plain/wholesome.
#   2. Softening retries toward "sporty / athletic / more coverage" backfires:
#      the bikini (level-0) tries get flagged and only the covered retries
#      pass, so outputs came out as one-pieces / rashguard+leggings. So every
#      level stays a genuine two-piece bikini and explicitly forbids the
#      wardrobe failure modes. Lower yield, but what passes is actually a bikini.
BIKINI_LEVELS = [
    "wearing a black two-piece bikini — a bikini top and separate bikini "
    "bottoms — with subtle goth details (thin straps, small studs or lace "
    "trim). A normal beach bikini, two separate pieces. It is NOT a one-piece "
    "swimsuit, NOT a bodysuit, NOT a leotard, NOT a rashguard, NOT a crop top, "
    "NOT leggings",
    "wearing a simple black two-piece bikini — a triangle or halter bikini top "
    "and separate bikini bottoms. A normal beach bikini. It is NOT a one-piece, "
    "NOT a bodysuit, NOT a rashguard, NOT a crop top, NOT leggings",
    "wearing a classic plain black two-piece bikini — a bikini top and bikini "
    "bottoms, the ordinary kind worn at the beach. It is NOT a one-piece, NOT a "
    "bodysuit, NOT a rashguard, NOT a crop top, NOT leggings",
]


def _build_prompt(setting, softening_level=0):
    bikini = BIKINI_LEVELS[min(softening_level, len(BIKINI_LEVELS) - 1)]
    return (
        "Generate a new photo of the SAME woman shown in the reference images. "
        "Preserve her face, hair color and texture, eye color, complexion and "
        "overall identity EXACTLY — same person, do not change her features.\n"
        "\n"
        "AGE — CRITICAL: she is a mature ADULT woman in her mid-to-late "
        "twenties, clearly of legal age, with a grown adult woman's face and "
        "figure. Do NOT make her look youthful, teenage, schoolgirl, or "
        "childlike.\n"
        "\n"
        f"WARDROBE: {bikini}.\n"
        "\n"
        f"SETTING: {setting}.\n"
        "\n"
        "POSE / FRAMING — CRITICAL: a FULL-BODY vertical shot. Show her WHOLE "
        "body from head to at least the knees (ideally head to feet), standing. "
        "She FILLS the frame edge-to-edge — large in the frame, with NO big "
        "empty / negative space around her and no wide empty background. Either "
        "a full-length mirror selfie (phone visible in her hand) or an "
        "arm's-length full-body selfie. Relaxed, natural posture. " + EXPRESSION + ".\n"
        "\n"
        "LIGHTING / CAMERA — CRITICAL (goth night-flash look, makes it look "
        "REAL not a studio render):\n"
        "  • Shot on a PHONE at night or in the dark with the HARSH DIRECT "
        "ON-CAMERA FLASH — bright hard flash on her, falling off into deep "
        "shadow / darkness behind. Like a flash selfie on a dark beach at night.\n"
        "  • Dark, moody surroundings; the flash is the main light. Slight "
        "flash overexposure on the skin is realistic and good.\n"
        "  • ABSOLUTELY NOT bright daylight, NOT sunny, NOT a cheerful bright "
        "beach — dark / nighttime / sunset ONLY.\n"
        "  • Smartphone camera-flash look ONLY — NOT professional photography, "
        "NOT a magazine shoot. NO bokeh, NO depth-of-field blur, NO cinematic "
        "color grading, NO HDR, NO skin smoothing / retouching.\n"
        "  • Slight imperfections are GOOD: sensor noise, slightly imperfect "
        "framing, natural skin texture (visible pores — NOT airbrushed), a "
        "stray hair or two.\n"
        "  • Photorealistic, candid, Instagram-feed feel — a phone photo she "
        "took herself at night.\n"
        "\n"
        "COMPOSITION: vertical 9:16 Instagram-story aspect. Her body fills the "
        "frame top-to-bottom; minimal empty space, no letterboxing, no borders, "
        "no captions or text overlays."
    )


def _model_ref_folders(svc, model):
    """Return the model's REFERENCE face folder(s).

    Convention in Drive: the canonical reference face lives in a folder named
    `reference <Model>` (e.g. "reference Carolina"), NOT in folders that merely
    start with the model name — those (e.g. "Carolina Goth Nurse") are finished
    CONTENT/OUTPUT folders full of unrelated generated images, and sampling them
    produced faces that looked nothing like the model.

    Prefer the exact `reference <model>` folder. If it doesn't exist, fall back
    to any `reference <model> …` variant folder (some models only have variants
    like "reference Kira new goth"). Returns a list of {id, name}.
    """
    ml = model.lower().strip()
    q = ("mimeType='application/vnd.google-apps.folder' and trashed=false "
         f"and name contains 'reference {model}'")
    res = svc.files().list(
        q=q, fields='files(id,name)', pageSize=100,
        supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    files = res.get('files') or []
    exact = [f for f in files if (f.get('name') or '').lower() == f'reference {ml}']
    if exact:
        return exact
    pref = f'reference {ml}'
    return [f for f in files if (f.get('name') or '').lower().startswith(pref)]


def _is_safety_block(err_str):
    s = (err_str or '').lower()
    return 'flag' in s or 'sensitive' in s or 'safety' in s


def _build_data_uris(svc, pool):
    """Sample refs and return base64 data URIs for the generation."""
    chosen = random.sample(pool, min(REFS_PER_CALL, len(pool)))
    uris = []
    for f in chosen:
        b = _ag._download_image_bytes(svc, f['id'])
        mt = (f.get('mimeType') or '').lower()
        if not mt.startswith('image/'):
            mt = 'image/jpeg'
        uris.append(f"data:{mt};base64,{base64.b64encode(b).decode('ascii')}")
    return uris


def _gen_one_bikini(svc, data_uris, parent_id, setting, idx, count, emit):
    """Generate one goth-bikini image (with safety retries) → upload to Drive.
    Returns (drive_id, err). `data_uris` are the ref images (already encoded)."""
    out_url = None
    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # Softening ramps up only after a safety flag, so the first tries keep
        # the full look and we only concede coverage when forced to.
        soften = max(0, attempt - 1)
        prompt = _build_prompt(setting, soften)
        try:
            rid = _wan_submit(data_uris, prompt)
            out_url = _ag._wavespeed_wait(rid, max_wait=WAVE_MAX_WAIT)
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            tag = "🚫 flagged" if _is_safety_block(last_err) else "⚠️"
            logger.info(f"[bikini] {idx}/{count} attempt {attempt}/{MAX_ATTEMPTS} {tag}: {last_err}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(5)
    if not out_url:
        return None, f"all {MAX_ATTEMPTS} attempts failed; last: {last_err}"

    img = None
    for _ in range(3):                       # output fetch can drop; retry a few times
        try:
            r = requests.get(out_url, timeout=90); r.raise_for_status()
            img = r.content
            break
        except Exception as e:
            last_err = f"output fetch err: {e}"
            time.sleep(3)
    if img is None:
        return None, last_err
    ts = time.strftime('%Y%m%d_%H%M%S')
    fname = f"bikini_{_setting_tag(setting)}_{ts}_{random.randint(1000,9999)}.jpg"
    drive_id = _ag._upload_bytes_to_drive(svc, parent_id, fname, img)
    return drive_id, None


def _setting_tag(setting):
    # short filename hint from the setting (first word), best-effort
    return (setting.split(' ')[0] or 'goth').strip().lower()


def generate_bikini_batch(model, count, emit):
    """Resolve model refs → generate `count` goth-bikini images CONCURRENTLY
    into a fresh Drive folder. Returns (batch_url, ok_count, errors)."""
    svc = _ag._drive_service()
    folders = _model_ref_folders(svc, model)
    if not folders:
        return None, 0, [f"no reference folders found for '{model}' in Drive"]
    pool = []
    for f in folders:
        try:
            pool.extend(_ag._list_images_in_folder(svc, f['id']))
        except Exception:
            pass
    if len(pool) < 1:
        return None, 0, [f"no reference images found for '{model}' — expected a "
                         f"'reference {model}' folder in Drive with at least 1 face photo"]
    emit(f"📁 {len(folders)} ref folder(s) · {len(pool)} ref image(s) for {model}")

    # Encode refs ONCE (they're the same across the whole batch).
    data_uris = _build_data_uris(svc, pool)

    root = _ag._ensure_folder(svc, _ag.OUTPUT_ROOT_NAME)
    ts = time.strftime('%Y%m%d_%H%M%S')
    batch_id = _ag._ensure_folder(svc, f"bikini_{model}_{ts}", root)
    url = _ag._folder_drive_url(batch_id)

    def _worker(i):
        # Each thread gets its OWN Drive service — googleapiclient/httplib2 is
        # not thread-safe, so sharing one `svc` across workers would corrupt it.
        svc_t = _ag._drive_service()
        setting = SETTINGS[(i - 1) % len(SETTINGS)]
        try:
            _id, err = _gen_one_bikini(svc_t, data_uris, batch_id, setting, i, count, emit)
            return i, _id, err
        except Exception as e:
            return i, None, f"{type(e).__name__}: {e}"

    emit(f"🖤 generating {count} goth-bikini images (up to {MAX_WORKERS} at a time)…")
    ok, errs = 0, []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, count)) as ex:
        futs = [ex.submit(_worker, i) for i in range(1, count + 1)]
        for fut in as_completed(futs):
            i, _id, err = fut.result()
            if err:
                errs.append(f"{i}: {err}")
                emit(f"❌ {i}/{count} failed — {'safety-flagged all retries' if _is_safety_block(err) else err[:90]}")
            else:
                ok += 1
                emit(f"✅ {ok}/{count} done")
    return url, ok, errs


# ─── Telegram flow ─────────────────────────────────────────────────────────

def _model_kb():
    rows = []
    try:
        for m in (cloak._known_models() or []):
            rows.append([InlineKeyboardButton(f"👤 {m}", callback_data=f"bikini:model:{m}")])
    except Exception as e:
        logger.warning(f"[bikini] model list err: {e}")
    rows.append([InlineKeyboardButton("✖ cancel", callback_data="bikini:cancel")])
    return InlineKeyboardMarkup(rows)


def _count_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{n}", callback_data=f"bikini:count:{n}") for n in COUNT_OPTIONS],
        [InlineKeyboardButton("✖ cancel", callback_data="bikini:cancel")],
    ])


async def bikini_gen_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🖤 *Goth bikini batch generator*\n\n"
        "Batch of *goth-themed* two-piece bikini IG-story images of one of your "
        "models (her face pulled from her `reference <model>` Drive folder — no "
        "upload). Dark/black bikini, night · sunset · dark settings, phone "
        "night-flash look, full-body framing, soft neutral-innocent expression. "
        "Uploaded to a Drive folder.\n\n"
        f"Engine: `Wan 2.7 Image Edit Pro` · ~$0.075/img · runs concurrently. "
        "A few minutes per batch.\n\n"
        "Pick a model 👇",
        parse_mode='Markdown', reply_markup=_model_kb())


async def bikini_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(':')
    action = parts[1] if len(parts) > 1 else ''

    if action == 'cancel':
        await q.edit_message_text("✖ cancelled.")
        return

    if action == 'model':
        model = parts[2]
        context.user_data['bikini_model'] = model
        await q.edit_message_text(
            f"👤 *{model}* selected.\n\nHow many images?",
            parse_mode='Markdown', reply_markup=_count_kb())
        return

    if action == 'count':
        model = context.user_data.get('bikini_model')
        if not model:
            await q.edit_message_text("⚠️ session expired — run /bikini_gen again.")
            return
        count = min(BATCH_MAX, int(parts[2]))
        await q.edit_message_text(
            f"🖤 generating *{count}* goth-bikini images for *{model}*…\n\n"
            f"Running up to {MAX_WORKERS} at once (Wan 2.7). A few minutes — "
            f"I'll post the Drive link when it's done; you can keep using the bot.",
            parse_mode='Markdown')

        loop = asyncio.get_running_loop()
        chat = q.message.chat

        def emit(m):
            try:
                asyncio.run_coroutine_threadsafe(chat.send_message(m), loop)
            except Exception:
                pass

        url, ok, errs = await asyncio.to_thread(generate_bikini_batch, model, count, emit)

        if not url:
            await chat.send_message(f"❌ couldn't start: {errs[0] if errs else 'unknown'}")
            return
        tail = ""
        if errs:
            tail = f"\n⚠️ {len(errs)} failed (e.g. {errs[0][:120]})"
        await chat.send_message(
            f"🎉 *Done* — {ok}/{count} images for *{model}*\n"
            f"📂 [Open Drive folder]({url})\n`{url}`{tail}",
            parse_mode='Markdown', disable_web_page_preview=True)
