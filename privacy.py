"""
/privacy — Privacy policy generator.

Generates randomized Meta-compliant privacy policies. Two layers of
variability so multiple Meta developer accounts don't share a
fingerprint:
  1. Structure jitter — required + optional sections, ~30% drop rate
  2. Phrasing jitter — every header + paragraph picked from 3-5 variants
  3. Anti-fingerprint pass — synonym substitution + sentence dropout

Two host providers (round-robin via user choice):
  - telegra.ph (Telegram's anonymous publishing platform)
  - rentry.co (anonymous markdown paste host)

The content is rendered once then converted to the host's native format.
"""

import os
import json
import time
import logging
import random as _random
import html as _h
import re

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


# Public entry point: privacy_command + privacy_provider_callback +
# privacy_text_received (the last is called by bot.py's central router
# when expecting_privacy_app_name is set).


def _telegraph_privacy_policy_content(app_name):
    """Generate randomized Meta-compliant privacy policy in telegra.ph
    node format. See module docstring for variability details."""
    safe = (app_name or 'this App').strip() or 'this App'
    today = time.strftime('%B %d, %Y')
    R = _random.Random()
    heading_tag = R.choice(['h3', 'h4'])

    def h(text): return {'tag': heading_tag, 'children': [text]}
    def p(*children): return {'tag': 'p', 'children': list(children)}
    def li(text): return {'tag': 'li', 'children': [text]}
    def ul_pick(items, lo, hi):
        n = R.randint(lo, min(hi, len(items)))
        return {'tag': 'ul', 'children': [li(t) for t in R.sample(items, n)]}
    def pick(*opts): return R.choice(opts)

    def sec_intro():
        out = [p(pick(
            f"Effective date: {today}.",
            f"Last updated: {today}.",
            f"This policy is effective as of {today}.",
            f"In effect from {today}.",
        ))]
        out.append(p(pick(
            f"This Privacy Policy describes how {safe} (\"we\", \"us\", "
            f"or \"our\") collects, uses, and shares information when "
            f"you use the application.",
            f"This document explains the data practices of {safe} "
            f"(referred to as \"we\" or \"the developer\") in connection "
            f"with the application.",
            f"At {safe}, we take privacy seriously. This Policy outlines "
            f"what information we gather and how we handle it.",
            f"This Privacy Notice applies to {safe} and clarifies the "
            f"data we process when you interact with the service.",
            f"The following statement explains how {safe} treats personal "
            f"information collected through the application.",
        )))
        return out

    def sec_collect():
        return [h(pick("Information We Collect", "Data We Collect",
                       "What We Collect", "Information Gathered")),
                p(pick(
                    "Depending on the features you use, we may collect:",
                    "We may receive or collect the following:",
                    "The data we process can include:",
                )),
                ul_pick([
                    "Email address and contact details you provide",
                    "Profile information you choose to share",
                    "Content you submit through the application",
                    "Device and browser information",
                    "Usage analytics and interaction data",
                    "IP address and approximate location",
                ], 3, 5)]

    def sec_use():
        return [h(pick("How We Use Your Information", "Use of Data",
                       "Purposes of Processing", "Why We Process Data")),
                p(pick(
                    "We use the information we collect to provide, "
                    "maintain, and improve the service.",
                    "The data helps us deliver core functionality, "
                    "improve features, and ensure security.",
                    "Information is used to operate and personalize "
                    "the application.",
                ))]

    def sec_sharing():
        return [h(pick("Sharing of Information", "Who We Share With",
                       "Disclosure of Data")),
                p(pick(
                    "We do not sell your personal information. We may "
                    "share data with service providers acting on our "
                    "behalf, or when required by law.",
                    "Information is not sold to third parties. Limited "
                    "sharing occurs with subprocessors who help us "
                    "operate the service.",
                ))]

    def sec_retention():
        return [h(pick("Data Retention", "How Long We Keep Data")),
                p(pick(
                    "We retain data only as long as necessary to fulfill "
                    "the purposes outlined in this policy.",
                    "Information is kept for the period required to "
                    "operate the service and comply with legal duties.",
                ))]

    def sec_rights():
        return [h(pick("Your Rights", "User Rights", "Choices You Have")),
                p(pick(
                    "Depending on your jurisdiction, you may have rights "
                    "to access, correct, or delete your information.",
                    "You may have legal rights regarding your data. "
                    "Contact us to exercise them.",
                ))]

    def sec_cookies():
        return [h(pick("Cookies and Tracking", "Use of Cookies")),
                p(pick(
                    "We may use cookies and similar tracking technologies "
                    "to maintain sessions and improve user experience.",
                    "The application uses cookies for authentication and "
                    "to remember preferences.",
                ))]

    def sec_children():
        return [h(pick("Children's Privacy", "Use by Minors")),
                p(pick(
                    "The service is not directed to children under 13 "
                    "and we do not knowingly collect data from them.",
                    "We do not intentionally collect personal information "
                    "from minors under 13.",
                ))]

    def sec_third_party():
        return [h(pick("Third-Party Services", "Third Parties")),
                p(pick(
                    "The application may integrate with third-party "
                    "services whose own privacy policies govern your "
                    "data on those platforms.",
                    "We work with third-party providers; their handling "
                    "of data is governed by their own policies.",
                ))]

    def sec_security():
        return [h(pick("Security", "Data Security")),
                p(pick(
                    "We implement reasonable measures to protect your "
                    "data, but no method of transmission is 100% secure.",
                    "Industry-standard safeguards protect information; "
                    "however, no system can be guaranteed completely "
                    "secure.",
                ))]

    def sec_changes():
        return [h(pick("Changes to This Policy", "Policy Updates")),
                p(pick(
                    "We may update this Policy from time to time. "
                    "Continued use of the service after changes "
                    "constitutes acceptance.",
                    "This Policy may be revised. Material changes will "
                    "be reflected in the effective date above.",
                ))]

    def sec_international():
        return [h(pick("International Data Transfers",
                       "International Transfers")),
                p(pick(
                    "Your information may be processed in countries "
                    "other than your own. We take steps to ensure "
                    "adequate protection.",
                    "Cross-border data transfers may occur in the course "
                    "of operating the service.",
                ))]

    def sec_legal_basis():
        return [h(pick("Legal Basis for Processing",
                       "Legal Bases We Rely On")),
                p(pick(
                    "When applicable, the lawful bases on which we rely "
                    "include user consent, contractual necessity, our "
                    "legitimate interests, and compliance with legal "
                    "duties.",
                    "Processing is grounded in your consent, performance "
                    "of a contract, our legitimate interests, or "
                    "applicable law.",
                ))]

    def sec_contact():
        return [h(pick("Contact Us", "How to Contact Us", "Contact")),
                p(pick(
                    f"Questions about this Policy can be sent to the "
                    f"developer's support channel listed in the "
                    f"application.",
                    f"For privacy-related questions about {safe}, please "
                    f"reach out via the in-app support contact.",
                ))]

    # Assemble — required + shuffled optional + drop probability
    nodes = []
    for sec in (sec_intro, sec_collect, sec_use):
        nodes.extend(sec())
    optional = [sec_sharing, sec_retention, sec_rights, sec_cookies,
                sec_children, sec_third_party, sec_security,
                sec_changes, sec_international, sec_legal_basis]
    R.shuffle(optional)
    drop_p = R.uniform(0.20, 0.40)
    for sec in optional:
        if R.random() >= drop_p:
            nodes.extend(sec())
    nodes.extend(sec_contact())
    # Anti-fingerprint pass
    return _walk_apply_privacy_anti_fingerprint(nodes, R)


