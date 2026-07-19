"""Privacy policies must vary in STRUCTURE + register, not just wording, so no two
look alike and some read amateur/structureless. Verifies:
  • the rigid '8 topics / headed sections / 400-1100 words' clamp is GONE
  • each call rolls a structure shape; amateur personas bias to bare/listy/loose
  • the shape's instruction + word target reach the LLM prompt
  • the 4 compliance essentials (collect / use / Meta sharing / contact) are still required
Mocks OpenAI so nothing leaves the process."""
import privacy

ok = True
def chk(n, c):
    global ok; ok = ok and c; print(('  PASS — ' if c else '  FAIL — ') + n)

# ── prompt no longer forces the old professional skeleton ──
import random
persona = {'id': 'gen_z', 'voice': 'gen z casual lowercase'}
CAP = {}
def fake_post(url, headers=None, json=None, timeout=None):
    CAP['payload'] = json
    class R:
        status_code = 200
        def json(self): return {'choices': [{'message': {'content': '<p>hey we collect '
                                'basic stuff, use it to run the app, might share with meta, '
                                'dm us to ask questions</p>'}}]}
    return R()
privacy.requests.post = fake_post
import os
os.environ['OPENAI_API_KEY'] = 'sk-test'

html, err = privacy._llm_generate_privacy_html('Sample App', persona,
                                               shape=privacy._PRIV_SHAPES[2])  # 'bare'
sysmsg = CAP['payload']['messages'][0]['content']
chk("LLM call succeeds (mocked)", html and not err)
chk("dropped the rigid 'eight topics' clamp", 'eight topics' not in sysmsg.lower())
chk("dropped the fixed '400-1100 words' clamp", '400-1100' not in sysmsg)
chk("does NOT force h3/h4 headings on every doc",
    'ONLY if' in sysmsg and 'no headings' in sysmsg.lower())
chk("still requires the 4 compliance essentials",
    all(w in sysmsg.lower() for w in ('what data', 'how that data is used', 'meta apis', 'contact')))
chk("bare shape instruction reaches the prompt", 'NO headings at all' in sysmsg)
chk("bare shape's short word target reaches the prompt", '110-330' in sysmsg)

# ── shape roll biases amateur personas toward the messy shapes ──
R = random.Random(42)
amateur = [privacy._pick_shape(R, 'no_formal_education')['id'] for _ in range(400)]
prof = [privacy._pick_shape(R, 'corporate_lawyer')['id'] for _ in range(400)]
def frac(lst, ids): return sum(1 for x in lst if x in ids) / len(lst)
chk("amateur persona leans unstructured (bare/listy/loose > sectioned)",
    frac(amateur, {'bare', 'listy', 'loose'}) > 0.75)
chk("amateur persona rarely 'sectioned'", frac(amateur, {'sectioned'}) < 0.15)
chk("professional persona still often organized (sectioned/loose)",
    frac(prof, {'sectioned', 'loose'}) > 0.5)
chk("both registers CAN occur for a persona (not deterministic)",
    len(set(amateur)) >= 3 and len(set(prof)) >= 3)

# ── dispatch records the shape in meta + passes it down ──
SEEN = {}
def fake_llm(app, per, use_case=None, retries=4, shape=None):
    SEEN['shape'] = shape['id'] if shape else None
    SEEN['persona'] = per['id']
    return '<p>ok</p>', None
privacy._llm_generate_privacy_html = fake_llm
privacy._html_to_telegraph_nodes = lambda h: [{'tag': 'p', 'children': ['ok']}]
privacy._telegraph_nodes_to_markdown = lambda n: 'ok'
privacy._post_to_host = getattr(privacy, '_post_to_host', None)
# stub the host POST so no network: make provider path return a url
privacy._create_telegraph_page = lambda *a, **k: ('https://telegra.ph/x', None)
privacy._create_rentry_page = lambda *a, **k: ('https://rentry.co/x', None)

url, err, meta = privacy._create_privacy_policy_dispatch(app_name='Sample App', use_llm=True)
chk("dispatch rolled a shape and recorded it in meta",
    meta and meta.get('shape') in {'sectioned', 'loose', 'bare', 'listy'})
chk("dispatch passed the SAME shape down to the LLM", SEEN.get('shape') == meta.get('shape'))

print('\n' + ('✅ ALL PASS' if ok else '❌ FAIL'))
