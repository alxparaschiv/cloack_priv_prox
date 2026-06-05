"""/artistic_bg — Generate a new unique image whose style matches a folder
of reference images in Drive.

Engine: WaveSpeed `google/nano-banana-pro/edit` (Gemini 3 Pro Image,
~$0.07-0.20/img). The `images` array accepts multiple references — same
pattern reel_bot uses when it sends a model+pose pair. Picks 6 random
images from the reference folder, sends them all in the `images` array
with a prompt that asks for a new visually-distinct image in the same
aesthetic.

Standalone Telegram command first (this commit). The image wizard
integration lands next.

Output is saved to a single root Drive folder:
   `Images generated for account setup in GeeLark/`
   sub-folder per profile when invoked from the wizard;
   the standalone command saves directly into the root with a timestamped name.

Env deps:
  - WAVESPEED_API_KEY        — WaveSpeed AI bearer token
  - REEL_GOOGLE_TOKEN_PICKLE — Drive auth for reel-bot-Carolina's Drive (where
                               'Images bg goth artistic' + the output root live).
                               This is a SEPARATE pickle from GOOGLE_TOKEN_PICKLE
                               (which points at cloak's own Drive for Meta blobs
                               etc.) — we don't want the two getting tangled.
"""
import os
import io
import base64
import json
import time
import random
import logging
import pickle

import requests
from telegram import Update
from telegram.ext import ContextTypes

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

logger = logging.getLogger(__name__)

WAVESPEED_API_BASE = 'https://api.wavespeed.ai/api/v3'
WAVESPEED_API_KEY = os.environ.get('WAVESPEED_API_KEY', '')

REF_FOLDER_NAME    = 'Images bg goth artistic'
OUTPUT_ROOT_NAME   = 'Images generated for account setup in GeeLark'
REFS_PER_CALL      = 6
ENGINE             = 'nano-banana-pro'  # Gemini 3 Pro Image; ~$0.07-0.20/img

# Prompt designed for no-people aesthetic-matching backgrounds. The model
# tends to insert subjects unless explicitly told not to.
PROMPT = (
    "Create a new, completely unique artistic background image — visually "
    "DISTINCT from any single reference but in the same aesthetic family "
    "as the reference images: same color palette, same mood, same artistic "
    "treatment, same compositional feel. The new image must NOT copy or "
    "directly reproduce any single reference; combine and reinterpret their "
    "style into something fresh. No people, no faces, no human figures. "
    "Pure background art — abstract textures, surfaces, patterns, "
    "atmospheres, environments. Square 1:1 composition suitable for a "
    "profile background."
)


# ─── Drive ─────────────────────────────────────────────────────────────────

def _drive_service():
    """Build a Drive client using reel-bot-Carolina's pickle.

    REEL_GOOGLE_TOKEN_PICKLE is the SEPARATE pickle that authenticates against
    the Drive holding 'Images bg goth artistic' + 'Images generated for account
    setup in GeeLark'. We deliberately don't use cloak's own GOOGLE_TOKEN_PICKLE
    here — those are different Drives.
    """
    raw = os.environ.get('REEL_GOOGLE_TOKEN_PICKLE') \
          or os.environ.get('GOOGLE_TOKEN_PICKLE')   # fallback for local testing
    if not raw:
        raise RuntimeError("neither REEL_GOOGLE_TOKEN_PICKLE nor GOOGLE_TOKEN_PICKLE is set")
    creds = pickle.loads(base64.b64decode(raw))
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def _find_folder_by_name(svc, name, parent_id=None):
    q = [
        "mimeType='application/vnd.google-apps.folder'",
        "trashed=false",
        f"name='{name}'",
    ]
    if parent_id:
        q.append(f"'{parent_id}' in parents")
    res = svc.files().list(q=' and '.join(q),
                           fields='files(id,name,createdTime)',
                           orderBy='createdTime',
                           supportsAllDrives=True,
                           includeItemsFromAllDrives=True).execute()
    items = res.get('files') or []
    return items[0] if items else None


def _ensure_folder(svc, name, parent_id=None):
    """Find a folder by exact name (under optional parent), create if missing."""
    f = _find_folder_by_name(svc, name, parent_id)
    if f:
        return f['id']
    body = {'name': name, 'mimeType': 'application/vnd.google-apps.folder'}
    if parent_id:
        body['parents'] = [parent_id]
    f = svc.files().create(body=body, fields='id',
                           supportsAllDrives=True).execute()
    return f['id']


