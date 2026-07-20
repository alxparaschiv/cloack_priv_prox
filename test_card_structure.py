"""The operator's /account_pack card + .txt use the SAME 5-STEP block structure as
the autonomous VA card (1 proxy → 2 FB account → 3 FB Page → 4 Meta app → 5 workflow).
Backup managers skip the Page + Workflow steps."""
import account_pack
import fb_poster_registry as R

ok = True
def chk(n, c):
    global ok; ok = ok and c; print(('  PASS — ' if c else '  FAIL — ') + n)

REC = {
    'account': 'FB META POSTER 004', 'model': 'Carolina', 'kind': 'primary',
    'handle': 'carnivyn', 'password': 'WhisperingBamboo7#@', 'first': 'Marco', 'last': 'Bianchi',
    'gender': 'male', 'birthdate_display': 'September 9, 1998', 'age': 27, 'heritage': 'Italian',
    'phone10': '2607032128', 'proxy': 'socks5://u:p@geo.iproyal.com:12321',
    'page_name_1': 'Ember Witch', 'page_name_2': 'Storm Noir', 'page_category': 'Artist',
    'cloak_link': 'https://x/slug', 'bio': 'the goth girl', 'block_countries': 'India, Pakistan',
    'block_words': 'slop, ai', 'account_bg_image_url': 'https://drive/x', 'page_bg_image_url': 'https://drive/y',
    'app_name': 'test app', 'dev_app_role': 'Developer', 'privacy_url': 'https://telegra.ph/x',
    'rambler_login': 'bog@rambler.ru:pw', 'workflow_name': 'carolina_goth',
    'schedule': '4x_daily_v4', 'output_folder_name': 'Output Carolina Goth 1', 'created_utc': '2026-07-20',
}

# ── primary account: all 5 steps, in order ──
card = account_pack._format_card(1, 2, REC)
txt = R.account_txt(REC)
for name, s in (('card', card), ('txt', txt)):
    chk(f"{name}: has STEP 1 Proxy", 'STEP 1' in s and 'Proxy' in s)
    chk(f"{name}: has STEP 2 Create the Facebook account", 'STEP 2' in s and 'Facebook account' in s)
    chk(f"{name}: has STEP 3 Create the Facebook Page", 'STEP 3' in s and 'Facebook Page' in s)
    chk(f"{name}: has STEP 4 Meta developer app", 'STEP 4' in s and 'Meta developer app' in s)
    chk(f"{name}: has STEP 5 Workflow", 'STEP 5' in s and 'Workflow' in s)
    # steps appear IN ORDER
    order = [s.index(f'STEP {i}') for i in range(1, 6)]
    chk(f"{name}: steps are in ascending order", order == sorted(order))
    # key fields land under the right step
    chk(f"{name}: proxy present", 'iproyal.com' in s)
    chk(f"{name}: handle + password present", 'carnivyn' in s and 'WhisperingBamboo7#@' in s)
    chk(f"{name}: page names present", 'Ember Witch' in s and 'Storm Noir' in s)
    chk(f"{name}: workflow folder present", 'Output Carolina Goth 1' in s)

# ── backup manager: skips Page (3) + Workflow (5); Meta app becomes STEP 3 ──
BM = dict(REC, kind='backup_manager', account='FB BACKUP MANAGER 001')
bcard = account_pack._format_card(1, 1, BM)
btxt = R.account_txt(BM)
for name, s in (('bm-card', bcard), ('bm-txt', btxt)):
    chk(f"{name}: no 'Facebook Page' step", 'Facebook Page' not in s)
    chk(f"{name}: no STEP 5 Workflow", 'STEP 5' not in s)
    chk(f"{name}: Meta app is STEP 3", 'STEP 3' in s and 'Meta developer app' in s)
    chk(f"{name}: still has proxy + account steps", 'STEP 1' in s and 'STEP 2' in s)

# ── minimal mode: 'use your own' preserved inside the structure ──
MIN = dict(REC, minimal=True, proxy='', phone10='')
mc = account_pack._format_card(1, 1, MIN)
chk("minimal: STEP 1 shows 'use your own proxy'", 'use your own proxy' in mc)
chk("minimal: FB phone shows 'use your own number'", 'use your own number' in mc)
chk("minimal: still structured (STEP 1..5)", all(f'STEP {i}' in mc for i in range(1, 6)))

print('\n' + ('✅ ALL PASS' if ok else '❌ FAIL'))
print('\n----- SAMPLE CARD -----\n' + card.replace('<b>', '').replace('</b>', '')
      .replace('<code>', '').replace('</code>', '').replace('<i>', '').replace('</i>', ''))