# ─── Synonym substitution + sentence dropout ──────────────────────────

_PRIVACY_SYNONYMS = {
    r'\bcollect\b':       ['collect', 'gather', 'obtain', 'receive', 'capture'],
    r'\bcollects\b':      ['collects', 'gathers', 'obtains', 'receives'],
    r'\bcollected\b':     ['collected', 'gathered', 'obtained'],
    r'\buse\b':           ['use', 'process', 'utilize', 'handle'],
    r'\buses\b':          ['uses', 'processes', 'utilizes'],
    r'\busing\b':         ['using', 'processing', 'utilizing'],
    r'\bshare\b':         ['share', 'disclose', 'transfer', 'release'],
    r'\bshares\b':        ['shares', 'discloses', 'transfers'],
    r'\bshared\b':        ['shared', 'disclosed', 'transferred'],
    r'\bsharing\b':       ['sharing', 'disclosing', 'transferring'],
    r'\binformation\b':   ['information', 'data', 'details', 'records'],
    r'\bdata\b':          ['data', 'information', 'records'],
    r'\bwe\b':            ['we', 'we', 'we', 'our team'],
    r'\bWe\b':            ['We', 'We', 'We', 'Our team'],
    r'\bour\b':           ['our', 'our', "the developer's"],
    r'\bapp\b':           ['app', 'application', 'service'],
    r'\bapplication\b':   ['application', 'app', 'service'],
    r'\buser\b':          ['user', 'visitor', 'individual'],
    r'\busers\b':         ['users', 'visitors', 'individuals'],
    r'\bmay\b':           ['may', 'might', 'could'],
    r'\bcookies\b':       ['cookies', 'tracking technologies'],
    r'\bprovide\b':       ['provide', 'supply', 'deliver'],
    r'\bservice\b':       ['service', 'application', 'platform'],
    r'\babout\b':         ['about', 'regarding', 'concerning'],
    r'\bsuch as\b':       ['such as', 'including', 'like'],
    r'\bin order to\b':   ['in order to', 'to', 'so that we may'],
    r'\bplease\b':        ['please', 'kindly'],
}


