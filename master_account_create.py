"""Master flow for ONE account end-to-end.

Stages:
  1. Setup: fresh IPRoyal proxy + cookies push + session start
  2. FB login confirm
  3. Dev portal → Get Started → Register Continue
  4. Verify Account: type phone, snapshot SMS, Send, poll NEW, type code, Continue
       — If "You can only complete this action in Accounts Center" → AC bind detour
  5. Review Email → Confirm Email (cookies skip the code)
  6. Contact info auto-advance
  7. About You → random role → Complete Registration
  8. 6-MIN SILENT WAIT
  9. FB app: Create App → name → Next → All(19) → Manage Page → Next → Business no → Next → Next → Create app → password
 10. IG app: same flow but Manage messaging & content on Instagram
 11. Privacy URL via privacy.py
 12. Save .txt to Drive blobs folder + upsert CSV
"""
import sys, os, asyncio, requests, base64, time, re, json, random, imaplib, pickle
import email as em

sys.path.insert(0, '/app')
import proxy as pm, meta_dev as mdm, accounts_sheet as asm, privacy
import human_behavior as hbeh
from textverified_client import _client as tv_client
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

CHAT = 1984534885
TOK = os.environ['TELEGRAM_BOT_TOKEN']
OAI = os.environ['OPENAI_API_KEY']
H_GL = {'Authorization': f'Bearer {pm.GOLOGIN_API_KEY}', 'Content-Type': 'application/json'}
BLOBS_FOLDER_ID = '1orSrIksb2cAhxLwubKSBYsws4b8ordaJ'

ADJ = ['Tester','Test','Demo','Sample','My','First','New','Trial','Quick','Simple']
NOUN = ['App','Project','Build']
ROLES = ['Analyst','Marketer','Product manager']

def hb(t):
    print(f'HB: {t}', flush=True)
    try: requests.post(f'https://api.telegram.org/bot{TOK}/sendMessage', json={'chat_id':CHAT,'text':f'⚙️ {t[:300]}'}, timeout=15)
    except: pass
def shot(b, c):
    try: requests.post(f'https://api.telegram.org/bot{TOK}/sendPhoto', files={'photo':('s.png',b,'image/png')}, data={'chat_id':CHAT,'caption':c[:1024]}, timeout=30)
    except: pass
def vision(png, q, model='gpt-4o', max_tok=500):
    if not png:
        hb('vision skipped (no png)')
        return '{}'
    b64 = base64.b64encode(png).decode()
    r = requests.post('https://api.openai.com/v1/chat/completions',
        headers={'Authorization':f'Bearer {OAI}'},
        json={'model':model,'messages':[{'role':'user','content':[
            {'type':'text','text':q},
            {'type':'image_url','image_url':{'url':f'data:image/png;base64,{b64}'}}]}],'max_tokens':max_tok}, timeout=45)
    return r.json()['choices'][0]['message']['content']
def pj(v):
    s = v.strip()
    if '```' in s: s = s.split('```')[1].lstrip('json').strip()
    try: return json.loads(s)
    except: return {}

def random_name():
    return hbeh.random_app_name()

def parse_blob(blob):
    """email:fbpw:email:emailpw:profile_url:dob:UA:base64_cookies → dict
    URL contains 'https://' so split(':') over-splits. Use cookies for fb_id,
    pattern-match the URL."""
    b64 = blob.rsplit(':', 1)[-1]
    cookies = json.loads(base64.b64decode(b64).decode('utf-8'))
    fb_id = next((c['value'] for c in cookies if c['name']=='c_user'), '?')
    head = blob.split(':')
    email = head[0]; fb_pw = head[1]; email_pw = head[3]
    m = re.search(r'(https://www\.facebook\.com/[^:]+)', blob)
    profile_url = m.group(1) if m else ''
    return {
        'email': email, 'fb_pw': fb_pw, 'email_pw': email_pw,
        'profile_url': profile_url, 'cookies': cookies,
        'fb_id': fb_id, 'blob': blob,
    }

