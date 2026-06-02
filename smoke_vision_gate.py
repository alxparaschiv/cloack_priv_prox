#!/usr/bin/env python3
"""Smoke test for the 3-stage vision_gate (DESCRIBE → ANSWER) used in
master_account_create.py. Validates that GPT-4o's output conforms to the
expected format on real FB/AC/dashboard screenshots.

Usage:
    OPENAI_API_KEY=sk-... python3 smoke_vision_gate.py path1.png "Is this the wizard Register dialog?" \
                                                      path2.png "Is this the AC Add mobile number modal with phone typed?"

Or run with the built-in test cases:
    OPENAI_API_KEY=sk-... python3 smoke_vision_gate.py --self-test ~/Desktop

Returns nonzero if any reply fails to parse into a YES/NO answer.
"""
import os, sys, base64, json, re, glob
import requests

OAI = os.getenv('OPENAI_API_KEY')
if not OAI:
    print('❌ OPENAI_API_KEY not set in env'); sys.exit(2)


def gate(png_bytes, question):
    """Same prompt master_account_create.vision_gate uses."""
    b64 = base64.b64encode(png_bytes).decode()
    prompt = (
        "TWO-PART CHECK. Be precise — your description must match the image, not the question.\n"
        "PART A (DESCRIBE): In one sentence, describe what is literally on the page right now "
        "(main heading + main interactive element/state). Do NOT mention the question yet.\n"
        f"PART B (ANSWER the question): {question}\n\n"
        "Reply EXACTLY in this format (two lines):\n"
        "DESCRIBE: <1-sentence factual description of what's on screen>\n"
        "ANSWER: YES — <1-sent reason> | OR | ANSWER: NO — <1-sent reason>"
    )
    r = requests.post('https://api.openai.com/v1/chat/completions',
        headers={'Authorization': f'Bearer {OAI}'},
        json={'model': 'gpt-4o',
              'messages': [{'role': 'user', 'content': [
                  {'type': 'text', 'text': prompt},
                  {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}}]}],
              'max_tokens': 300},
        timeout=45)
    return r.json()['choices'][0]['message']['content']


def parse(reply):
    upper = reply.upper()
    yes = ('ANSWER: YES' in upper) or ('ANSWER:YES' in upper)
    no = ('ANSWER: NO' in upper) or ('ANSWER:NO' in upper)
    if not yes and not no:
        for ln in reply.strip().splitlines():
            t = ln.strip().upper()
            if t.startswith('YES'): yes = True; break
            if t.startswith('NO'):  no = True; break
    describe = ''
    for ln in reply.strip().splitlines():
        if ln.upper().startswith('DESCRIBE'):
            describe = ln.split(':', 1)[1].strip() if ':' in ln else ln
            break
    return {'yes': yes, 'no': no, 'parsed_describe': describe[:200], 'raw': reply}


def run_one(path, question, expected=None):
    if not os.path.exists(path):
        print(f'  ❌ missing: {path}'); return False
    with open(path, 'rb') as f: png = f.read()
    print(f'\n📸 {os.path.basename(path)}')
    print(f'   Q: {question}')
    try:
        reply = gate(png, question)
    except Exception as e:
        print(f'   ❌ API err: {e}'); return False
    out = parse(reply)
    if out['yes']:    ans = 'YES'
    elif out['no']:   ans = 'NO'
    else:             ans = '⚠️ UNPARSEABLE'
    print(f'   DESCRIBE: {out["parsed_describe"]}')
    print(f'   ANSWER:   {ans}')
    if expected is not None:
        match = (expected == 'YES' and out['yes']) or (expected == 'NO' and out['no'])
        print(f'   EXPECTED: {expected} → {"✅ PASS" if match else "❌ FAIL"}')
        return match
    return out['yes'] or out['no']


def self_test(desktop_dir):
    """Pick 5 most recent screenshots from desktop, run sanity questions."""
    files = sorted(glob.glob(os.path.join(desktop_dir, 'Screenshot*.png')), key=os.path.getmtime, reverse=True)
    if not files:
        print(f'no screenshots in {desktop_dir}'); return False
    print(f'🧪 self-test on {min(5, len(files))} most recent screenshots from {desktop_dir}')
    # The questions are deliberately generic — purpose is to verify the format parses,
    # not to verify domain-specific correctness (since the user's manual screenshots
    # may not represent FB wizard states).
    cases = []
    for f in files[:5]:
        cases.append((f, "Does this screenshot contain readable English text and at least one button or interactive element?"))
    results = [run_one(p, q) for p, q in cases]
    n_pass = sum(1 for r in results if r)
    print(f'\n📊 self-test: {n_pass}/{len(results)} replies parsed cleanly into YES/NO')
    return n_pass == len(results)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == '--self-test':
        ok = self_test(sys.argv[2])
        sys.exit(0 if ok else 1)
    args = sys.argv[1:]
    if len(args) < 2 or len(args) % 2 != 0:
        print(__doc__); sys.exit(2)
    pairs = list(zip(args[0::2], args[1::2]))
    results = [run_one(p, q) for p, q in pairs]
    n_pass = sum(1 for r in results if r)
    print(f'\n📊 {n_pass}/{len(results)} replies parsed cleanly')
    sys.exit(0 if n_pass == len(results) else 1)


if __name__ == '__main__':
    main()
