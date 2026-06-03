#!/usr/bin/env python3
"""Force-finish the Create App wizard — recreates F34/K34 behavior from June 1.

Per VP34 Telegram logs from June 1 (the empirically working run that created
LaunchLy app ID 2107024670234005), the working pattern was:
  1. Try use-case checkbox click — TOLERATE errors (Locator timeout, etc.)
  2. Click Next ANYWAY ("Next clicked → advancing")
  3. Click "I don't want to connect business portfolio"
  4. Click Next (business)
  5. Click Next (Requirements)
  6. Click Create app (Overview)
  7. Password popup if it appears
  8. Navigate /apps → verify

No vision gates. No halt. Just push through and check the final state.

Usage:
    BLOB=<blob>
    APP_NAME=<name>
    USE_CASE_TEXT=<exact card text e.g. "Manage everything on your Page">
    railway run python3 force_complete_app_wizard.py "Validated Profile 1"
"""
import os, sys, asyncio, random, requests, base64
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from master_account_create import (
    parse_blob, hb, shot, safe_screenshot, vision, pj, click_btn,
    find_visible_input, hbeh,
)
import meta_dev as mdm
from playwright.async_api import async_playwright

GL = os.environ['GOLOGIN_API_KEY']
BLOB = os.environ['BLOB']
APP_NAME = os.environ['APP_NAME']
USE_CASE_TEXT = os.environ.get('USE_CASE_TEXT', 'Manage everything on your Page')


async def main(profile_name):
    profs = mdm._list_validated_profiles()
    target = next((p for p in profs if p['name'] == profile_name), None)
    if not target: sys.exit(f'profile {profile_name!r} not found')
    profile_id = target['id']
    acc = parse_blob(BLOB)

    hb(f'━━━ FORCE-FINISH wizard for "{APP_NAME}" ({USE_CASE_TEXT}) on {profile_id[:12]}… ━━━')

    cdp = f'wss://cloudbrowser.gologin.com/connect?token={GL}&profile={profile_id}'
    async with async_playwright() as p:
        br = await p.chromium.connect_over_cdp(cdp, timeout=60000)
        ctx = br.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.bring_to_front()
        shot(await safe_screenshot(page), f'[F-1-start] starting force-finish for {APP_NAME}')

        # ─── STEP 1: Try to click use-case checkbox (tolerant) ───
        hb(f'on Use Cases page → selecting "{USE_CASE_TEXT}"')
        try:
            for f in page.frames:
                try:
                    loc = f.locator(f'text="{USE_CASE_TEXT}"').last
                    if await loc.count() > 0:
                        await loc.scroll_into_view_if_needed(timeout=8000)
                        await asyncio.sleep(2)
                        box = await loc.bounding_box()
                        if box:
                            # Try multiple offsets (FB may have moved the checkbox)
                            for offset_x in [box['width']+200, 800, box['width']-30]:
                                await page.mouse.click(box['x']+offset_x, box['y']+box['height']/2)
                                await asyncio.sleep(0.8)
                        break
                except Exception as e:
                    hb(f'radio click err: {str(e)[:120]}')
        except Exception as e:
            hb(f'use-case section err: {str(e)[:120]}')
        shot(await safe_screenshot(page), '[F-2-after-use-case-click]')

        # ─── STEP 2: Click Next (regardless of whether checkbox got checked) ───
        try:
            await click_btn(page, 'Next')
            hb('Next clicked → advancing')
        except Exception as e:
            hb(f'Next click err: {e}')
        await asyncio.sleep(8)
        shot(await safe_screenshot(page), '[F-3-after-use-case-next]')

        # ─── STEP 3: Business page → "I don't want" ───
        hb('on Business page → selecting "I dont want"')
        try:
            for f in page.frames:
                try:
                    loc = f.locator('text=/I don.t want to connect a business portfolio/').last
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click()
                        hb('business choice clicked')
                        break
                except: pass
        except: pass
        await asyncio.sleep(3)
        shot(await safe_screenshot(page), '[F-4-after-business-choice]')

        # ─── STEP 4: Click Next (Business) ───
        try:
            await click_btn(page, 'Next')
            hb('Business Next clicked')
        except Exception as e:
            hb(f'Business Next err: {e}')
        await asyncio.sleep(8)
        shot(await safe_screenshot(page), '[F-5-after-business-next]')

        # ─── STEP 5: Requirements → Next ───
        hb('on Requirements page → Next')
        try:
            await click_btn(page, 'Next')
        except Exception as e:
            hb(f'Requirements Next err: {e}')
        await asyncio.sleep(8)
        shot(await safe_screenshot(page), '[F-6-after-requirements]')

        # ─── STEP 6: Overview → Create app ───
        hb('on Overview → Create app')
        try:
            await click_btn(page, 'Create app')
        except Exception as e:
            hb(f'Create app err: {e}')
        await asyncio.sleep(6)
        shot(await safe_screenshot(page), '[F-7-after-create-app]')

        # ─── STEP 7: Password popup (tolerant 20-iter loop) ───
        for i in range(20):
            await asyncio.sleep(2)
            for f in page.frames:
                try:
                    el = await f.query_selector('input[type="password"]')
                    if el and await el.is_visible():
                        hb(f'[pw-popup iter {i+1}] password input found — filling')
                        await el.click(); await asyncio.sleep(1)
                        await el.fill('')
                        await el.type(acc['fb_pw'], delay=140)
                        await asyncio.sleep(3)
                        try: await click_btn(page, 'Submit')
                        except: pass
                        await asyncio.sleep(15)
                        shot(await safe_screenshot(page), '[F-8-after-password-submit]')
                        break
                except: pass
            else: continue
            break

        # ─── STEP 8: Verify via /apps ───
        await asyncio.sleep(5)
        try:
            await page.goto('https://developers.facebook.com/apps', wait_until='domcontentloaded', timeout=60000)
        except: pass
        await asyncio.sleep(8)
        shot(await safe_screenshot(page), '[F-9-final-state]')
        body = await page.evaluate("() => document.body.innerText")
        import re
        # Look for app name + 15-19 digit App ID
        if APP_NAME in body:
            ids = re.findall(r'\b(\d{15,19})\b', body)
            hb(f'🎉 FB app {APP_NAME} visible in apps list — possible IDs found: {ids[:5]}')
            shot(await safe_screenshot(page), f'[F-10-apps-list] {APP_NAME} present')
            # Vision for precise app_id
            png = await safe_screenshot(page)
            v = vision(png, f'In the apps list, find "{APP_NAME}". Reply ONLY JSON: {{"app_id":"<the App ID number visible next to it, or null>"}}')
            d = pj(v)
            app_id = d.get('app_id')
            hb(f'app_id (via vision): {app_id}')
            print(f'\nAPP_ID={app_id}')
        else:
            hb(f'❌ {APP_NAME} NOT visible in /apps — wizard did not actually create the app')
            shot(await safe_screenshot(page), f'[F-10-apps-list] {APP_NAME} MISSING')
            print(f'\nAPP_ID=None')

        await br.close()


if __name__ == '__main__':
    profile_name = sys.argv[1]
    asyncio.run(main(profile_name))