def start_session(profile_id):
    """HARD RULE [feedback-never-touch-gologin-proxy]: no proxy ops. Restart session only."""
    try: requests.delete(f'https://api.gologin.com/browser/{profile_id}/web', headers=H_GL, timeout=15)
    except: pass
    time.sleep(2)
    for _ in range(20):
        r = requests.post(f'https://api.gologin.com/browser/{profile_id}/web', headers=H_GL, json={}, timeout=45)
        info = r.json() if r.status_code in (200,202) else {}
        if info.get('status') == 'profileStatuses.running':
            return 'no-proxy-change'
        time.sleep(3)
    return None

def rent_phone():
    """7-day non-renewable Facebook rental. Returns (rental_id, e164_phone, phone_10digit)."""
    cli = tv_client()
    sale = cli._request('POST', '/api/pub/v2/reservations/rental', json={
        'allowBackOrderReservations': False, 'alwaysOn': True,
        'duration': 'sevenDay', 'isRenewable': False,
        'numberType': 'mobile', 'serviceName': 'facebook', 'capability': 'sms'
    })
    sale_id = sale['href'].rstrip('/').split('/')[-1]
    time.sleep(3)
    sale_data = cli._request('GET', f'/api/pub/v2/sales/{sale_id}')
    rental_id = sale_data['reservations'][0]['id']
    detail = cli._request('GET', f'/api/pub/v2/reservations/rental/nonrenewable/{rental_id}')
    phone = detail.get('phoneNumber') or detail.get('number') or ''
    phone10 = re.sub(r'\D','',phone)[-10:]
    return rental_id, phone, phone10

def list_sms(rental_id):
    body = tv_client()._request('GET', f'/api/pub/v2/sms?ReservationId={rental_id}')
    return body if isinstance(body, list) else (body.get('data') or [])

def email_snap(email, pw):
    seen=set()
    try:
        m=imaplib.IMAP4_SSL('imap.rambler.ru',993,timeout=15)
        m.login(email, pw); m.select('INBOX')
        _,ids=m.search(None,'ALL')
        for eid in (ids[0].split() if ids and ids[0] else [])[-30:]:
            seen.add(eid)
        m.logout()
    except: pass
    return seen

def email_poll(email, pw, seen):
    try:
        m=imaplib.IMAP4_SSL('imap.rambler.ru',993,timeout=15)
        m.login(email,pw); m.select('INBOX')
        _,ids=m.search(None,'(SUBJECT "security code")')
        for eid in (ids[0].split() if ids and ids[0] else [])[::-1][:10]:
            if eid in seen: continue
            _,data=m.fetch(eid,'(RFC822)')
            msg=em.message_from_bytes(data[0][1])
            subj=msg.get('Subject','')
            cm=re.search(r'\b(\d{5,8})\b', subj)
            if cm: m.logout(); return cm.group(1)
            body=''
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == 'text/plain':
                        body += part.get_payload(decode=True).decode('utf-8','replace')
            else: body=(msg.get_payload(decode=True) or b'').decode('utf-8','replace')
            cm=re.search(r'\b(\d{5,8})\b', body)
            if cm: m.logout(); return cm.group(1)
        m.logout()
    except: pass
    return None

async def click_btn(page, name):
    for f in page.frames:
        try:
            loc=f.get_by_role('button', name=name, exact=False)
            if await loc.count()>0 and await loc.first.is_enabled():
                await loc.first.click(); return True
            loc=f.locator(f'div[role="button"]:has-text("{name}")').first
            if await loc.count()>0:
                await loc.click(force=True); return True
        except: pass
    return False

async def click_text(page, txt):
    for f in page.frames:
        try:
            loc=f.locator(f'text="{txt}"').last
            if await loc.count()>0 and await loc.is_visible():
                await loc.click(); return True
        except: pass
    return False

