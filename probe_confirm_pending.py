#!/usr/bin/env python3
"""Probe: resume a Pending confirmation phone in Accounts Center.

Opens VP2+CE → AC contact_points → clicks the pending phone entry → enters
the latest SMS code from the rental (or env CONFIRMATION_CODE) → submits.

This handles the state where master_account_create.py's ac_bind_flow was
interrupted after the phone was added but BEFORE the SMS code was confirmed.

Usage:
  CONFIRMATION_CODE=515443 railway run python3 probe_confirm_pending.py
  # or omit CONFIRMATION_CODE to pull the latest SMS from the rental
"""
import asyncio, os, sys, time, base64, re
sys.path.insert(0, os.path.dirname(__file__))

import requests
from playwright.async_api import async_playwright

from textverified_client import _client as tv_client

GL_TOKEN = os.environ.get('GOLOGIN_API_KEY')
PROFILE_NAME = "Validated Profile 2 + CE"
TARGET_PHONE = "+16302878102"
RENTAL_ID = "lr_01KT70W9XXRP2QD886W9JM1CSG"

def find_profile_id():
    r = requests.get('https://api.gologin.com/browser/v2',
                     headers={'Authorization': f'Bearer {GL_TOKEN}'},
                     params={'limit':100}, timeout=30)
    r.raise_for_status()
    for p in (r.json().get('profiles') or r.json().get('data') or r.json()):
        if p.get('name') == PROFILE_NAME: return p['id']
    raise RuntimeError(f'no profile named {PROFILE_NAME!r}')

def start_session(profile_id):
    info = {}
    for _ in range(20):
        r = requests.post(f'https://api.gologin.com/browser/{profile_id}/web',
                          headers={'Authorization': f'Bearer {GL_TOKEN}'}, json={}, timeout=45)
        if r.status_code in (200,202):
            info = r.json()
            if info.get('status') == 'profileStatuses.running': break
        time.sleep(3)
    else:
        raise RuntimeError(f'session never running: {info!r}')
    time.sleep(15)

def stop_session(profile_id):
    try: requests.delete(f'https://api.gologin.com/browser/{profile_id}/web',
                          headers={'Authorization': f'Bearer {GL_TOKEN}'}, timeout=30)
    except: pass

def latest_sms_code():
    body = tv_client()._request('GET', f'/api/pub/v2/sms?ReservationId={RENTAL_ID}')
    sms = body if isinstance(body, list) else (body.get('data') or [])
    sms.sort(key=lambda s: s.get('createdAt',''), reverse=True)
    for s in sms:
        m = re.search(r'\b(\d{4,8})\b', s.get('smsContent') or '')
        if m: return m.group(1), s.get('createdAt','')
    return None, None

async def cdp_shot(ctx, page, label, out='/tmp/probe-confirm-pending'):
    cdp = await ctx.new_cdp_session(page)
    r = await cdp.send('Page.captureScreenshot', {'format':'jpeg','quality':70})
    p = f'{out}-{label}.jpg'
    with open(p,'wb') as f: f.write(base64.b64decode(r['data']))
    print(f'shot: {p}', flush=True)

