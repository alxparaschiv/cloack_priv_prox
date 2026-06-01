"""FULL PIPELINE — Stages 0-12 end-to-end for one Meta Dev account.

Chains:
  1. master_account_create.run_account()      — Stages 0-9 (login, AC bind, wizard, FB + IG apps)
  2. shard1_publish.main()                    — Stages 10-11 (privacy URL, save, Show secret, publish)
  3. shard2_perms_token.main()                — Stage 12 (Customize 4 perms, Explorer typeahead 5 perms,
                                                Generate Access Token, OAuth walker, extend programmatically)
  4. Save final Drive blob with all credentials + CSV upsert.

Usage:
  python3 full_pipeline.py <blob> <profile_name>

  Optional env:
    REUSE_RENTAL_ID + REUSE_RENTAL_PHONE  — reuse existing rental instead of buying new
    SKIP_SESSION_RESTART=1                — skip GoLogin DELETE+POST (use existing live session)
    SKIP_AC_BINDING=1                     — skip Phase C-NEW (phone already bound externally)
    SKIP_SHARD1=1                         — skip stage 10 (already published)
    SKIP_SHARD2=1                         — skip stage 12 (already have token)

Triggered from Telegram via /setup_full on bot.py.
"""
import sys, os, asyncio, json, subprocess, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, '/app')
import master_account_create as mac

def hb(t):
    print(f'FP: {t}', flush=True)
    import requests
    try: requests.post(f'https://api.telegram.org/bot{os.environ["TELEGRAM_BOT_TOKEN"]}/sendMessage', json={'chat_id': 1984534885, 'text': f'🛤 FP: {t[:300]}'}, timeout=15)
    except: pass

async def main():
    blob = sys.argv[1]
    profile_name = sys.argv[2]
    acc = mac.parse_blob(blob)
    profs = __import__('meta_dev')._list_validated_profiles()
    target = next((p for p in profs if p['name'] == profile_name), None)
    if not target: sys.exit(f'profile {profile_name!r} not found')
    profile_id = target['id']
    fb_pw = acc['fb_pw']

    hb(f'━━━ FULL PIPELINE START: {profile_name} / {acc["email"]} ━━━')
    pipeline_start = time.time()

    # ─── STAGES 0-9 (master_account_create) ──────────────────────────────
    hb('▶ stages 0-9: master_account_create.run_account()')
    result = await mac.run_account(profile_id, acc)
    if result is None or result.get('status') != 'apps_created':
        hb(f'❌ master failed — status={result.get("status") if result else "None"}')
        # Still save the partial result
        mac.save_account_record(profile_name, acc, result or {'status':'master_failed','rental_phone':'','rental_id':'','role':'','fb_app':None,'fb_app_id':None,'ig_app':None,'ig_app_id':None,'privacy_url':None})
        return
    fb_app_id = result['fb_app_id']
    ig_app_id = result.get('ig_app_id')
    privacy_url = result.get('privacy_url')
    fb_app_name = result['fb_app']
    hb(f'✅ master done — FB={fb_app_id} IG={ig_app_id}')

    # ─── STAGE 10 (shard1: privacy + publish + secret) ───────────────────
    app_secret = None
    if os.environ.get('SKIP_SHARD1') != '1':
        hb('▶ stage 10: shard1_publish (privacy URL + save + publish + secret)')
        env = {**os.environ, 'SHARD1_APP_ID': str(fb_app_id), 'SHARD1_PRIVACY_URL': privacy_url or '', 'SHARD1_FB_PW': fb_pw}
        proc = subprocess.run(['python3', '/app/shard1_publish.py', profile_id], env=env, capture_output=True, text=True, timeout=900)
        hb(f'shard1 stdout (last 300): {proc.stdout[-300:]}')
        if proc.returncode != 0:
            hb(f'❌ shard1 returned {proc.returncode}; stderr: {proc.stderr[-400:]}')
        else:
            try:
                if os.path.exists('/tmp/shard1_result.json'):
                    s1 = json.loads(open('/tmp/shard1_result.json').read())
                    app_secret = s1.get('app_secret')
                    hb(f'✅ shard1 done — published={s1.get("published")} secret={app_secret[:10] if app_secret else None}...')
            except Exception as e: hb(f'shard1 result parse err: {e}')
    else:
        hb('⏭ SKIP_SHARD1=1, stage 10 skipped')

    # ─── STAGE 12 (shard2: perms + token) ────────────────────────────────
    long_user_token = None
    scopes = []
    if os.environ.get('SKIP_SHARD2') != '1':
        if not app_secret:
            hb('⚠️ no app_secret captured — cannot extend token. Skipping shard2.')
        else:
            hb('▶ stage 12: shard2_perms_token (customize 4 perms + 10min cooldown + Explorer 5 perms + Generate + extend)')
            env = {**os.environ, 'APP_ID': str(fb_app_id), 'APP_SECRET': app_secret, 'FB_PW': fb_pw}
            proc = subprocess.run(['python3', '/app/shard2_perms_token.py', profile_id], env=env, capture_output=True, text=True, timeout=2400)
            hb(f'shard2 stdout (last 400): {proc.stdout[-400:]}')
            if proc.returncode != 0:
                hb(f'❌ shard2 returned {proc.returncode}; stderr: {proc.stderr[-400:]}')
            else:
                try:
                    if os.path.exists('/tmp/shard2_result.json'):
                        s2 = json.loads(open('/tmp/shard2_result.json').read())
                        long_user_token = s2.get('long_user_token')
                        scopes = s2.get('scopes', [])
                        hb(f'✅ shard2 done — token={long_user_token[:30] if long_user_token else None}... scopes={scopes}')
                except Exception as e: hb(f'shard2 result parse err: {e}')
    else:
        hb('⏭ SKIP_SHARD2=1, stage 12 skipped')

    # ─── Final save: enrich the Drive blob with shard1+2 result ──────────
    if app_secret or long_user_token:
        result['app_secret'] = app_secret
        result['long_user_token'] = long_user_token
        result['scopes'] = scopes
        result['status'] = 'apps_created_published_with_token' if long_user_token else ('apps_created_published' if app_secret else 'apps_created')
        # Update notes to include shard1/2 results — re-call save_account_record with enriched result
        # save_account_record reads result.get('status') etc.
    mac.save_account_record(profile_name, acc, result)
    elapsed = time.time() - pipeline_start
    hb(f'🎉 FULL PIPELINE DONE in {elapsed/60:.1f} min — status={result["status"]}')

if __name__ == '__main__':
    asyncio.run(main())