async def find_visible_input(page, exclude_with_value=True, y_range=None):
    """Find a visible non-email/password text input. Skip search bar by y_range."""
    for f in page.frames:
        for el in await f.query_selector_all('input'):
            if not await el.is_visible(): continue
            typ = await el.get_attribute('type') or 'text'
            if typ in ('hidden','checkbox','radio','submit','file','email','password','tel','number'): continue
            ph = (await el.get_attribute('placeholder') or '').lower()
            if 'search' in ph: continue
            val = await el.input_value()
            if exclude_with_value and val: continue
            if '@' in val: continue
            if y_range:
                box = await el.bounding_box()
                if not box or not (y_range[0] <= box['y'] <= y_range[1]): continue
            return (f, el)
    return None


async def run_account(profile_id, acc):
    from playwright.async_api import async_playwright

    hb(f'━━━━━━━ {acc["email"]} → Profile id={profile_id[:10]}… ━━━━━━━')
    _hb_state = {'pos': None}  # human-behavior cursor tracking

    # Phase A: setup proxy + cookies + session
    ip = start_session(profile_id)
    if not ip: hb(f'❌ session start failed'); return None
    hb(f'session running, IP={ip}')
    ok, _ = mdm._persist_cookies_to_gologin_profile(profile_id, acc['cookies'])
    hb(f'cookies push: {ok}')
    try: requests.delete(f'https://api.gologin.com/browser/{profile_id}/web', headers=H_GL, timeout=15)
    except: pass
    time.sleep(3)
    info = {}
    for attempt in range(20):
        r = requests.post(f'https://api.gologin.com/browser/{profile_id}/web', headers=H_GL, json={}, timeout=45)
        info = r.json() if r.status_code in (200,202) else {}
        if info.get('status') == 'profileStatuses.running': break
        time.sleep(3)
    else:
        hb(f'❌ session restart failed after 20 retries; last={info!r}'); return None

    cdp = f'wss://cloudbrowser.gologin.com/connect?token={pm.GOLOGIN_API_KEY}&profile={profile_id}'

    # Rent phone
    rental_id, phone_e164, phone10 = rent_phone()
    hb(f'📞 rental {rental_id}: {phone_e164}')

    role = random.choice(ROLES)
    fb_app_name = random_name()
    ig_app_name = random_name()
    while ig_app_name == fb_app_name: ig_app_name = random_name()
    hb(f'🎲 role={role}  FB app={fb_app_name!r}  IG app={ig_app_name!r}')

    result = {'fb_app': None, 'fb_app_id': None, 'ig_app': None, 'ig_app_id': None,
              'rental_id': rental_id, 'rental_phone': phone_e164, 'role': role,
              'privacy_url': None, 'status': 'in_progress', 'last_error': None}

    async with async_playwright() as p:
        br = await p.chromium.connect_over_cdp(cdp, timeout=60000)
        ctx = br.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Phase B: confirm FB login
        try:
            await page.goto('https://www.facebook.com/', wait_until='domcontentloaded', timeout=60000)
        except: pass
        await hbeh.sleep(5.6, 12.0)
        s = await page.screenshot(type='png')
        shot(s, f'1️⃣ FB.com first load — {acc["email"]}')
        v = pj(vision(s, 'Reply ONLY JSON: {"is_logged_in":true|false,"profile_name_visible":"..."}'))
        hb(f'FB.com: logged_in={v.get("is_logged_in")} name={v.get("profile_name_visible")}')
        if not v.get('is_logged_in'):
            result['status'] = 'fb_login_failed'
            return result

        # Phase B.5: warm-up browse on fb.com — human-cadence signal
        # See project-self-critique-and-warmup-hypotheses.md
        hb('🛋️ warm-up browse 90s (scroll/hover fb.com)')
        try:
            await hbeh.fb_warmup_browse(page, ctx, seconds=90, state=_hb_state)
        except Exception as e:
            hb(f'warmup err (swallowed): {e}')

        # Phase C: dev portal Get Started
        await page.goto('https://developers.facebook.com/', wait_until='domcontentloaded', timeout=60000)
        await hbeh.sleep(5.6, 12.0)
        # Dismiss cookies banner
        await click_btn(page, 'Allow all cookies')
        await asyncio.sleep(3)
        # Click Get Started in top header
        clicked = False
        for sel in ['header a:has-text("Get Started")','nav a:has-text("Get Started")','[role="banner"] a:has-text("Get Started")']:
            try:
                els = await page.query_selector_all(sel)
                for el in els:
                    box = await el.bounding_box()
                    if box and box['y']<250 and await el.is_visible():
                        await el.click(); clicked=True; break
                if clicked: break
            except: pass
        hb(f'Get Started: {clicked}')
        await hbeh.sleep(5.6, 12.0)
        shot(await page.screenshot(type='png'), '2️⃣ after Get Started')

        # Phase D: walk Register → Verify
        # Register stage usually has just Continue
        d = pj(vision(await page.screenshot(type='png'), '''Reply ONLY JSON:
{"sidebar_step_active":"<step>","main_cta":"<text>","main_cta_enabled":true|false,"any_error_text":"<verbatim or null>"}'''))
        if d.get('sidebar_step_active') == 'Register':
            await asyncio.sleep(3)
            await click_btn(page, 'Continue')
            await hbeh.sleep(7.0, 15.0)

        # Phase D2: Verify Account — check for AC redirection
        s, d = await (lambda: (None, None))()  # placeholder
        s = await page.screenshot(type='png')
        d = pj(vision(s, '''Reply ONLY JSON:
{"page_kind":"verify_account|review_email|email_code|contact_info|about_you|dashboard|ac_required|other",
 "any_error_text":"<verbatim or null>",
 "phone_input_visible":true|false,
 "main_cta":"<text>"}'''))
        hb(f'verify state: {d}')
        if 'Accounts Center' in (d.get('any_error_text') or ''):
            hb('⚠️ AC binding required first — running AC flow')
            ac_ok = await ac_bind_flow(page, phone10, acc['email'], acc['email_pw'], v.get('profile_name_visible',''))
            if not ac_ok:
                result['status'] = 'ac_bind_failed'
                return result
            # Back to dev portal verify
            await page.goto('https://developers.facebook.com/async/registration/dialog/?src=default', wait_until='domcontentloaded', timeout=60000)
            await hbeh.sleep(5.6, 12.0)

        # Type phone in verify account
        await asyncio.sleep(3)
        pin = await find_visible_input(page, exclude_with_value=True)
        if not pin:
            # Maybe alternative — search by placeholder
            for f in page.frames:
                for sel in ['input[placeholder*="phone" i]','input[type="tel"]']:
                    try:
                        el = await f.query_selector(sel)
                        if el and await el.is_visible(): pin=(f,el); break
                    except: pass
                if pin: break
        if not pin: hb('❌ no phone input'); result['status']='no_phone_input'; return result
        await pin[1].click(); await asyncio.sleep(1.5); await pin[1].fill('')
        await pin[1].type(phone10, delay=140)
        hb(f'typed phone {phone10}'); await hbeh.sleep(3.5, 7.5)

        seen_sms = set(str(it.get('id','')) for it in list_sms(rental_id))
        ok = await click_btn(page, 'Send Verification SMS')
        if not ok: ok = await click_btn(page, 'Send verification')
        hb(f'Send SMS: {ok}')
        if not ok: result['status']='no_send_btn'; return result
        await hbeh.sleep(7.0, 15.0)

        hb('📨 polling for SMS code (5min)…')
        code=None; deadline=time.time()+300
        while time.time()<deadline:
            for it in list_sms(rental_id):
                if str(it.get('id','')) in seen_sms: continue
                t=it.get('smsContent') or ''
                cm=re.search(r'\b(\d{4,8})\b', t)
                if cm: code=cm.group(1); break
            if code: break
            await hbeh.sleep(7.0, 15.0)
        if not code: result['status']='no_sms'; return result
        hb(f'📨 SMS: {code}')
        shot(await page.screenshot(type='png'), f'3️⃣ got SMS code {code}')

        await hbeh.sleep(2.8, 6.0)
        ci = await find_visible_input(page, exclude_with_value=True)
        if not ci: hb('❌ no code input'); result['status']='no_code_input'; return result
        await ci[1].click(); await asyncio.sleep(0.5); await ci[1].type(code, delay=140)
        hb(f'typed code'); await hbeh.sleep(3.5, 7.5)
        await click_btn(page, 'Continue')
        await hbeh.sleep(8.4, 18.0)

        # Phase E: Review Email → Confirm Email (no code expected — cookies skip)
        d = pj(vision(await page.screenshot(type='png'), '''Reply ONLY JSON:
{"page_kind":"review_email|contact_info|about_you|other","main_cta":"<text>"}'''))
        if d.get('page_kind') in ('review_email',):
            await asyncio.sleep(3)
            await click_btn(page, 'Confirm Email')
            await hbeh.sleep(7.0, 15.0)

        # Phase F: Contact info → just continue if shown
        d = pj(vision(await page.screenshot(type='png'), '''Reply ONLY JSON:
{"page_kind":"contact_info|about_you|other","main_cta":"<text>"}'''))
        if d.get('page_kind') == 'contact_info':
            await asyncio.sleep(3)
            await click_btn(page, 'Continue')
            await hbeh.sleep(7.0, 15.0)

        # Phase G: About You — random role + Complete Registration
        d = pj(vision(await page.screenshot(type='png'), '''Reply ONLY JSON:
{"page_kind":"about_you|dashboard|other","heading":"<verbatim>"}'''))
        if d.get('page_kind') != 'about_you' and 'describes' not in (d.get('heading','').lower()):
            # Maybe we're already past about_you and on dashboard?
            if 'apps' in d.get('heading','').lower() or 'create' in d.get('heading','').lower():
                hb('⚠️ skipped About You — already at dashboard?')
            else:
                hb(f'❌ unexpected page: {d}'); result['status']='unexpected_post_phone'; return result
        else:
            # Click role card
            await hbeh.sleep(2.8, 6.0)
            await click_text(page, role)
            await hbeh.sleep(3.5, 7.5)
            # Vision verify CR enabled
            d2 = pj(vision(await page.screenshot(type='png'), 'Reply ONLY JSON: {"main_cta_enabled":true|false}'))
            if not d2.get('main_cta_enabled'):
                # Try clicking far right of viewport at role's y
                hb('CR not enabled, trying mouse click on card right side')
                for f in page.frames:
                    try:
                        loc = f.locator(f'text="{role}"').last
                        if await loc.count()>0:
                            box = await loc.bounding_box()
                            if box:
                                await hbeh.click(page, box['x']+800, box['y']+box['height']/2, _hb_state)
                                break
                    except: pass
                await hbeh.sleep(2.8, 6.0)
            await hbeh.sleep(3.5, 7.5)
            cr = await click_btn(page, 'Complete Registration')
            hb(f'🎯 Complete Registration: {cr}')
            if not cr: result['status']='no_cr_btn'; return result
            # 6-MIN SILENT WAIT — DO NOTHING
            shot(await page.screenshot(type='png'), '4️⃣ Complete Registration clicked — entering 6-min silent wait')
            hb('⏳ 6-min silent wait per RUNBOOK §post-CR')
            await hbeh.sleep(252.0, 540.0)

        # Phase H: verify dashboard
        d = pj(vision(await page.screenshot(type='png'), '''Reply ONLY JSON:
{"page_kind":"about_you|my_apps|dashboard|other","main_cta":"<text>"}'''))
        hb(f'post-wait: {d}')
        if d.get('page_kind') not in ('my_apps','dashboard') and 'apps' not in (d.get('main_cta','').lower() + ' '):
            # Still on about_you — Meta is processing slowly. Try another 3 min
            hb('still not on dashboard, +3min wait')
            await hbeh.sleep(126.0, 270.0)
            d = pj(vision(await page.screenshot(type='png'), '''Reply ONLY JSON:
{"page_kind":"about_you|my_apps|dashboard|other","main_cta":"<text>"}'''))
        if d.get('page_kind') not in ('my_apps','dashboard') and 'create app' not in (d.get('main_cta','').lower()):
            shot(await page.screenshot(type='png'), '❌ no dashboard after 9min')
            result['status']='no_dashboard'; return result
        hb('✅ dashboard reached')
        shot(await page.screenshot(type='png'), '5️⃣ dashboard reached — starting FB app')

        # Phase I: Create FB app
        fb_id = await create_app_wizard(page, fb_app_name, 'Manage everything on your Page', acc['fb_pw'])
        result['fb_app'] = fb_app_name
        result['fb_app_id'] = fb_id
        hb(f'FB app: {fb_app_name} → {fb_id}')

        # Phase J: navigate back to /apps + Create IG app
        await page.goto('https://developers.facebook.com/apps', wait_until='domcontentloaded', timeout=60000)
        await hbeh.sleep(5.6, 12.0)
        ig_id = await create_app_wizard(page, ig_app_name, 'Manage messaging & content on Instagram', acc['fb_pw'])
        result['ig_app'] = ig_app_name
        result['ig_app_id'] = ig_id
        hb(f'IG app: {ig_app_name} → {ig_id}')
        shot(await page.screenshot(type='png'), '7️⃣ both apps created — final state')

    # Phase K: privacy URL (telegra.ph) for FB app
    try:
        url, err = privacy._create_telegraph_privacy_policy(fb_app_name)
        result['privacy_url'] = url
        hb(f'privacy URL for FB app: {url}')
    except Exception as e:
        hb(f'privacy err: {e}')

    result['status'] = 'apps_created'
    return result


