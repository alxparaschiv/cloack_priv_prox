"""Shard 3 + 4 (2026-07-22, user):
  • Account-package profile pics = SOLID dark-palette colors only (no artistic/AI).
  • Compilation workflow names KEEP their trailing index and have NO trailing '_'.
Pure-unit; monkeypatches the Drive layer so nothing hits the network."""
import os
os.environ.setdefault('OPERATOR_TOKEN_PICKLE', 'x')
import account_pack as ap
import bg as _bg

ok = True
def ck(name, cond):
    global ok; ok = ok and cond
    print(('  PASS — ' if cond else '  FAIL — ') + name)

# ── SHARD 4: workflow name from folder ──────────────────────────────────────
print("── Shard 4: workflow name keeps index, no trailing '_' ──")
cases = {
    'Output Carolina Goth Cosplay Compilations 6': 'carolina_goth_cosplay_compilations_6',
    'Output Kira Goth Cosplay Compilations 6':     'kira_goth_cosplay_compilations_6',
    'Output Carolina Goth StackPOOLI_A Infinity Set 20 1':
        'carolina_goth_infinity_set_20_1',
    'Output Kira Goth StackPOOLG_AS-BR Mixed 1 1': 'kira_goth_mixed_1_1',
    '': '',
}
for folder, want in cases.items():
    got = ap._workflow_name_from_folder(folder)
    ck(f"{folder!r} → {got!r}", got == want)
    if got:
        ck(f"  …no trailing underscore ({got!r})", not got.endswith('_'))

# the compilation index MUST survive (the exact bug the user reported)
_comp = ap._workflow_name_from_folder('Output Carolina Goth Cosplay Compilations 6')
ck("compilation index '6' is present (was being dropped)", _comp.endswith('_6'))

# ── SHARD 3: profile-pic background = solid dark-palette color ───────────────
print("── Shard 3: profile bg is a SOLID dark-palette color ──")
_seen_styles = []
_orig_gen = _bg._generate_one_png
_bg._generate_one_png = lambda style_key: (_seen_styles.append(style_key)
                                           or (b'PNGDATA', f'bg_{style_key}.png'))
# stub the Drive plumbing account_pack pulls off artistic_bg_gen
_ag = ap.__dict__.get('artistic_bg_gen')
import artistic_bg_gen as _ag
_ag._drive_service = lambda: object()
_ag._ensure_folder = lambda svc, name, parent=None: 'FOLDER'
_ag.OUTPUT_ROOT_NAME = 'ROOT'
_ag._upload_bytes_to_drive = lambda svc, parent, fname, png, mime=None: 'DRIVEID_' + fname

_id = ap._gen_one_profile_bg('SomeAccount', procedural_only=True)
ck("bg generated an id", bool(_id) and _id.startswith('DRIVEID_'))
ck("bg used the 'solid' style (never 'artistic'/AI)", _seen_styles == ['solid'])

# palette must be dark/goth — no bright/neon (every channel stays muted)
def _bright(hexv):
    h = hexv.lstrip('#'); r, g, b = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
    return max(r, g, b)
_palette = [h for _, h in _bg.PROFILE_COLOR_PALETTE]
ck("palette is all-dark (no channel > 130 → no bright colors)",
   all(_bright(h) <= 130 for h in _palette))
ck("solid render works on a palette color",
   isinstance(_orig_gen('solid'), tuple))

print('\n' + ('✅ ALL PASS' if ok else '❌ FAIL'))
raise SystemExit(0 if ok else 1)
