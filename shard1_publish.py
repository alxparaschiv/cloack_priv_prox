"""SHARD 1 — set privacy URL + save + publish + capture App Secret.

Per project-shard1-publish-runbook.md.

Usage: SHARD1_APP_ID=<id> SHARD1_PRIVACY_URL=<url> SHARD1_FB_PW=<pw> python3 shard1_publish.py <profile_id>

For META APP 12:
  SHARD1_APP_ID=2107024670234005
  SHARD1_PRIVACY_URL=https://telegra.ph/Privacy-Policy--LaunchLy-06-01
  Profile: 6a1ca48c5cdf7b25a1f4f876
"""
import asyncio, os, sys, base64, requests, re, random, json
sys.path.insert(0, '/app')
from playwright.async_api import async_playwright

GL = os.environ['GOLOGIN_API_KEY']
TOK = os.environ['TELEGRAM_BOT_TOKEN']
OAI = os.environ['OPENAI_API_KEY']
CHAT = 1984534885

APP_ID = os.environ['SHARD1_APP_ID']
PRIVACY_URL = os.environ['SHARD1_PRIVACY_URL']
FB_PW = os.environ.get('SHARD1_FB_PW', '')
PID = sys.argv[1]

def hb(t):
    print(f'S1: {t}', flush=True)
    try: requests.post(f'https://api.telegram.org/bot{TOK}/sendMessage', json={'chat_id':CHAT,'text':f'⚙️ S1: {t[:300]}'}, timeout=15)
    except: pass

async def shot(ctx, page, label):
    try:
        c = await ctx.new_cdp_session(page)
        res = await asyncio.wait_for(c.send('Page.captureScreenshot', {'format':'jpeg','quality':55}), timeout=15)
        png = base64.b64decode(res['data'])
        requests.post(f'https://api.telegram.org/bot{TOK}/sendPhoto', files={'photo':('s.jpg',png,'image/jpeg')}, data={'chat_id':CHAT,'caption':f'[S1-{label}]'}, timeout=30)
    except Exception as e: hb(f'shot err: {e}')

async def j(min_s, max_s=None):
    """Jittered sleep."""
    if max_s is None: max_s = min_s * 1.4
    await asyncio.sleep(random.uniform(min_s, max_s))