def _list_images_in_folder(svc, folder_id):
    out = []
    page_token = None
    while True:
        q = (f"'{folder_id}' in parents and trashed=false and "
             f"(mimeType contains 'image/' or "
             f"name contains '.jpg' or name contains '.jpeg' or name contains '.png')")
        res = svc.files().list(q=q, fields='nextPageToken, files(id,name,mimeType)',
                               pageSize=100, pageToken=page_token,
                               supportsAllDrives=True,
                               includeItemsFromAllDrives=True).execute()
        for f in res.get('files') or []:
            mt = (f.get('mimeType') or '').lower()
            nm = (f.get('name') or '').lower()
            if mt.startswith('image/') or nm.endswith(('.jpg', '.jpeg', '.png')):
                out.append(f)
        page_token = res.get('nextPageToken')
        if not page_token: break
    return out


def _download_image_bytes(svc, file_id):
    buf = io.BytesIO()
    request = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def _upload_bytes_to_drive(svc, parent_folder_id, file_name, content_bytes,
                            mime='image/jpeg'):
    media = MediaIoBaseUpload(io.BytesIO(content_bytes), mimetype=mime,
                              resumable=False)
    body = {'name': file_name, 'parents': [parent_folder_id]}
    res = svc.files().create(body=body, media_body=media, fields='id',
                             supportsAllDrives=True).execute()
    return res['id']


# ─── WaveSpeed ─────────────────────────────────────────────────────────────

def _wavespeed_submit_multi_ref(images_data_uris, prompt=PROMPT,
                                  engine=ENGINE,
                                  aspect_ratio='1:1', resolution='2k'):
    """Submit a multi-reference image-edit task to nano-banana-pro.
    Returns request_id or raises.

    Body shape matches reel_bot.wavespeed_submit_image_edit's nano-banana
    branch: aspect_ratio + resolution (NOT size). The images[] array can
    hold multiple data-URI references; nano-banana-pro reads all of them.
    """
    if not WAVESPEED_API_KEY:
        raise RuntimeError("WAVESPEED_API_KEY not set in env")
    url = f"{WAVESPEED_API_BASE}/google/{engine}/edit"
    body = {
        'prompt': prompt,
        'images': images_data_uris,   # list of data:image/jpeg;base64,... strings
        'aspect_ratio': aspect_ratio,
        'resolution': resolution,
        'output_format': 'jpeg',
        'enable_sync_mode': False,
        'enable_base64_output': False,
    }
    r = requests.post(url, json=body, timeout=60,
                      headers={'Authorization': f'Bearer {WAVESPEED_API_KEY}',
                               'Content-Type': 'application/json'})
    if r.status_code != 200:
        raise RuntimeError(f"WaveSpeed submit HTTP {r.status_code}: {r.text[:400]}")
    j = r.json()
    rid = (j.get('data') or {}).get('id') or j.get('id')
    if not rid:
        raise RuntimeError(f"WaveSpeed submit: no request id in response: {j}")
    return rid