def _apply_privacy_word_subs(text, R):
    if not isinstance(text, str) or not text:
        return text
    for pattern, alts in _PRIVACY_SYNONYMS.items():
        def _sub(m, alts=alts):
            original = m.group(0)
            new = R.choice(alts)
            if original and original[0].isupper() and new and not new[0].isupper():
                new = new[0].upper() + new[1:]
            return new
        try:
            text = re.sub(pattern, _sub, text)
        except Exception:
            pass
    return text


def _apply_privacy_sentence_dropout(text, R, drop_prob=0.15):
    if not isinstance(text, str) or not text:
        return text
    sentences = re.split(r'(?<=[.!?])\s+', text)
    if len(sentences) < 3:
        return text
    kept = []
    for s in sentences:
        if R.random() < drop_prob and len(kept) >= 2:
            continue
        kept.append(s)
    return ' '.join(kept)


def _walk_apply_privacy_anti_fingerprint(nodes, R):
    if isinstance(nodes, str):
        s = _apply_privacy_word_subs(nodes, R)
        return _apply_privacy_sentence_dropout(s, R)
    if isinstance(nodes, list):
        return [_walk_apply_privacy_anti_fingerprint(n, R) for n in nodes]
    if isinstance(nodes, dict):
        new = dict(nodes)
        tag = new.get('tag', '')
        if 'children' in new:
            if tag in ('h3', 'h4'):
                new['children'] = [
                    _apply_privacy_word_subs(c, R) if isinstance(c, str)
                    else _walk_apply_privacy_anti_fingerprint(c, R)
                    for c in new['children']]
            else:
                new['children'] = [_walk_apply_privacy_anti_fingerprint(c, R)
                                    for c in new['children']]
        return new
    return nodes


# ─── Telegra.ph host ──────────────────────────────────────────────────

def _create_telegraph_privacy_policy(app_name):
    safe = (app_name or '').strip()
    if not safe:
        return None, "App name is empty."
    if len(safe) > 64:
        return None, "App name too long (max 64 chars)."
    short = safe[:32] or 'app'
    try:
        r = requests.post('https://api.telegra.ph/createAccount',
            data={'short_name': short, 'author_name': safe}, timeout=15)
        j = r.json()
        if not j.get('ok'):
            return None, f"createAccount failed: {j.get('error') or r.text[:160]}"
        token = j['result']['access_token']
    except Exception as e:
        return None, f"createAccount HTTP error: {e}"
    content = _telegraph_privacy_policy_content(safe)
    try:
        r = requests.post('https://api.telegra.ph/createPage',
            data={'access_token': token,
                  'title': f"Privacy Policy — {safe}",
                  'author_name': safe,
                  'content': json.dumps(content),
                  'return_content': 'false'}, timeout=15)
        j = r.json()
        if not j.get('ok'):
            return None, f"createPage failed: {j.get('error') or r.text[:160]}"
        return j['result']['url'], None
    except Exception as e:
        return None, f"createPage HTTP error: {e}"


# ─── Rentry.co host ───────────────────────────────────────────────────

def _telegraph_nodes_to_markdown(nodes):
    if isinstance(nodes, str):
        return nodes
    if isinstance(nodes, list):
        return ''.join(_telegraph_nodes_to_markdown(n) for n in nodes)
    if not isinstance(nodes, dict):
        return ''
    tag = nodes.get('tag', '')
    children = nodes.get('children', [])
    inner = _telegraph_nodes_to_markdown(children) if children else ''
    if tag == 'h3':   return f"\n### {inner}\n\n"
    if tag == 'h4':   return f"\n#### {inner}\n\n"
    if tag == 'p':    return f"{inner}\n\n"
    if tag == 'ul':
        items = []
        for c in children:
            if isinstance(c, dict) and c.get('tag') == 'li':
                items.append(f"- {_telegraph_nodes_to_markdown(c.get('children', []))}")
        return '\n'.join(items) + '\n\n'
    if tag == 'li':       return inner
    if tag == 'strong':   return f"**{inner}**"
    if tag == 'em':       return f"*{inner}*"
    if tag == 'a':
        href = (nodes.get('attrs') or {}).get('href', '')
        return f"[{inner}]({href})"
    if tag == 'blockquote': return f"> {inner}\n\n"
    if tag == 'hr':         return "\n---\n\n"
    return inner


