"""Drive image picker with GPT-4o vision filter.

Used by /ig_setup_private (Shard C): scan a Drive folder of candidate
photos, ask GPT-4o-mini for each "is a face visible? seducing/cleavage
score 1-10?", filter face-visible ones out, rank by score, send the top
N as Telegram thumbnails for the user to pick the final profile picture.

Per user spec (2026-06-04):
  - Face MUST NOT be visible in the picked photo (we use vision, not OpenCV)
  - Prefer 'teasing / cleavage / seducing body forms' (vision-judged 1-10)
  - Present ~10-15 candidates back to the user via Telegram
  - User picks one, that one becomes the IG profile picture

Cost note: each GPT-4o-mini image call is ~$0.005. A 50-image folder
scan = ~$0.25. Caller should ideally cap the candidate pool to a
reasonable number.
"""
import os
import io
import base64
import json
import logging
import pickle

import requests
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
MAX_CANDIDATES_PRESENTED = 12       # show this many to the user max
MAX_IMAGES_TO_SCAN = 30             # don't scan more than this in one folder (cost cap)
OPENAI_MODEL = 'gpt-4o-mini'        # vision-capable, cheap (~$0.005/image)


def _drive_service():
    """Build a Drive v3 service from GOOGLE_TOKEN_PICKLE env (same pattern
    as master_account_create.py)."""
    creds = pickle.loads(base64.b64decode(os.environ['GOOGLE_TOKEN_PICKLE']))
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def _find_folder_by_name(svc, query, parent_id=None):
    """Find a Drive folder whose name CONTAINS `query` (case-insensitive substring).
    Returns the FIRST match by createdTime asc, or None.

    If multiple folders match the query, we pick the OLDEST (mirrors
    reel_bot.py's deterministic resolver — see _drive_get_or_create_path
    rationale: prevents the resolver oscillating between duplicates).
    """
    q_parts = [
        "mimeType='application/vnd.google-apps.folder'",
        "trashed=false",
    ]
    if parent_id:
        q_parts.append(f"'{parent_id}' in parents")
    # Drive's name search supports `contains` for partial match
    q_parts.append(f"name contains '{query}'")
    q = " and ".join(q_parts)
    try:
        res = svc.files().list(
            q=q, fields='files(id,name,createdTime)',
            orderBy='createdTime', pageSize=20,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
    except Exception as e:
        logger.warning(f"[drive_picker] folder search err: {e}")
        return None
    items = res.get('files') or []
    if not items:
        return None
    # Case-insensitive sub-filter on the client side (Drive's `contains` is
    # case-sensitive on word tokens, not free substring — be lenient here)
    target = query.lower()
    matches = [it for it in items if target in (it.get('name') or '').lower()]
    if not matches:
        matches = items  # fall back to whatever the API said matched
    return matches[0]


def _list_images_in_folder(svc, folder_id):
    """List all image files (JPEG/PNG/HEIC) directly inside the folder.
    Returns a list of {id, name, mimeType, size}."""
    all_imgs = []
    page_token = None
    while True:
        q = (f"'{folder_id}' in parents and trashed=false and "
             f"(mimeType contains 'image/' or "
             f"name contains '.jpg' or name contains '.jpeg' or name contains '.png')")
        res = svc.files().list(
            q=q, fields='nextPageToken, files(id,name,mimeType,size)',
            pageSize=100, pageToken=page_token,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        for f in res.get('files') or []:
            mt = (f.get('mimeType') or '').lower()
            nm = (f.get('name') or '').lower()
            if mt.startswith('image/') or nm.endswith(('.jpg', '.jpeg', '.png', '.heic')):
                all_imgs.append(f)
        page_token = res.get('nextPageToken')
        if not page_token:
            break
    return all_imgs


def _download_image_bytes(svc, file_id):
    """Download a Drive image into bytes. Returns bytes or None."""
    try:
        buf = io.BytesIO()
        request = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"[drive_picker] download {file_id} err: {e}")
        return None


def _vision_score(img_bytes):
    """Ask GPT-4o-mini: face visible? seducing/cleavage score 1-10?
    Returns dict {face_visible: bool, score: int, reason: str} or None on err.
    """
    if not OPENAI_API_KEY:
        return None
    b64 = base64.b64encode(img_bytes).decode('ascii')
    body = {
        'model': OPENAI_MODEL,
        'temperature': 0.1,
        'max_tokens': 120,
        'messages': [
            {
                'role': 'system',
                'content': (
                    "You are scoring a candidate profile photo for an adult-leaning "
                    "Instagram account. Reply with STRICT JSON only, no prose: "
                    '{"face_visible": true|false, "score": 1-10, "reason": "<one short phrase>"}. '
                    "face_visible is true if any part of the face above the chin is clearly "
                    "identifiable (eyes/nose/mouth visible). score: 1=plain/no body emphasis, "
                    "10=very seducing/teasing body framing (cleavage, curves, suggestive posture). "
                    "Reject (face_visible=true) any image showing eyes, nose, or mouth — "
                    "even partially. Background/non-person photos = face_visible:false, score:1."
                ),
            },
            {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': 'Score this candidate.'},
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}},
                ],
            },
        ],
    }
    try:
        r = requests.post(
            'https://api.openai.com/v1/chat/completions',
            headers={'Authorization': f'Bearer {OPENAI_API_KEY}'},
            json=body, timeout=60,
        )
        r.raise_for_status()
        content = r.json()['choices'][0]['message']['content'].strip()
        # Tolerate models that wrap JSON in code fences
        if content.startswith('```'):
            content = content.strip('`')
            if content.startswith('json'):
                content = content[4:].strip()
        return json.loads(content)
    except Exception as e:
        logger.warning(f"[drive_picker] vision err: {e}")
        return None


