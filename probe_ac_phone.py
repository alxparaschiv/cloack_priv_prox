#!/usr/bin/env python3
"""Probe: open Validated Profile 2 + CE in GoLogin, navigate to Accounts Center
contact_points, screenshot the page, dump the body text around the phone number
to PROVE whether +16302878102 is bound. No mutations.

Usage:
  railway run python3 probe_ac_phone.py
"""
import asyncio, os, sys, json, time, base64
sys.path.insert(0, os.path.dirname(__file__))

from playwright.async_api import async_playwright
import requests

GL_TOKEN = os.environ.get('GOLOGIN_API_KEY') or os.environ.get('GOLOGIN_TOKEN') or os.environ.get('GO_LOGIN_TOKEN')
PROFILE_NAME = "Validated Profile 2 + CE"
TARGET_PHONE = "+16302878102"

def find_profile_id():
    """Look up GoLogin profile id by display name."""
    r = requests.get(
        'https://api.gologin.com/browser/v2',
        headers={'Authorization': f'Bearer {GL_TOKEN}'},
        params={'limit': 100},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    profiles = data.get('profiles') or data.get('data') or data
    for p in profiles:
        if p.get('name') == PROFILE_NAME:
            return p['id']
    raise RuntimeError(f'no profile named {PROFILE_NAME!r}')

def start_session(profile_id):
    # match master_account_create.py: POST with empty body, poll until status=running
    info = {}
    for _ in range(20):
        r = requests.post(
            f'https://api.gologin.com/browser/{profile_id}/web',
            headers={'Authorization': f'Bearer {GL_TOKEN}'},
            json={},
            timeout=45,
        )
        if r.status_code in (200, 202):
            info = r.json()
            if info.get('status') == 'profileStatuses.running':
                break
        time.sleep(3)
    else:
        raise RuntimeError(f'session start never reached running: {info!r}')
    time.sleep(15)  # settle for cookies
    return info

def stop_session(profile_id):
    try:
        requests.delete(
            f'https://api.gologin.com/browser/{profile_id}/web',
            headers={'Authorization': f'Bearer {GL_TOKEN}'},
            timeout=30,
        )
    except: pass

async def main():
    pid = find_profile_id()
    print(f'profile id: {pid}', flush=True)
    start_session(pid)
    cdp = f'wss://cloudbrowser.gologin.com/connect?token={GL_TOKEN}&profile={pid}'
    print(f'cdp: {cdp[:60]}...{cdp[-20:]}', flush=True)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp, timeout=60000)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            print('navigating to AC contact_points', flush=True)
            await page.goto('https://accountscenter.facebook.com/youraccount/contact_points/',
                            wait_until='domcontentloaded', timeout=60000)
            await asyncio.sleep(8)
            body = await page.evaluate("() => document.body.innerText")
            print('--- AC PAGE BODY (full) ---', flush=True)
            print(body, flush=True)
            print('--- /AC PAGE BODY ---', flush=True)
            # Snapshot
            cdp = await ctx.new_cdp_session(page)
            shot = await cdp.send('Page.captureScreenshot', {'format':'jpeg','quality':70})
            png_b = base64.b64decode(shot['data'])
            outp = '/tmp/probe-ac-phone.jpg'
            with open(outp,'wb') as f: f.write(png_b)
            print(f'screenshot saved: {outp} ({len(png_b)} bytes)', flush=True)
            # Logic
            idx = body.find(TARGET_PHONE)
            if idx >= 0:
                ctx_after = body[idx:idx+120]
                print(f'\nFOUND {TARGET_PHONE} at idx={idx}', flush=True)
                print(f'context (next 120 chars): {ctx_after!r}', flush=True)
                if 'Pending confirmation' in ctx_after:
                    print('VERDICT: PHONE PRESENT BUT PENDING CONFIRMATION (not bound)', flush=True)
                else:
                    print('VERDICT: PHONE BOUND ✅', flush=True)
            else:
                print(f'\n{TARGET_PHONE} NOT FOUND in AC page body', flush=True)
                print('VERDICT: PHONE NOT BOUND ❌', flush=True)
            await browser.close()
    finally:
        stop_session(pid)
        print('session closed', flush=True)

if __name__ == '__main__':
    asyncio.run(main())