async def ac_bind_flow(page, phone10, email, email_pw, profile_name):
    """Bind phone to FB account via Accounts Center first, then return."""
    hb('🔁 entering AC binding flow')
    await page.goto('https://accountscenter.facebook.com/personal_info/contact_points/', wait_until='domcontentloaded', timeout=60000)
    await hbeh.sleep(4.9, 10.5)
    await asyncio.sleep(3)
    await click_btn(page, 'Add new contact')
    await hbeh.sleep(3.5, 7.5)
    await click_btn(page, 'Add mobile number')
    await hbeh.sleep(4.2, 9.0)
    pin = None
    for f in page.frames:
        for sel in ['input[placeholder*="mobile" i]','input[placeholder*="phone" i]','input[type="tel"]']:
            try:
                el = await f.query_selector(sel)
                if el and await el.is_visible(): pin=(f,el); break
            except: pass
        if pin: break
    if not pin: hb('❌ no AC phone input'); return False
    await pin[1].click(); await asyncio.sleep(1); await pin[1].fill('')
    await pin[1].type(phone10, delay=140)
    await hbeh.sleep(2.8, 6.0)
    # Click profile name row (toggle checkbox)
    if profile_name:
        for f in page.frames:
            try:
                loc = f.locator(f'text="{profile_name}"').last
                if await loc.count()>0 and await loc.is_visible():
                    await loc.click(); break
            except: pass
    else:
        # Click Facebook text as proxy
        for f in page.frames:
            try:
                loc = f.locator('text=Facebook').last
                if await loc.count()>0 and await loc.is_visible():
                    await loc.click(); break
            except: pass
    await hbeh.sleep(2.8, 6.0)
    seen_email = email_snap(email, email_pw)
    await asyncio.sleep(3)
    await click_btn(page, 'Next')
    await hbeh.sleep(7.0, 15.0)
    hb('📨 polling for AC email code (3min)…')
    code = None
    deadline = time.time()+180
    while time.time()<deadline:
        code = email_poll(email, email_pw, seen_email)
        if code: break
        await hbeh.sleep(7.0, 15.0)
    if not code: hb('❌ no AC email code'); return False
    hb(f'AC code: {code}')
    await hbeh.sleep(2.8, 6.0)
    ci = await find_visible_input(page, exclude_with_value=True)
    if not ci: hb('❌ no AC code input'); return False
    await ci[1].click(); await asyncio.sleep(0.5); await ci[1].type(code, delay=140)
    await hbeh.sleep(3.5, 7.5)
    await click_btn(page, 'Next')
    await hbeh.sleep(5.6, 12.0)
    hb('✅ AC bound')
    return True