async def main():
    code = os.environ.get('CONFIRMATION_CODE')
    code_age_min = None
    if not code:
        code, created = latest_sms_code()
        print(f'latest SMS: {code} createdAt={created}', flush=True)
        if created:
            try:
                from datetime import datetime, timezone
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(created.replace('Z','+00:00'))).total_seconds()
                code_age_min = age / 60
                print(f'SMS age: {code_age_min:.1f} minutes', flush=True)
            except: pass
    if not code:
        print('❌ no code available', flush=True); return
    if code_age_min is not None and code_age_min > 10:
        print(f'⚠️ SMS code is {code_age_min:.1f}min old — likely expired. Will try anyway.', flush=True)

    pid = find_profile_id()
    print(f'profile id: {pid}', flush=True)
    start_session(pid)
    cdp_url = f'wss://cloudbrowser.gologin.com/connect?token={GL_TOKEN}&profile={pid}'
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp_url, timeout=60000)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto('https://accountscenter.facebook.com/personal_info/contact_points/',
                            wait_until='domcontentloaded', timeout=60000)
            await asyncio.sleep(6)
            await cdp_shot(ctx, page, '1-ac-page')
            body = await page.evaluate("() => document.body.innerText")
            if TARGET_PHONE not in body:
                print(f'❌ {TARGET_PHONE} not on page — abort'); return
            if 'Pending confirmation' not in body:
                print('✅ phone already confirmed — nothing to do'); return

            # Click on the phone entry to open the confirmation dialog.
            # FB renders the entry as a clickable row containing the phone + "Pending confirmation".
            clicked = False
            # Strategy 1: link/button containing the phone in the Contact information section
            for sel in [
                f'div[role="button"]:has-text("{TARGET_PHONE}")',
                f'a:has-text("{TARGET_PHONE}")',
                f'[role="listitem"]:has-text("{TARGET_PHONE}")',
                f'div:has-text("{TARGET_PHONE}"):has-text("Pending")',
            ]:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0:
                        await loc.click(timeout=5000)
                        clicked = True
                        print(f'clicked via {sel!r}', flush=True)
                        break
                except Exception as e:
                    print(f'sel {sel!r} err: {e}', flush=True)
            if not clicked:
                # Strategy 2: coordinate click on the phone text directly
                box = None
                try:
                    box = await page.evaluate(f"""
                        () => {{
                            const xpath = "//*[contains(text(), '{TARGET_PHONE}')]";
                            const r = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                            if (!r) return null;
                            const b = r.getBoundingClientRect();
                            return {{x: b.x + b.width/2, y: b.y + b.height/2}};
                        }}
                    """)
                except Exception as e:
                    print(f'xpath err: {e}', flush=True)
                if box:
                    print(f'coord click at ({box["x"]:.0f},{box["y"]:.0f})', flush=True)
                    await page.mouse.click(int(box['x']), int(box['y']))
                    clicked = True
            if not clicked:
                print('❌ could not click phone entry'); await cdp_shot(ctx, page, '2-stuck'); return

            await asyncio.sleep(5)
            await cdp_shot(ctx, page, '3-after-click')

            # Now we should be on a "Confirm phone" / code-entry view.
            # Look for "Confirm" button or a code input.
            body2 = await page.evaluate("() => document.body.innerText")
            print(f'\n--- AFTER CLICK ({len(body2)} chars) ---', flush=True)
            print(body2[:1500], flush=True)
            print('--- /AFTER CLICK ---\n', flush=True)

            # The dialog shows "Finish confirming this number?" with an inline blue text link
            # "Confirm number" inside the explanation paragraph. Click that link specifically
            # via text content (not role=button), then wait for the SMS modal.
            confirm_clicked = False
            for sel in [
                'text="Confirm number"',
                'a:has-text("Confirm number")',
                'span:has-text("Confirm number")',
                '[role="link"]:has-text("Confirm number")',
            ]:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0:
                        await loc.click(timeout=5000)
                        confirm_clicked = True
                        print(f'clicked "Confirm number" via {sel!r}', flush=True)
                        break
                except Exception as e: print(f'{sel} err: {e}', flush=True)
            if not confirm_clicked:
                print('❌ could not click "Confirm number" link', flush=True)
                await cdp_shot(ctx, page, '4-no-confirm-link')
                return
            # Wait for the SMS code modal to render (longer than 4s — FB animates the modal)
            await asyncio.sleep(10)
            await cdp_shot(ctx, page, '4-after-confirm-number-link')

            # Now find the code input and type the code
            code_input = None
            for sel in ['input[autocomplete="one-time-code"]', 'input[type="text"]', 'input[type="tel"]']:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        code_input = loc
                        print(f'found code input via {sel}', flush=True)
                        break
                except: pass
            if not code_input:
                print('❌ no code input visible'); await cdp_shot(ctx, page, '5-no-input'); return

            await code_input.click(); await asyncio.sleep(0.5)
            await code_input.type(code, delay=140)
            await asyncio.sleep(2)
            await cdp_shot(ctx, page, '6-code-typed')

            # Submit
            for label in ['Next','Confirm','Submit','Done']:
                try:
                    loc = page.get_by_role('button', name=label).first
                    if await loc.count() > 0:
                        print(f'submitting via {label!r}', flush=True)
                        await loc.click(timeout=5000)
                        await asyncio.sleep(6)
                        await cdp_shot(ctx, page, f'7-submitted-{label.lower()}')
                        break
                except Exception as e: print(f'submit {label} err: {e}', flush=True)

            # Re-check binding
            await page.goto('https://accountscenter.facebook.com/personal_info/contact_points/',
                            wait_until='domcontentloaded', timeout=60000)
            await asyncio.sleep(5)
            body3 = await page.evaluate("() => document.body.innerText")
            ci = body3.find('Contact information')
            if ci >= 0:
                sect = body3[ci:ci+2000]
                if TARGET_PHONE in sect:
                    win = sect[sect.find(TARGET_PHONE):sect.find(TARGET_PHONE)+200]
                    if 'Pending confirmation' not in win:
                        print(f'\n✅✅ PHONE BOUND — confirmation succeeded', flush=True)
                    else:
                        print(f'\n❌ still pending confirmation:\n{win}', flush=True)
            await cdp_shot(ctx, page, '8-final-ac')
            await browser.close()
    finally:
        stop_session(pid)
        print('session closed', flush=True)

if __name__ == '__main__':
    asyncio.run(main())