def _wavespeed_wait(request_id, max_wait=180, poll_every=3):
    """Poll until the task completes. Returns the output image URL or raises."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        r = requests.get(f"{WAVESPEED_API_BASE}/predictions/{request_id}/result",
                         headers={'Authorization': f'Bearer {WAVESPEED_API_KEY}'},
                         timeout=30)
        r.raise_for_status()
        j = r.json()
        d = j.get('data') or j
        status = d.get('status') or ''
        if status in ('completed', 'succeeded'):
            outputs = d.get('outputs') or []
            if outputs:
                first = outputs[0]
                return first if isinstance(first, str) else first.get('url')
            raise RuntimeError("WaveSpeed completed but no outputs")
        if status == 'failed':
            raise RuntimeError(f"WaveSpeed failed: {j}")
        time.sleep(poll_every)
    raise RuntimeError(f"WaveSpeed timed out after {max_wait}s (request_id={request_id})")


# ─── Main flow ─────────────────────────────────────────────────────────────

def generate_artistic_bg(profile_subfolder_name=None, send_progress=None):
    """End-to-end: pick 6 refs → call WaveSpeed → download → save to Drive.

    profile_subfolder_name: when set, creates / reuses that subfolder under
      the OUTPUT_ROOT_NAME for the saved image. None = save directly in root.
    send_progress: optional callable(text) for streaming updates.
    Returns (drive_file_id, local_temp_path, error_or_None).
    """
    def emit(msg):
        logger.info(f"[artistic_bg] {msg}")
        if send_progress:
            try: send_progress(msg)
            except Exception: pass

    # 1. Find reference folder
    svc = _drive_service()
    ref_folder = _find_folder_by_name(svc, REF_FOLDER_NAME)
    if not ref_folder:
        return None, None, f"reference folder '{REF_FOLDER_NAME}' not found in Drive"
    emit(f"📁 ref folder: {REF_FOLDER_NAME} ({ref_folder['id']})")

    # 2. List + sample
    images = _list_images_in_folder(svc, ref_folder['id'])
    if len(images) < REFS_PER_CALL:
        return None, None, (f"reference folder has only {len(images)} images, "
                            f"need at least {REFS_PER_CALL}")
    chosen = random.sample(images, REFS_PER_CALL)
    emit(f"🎲 picked {REFS_PER_CALL} random refs: {[c['name'] for c in chosen]}")

    # 3. Download + encode as data URIs
    data_uris = []
    for f in chosen:
        b = _download_image_bytes(svc, f['id'])
        mt = (f.get('mimeType') or '').lower()
        if not mt.startswith('image/'):
            mt = 'image/jpeg'
        b64 = base64.b64encode(b).decode('ascii')
        data_uris.append(f"data:{mt};base64,{b64}")

    # 4. Submit + wait
    emit("🤖 submitting to WaveSpeed wan-2.7-pro (multi-ref)…")
    try:
        rid = _wavespeed_submit_multi_ref(data_uris)
        emit(f"⏳ request {rid} — polling for result (max ~3 min)…")
        out_url = _wavespeed_wait(rid)
    except Exception as e:
        return None, None, f"{type(e).__name__}: {e}"
    emit(f"✅ generation complete — downloading output")

    # 5. Fetch the generated image
    try:
        img_r = requests.get(out_url, timeout=60)
        img_r.raise_for_status()
        img_bytes = img_r.content
    except Exception as e:
        return None, None, f"output fetch err: {type(e).__name__}: {e}"

    # 6. Save locally + upload to Drive
    ts = time.strftime('%Y%m%d_%H%M%S')
    fname = f'artistic_bg_{ts}.jpg'
    local_path = f'/tmp/{fname}'
    with open(local_path, 'wb') as f: f.write(img_bytes)

    out_root_id = _ensure_folder(svc, OUTPUT_ROOT_NAME)
    parent_id = out_root_id
    if profile_subfolder_name:
        parent_id = _ensure_folder(svc, profile_subfolder_name, out_root_id)

    drive_id = _upload_bytes_to_drive(svc, parent_id, fname, img_bytes)
    emit(f"💾 saved to Drive: {OUTPUT_ROOT_NAME}/"
         f"{profile_subfolder_name + '/' if profile_subfolder_name else ''}{fname}")
    return drive_id, local_path, None


# ─── Telegram command ───────────────────────────────────────────────────────

async def artistic_bg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One-shot: pick 6 random refs from Drive folder, generate, save, send."""
    msg = update.message
    if not WAVESPEED_API_KEY:
        await msg.reply_text(
            "❌ `WAVESPEED_API_KEY` not set in env. Add it to Railway and redeploy.",
            parse_mode='Markdown')
        return

    await msg.reply_text(
        "🎨 *Artistic background generator*\n\n"
        f"Reference folder: `{REF_FOLDER_NAME}`\n"
        f"Engine: `{ENGINE}` (Gemini 3 Pro Image, ~$0.07-0.20/img)\n"
        f"Picking {REFS_PER_CALL} random refs + generating…",
        parse_mode='Markdown')

    sent_updates = []  # collect progress messages locally; reply each to user

    async def progress_async(text):
        await msg.reply_text(text, parse_mode='Markdown')

    def sync_progress(text):
        # Schedule the async TG send from a thread-safe boundary.
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            asyncio.run_coroutine_threadsafe(progress_async(text), loop)
        except Exception: pass

    import asyncio
    drive_id, local_path, err = await asyncio.to_thread(
        generate_artistic_bg, None, sync_progress)
    if err:
        await msg.reply_text(f"❌ `{err}`", parse_mode='Markdown')
        return

    # Send the generated image back so the user can see it
    try:
        with open(local_path, 'rb') as f:
            await msg.reply_photo(
                photo=f,
                caption=(f"✅ generated\n"
                         f"Drive id: `{drive_id}`\n"
                         f"Saved to: `{OUTPUT_ROOT_NAME}/`"),
                parse_mode='Markdown')
    except Exception as e:
        await msg.reply_text(f"⚠️ generated but TG photo send failed: {e}")