async def create_app_wizard(page, app_name, use_case_text, fb_pw):
    """Walk Create App wizard for one app. Returns app ID from /apps list, or None."""
    hb(f'creating app: {app_name} ({use_case_text})')
    await click_btn(page, 'Create App') or await click_btn(page, 'Create app')
    await hbeh.sleep(5.6, 12.0)
    # Dismiss "new way" guidance modal
    await page.keyboard.press('Escape')
    await asyncio.sleep(3)
    # Clear search bar (sometimes typed-into garbage)
    for f in page.frames:
        try:
            for el in await f.query_selector_all('input[placeholder*="Search" i]'):
                if await el.is_visible():
                    await el.click(); await asyncio.sleep(0.3)
                    await el.press('Control+a'); await el.press('Delete')
                    break
        except: pass
    await hbeh.click(page, 100, 100, _hb_state)
    await asyncio.sleep(2)

    # Type App name in the y=200..350 input (avoid search bar at y=17)
    nin = await find_visible_input(page, exclude_with_value=False, y_range=(200, 400))
    if not nin: hb('❌ no name input'); return None
    await nin[1].click(); await asyncio.sleep(1)
    await nin[1].press('Control+a'); await nin[1].press('Delete'); await asyncio.sleep(0.5)
    await nin[1].type(app_name, delay=140)
    hb(f'name typed'); await hbeh.sleep(2.8, 6.0)
    await click_btn(page, 'Next')
    await hbeh.sleep(7.0, 15.0)

    # Use cases → All (19) → click the target use case card
    for f in page.frames:
        try:
            loc = f.locator('text="All (19)"').first
            if await loc.count()>0 and await loc.is_visible():
                await loc.click(); break
        except: pass
    await asyncio.sleep(3)
    # Scroll target into view + click its checkbox (use mouse click far right)
    for f in page.frames:
        try:
            loc = f.locator(f'text="{use_case_text}"').last
            if await loc.count()>0:
                await loc.scroll_into_view_if_needed(timeout=8000)
                await asyncio.sleep(2)
                box = await loc.bounding_box()
                if box:
                    await hbeh.click(page, box['x']+800, box['y']+box['height']/2, _hb_state)
                break
        except: pass
    await hbeh.sleep(2.8, 6.0)
    await click_btn(page, 'Next')
    await hbeh.sleep(7.0, 15.0)

    # Business: I don't want
    for f in page.frames:
        try:
            loc = f.locator('text=/I don.t want to connect a business portfolio/').last
            if await loc.count()>0 and await loc.is_visible():
                await loc.click(); break
        except: pass
    await hbeh.sleep(2.8, 6.0)
    await click_btn(page, 'Next')
    await hbeh.sleep(7.0, 15.0)
    # Requirements: just Next
    await click_btn(page, 'Next')
    await hbeh.sleep(7.0, 15.0)
    # Overview: Create app
    await click_btn(page, 'Create app')
    await hbeh.sleep(4.2, 9.0)
    # Password popup
    for i in range(20):
        await asyncio.sleep(2)
        for f in page.frames:
            try:
                el = await f.query_selector('input[type="password"]')
                if el and await el.is_visible():
                    await el.click(); await asyncio.sleep(1); await el.fill('')
                    await el.type(fb_pw, delay=140)
                    await asyncio.sleep(3)
                    await click_btn(page, 'Submit')
                    await hbeh.sleep(10.5, 22.5)
                    break
            except: pass
        else: continue
        break

    # Verify via /apps
    await hbeh.sleep(3.5, 7.5)
    await page.goto('https://developers.facebook.com/apps', wait_until='domcontentloaded', timeout=60000)
    await hbeh.sleep(5.6, 12.0)
    s = await page.screenshot(type='png')
    shot(s, f'6️⃣ /apps list after creating {app_name}')
    v = vision(s, f'In the apps list, find "{app_name}". Reply ONLY JSON: {{"app_listed":true|false,"app_id":"<the App ID number visible next to it, or null>"}}')
    d = pj(v)
    return d.get('app_id')


