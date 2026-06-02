#!/usr/bin/env python3
"""Resume an account-creation run from Phase H (app creation) onward.

Use case: the wizard (account registration, AC bind) is already complete and the
browser is at /apps (My Apps). We skip Phases A-G and run just app creation +
publish + perms/token via the same helper functions in master_account_create.py.

Usage:
    BLOB=<blob> railway run python3 resume_from_apps.py "Validated Profile 1"

Optional env:
    FB_APP_NAME=<name>       (default: random)
    IG_APP_NAME=<name>       (default: random)
    FB_USE_CASE=<text>       (default: "Manage everything on your Page")
    IG_USE_CASE=<text>       (default: "Manage messaging & content on Instagram")
    SKIP_SHARD1=1            skip publish phase
    SKIP_SHARD2=1            skip perms+token phase
"""
import os, sys, asyncio, requests, random

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import everything we need from master
from master_account_create import (
    parse_blob, random_name, hb, shot, safe_screenshot, vision, pj,
    create_app_wizard, phase_k_publish, phase_l_perms_and_token,
    save_account_record,
    hbeh,
)
import privacy as privacy_mod
import meta_dev as mdm
import accounts_sheet as asm
from playwright.async_api import async_playwright

GL = os.environ['GOLOGIN_API_KEY']
BLOB = os.environ['BLOB']


async def main(profile_name):
    profs = mdm._list_validated_profiles()
    target = next((p for p in profs if p['name'] == profile_name), None)
    if not target:
        sys.exit(f'profile {profile_name!r} not found')
    profile_id = target['id']
    acc = parse_blob(BLOB)

    hb(f'━━━ RESUME from Phase H (apps) on profile {profile_id[:12]}… ━━━')

    fb_app_name = os.environ.get('FB_APP_NAME') or random_name()
    ig_app_name = os.environ.get('IG_APP_NAME') or random_name()
    while ig_app_name == fb_app_name: ig_app_name = random_name()
    FB_USE_CASE = os.environ.get('FB_USE_CASE') or 'Manage everything on your Page'
    IG_USE_CASE = os.environ.get('IG_USE_CASE') or 'Manage messaging & content on Instagram'
    hb(f'🎲 FB app={fb_app_name!r} ({FB_USE_CASE}) | IG app={ig_app_name!r} ({IG_USE_CASE})')

    result = {
        'fb_app': fb_app_name, 'ig_app': ig_app_name,
        'fb_app_id': None, 'ig_app_id': None,
        'rental_phone': os.environ.get('REUSE_RENTAL_PHONE', ''),
        'rental_id': os.environ.get('REUSE_RENTAL_ID', ''),
        'role': 'resumed',
        'privacy_url': None,
        'status': 'in_progress',
    }

    cdp = f'wss://cloudbrowser.gologin.com/connect?token={GL}&profile={profile_id}'
    async with async_playwright() as p:
        br = await p.chromium.connect_over_cdp(cdp, timeout=60000)
        ctx = br.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.bring_to_front()

        # Navigate to /apps with retries
        for goto_try in range(4):
            try:
                await page.goto('https://developers.facebook.com/apps', wait_until='domcontentloaded', timeout=60000)
                break
            except Exception as e:
                hb(f'⚠️ goto /apps attempt {goto_try+1}/4: {str(e)[:120]}')
                await asyncio.sleep(8 + goto_try*4)
        await hbeh.sleep(5, 10)
        shot(await safe_screenshot(page), '⏭ resumed at /apps — starting FB app creation')

        # ─── Phase H: Create FB app ───
        fb_id = await create_app_wizard(page, fb_app_name, FB_USE_CASE, acc['fb_pw'])
        result['fb_app_id'] = fb_id
        hb(f'FB app: {fb_app_name} → {fb_id}')

        # ─── Phase I: navigate back + Create IG app ───
        try:
            await page.goto('https://developers.facebook.com/apps', wait_until='domcontentloaded', timeout=60000)
        except: pass
        await hbeh.sleep(5, 10)
        ig_id = await create_app_wizard(page, ig_app_name, IG_USE_CASE, acc['fb_pw'])
        result['ig_app_id'] = ig_id
        hb(f'IG app: {ig_app_name} → {ig_id}')

        # Final-state caption (truthful)
        fb_status = f'{fb_app_name}={fb_id}' if fb_id else f'❌ {fb_app_name} FAILED'
        ig_status = f'{ig_app_name}={ig_id}' if ig_id else f'❌ {ig_app_name} FAILED'
        shot(await safe_screenshot(page), f'7️⃣ final state — FB: {fb_status} | IG: {ig_status}')

        # ─── Privacy URL ───
        if fb_id:
            try:
                url, err = privacy_mod._create_telegraph_privacy_policy(fb_app_name)
                result['privacy_url'] = url
                hb(f'privacy URL: {url}')
            except Exception as e:
                hb(f'privacy err: {e}')

        # ─── Phase K: publish FB app ───
        if os.environ.get('SKIP_SHARD1') != '1' and fb_id and result['privacy_url']:
            try:
                await phase_k_publish(page, fb_id, result['privacy_url'], acc['fb_pw'], result)
            except Exception as e:
                hb(f'⚠️ shard1 failed: {e}')
                result['shard1_error'] = str(e)

        # ─── Phase L: perms + token ───
        if os.environ.get('SKIP_SHARD2') != '1' and fb_id and result.get('app_secret'):
            try:
                await phase_l_perms_and_token(page, ctx, fb_id, result['app_secret'], acc['fb_pw'], result)
            except Exception as e:
                hb(f'⚠️ shard2 failed: {e}')
                result['shard2_error'] = str(e)

    # Truthful status
    fb_ok = bool(result.get('fb_app_id'))
    ig_ok = bool(result.get('ig_app_id'))
    pub_ok = bool(result.get('published'))
    token_ok = bool(result.get('long_user_token'))
    if token_ok and pub_ok:
        result['status'] = 'apps_created_published_with_token'
    elif fb_ok and ig_ok:
        result['status'] = 'apps_created' + ('_published' if pub_ok else '')
    elif fb_ok or ig_ok:
        result['status'] = f'partial_apps_created (FB={"ok" if fb_ok else "fail"}, IG={"ok" if ig_ok else "fail"})'
    else:
        result['status'] = 'app_creation_failed'

    save_account_record(profile_name, acc, result)
    print(f'DONE  status={result.get("status")}')


if __name__ == '__main__':
    profile_name = sys.argv[1]
    asyncio.run(main(profile_name))
