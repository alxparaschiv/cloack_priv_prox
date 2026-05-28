"""accounts_sheet.py — Drive-backed audit log for FB-account-into-GoLogin
events. Stores as a CSV file at the Drive root: 'acc-setup-bot · accounts.csv'.

Why CSV-in-Drive instead of a native Google Sheet:
  - User's Carolina OAuth token only has 'drive' scope (not 'spreadsheets')
  - Their Cloud project doesn't have the Sheets API enabled
  - CSV in Drive works TODAY with no user setup. The user can open the
    file via Drive's "Open with → Google Sheets" anytime to view/edit
    as a real spreadsheet — Drive does the conversion on the fly.

Columns: Timestamp(UTC), GoLogin Profile, FB Email, FB Profile ID,
         Proxy host:port, IPRoyal Session, Status, Notes

Each append fetches the current CSV, adds the row, uploads the new content
back. Cheap for our use case (~1 row per Meta-Dev setup)."""
import os
import base64
import pickle
import logging
import datetime
import re
import io
import csv

logger = logging.getLogger(__name__)

CSV_FILENAME = 'acc-setup-bot · accounts.csv'
HEADER = ['Timestamp (UTC)', 'GoLogin Profile', 'FB Email', 'FB Profile ID',
          'Proxy host:port', 'IPRoyal Session', 'Status', 'Notes', 'Full Blob']

_CACHED_CREDS = None
_CACHED_FILE_ID = None


def _load_creds():
    global _CACHED_CREDS
    if _CACHED_CREDS is not None:
        return _CACHED_CREDS
    token_env = os.getenv('GOOGLE_TOKEN_PICKLE')
    if not token_env:
        logger.warning("[accounts_sheet] GOOGLE_TOKEN_PICKLE not set")
        return None
    try:
        creds = pickle.loads(base64.b64decode(token_env))
        from google.auth.transport.requests import Request
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        _CACHED_CREDS = creds
        return creds
    except Exception as e:
        logger.warning(f"[accounts_sheet] failed to load creds: {e}")
        return None


def _drive_service():
    creds = _load_creds()
    if not creds:
        return None
    from googleapiclient.discovery import build
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def _csv_bytes(rows):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode('utf-8')


def _parse_csv(content_bytes):
    if not content_bytes:
        return []
    buf = io.StringIO(content_bytes.decode('utf-8', 'replace'))
    return list(csv.reader(buf))


def _find_or_create_file():
    """Locate the CSV by name at Drive root, or create it with header row.
    Returns the file ID."""
    global _CACHED_FILE_ID
    if _CACHED_FILE_ID:
        return _CACHED_FILE_ID
    drive = _drive_service()
    if drive is None:
        return None
    q = (f"name='{CSV_FILENAME}' and "
         f"mimeType='text/csv' and trashed=false")
    res = drive.files().list(q=q, fields='files(id,name,parents)',
                             supportsAllDrives=True,
                             includeItemsFromAllDrives=True).execute()
    files = res.get('files') or []
    if files:
        fid = files[0]['id']
        logger.info(f"[accounts_sheet] reusing existing CSV {fid}")
        _CACHED_FILE_ID = fid
        return fid
    # Create with just the header row
    from googleapiclient.http import MediaInMemoryUpload
    body = {'name': CSV_FILENAME, 'mimeType': 'text/csv'}
    media = MediaInMemoryUpload(_csv_bytes([HEADER]), mimetype='text/csv')
    created = drive.files().create(body=body, media_body=media,
                                   fields='id', supportsAllDrives=True).execute()
    fid = created['id']
    logger.info(f"[accounts_sheet] created CSV {fid}")
    _CACHED_FILE_ID = fid
    return fid


def _read_all_rows():
    drive = _drive_service()
    fid = _find_or_create_file()
    if not (drive and fid):
        return []
    content = drive.files().get_media(fileId=fid).execute()
    return _parse_csv(content)


def upsert_entry(profile_name, fb_email, fb_profile_id, proxy_host_port,
                 proxy_session, status='cookie_persisted', notes='',
                 full_blob=''):
    """One row per (GoLogin Profile, FB Email). If a row with that pair
    already exists, UPDATE it in place (preserving the longest Full Blob seen
    so the canonical cookie-bearing blob doesn't get truncated by a later
    short call). Otherwise insert a new row at the end.

    Notes are appended (chronological, ' | ' separated, deduped) rather than
    overwritten so we keep a short audit trail in the same row.

    Returns the row index (1-based excluding header) on success, None on error."""
    drive = _drive_service()
    fid = _find_or_create_file()
    if not (drive and fid):
        return None
    rows = _read_all_rows()
    if not rows or rows[0] != HEADER:
        rows = [HEADER] + [
            (r + ['']*(len(HEADER) - len(r))) if r and r != HEADER else r
            for r in rows if r and r != HEADER
        ]
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    # Find an existing row for (profile_name, fb_email)
    target = None
    for i, r in enumerate(rows[1:], start=1):
        if len(r) >= 3 and r[1] == (profile_name or '') and r[2] == (fb_email or ''):
            target = i
            break

    if target is None:
        rows.append([ts, profile_name or '', fb_email or '', fb_profile_id or '',
                     proxy_host_port or '', proxy_session or '',
                     status or '', notes or '', full_blob or ''])
        idx = len(rows) - 1
        op = 'inserted'
    else:
        existing = rows[target] + ['']*(len(HEADER) - len(rows[target]))
        prior_notes = (existing[7] or '').strip()
        new_notes = (notes or '').strip()
        if new_notes and new_notes not in prior_notes:
            combined = f"{prior_notes} | {new_notes}" if prior_notes else new_notes
        else:
            combined = prior_notes
        prior_blob = existing[8] or ''
        new_blob = full_blob or ''
        kept_blob = new_blob if len(new_blob) > len(prior_blob) else prior_blob
        rows[target] = [
            ts, profile_name or existing[1], fb_email or existing[2],
            fb_profile_id or existing[3],
            proxy_host_port or existing[4],
            proxy_session or existing[5],
            status or existing[6],
            combined,
            kept_blob,
        ]
        idx = target
        op = 'updated'

    from googleapiclient.http import MediaInMemoryUpload
    media = MediaInMemoryUpload(_csv_bytes(rows), mimetype='text/csv')
    drive.files().update(fileId=fid, media_body=media,
                         supportsAllDrives=True).execute()
    logger.info(f"[accounts_sheet] {op} row {idx} for {profile_name}/{fb_email}")
    return idx


def append_entry(*args, **kwargs):
    """Backwards-compatible shim — delegates to upsert_entry so callers that
    used to append-only now safely update-in-place per (profile, email)."""
    return upsert_entry(*args, **kwargs)


def get_file_url():
    fid = _find_or_create_file()
    return f'https://drive.google.com/file/d/{fid}/view' if fid else None


def get_proxy_info_from_gologin_profile(profile_id, gologin_api_key):
    import requests
    try:
        r = requests.get(f'https://api.gologin.com/browser/{profile_id}',
                         headers={'Authorization': f'Bearer {gologin_api_key}'},
                         timeout=20)
        prx = r.json().get('proxy') or {}
        host = prx.get('host') or ''
        port = prx.get('port') or ''
        pw = prx.get('password', '') or ''
        m = re.search(r'_session-([A-Za-z0-9]+)', pw)
        return f'{host}:{port}' if host else '', m.group(1) if m else ''
    except Exception as e:
        logger.warning(f"[accounts_sheet] proxy lookup failed: {e}")
        return '', ''