def _create_rentry_privacy_policy(app_name):
    safe = (app_name or '').strip()
    if not safe:
        return None, "App name is empty."
    if len(safe) > 64:
        return None, "App name too long (max 64 chars)."
    nodes = _telegraph_privacy_policy_content(safe)
    md_body = _telegraph_nodes_to_markdown(nodes)
    md = f"# Privacy Policy — {safe}\n\n{md_body}"
    try:
        s = requests.Session()
        ua = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        s.get('https://rentry.co', timeout=15, headers={'User-Agent': ua})
        csrf = s.cookies.get('csrftoken')
        if not csrf:
            return None, "rentry.co CSRF cookie missing"
        r = s.post('https://rentry.co/api/new',
            data={'csrfmiddlewaretoken': csrf, 'text': md},
            headers={'Referer': 'https://rentry.co', 'User-Agent': ua},
            timeout=20)
        try:
            j = r.json()
        except Exception:
            return None, f"rentry.co non-JSON: {r.text[:200]}"
        if str(j.get('status')) != '200':
            return None, f"rentry.co failed: {j.get('content') or r.text[:200]}"
        return j.get('url'), None
    except Exception as e:
        return None, f"rentry.co HTTP error: {e}"


def _create_privacy_policy_dispatch(provider, app_name):
    if provider == 'rentry':
        return _create_rentry_privacy_policy(app_name)
    return _create_telegraph_privacy_policy(app_name)


# ─── Telegram handlers ────────────────────────────────────────────────

async def privacy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/privacy — start the provider-choice flow."""
    await update.message.reply_text(
        "📜 <b>Privacy Policy Generator</b>\n\n"
        "Pick a hosting provider:\n\n"
        "🪶 <b>Telegra.ph</b> — Telegram's publishing platform. URL: <code>telegra.ph/...</code>\n"
        "📄 <b>Rentry.co</b> — anonymous markdown paste host. URL: <code>rentry.co/...</code>\n\n"
        "<i>Both produce randomized policies. Alternating providers prevents "
        "Meta from fingerprinting accounts via shared privacy-URL domain.</i>",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🪶 Telegra.ph",
                                  callback_data="privacy_provider_telegraph")],
            [InlineKeyboardButton("📄 Rentry.co",
                                  callback_data="privacy_provider_rentry")],
        ]))


async def privacy_provider_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked a provider — prompt for app name."""
    query = update.callback_query
    await query.answer()
    provider = (query.data or '').replace('privacy_provider_', '')
    if provider not in ('telegraph', 'rentry'):
        provider = 'telegraph'
    context.user_data['privacy_provider'] = provider
    context.user_data['expecting_privacy_app_name'] = True
    provider_label = '🪶 Telegra.ph' if provider == 'telegraph' else '📄 Rentry.co'
    await query.edit_message_text(
        f"📜 <b>Privacy Policy Generator</b> — {provider_label}\n\n"
        f"Send the <b>name of your app</b> as your next message "
        f"(e.g. <code>Caroly Reel Bot</code>).\n\n"
        f"<i>Send /cancel to abort.</i>",
        parse_mode='HTML')


async def privacy_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called by the bot's main text router when expecting_privacy_app_name."""
    if not context.user_data.get('expecting_privacy_app_name'):
        return
    text = (update.message.text or '').strip()
    if text.lower() in ('/cancel', 'cancel'):
        context.user_data.pop('expecting_privacy_app_name', None)
        context.user_data.pop('privacy_provider', None)
        await update.message.reply_text("Privacy generation cancelled.")
        return
    context.user_data.pop('expecting_privacy_app_name', None)
    provider = context.user_data.pop('privacy_provider', 'telegraph')
    provider_label = '🪶 Telegra.ph' if provider == 'telegraph' else '📄 Rentry.co'
    import asyncio
    await update.message.reply_text(
        f"📜 Generating privacy policy for <b>{_h.escape(text)}</b> via {provider_label}…",
        parse_mode='HTML')
    try:
        url, err = await asyncio.to_thread(
            _create_privacy_policy_dispatch, provider, text)
    except Exception as e:
        url, err = None, f"crashed: {e}"
    if err or not url:
        await update.message.reply_text(
            f"❌ {provider_label} create failed: <code>{_h.escape(str(err)[:300])}</code>",
            parse_mode='HTML')
        return
    await update.message.reply_text(
        f"✅ <b>Privacy Policy created</b> ({provider_label})\n\n"
        f"<b>App:</b> <code>{_h.escape(text)}</code>\n"
        f"<b>URL:</b> {url}\n\n"
        f"Paste this URL into the Meta App dashboard's "
        f"<i>Privacy Policy URL</i> field.",
        parse_mode='HTML', disable_web_page_preview=False)