async def main():
    cdp = f'wss://cloudbrowser.gologin.com/connect?token={GL}&profile={PID}'
    async with async_playwright() as pw:
        br = await pw.chromium.connect_over_cdp(cdp, timeout=60000)
        ctx = br.contexts[0]
        page = next((p for p in ctx.pages if 'developers.facebook.com' in p.url), None) or ctx.pages[0]
        await page.bring_to_front()
        hb(f'━━━ Shard 1: publish app {APP_ID} ━━━')

        # 1. Settings/Basic
        await page.goto(f'https://developers.facebook.com/apps/{APP_ID}/settings/basic/', wait_until='domcontentloaded', timeout=60000)
        await j(8, 14)
        await shot(ctx, page, '1-settings-basic')

        # 2. Find + fill privacy URL via placeholder
        priv_loc = page.locator('input[placeholder="Privacy policy for Login dialog and app details"]').first
        await priv_loc.scroll_into_view_if_needed(timeout=8000)
        await j(1, 2)
        await priv_loc.click()
        await page.keyboard.press('Control+A'); await j(0.3, 0.6)
        await page.keyboard.press('Delete'); await j(0.5, 1)
        await page.keyboard.type(PRIVACY_URL, delay=random.randint(50, 90))
        await j(2, 3.5)
        val = await priv_loc.input_value()
        hb(f'privacy typed: {val[:60]}')
        if val != PRIVACY_URL:
            hb(f'❌ privacy URL mismatch — got {val!r}'); return
        await shot(ctx, page, '2-privacy-typed')

        # 3. Save changes — locator + scroll into view
        save_loc = page.get_by_role('button', name='Save changes').first
        await save_loc.scroll_into_view_if_needed(timeout=8000)
        await j(1.5, 2.5)
        await save_loc.click(timeout=8000)
        hb('Save changes clicked'); await j(7, 12)
        await shot(ctx, page, '3-after-save')

        # 4. Reload + verify persistence
        await page.reload(wait_until='domcontentloaded'); await j(8, 12)
        final = await page.locator('input[placeholder="Privacy policy for Login dialog and app details"]').first.input_value()
        if final != PRIVACY_URL:
            hb(f'❌ privacy URL did not persist after reload — got {final!r}'); return
        hb('✅ privacy URL persisted'); await shot(ctx, page, '4-privacy-persisted')

        # 5. Capture App Secret via Show button
        secret = None
        try:
            # Find "Show" button next to App Secret field
            show_clicked = await page.evaluate("""() => {
                const all = Array.from(document.querySelectorAll('button,div[role=button],a'));
                for (const b of all) {
                    if ((b.innerText||'').trim() === 'Show' && b.offsetParent !== null) {
                        const r = b.getBoundingClientRect();
                        if (r.y > 200 && r.y < 700) { b.click(); return true; }
                    }
                }
                return false;
            }""")
            hb(f'Show clicked: {show_clicked}')
            await j(3, 5)
            await shot(ctx, page, '5-after-show')
            # Password modal — fill + submit
            pwin = await page.query_selector('input[type="password"]')
            if pwin and await pwin.is_visible():
                await pwin.click(); await j(1, 2)
                await pwin.fill('')
                await pwin.type(FB_PW, delay=random.randint(60, 100))
                await j(2, 3)
                await page.get_by_role('button', name='Submit').click(timeout=8000)
                hb('Submit password clicked')
                await j(5, 8)
                # Capture 32-hex secret
                secret = await page.evaluate("""() => {
                    for (const inp of document.querySelectorAll('input')) {
                        const val = (inp.value || '').trim();
                        if (/^[a-f0-9]{32}$/.test(val)) return val;
                    }
                    return null;
                }""")
                hb(f'App Secret captured: {secret[:10] if secret else None}...')
                await shot(ctx, page, '6-secret-captured')
        except Exception as e: hb(f'secret capture err: {e}')

        # 6. Navigate to Publish via SIDEBAR click (NOT direct URL — that 302s to dashboard)
        sidebar_pub = await page.evaluate("""() => {
            for (const el of document.querySelectorAll('div[role="button"], a, button')) {
                const t = (el.innerText || '').trim();
                if (t.includes('App Publish Status')) {
                    const r = el.getBoundingClientRect();
                    if (r.x < 300) return {x: r.x + r.width/2, y: r.y + r.height/2};
                }
            }
            return null;
        }""")
        if not sidebar_pub:
            hb('❌ sidebar App Publish Status item not found'); return
        hb(f'sidebar pub at ({sidebar_pub["x"]:.0f},{sidebar_pub["y"]:.0f})')
        await page.mouse.move(sidebar_pub['x']-30, sidebar_pub['y']-10, steps=8); await j(0.3, 0.7)
        await page.mouse.move(sidebar_pub['x'], sidebar_pub['y'], steps=5); await j(0.2, 0.4)
        await page.mouse.click(sidebar_pub['x'], sidebar_pub['y'])
        await j(8, 14)
        await shot(ctx, page, '7-go-live-page')

        # 7. Click action Publish button — use .last
        pub = page.get_by_role('button', name='Publish').last
        try: await pub.scroll_into_view_if_needed(timeout=5000)
        except: pass
        await j(1.5, 2.5)
        await pub.click(timeout=8000)
        hb('Publish action clicked')
        await j(12, 18)
        await shot(ctx, page, '8-after-publish')

        # 8. Verify Published: body should say "Published" and NOT "Unpublished"
        body = await page.evaluate("() => document.body.innerText.substring(0, 800)")
        if 'Published' in body and 'Unpublished' not in body[:500]:
            hb('✅ APP PUBLISHED'); await shot(ctx, page, '9-PUBLISHED')
        else:
            hb(f'⚠️ publish state unclear. body[:400]: {body[:400]}')

        # Output summary
        out = {
            'app_id': APP_ID,
            'privacy_url': PRIVACY_URL,
            'app_secret': secret,
            'published': 'Published' in body and 'Unpublished' not in body[:500],
        }
        hb(f'━━━ Shard 1 result: {json.dumps(out, default=str)[:300]}')
        # Save to /tmp for next stage to read
        open('/tmp/shard1_result.json', 'w').write(json.dumps(out))
        print('SHARD1_RESULT=' + json.dumps(out))

asyncio.run(main())
