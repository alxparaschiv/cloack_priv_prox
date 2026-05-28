"""accounts_sheet.py — Drive-backed audit log for every FB account loaded
into a GoLogin profile by acc-setup-bot.

Reuses Carolina's GOOGLE_TOKEN_PICKLE OAuth token (env var copied over).
On first call, creates a Google Sheet at the Drive root named
'acc-setup-bot · accounts'. Each row = one FB-account-into-GoLogin event:

   Timestamp(UTC) | GoLogin Profile | FB Email | FB Profile ID | Proxy host:port | IPRoyal Session | Status | Notes

Idempotent header row — first call writes it; subsequent calls just append data."""
import os
import base64
import pickle
import logging
import datetime
import re

logger = logging.getLogger(__name__)

SHEET_TITLE = 'acc-setup-bot · accounts'
HEADER = ['Timestamp (UTC)', 'GoLogin Profile', 'FB Email', 'FB Profile ID',
          'Proxy host:port', 'IPRoyal Session', 'Status', 'Notes']

_CACHED_CREDS = None
_CACHED_SHEET_ID = None


def _load_creds():
    """Load credentials from GOOGLE_TOKEN_PICKLE env var (same as Carolina).
    Returns Credentials object or None."""
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
    """Build a Drive v3 client (used to find/create the sheet file)."""
    creds = _load_creds()
    if not creds:
        return None
    from googleapiclient.discovery import build
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def _sheets_service():
    """Build a Sheets v4 client (used to read/append rows)."""
    creds = _load_creds()
    if not creds:
        return None
    from googleapiclient.discovery import build
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


def _find_or_create_sheet():
    """Locate the spreadsheet by title; create with header row if not found.
    Returns the spreadsheet ID."""
    global _CACHED_SHEET_ID
    if _CACHED_SHEET_ID:
        return _CACHED_SHEET_ID
    drive = _drive_service()
    if drive is None:
        return None
    # Search for an existing spreadsheet with our title
    q = (f"name='{SHEET_TITLE}' and "
         f"mimeType='application/vnd.google-apps.spreadsheet' and trashed=false")
    res = drive.files().list(q=q, fields='files(id,name)',
                             supportsAllDrives=True,
                             includeItemsFromAllDrives=True).execute()
    files = res.get('files') or []
    if files:
        sid = files[0]['id']
        logger.info(f"[accounts_sheet] reusing existing sheet {sid}")
        _CACHED_SHEET_ID = sid
        return sid
    # Create a fresh spreadsheet via the Sheets API (returns ID directly).
    sheets = _sheets_service()
    if sheets is None:
        return None
    body = {
        'properties': {'title': SHEET_TITLE},
        'sheets': [{'properties': {'title': 'accounts'}}],
    }
    resp = sheets.spreadsheets().create(body=body, fields='spreadsheetId').execute()
    sid = resp['spreadsheetId']
    # Write the header row
    sheets.spreadsheets().values().update(
        spreadsheetId=sid, range='accounts!A1:H1',
        valueInputOption='RAW',
        body={'values': [HEADER]}).execute()
    logger.info(f"[accounts_sheet] created new sheet {sid}")
    _CACHED_SHEET_ID = sid
    return sid


def append_entry(profile_name, fb_email, fb_profile_id, proxy_host_port,
                 proxy_session, status='cookie_persisted', notes=''):
    """Append one row. Safe to call repeatedly; the sheet auto-creates on
    the first call. Returns the new row index, or None on error."""
    sid = _find_or_create_sheet()
    if not sid:
        return None
    sheets = _sheets_service()
    if sheets is None:
        return None
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    row = [ts, profile_name or '', fb_email or '', fb_profile_id or '',
           proxy_host_port or '', proxy_session or '',
           status or '', notes or '']
    resp = sheets.spreadsheets().values().append(
        spreadsheetId=sid, range='accounts!A:H',
        valueInputOption='USER_ENTERED',
        insertDataOption='INSERT_ROWS',
        body={'values': [row]}).execute()
    updated_range = (resp.get('updates') or {}).get('updatedRange', '?')
    logger.info(f"[accounts_sheet] appended → {updated_range}")
    return updated_range


def get_sheet_url():
    sid = _find_or_create_sheet()
    if not sid: return None
    return f'https://docs.google.com/spreadsheets/d/{sid}/edit'


def get_proxy_info_from_gologin_profile(profile_id, gologin_api_key):
    """Look up the IPRoyal session embedded in the profile's proxy password.
    Returns (host_port:str, session_id:str) — both '' if unavailable."""
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