# ─── Telegram-facing entry point ────────────────────────────────────────────

async def pick_from_drive_folder(update, context, folder_query, on_pick):
    """Run the full scan→filter→present flow.

    folder_query : substring of the Drive folder name to search for
    on_pick      : async callback (file_local_path) → None — called after
                   the user selects one image. Caller (the wizard) provides
                   what to do next.

    Stores per-message state in context.chat_data['drive_picker_candidates']
    keyed by the inline keyboard button payload, so the callback handler
    can resolve a click back to a local file path.
    """
    await update.message.reply_text(
        f"🔎 searching Drive for a folder matching `{folder_query}`…",
        parse_mode='Markdown')

    svc = _drive_service()
    folder = _find_folder_by_name(svc, folder_query)
    if not folder:
        await update.message.reply_text(
            f"❌ no Drive folder found matching `{folder_query}`. "
            f"Try a different keyword or pick a different source.",
            parse_mode='Markdown')
        return False

    await update.message.reply_text(
        f"📁 folder: `{folder['name']}` ({folder['id']}). Listing images…",
        parse_mode='Markdown')

    images = _list_images_in_folder(svc, folder['id'])
    if not images:
        await update.message.reply_text(
            f"⚠️ folder `{folder['name']}` has no images.",
            parse_mode='Markdown')
        return False

    pool = images[:MAX_IMAGES_TO_SCAN]
    await update.message.reply_text(
        f"🤖 scanning {len(pool)} images with GPT-4o (filter: face NOT visible, "
        f"rank: seducing 1-10). ~{len(pool)*0.005:.2f} USD, ~{len(pool)*2}s…",
        parse_mode='Markdown')

    scored = []
    for img in pool:
        b = _download_image_bytes(svc, img['id'])
        if not b:
            continue
        v = _vision_score(b)
        if not v:
            continue
        scored.append({
            'id': img['id'], 'name': img['name'],
            'face': bool(v.get('face_visible')),
            'score': int(v.get('score') or 0),
            'reason': v.get('reason', ''),
            'bytes': b,
        })

    if not scored:
        await update.message.reply_text("❌ vision scoring failed on every image. Aborting.")
        return False

    # Filter: face must NOT be visible. Rank by seducing-score desc.
    safe = [s for s in scored if not s['face']]
    if not safe:
        await update.message.reply_text(
            f"⚠️ all {len(scored)} scanned images had a visible face. "
            f"Pick a different folder or upload manually.",
            parse_mode='Markdown')
        return False
    safe.sort(key=lambda s: s['score'], reverse=True)
    top = safe[:MAX_CANDIDATES_PRESENTED]

    await update.message.reply_text(
        f"📸 *{len(top)} candidates* (filtered {len(scored)-len(safe)} face-visible, "
        f"showing top by seducing-score):",
        parse_mode='Markdown')

    # Stash candidates in chat_data so the callback can find them
    context.chat_data['drive_picker_candidates'] = {}
    context.chat_data['drive_picker_on_pick'] = on_pick
    for i, s in enumerate(top):
        # Save bytes to /tmp so we can attach them to TG + retrieve later on pick
        local_path = f"/tmp/drive_pick_{s['id']}.jpg"
        with open(local_path, 'wb') as f:
            f.write(s['bytes'])
        context.chat_data['drive_picker_candidates'][str(i)] = {
            'local_path': local_path, 'score': s['score'], 'name': s['name'],
        }
        await update.message.reply_photo(
            photo=open(local_path, 'rb'),
            caption=f"#{i+1} — score {s['score']}/10 — {s['reason'][:60]}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"✅ Pick #{i+1}",
                                     callback_data=f"drive_pick:{i}")
            ]]),
        )
    return True


async def drive_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the inline-keyboard click after the user picks one candidate."""
    query = update.callback_query
    await query.answer()
    if not (query.data or '').startswith('drive_pick:'):
        return
    idx = query.data.split(':', 1)[1]
    cands = context.chat_data.get('drive_picker_candidates') or {}
    on_pick = context.chat_data.get('drive_picker_on_pick')
    chosen = cands.get(idx)
    if not chosen or not on_pick:
        await query.edit_message_caption(caption="⚠️ pick state expired — re-run the wizard.")
        return
    await query.edit_message_caption(
        caption=f"✅ picked #{int(idx)+1} (score {chosen['score']}/10) — {chosen['name']}")
    # Clear candidates so further clicks no-op
    context.chat_data.pop('drive_picker_candidates', None)
    context.chat_data.pop('drive_picker_on_pick', None)
    await on_pick(update, context, chosen['local_path'])