def save_account_record(profile_name, acc, result):
    """Write .txt to Drive folder + upsert CSV."""
    creds = pickle.loads(base64.b64decode(os.environ['GOOGLE_TOKEN_PICKLE']))
    svc = build('drive','v3',credentials=creds, cache_discovery=False)

    safe = profile_name.replace(' ','_')
    fname = f'{safe}__{acc["email"].split("@")[0]}.txt'
    body_str = f'''═══════════════════════════════════════════════
ACCOUNT — {profile_name}
═══════════════════════════════════════════════

GoLogin Profile : {profile_name}
FB Email        : {acc["email"]}
FB Profile ID   : {acc["fb_id"]}
Status          : {result.get("status")}
Created         : {time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}
Rental Phone    : {result.get("rental_phone")}
Rental ID       : {result.get("rental_id")}
Role chosen     : {result.get("role")}

FB APP:  {result.get("fb_app") or "(failed)"}  →  ID {result.get("fb_app_id") or "(missing)"}
IG APP:  {result.get("ig_app") or "(failed)"}  →  ID {result.get("ig_app_id") or "(missing)"}

PRIVACY POLICY URL (paste into FB app Settings → Privacy Policy URL):
{result.get("privacy_url") or "(failed)"}

═══════════════════════════════════════════════
FULL BLOB (paste into bot for cookie-restore):
═══════════════════════════════════════════════
{acc["blob"]}
'''
    # Upload .txt to folder (replace if exists)
    q = f"name='{fname}' and '{BLOBS_FOLDER_ID}' in parents and trashed=false"
    existing = svc.files().list(q=q, fields='files(id)').execute().get('files') or []
    media = MediaInMemoryUpload(body_str.encode('utf-8'), mimetype='text/plain')
    if existing:
        fid = existing[0]['id']
        svc.files().update(fileId=fid, media_body=media).execute()
    else:
        fid = svc.files().create(body={'name':fname,'parents':[BLOBS_FOLDER_ID]}, media_body=media, fields='id').execute()['id']
    blob_link = f'https://drive.google.com/file/d/{fid}/view'

    # Upsert CSV via accounts_sheet
    notes = f'FB app {result.get("fb_app") or "?"} ({result.get("fb_app_id") or "?"}) + IG app {result.get("ig_app") or "?"} ({result.get("ig_app_id") or "?"}). Privacy: {result.get("privacy_url") or "(none)"}.'
    asm.upsert_entry(profile_name, acc["email"], acc["fb_id"],
        'mobile_iproyal','active',
        status=result.get('status','unknown'),
        notes=notes, full_blob=blob_link,
        rental_phone=result.get('rental_phone',''), rental_id=result.get('rental_id',''))
    print(f'SAVED: {profile_name} → {blob_link}')
    return blob_link


# ─── Dispatch from CLI ────────────────────────────────────────────────
if __name__ == '__main__':
    blob = sys.argv[1]
    profile_name = sys.argv[2]  # e.g. 'Validated Profile 5'
    acc = parse_blob(blob)
    profs = mdm._list_validated_profiles()
    target = next((p for p in profs if p['name']==profile_name), None)
    if not target: sys.exit(f'profile {profile_name!r} not found')
    profile_id = target['id']
    result = asyncio.run(run_account(profile_id, acc))
    if result is None:
        result = {'status':'aborted_early', 'rental_phone':'', 'rental_id':'', 'role':'', 'fb_app':None, 'fb_app_id':None, 'ig_app':None, 'ig_app_id':None, 'privacy_url':None}
    save_account_record(profile_name, acc, result)
    print(f'DONE  status={result.get("status")}')
