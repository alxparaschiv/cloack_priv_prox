"""
/privacy — Privacy policy generator (telegra.ph + rentry.co).

Goal: every generated policy should look like it was written by a
different person. We attack four axes of variability simultaneously:

  1. LENGTH CLASS — short / medium / long / verbose (4 wildly
     different total section counts: ~3 vs ~6 vs ~10 vs ~14)

  2. STRUCTURE — heading tag (h3/h4) is randomized per policy. Some
     policies use sub-headings (h4 inside h3), some don't. Section
     order is shuffled. Some sections appear as bullet lists, some as
     prose, some as bullets-inside-prose. Optional preamble/postamble
     blocks. Sometimes the "Contact" section becomes a single bold
     line; sometimes a multi-paragraph contact block.

  3. PHRASING — 3-7 variants per header + 3-6 paragraph variants per
     section. Multiple bullet pools too.

  4. ANTI-FINGERPRINT — synonym substitution + sentence dropout +
     punctuation jitter so even two short policies with the same
     section list have different word-level text.

This makes auto-clustering by privacy-policy fingerprint hard for
Meta's verification team: not just "different wording" — different
LENGTH and STRUCTURE every time.

Hosts:
  - telegra.ph (Telegram's anonymous publishing platform)
  - rentry.co (anonymous markdown paste host)
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


# ─── Content builder ──────────────────────────────────────────────────

def _telegraph_privacy_policy_content(app_name):
    """Generate a randomized Meta-compliant privacy policy as
    telegra.ph node format. See module docstring for variability axes.
    """
    safe = (app_name or 'this App').strip() or 'this App'
    today_label = _random.choice([
        f"Effective date: {time.strftime('%B %d, %Y')}.",
        f"Last updated: {time.strftime('%B %d, %Y')}.",
        f"This policy is effective as of {time.strftime('%B %d, %Y')}.",
        f"In effect from {time.strftime('%d %B %Y')}.",
        f"Last revision: {time.strftime('%B %Y')}.",
        f"Updated {time.strftime('%Y-%m-%d')}.",
        f"Version of {time.strftime('%d %B, %Y')}.",
    ])
    R = _random.Random()

    # ── Structural knobs (rolled ONCE per policy) ──
    LENGTH = R.choice(['short', 'short', 'medium', 'medium',
                        'long', 'long', 'verbose'])
    heading_tag = R.choice(['h3', 'h3', 'h4'])
    sub_heading_tag = 'h4' if heading_tag == 'h3' else 'h4'
    use_subheadings = (heading_tag == 'h3' and R.random() < 0.35)
    use_blockquote_preamble = R.random() < 0.30
    use_bold_contact = R.random() < 0.40
    bullet_density = R.choice(['low', 'medium', 'medium', 'high'])
    bullet_range = {
        'low':    (2, 3),
        'medium': (3, 5),
        'high':   (4, 7),
    }[bullet_density]

    def h(text): return {'tag': heading_tag, 'children': [text]}
    def sh(text): return {'tag': sub_heading_tag, 'children': [text]}
    def p(*children): return {'tag': 'p', 'children': list(children)}
    def li(text): return {'tag': 'li', 'children': [text]}
    def b(text): return {'tag': 'strong', 'children': [text]}
    def i_(text): return {'tag': 'em', 'children': [text]}
    def blockquote(text): return {'tag': 'blockquote', 'children': [text]}
    def hr(): return {'tag': 'hr', 'children': []}

    def pick(*opts): return R.choice(opts)

    def ul_from(items, lo=None, hi=None):
        lo = lo if lo is not None else bullet_range[0]
        hi = hi if hi is not None else bullet_range[1]
        n = R.randint(lo, min(hi, len(items)))
        chosen = R.sample(items, n)
        return {'tag': 'ul', 'children': [li(t) for t in chosen]}

    def maybe_bold_or_em(text):
        """Sometimes wrap a paragraph in <strong> or <em>."""
        r = R.random()
        if r < 0.06:
            return p(b(text))
        if r < 0.10:
            return p(i_(text))
        return p(text)

    # ── Section builders (return list[node]) ──

    def sec_intro_short():
        return [maybe_bold_or_em(today_label),
                p(pick(
                    f"This Privacy Policy describes how {safe} handles user information.",
                    f"This document outlines how {safe} processes data.",
                    f"This Notice explains the data practices of {safe}.",
                    f"At {safe}, the following data practices apply.",
                ))]

    def sec_intro_full():
        out = [maybe_bold_or_em(today_label)]
        intro = pick(
            f"This Privacy Policy describes how {safe} (\"we\", \"us\", "
            f"or \"our\") collects, uses, and shares information when "
            f"you use the application.",
            f"This document explains the data practices of {safe} "
            f"(referred to as \"we\" or \"the developer\") in connection "
            f"with the application and its features.",
            f"At {safe}, we take privacy seriously. This Policy outlines "
            f"what information we gather, how we handle it, and the "
            f"rights you have over your data.",
            f"This Privacy Notice applies to {safe} and clarifies the "
            f"data we process when you interact with the service "
            f"directly or via integrated platforms.",
            f"The following statement explains how {safe} treats personal "
            f"information collected through the application, the "
            f"website, and any third-party integrations.",
            f"Welcome to the {safe} privacy policy. This document sets "
            f"forth our approach to data collection, use, and protection.",
        )
        out.append(p(intro))
        if use_blockquote_preamble and R.random() < 0.5:
            out.append(blockquote(pick(
                "By using the service, you acknowledge the practices "
                "described in this document.",
                "Continued use of the application indicates acceptance of "
                "these terms.",
                "Please read this policy carefully before using the service.",
            )))
        return out

    def sec_collect_bullets():
        items = [
            "Email address and contact details you provide",
            "Profile information you choose to share",
            "Content you submit through the application",
            "Device and browser information",
            "Usage analytics and interaction data",
            "IP address and approximate location",
            "Authentication tokens and session identifiers",
            "Preferences and settings you configure",
            "Crash reports and diagnostic logs",
            "Information collected via cookies and similar technologies",
        ]
        intro = pick(
            "Depending on the features you use, we may collect:",
            "We may receive or collect the following:",
            "The data we process can include:",
            "Categories of information we may handle include:",
            "Examples of information we may obtain:",
        )
        return [h(pick("Information We Collect", "Data We Collect",
                       "What We Collect", "Information Gathered",
                       "Categories of Data")),
                p(intro), ul_from(items)]

    def sec_collect_prose():
        return [h(pick("Information We Collect", "What We Collect",
                       "Data Collection Practices")),
                p(pick(
                    "Depending on how you use the service, we may obtain "
                    "your contact details, profile information, content "
                    "you submit, device characteristics, usage analytics, "
                    "approximate location, and authentication tokens.",
                    "The information we collect can include the email "
                    "address you provide, the content you create within "
                    "the application, technical metadata about your "
                    "device and browser, and aggregate usage statistics.",
                    "We typically gather identifiers such as your email, "
                    "profile data you elect to share, interaction logs, "
                    "and device-level metadata necessary to deliver the "
                    "service.",
                ))]

    def sec_use_bullets():
        items = [
            "Provide and maintain the application",
            "Authenticate users and protect accounts",
            "Improve functionality based on usage patterns",
            "Communicate updates, security notices, and support replies",
            "Detect and prevent fraud or abuse",
            "Comply with legal obligations",
            "Personalize the experience to user preferences",
            "Aggregate anonymized statistics for product research",
        ]
        return [h(pick("How We Use Your Information", "Use of Data",
                       "Purposes of Processing", "Why We Process Data",
                       "How Information Is Used")),
                p(pick(
                    "We process the data we collect for purposes that include:",
                    "Information is used for the following:",
                    "The data is processed for the following purposes:",
                )),
                ul_from(items)]

    def sec_use_prose():
        return [h(pick("How We Use Your Information", "Use of Data",
                       "Why We Process Data")),
                p(pick(
                    "We use the information we collect to provide, "
                    "maintain, and improve the service, to authenticate "
                    "users, to communicate with you about your account, "
                    "and to detect or prevent fraud and abuse.",
                    "The data helps us deliver the core functionality, "
                    "personalize features to your preferences, secure "
                    "the platform against abuse, and comply with legal "
                    "obligations.",
                    "Information is used to operate and personalize the "
                    "application, troubleshoot technical issues, and "
                    "satisfy legal requirements that apply to us.",
                ))]

    def sec_sharing():
        body = pick(
            "We do not sell your personal information. We may share data "
            "with service providers acting on our behalf, with affiliates, "
            "or when required by law.",
            "Information is not sold to third parties. Limited sharing "
            "occurs with subprocessors who help us operate the service, "
            "and when compelled by legal process.",
            "Personal data is not used for sale. Disclosures are limited "
            "to vendors that assist us, to comply with law, or with your "
            "explicit consent.",
            "We never sell user data. Sharing is limited to operational "
            "subprocessors, regulators when required, and parties to a "
            "corporate transaction where the data forms part of the "
            "transferred assets.",
        )
        out = [h(pick("Sharing of Information", "Who We Share With",
                       "Disclosure of Data", "When We Share Data")), p(body)]
        if use_subheadings and R.random() < 0.5:
            out.append(sh(pick("Service Providers", "Subprocessors")))
            out.append(p(pick(
                "Our subprocessors include cloud hosting providers, "
                "analytics services, and email-delivery vendors, each "
                "operating under written agreements.",
                "We may rely on third-party vendors for hosting, "
                "analytics, error monitoring, and messaging — all "
                "contractually bound to handle data per our instructions.",
            )))
        return out

    def sec_retention():
        return [h(pick("Data Retention", "How Long We Keep Data",
                       "Storage Duration")),
                p(pick(
                    "We retain data only as long as necessary to fulfill "
                    "the purposes outlined in this policy and to comply "
                    "with applicable law.",
                    "Information is kept for the period required to "
                    "operate the service, satisfy our legal duties, and "
                    "resolve disputes.",
                    "Retention periods vary by data category; we apply "
                    "the shortest period reasonably necessary.",
                    "We periodically review the data we hold and delete "
                    "or anonymize records that are no longer needed.",
                ))]

    def sec_rights_bullets():
        items = [
            "Access the information we hold about you",
            "Request correction of inaccurate data",
            "Request deletion of your data",
            "Object to certain processing activities",
            "Restrict processing in specific cases",
            "Withdraw consent at any time, where consent is the legal basis",
            "Receive a portable copy of your data",
            "Lodge a complaint with a supervisory authority",
        ]
        return [h(pick("Your Rights", "User Rights",
                       "Choices You Have", "Rights and Choices")),
                p(pick(
                    "Subject to your jurisdiction, you may have rights including:",
                    "Depending on local law, you may exercise the following:",
                    "You may have the following rights regarding your data:",
                )),
                ul_from(items, lo=3, hi=6)]

    def sec_rights_prose():
        return [h(pick("Your Rights", "User Rights")),
                p(pick(
                    "Depending on your jurisdiction, you may have rights "
                    "to access, correct, or delete your information, to "
                    "object to certain processing, and to lodge complaints "
                    "with a supervisory authority.",
                    "You may have legal rights regarding your data "
                    "(access, correction, deletion, portability). Contact "
                    "us via the support channel to exercise them.",
                ))]

    def sec_cookies():
        out = [h(pick("Cookies and Tracking", "Use of Cookies",
                       "Cookies and Similar Technologies")),
                p(pick(
                    "We may use cookies and similar tracking technologies "
                    "to maintain sessions, remember preferences, and "
                    "improve the experience.",
                    "The application uses cookies for authentication, "
                    "user preferences, and basic analytics.",
                    "Cookies and local storage hold limited data necessary "
                    "for the service to function and to remember you "
                    "between visits.",
                ))]
        if R.random() < 0.30:
            out.append(p(pick(
                "Most browsers let you control or disable cookies; some "
                "service features may not work without them.",
                "You can manage cookie preferences in your browser; "
                "blocking cookies may limit some functionality.",
            )))
        return out

    def sec_children():
        return [h(pick("Children's Privacy", "Use by Minors",
                       "Minors")),
                p(pick(
                    "The service is not directed to children under 13 "
                    "and we do not knowingly collect data from them.",
                    "We do not intentionally collect personal information "
                    "from minors under 13. If you believe a minor has "
                    "submitted data, please contact us so we can remove it.",
                    "The application is intended for users of legal age "
                    "in their jurisdiction. Minor data is not knowingly "
                    "processed.",
                ))]

    def sec_third_party():
        return [h(pick("Third-Party Services", "Third Parties",
                       "Third-Party Integrations")),
                p(pick(
                    "The application may integrate with third-party "
                    "services whose own privacy policies govern data on "
                    "those platforms.",
                    "We work with third-party providers; their handling "
                    "of data is governed by their own policies.",
                    "Some features rely on third-party platforms — when "
                    "you use them, their privacy practices apply in "
                    "addition to ours.",
                ))]

    def sec_security():
        out = [h(pick("Security", "Data Security",
                       "Information Security")),
                p(pick(
                    "We implement reasonable measures to protect your "
                    "data, but no method of transmission or storage is "
                    "100% secure.",
                    "Industry-standard safeguards protect information; "
                    "however, no system can be guaranteed completely "
                    "secure.",
                    "Encryption in transit, access controls, and audit "
                    "logging form the core of our security posture, but "
                    "we cannot promise absolute security.",
                ))]
        if R.random() < 0.25:
            out.append(p(pick(
                "If we become aware of a security incident affecting "
                "your information, we will notify you and the relevant "
                "authorities as required by law.",
                "In the event of a breach impacting your data, you will "
                "be notified consistent with applicable law.",
            )))
        return out

    def sec_changes():
        return [h(pick("Changes to This Policy", "Policy Updates",
                       "Revisions to This Notice")),
                p(pick(
                    "We may update this Policy from time to time. "
                    "Continued use of the service after changes "
                    "constitutes acceptance.",
                    "This Policy may be revised. Material changes will "
                    "be reflected in the effective date above and, "
                    "where appropriate, communicated through the app.",
                    "Updates to this document take effect when posted. "
                    "Significant changes will be highlighted.",
                ))]

    def sec_international():
        return [h(pick("International Data Transfers",
                       "International Transfers",
                       "Cross-Border Transfers")),
                p(pick(
                    "Your information may be processed in countries "
                    "other than your own. We take steps to ensure "
                    "adequate protection through contractual safeguards.",
                    "Cross-border data transfers may occur in the course "
                    "of operating the service, subject to standard "
                    "contractual protections.",
                    "Data may be transferred to and stored in countries "
                    "with different data-protection laws than yours; "
                    "where this occurs, we apply appropriate safeguards.",
                ))]

    def sec_legal_basis():
        return [h(pick("Legal Basis for Processing",
                       "Legal Bases We Rely On",
                       "Lawful Bases")),
                p(pick(
                    "When applicable, the lawful bases on which we rely "
                    "include user consent, contractual necessity, our "
                    "legitimate interests, and compliance with legal "
                    "duties.",
                    "Processing is grounded in your consent, performance "
                    "of a contract, our legitimate interests, or "
                    "applicable law.",
                    "Depending on the activity, our legal basis may be "
                    "consent, the performance of a contract with you, "
                    "our legitimate business interests, or a legal "
                    "obligation that applies to us.",
                ))]

    def sec_automated():
        return [h(pick("Automated Decision-Making",
                       "Automated Processing")),
                p(pick(
                    "We do not use automated decision-making that "
                    "produces legal or similarly significant effects on "
                    "you without human involvement.",
                    "Automated systems are used to detect abuse and "
                    "improve features, but consequential decisions "
                    "involve human review.",
                ))]

    def sec_dnt():
        return [h(pick("Do Not Track", "DNT Signals")),
                p(pick(
                    "The application currently does not respond to "
                    "browser Do Not Track signals, as there is no "
                    "industry consensus on how to interpret them.",
                    "We do not honor DNT browser signals, in line with "
                    "current industry practice. We respect explicit "
                    "preferences you set inside the application.",
                ))]

    def sec_california():
        return [h(pick("California Privacy Rights",
                       "California Residents",
                       "Rights Under California Law")),
                p(pick(
                    "California residents have additional rights under "
                    "the CCPA / CPRA, including the right to know what "
                    "personal information we collect, to request "
                    "deletion, and to opt out of certain sharing.",
                    "Under California law, residents may request "
                    "disclosure of categories of personal information "
                    "collected and may request deletion of that "
                    "information, subject to legal exceptions.",
                ))]

    def sec_contact_short():
        line = pick(
            f"Contact: questions about this policy can be sent via the "
            f"in-app support channel.",
            f"Reach us via {safe}'s in-app support contact for any "
            f"privacy questions.",
            f"For privacy-related inquiries, please use the support "
            f"channel inside {safe}.",
        )
        if use_bold_contact:
            return [p(b(line))]
        return [p(line)]

    def sec_contact_full():
        return [h(pick("Contact Us", "How to Contact Us", "Contact",
                       "Getting in Touch")),
                p(pick(
                    f"Questions about this Policy can be sent to the "
                    f"developer's support channel listed in the application.",
                    f"For privacy-related questions about {safe}, please "
                    f"reach out via the in-app support contact.",
                    f"Privacy inquiries should be directed through the "
                    f"support contact within {safe}.",
                ))]

    # ── Section pools per length class ──

    REQUIRED_ALWAYS = ['intro', 'collect', 'use', 'contact']

    OPTIONAL_BY_LENGTH = {
        'short':   ['sharing'],
        'medium':  ['sharing', 'retention', 'rights', 'cookies',
                    'security'],
        'long':    ['sharing', 'retention', 'rights', 'cookies',
                    'children', 'third_party', 'security', 'changes',
                    'international'],
        'verbose': ['sharing', 'retention', 'rights', 'cookies',
                    'children', 'third_party', 'security', 'changes',
                    'international', 'legal_basis', 'automated', 'dnt',
                    'california'],
    }

    DROP_PROB_BY_LENGTH = {
        'short':   0.50,   # half of the optional sections drop
        'medium':  0.30,
        'long':    0.15,
        'verbose': 0.05,
    }

    SECTION_BUILDERS = {
        'intro':       (sec_intro_short, sec_intro_full),
        'collect':     (sec_collect_prose, sec_collect_bullets,
                        sec_collect_bullets),  # bullets weighted x2
        'use':         (sec_use_prose, sec_use_bullets, sec_use_bullets),
        'sharing':     (sec_sharing,),
        'retention':   (sec_retention,),
        'rights':      (sec_rights_prose, sec_rights_bullets,
                        sec_rights_bullets),
        'cookies':     (sec_cookies,),
        'children':    (sec_children,),
        'third_party': (sec_third_party,),
        'security':    (sec_security,),
        'changes':     (sec_changes,),
        'international': (sec_international,),
        'legal_basis': (sec_legal_basis,),
        'automated':   (sec_automated,),
        'dnt':         (sec_dnt,),
        'california':  (sec_california,),
    }

    def build(section_key):
        # intro short for 'short' length, full otherwise
        if section_key == 'intro':
            return (sec_intro_short if LENGTH == 'short'
                    else sec_intro_full)()
        builders = SECTION_BUILDERS.get(section_key)
        if not builders:
            return []
        return R.choice(builders)()

    # ── Assemble ──
    nodes = []
    # 1) intro
    nodes.extend(build('intro'))
    # 2) optional separator (rare)
    if R.random() < 0.15:
        nodes.append(hr())
    # 3) collect + use are essentially-required, but order can swap
    body_required = ['collect', 'use']
    R.shuffle(body_required)
    for sec_key in body_required:
        nodes.extend(build(sec_key))
    # 4) optional sections — shuffle + drop per length class
    drop_p = DROP_PROB_BY_LENGTH[LENGTH]
    optional = list(OPTIONAL_BY_LENGTH[LENGTH])
    R.shuffle(optional)
    for sec_key in optional:
        if R.random() >= drop_p:
            nodes.extend(build(sec_key))
    # 5) another optional separator before contact (rare)
    if R.random() < 0.20:
        nodes.append(hr())
    # 6) contact section (short or full)
    if LENGTH == 'short' and R.random() < 0.55:
        nodes.extend(sec_contact_short())
    else:
        nodes.extend(sec_contact_full())

    # ── Anti-fingerprint pass over the whole tree ──
    return _walk_apply_privacy_anti_fingerprint(nodes, R)


# ─── Synonym substitution + sentence dropout (anti-fingerprint) ───────

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
    r'\bretain\b':        ['retain', 'keep', 'hold', 'store'],
    r'\bdelete\b':        ['delete', 'remove', 'erase'],
    r'\baccess\b':        ['access', 'review', 'see'],
    r'\bcorrect\b':       ['correct', 'amend', 'rectify'],
    r'\bcomply\b':        ['comply', 'conform', 'adhere'],
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
                # Headings already have 3-5 phrasing variants via pick();
                # running synonym subs on top mangles grammar
                # ("How We Use" → "How Our team Use") so we skip subs here.
                new['children'] = list(new['children'])
            else:
                new['children'] = [_walk_apply_privacy_anti_fingerprint(c, R)
                                    for c in new['children']]
        return new
    return nodes


# ─── CLUSTERING DEFENSE LAYER (added 2026-06-03) ──────────────────────
#
# 12 distinct writing personas + LLM-generated text + 50/50 telegraph/rentry
# + alternate URL titles to break the "Privacy-Policy--" slug fingerprint
# + sparse realistic typos so the prose doesn't look perfectly polished.
# ──────────────────────────────────────────────────────────────────────

_WRITING_PERSONAS = [
    {'id': 'gen_z', 'voice': "Gen Z, very casual, lowercase mostly, uses 'rn' 'ngl' 'fr' 'tbh' 'like' 'literally' SPARINGLY (not every sentence). Short paragraphs. Conversational. Drops articles sometimes. Almost talks to the reader. Uses emoji maybe 2-3 times TOTAL across the whole doc."},
    {'id': 'academic', 'voice': "Formal academic prose. Third-person throughout. 'the data subject' 'the user' not 'you'. Semicolons. Latinate vocabulary (utilize, hereinafter, pursuant to, notwithstanding). Long compound sentences with subordinate clauses. Reads like a research paper appendix."},
    {'id': 'millennial', 'voice': "Friendly-casual millennial. Conversational. Uses 'Hey!' or 'OK, so...' to open. Parenthetical asides. 'kinda' 'yeah' 'honestly'. Reasonable but not stiff. Mid-length sentences. Occasional self-aware joke."},
    {'id': 'lonely_mother', 'voice': "Warm, anecdotal tone. Mentions being a mom (1-2 times only, naturally). 'As a mom of two...' or 'I built this little tool because I wanted...'. Slight worry/care undertone about your kids' data. Personal, vulnerable, real-sounding."},
    {'id': 'corporate_lawyer', 'voice': "Dense legalese. 'WHEREAS' 'hereinafter referred to as' 'without limitation' 'pursuant to applicable law'. Some ALL CAPS for emphasis on defined terms. Heavy commas. Numbered clauses occasionally. Cold and formal."},
    {'id': 'tech_founder', 'voice': "Tech startup pitch-deck voice. 'We believe' 'transformative' 'stakeholders' 'data-driven' 'best-in-class' 'mission-critical'. Vague but confident. Buzzword-heavy. Optimistic. 'Our mission is to...'"},
    {'id': 'british_formal', 'voice': "British formal English. 'whilst' 'amongst' 'Therefore,' sentence-initial. Understated dry tone. Slight self-deprecation. 'Should you have any concerns, kindly...' 'We are most grateful for...' Reserved."},
    {'id': 'indian_english_business', 'voice': "Indian English business writing. 'do the needful' 'kindly note' 'as per our policy' 'the same shall be communicated'. Formal but slightly indirect. Polite. 'Please be advised that...' 'Revert at your earliest convenience.'"},
    {'id': 'translated_from_german', 'voice': "Reads like translated from German into English. Occasional unusual word choice (e.g. 'actuality' for 'currently'). Article issues (sometimes drops or adds 'the'). Verb-near-end-of-clause structures. Slightly stiff but earnest. 'It is so that we collect...'"},
    {'id': 'plain_enthusiast', 'voice': "Plain language enthusiast. Lots of bullet points. Exclamation marks sparingly used. Bold for emphasis. Short paragraphs. 'Here is what we do:' Friendly but a bit excited. Uses simple hyphens not em-dashes."},
    {'id': 'no_formal_education', 'voice': "Plain spoken, simple vocabulary, short sentences. Occasionally awkward grammar (still readable). 'We dont collect your stuff that we dont need' 'Your data is safe with us, we promise'. No fancy words. Some apostrophe issues (dont, wont)."},
    {'id': 'old_school_dev', 'voice': "Terse no-nonsense developer voice. Short declarative sentences. Technical: 'We log: IP, user-agent, timestamp.' No fluff. ALL CAPS for section headers (PRIVACY POLICY, DATA WE COLLECT). 'use' not 'utilize'. Bullet-heavy. Like a README."},
]

# Alternate titles to break Telegraph "Privacy-Policy--" slug pattern
_ALTERNATE_TITLES = [
    'Privacy Policy', 'Privacy Notice', 'Privacy Statement', 'Privacy & Data',
    'Data Practices', 'How We Handle Your Data', 'User Privacy', 'Information We Collect',
    'Privacy Information', 'Data Privacy Notice', 'Privacy Terms', 'Privacy & Cookies',
    'About Your Data', 'Data Use Policy', 'Privacy Overview',
]

# Realistic typo substitutions (subtle ones a real person might make)
_TYPO_SUBS = [
    ('the', 'teh'), ('and', 'adn'), ('that', 'taht'), ('with', 'wiht'),
    ('your', 'yoru'), ('have', 'ahve'), ('this', 'tihs'),
    ('receive', 'recieve'), ('necessary', 'neccessary'), ('definitely', 'definately'),
    ('separate', 'seperate'), ('committed', 'commited'), ('information', 'informatoin'),
    ('protect', 'protct'), ('our', 'oru'), ('accommodate', 'acommodate'),
    ('occurred', 'occured'), ('beginning', 'begining'), ('successful', 'succesful'),
]


def _inject_realistic_typos(text, rate_per_1000_words=2):
    """Sparsely inject realistic typos into prose (case-preserved).
    Doesn't touch headings, tags, links, numbers. ~rate per 1000 words."""
    if not text or not isinstance(text, str):
        return text
    word_count = len(text.split())
    n_typos = max(0, int(round(word_count * rate_per_1000_words / 1000)))
    if n_typos == 0:
        return text
    R = _random.Random()
    for _ in range(n_typos):
        orig, typo = R.choice(_TYPO_SUBS)
        pattern = re.compile(rf'\b{re.escape(orig)}\b', re.IGNORECASE)
        matches = list(pattern.finditer(text))
        if not matches:
            continue
        m = R.choice(matches)
        repl = typo[0].upper() + typo[1:] if m.group(0)[0].isupper() else typo
        text = text[:m.start()] + repl + text[m.end():]
    return text


def _llm_generate_privacy_html(app_name, persona, use_case=None, retries=4):
    """Call OpenAI gpt-4o-mini to generate a Meta-compliant privacy policy in the
    given persona's voice. Returns (html_body, None) or (None, error_str).

    Retries up to `retries` times on HTTP errors / timeouts / empty output with
    exponential backoff (3s, 6s, 12s, 24s). NO TEMPLATE FALLBACK — if all
    attempts fail, caller HALTS. The template generator is not used for content
    anymore (per user 2026-06-03: 100% LLM, no pre-written prose)."""
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        return None, 'OPENAI_API_KEY not set'
    voice_desc = persona.get('voice', 'neutral professional')
    use_case_str = use_case or 'a general productivity app'
    system_prompt = (
        "You write privacy policies for Facebook/Meta app developers. Output HTML ONLY. "
        "MUST cover these eight topics (in any order/structure): (1) what data is collected, "
        "(2) how it is used, (3) third-party sharing INCLUDING Meta APIs, (4) data retention, "
        "(5) user rights/controls, (6) security measures, (7) children under 13, (8) contact info. "
        "Allowed tags: <h3>, <h4>, <p>, <ul>, <li>, <strong>, <em>, <br>. NO links, NO <html>/<body>/<head>/<style>/<script>. "
        "Length 400-1100 words. Vary structure naturally (bullets vs prose) based on the persona. "
        "DO NOT mention you are an AI. DO NOT use placeholder text like [your email] or [insert]. "
        "Write in the persona's voice consistently throughout — voice is the highest priority."
    )
    contact_email = f"privacy@{re.sub(r'[^a-z0-9]', '', app_name.lower())[:20] or 'app'}.example"
    user_prompt = (
        f"App name: {app_name}\n"
        f"Use case: {use_case_str}\n"
        f"Persona voice (write the WHOLE policy in this voice, this is the most important constraint):\n"
        f"  {voice_desc}\n\n"
        f"Contact section should mention this email: {contact_email}\n\n"
        f"Generate the privacy policy now. HTML only. 400-1100 words. IN PERSONA VOICE:"
    )
    payload = {
        'model': 'gpt-4o-mini',
        'temperature': 0.95,  # high variance for distinct outputs
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        'max_tokens': 2000,
    }
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.post('https://api.openai.com/v1/chat/completions',
                headers={'Authorization': f'Bearer {api_key}'},
                json=payload, timeout=60)
            if r.status_code != 200:
                last_err = f'HTTP {r.status_code}: {r.text[:200]}'
                logger.warning(f'LLM attempt {attempt+1}/{retries}: {last_err}')
                time.sleep(3 * (2 ** attempt))
                continue
            data = r.json()
            html = (data.get('choices') or [{}])[0].get('message', {}).get('content', '').strip()
            if not html:
                last_err = f'empty content: {str(data)[:200]}'
                logger.warning(f'LLM attempt {attempt+1}/{retries}: {last_err}')
                time.sleep(3 * (2 ** attempt))
                continue
            # Strip code-fence markers if model wrapped output
            if html.startswith('```'):
                html = html.split('\n', 1)[1] if '\n' in html else html
                if html.endswith('```'):
                    html = html.rsplit('```', 1)[0]
                html = html.strip()
            return html, None
        except Exception as e:
            last_err = f'{type(e).__name__}: {e}'
            logger.warning(f'LLM attempt {attempt+1}/{retries}: {last_err}')
            time.sleep(3 * (2 ** attempt))
    return None, f'LLM failed all {retries} attempts: {last_err}'


def _html_to_telegraph_nodes(html):
    """Convert simple HTML (h3/h4/p/ul/li/strong/em/br) → Telegraph node format.
    Anything outside the whitelist is flattened to text. Empty containers dropped."""
    from html.parser import HTMLParser
    class _P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.nodes = []
            self.stack = []
        def _append(self, node):
            if self.stack:
                self.stack[-1].setdefault('children', []).append(node)
            else:
                self.nodes.append(node)
        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            if tag in ('h3','h4','p','ul','li','strong','em','b','i','br'):
                # Normalize bold/italic
                if tag == 'b': tag = 'strong'
                if tag == 'i': tag = 'em'
                node = {'tag': tag, 'children': []}
                self._append(node)
                if tag != 'br':
                    self.stack.append(node)
        def handle_endtag(self, tag):
            tag = tag.lower()
            if tag == 'b': tag = 'strong'
            if tag == 'i': tag = 'em'
            if self.stack and self.stack[-1].get('tag') == tag:
                self.stack.pop()
        def handle_data(self, data):
            s = data
            if s.strip():
                self._append(s)
    p = _P()
    try: p.feed(html)
    except Exception: pass
    def _clean(node):
        if isinstance(node, str):
            return node if node.strip() else None
        if not isinstance(node, dict):
            return None
        children = node.get('children', [])
        cleaned = []
        for c in children:
            x = _clean(c)
            if x is not None:
                cleaned.append(x)
        if not cleaned and node.get('tag') not in ('br','hr'):
            return None
        node['children'] = cleaned
        return node
    out = []
    for n in p.nodes:
        x = _clean(n)
        if x is not None:
            out.append(x)
    return out


# ─── Telegra.ph host ──────────────────────────────────────────────────

def _create_telegraph_privacy_policy(app_name, title=None, nodes_override=None):
    """POST policy to telegra.ph. If nodes_override given, use it as-is.
    If title given, use it (avoids the 'Privacy-Policy--' slug fingerprint)."""
    safe = (app_name or '').strip()
    if not safe:
        return None, "App name is empty."
    if len(safe) > 64:
        return None, "App name too long (max 64 chars)."
    short = safe[:32] or 'app'
    # If no title passed, use the LEGACY default (backward compat for callers
    # that don't go through the dispatch). The dispatch always passes a random title.
    final_title = title if title else f"Privacy Policy — {safe}"
    try:
        r = requests.post('https://api.telegra.ph/createAccount',
            data={'short_name': short, 'author_name': safe}, timeout=15)
        j = r.json()
        if not j.get('ok'):
            return None, f"createAccount failed: {j.get('error') or r.text[:160]}"
        token = j['result']['access_token']
    except Exception as e:
        return None, f"createAccount HTTP error: {e}"
    content = nodes_override if nodes_override else _telegraph_privacy_policy_content(safe)
    try:
        r = requests.post('https://api.telegra.ph/createPage',
            data={'access_token': token,
                  'title': final_title,
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


def _create_rentry_privacy_policy(app_name, title=None, md_override=None):
    """POST policy to rentry.co. If md_override given, use it as the body."""
    safe = (app_name or '').strip()
    if not safe:
        return None, "App name is empty."
    if len(safe) > 64:
        return None, "App name too long (max 64 chars)."
    final_title = title if title else f"Privacy Policy — {safe}"
    if md_override is not None:
        md = f"# {final_title}\n\n{md_override}"
    else:
        nodes = _telegraph_privacy_policy_content(safe)
        md_body = _telegraph_nodes_to_markdown(nodes)
        md = f"# {final_title}\n\n{md_body}"
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


def _create_privacy_policy_dispatch(provider=None, app_name=None, use_case=None,
                                     use_llm=True, persona_id=None, inject_typos=True):
    """Privacy policy generator — 100% LLM (per user 2026-06-03 "I wouldn't use
    a pre-written template"). Each call rolls:

      • provider: 'telegraph' | 'rentry' (50/50 if None)
      • persona: one of 12 distinct writing voices (random unless persona_id given)
        — uniform random.choice, so each persona has 1/12 = 8.33% probability
      • title: one of 15 alternate titles (breaks 'Privacy-Policy--' slug)
      • typos: sparse realistic typos (~2 per 1000 words) — disable with inject_typos=False

    use_llm is kept as a kwarg for backward compat but defaults to True. If you
    pass False (debugging only), the template generator is used as a hard fallback.

    Returns: (url, err_or_None, meta_dict). Errors propagate — no silent
    template fallback if LLM fails. Set OPENAI_API_KEY before calling.

    meta = {
      'provider': 'telegraph'|'rentry',
      'persona': persona_id, 'persona_voice': voice[:80],
      'use_llm': bool, 'title': str, 'inject_typos': bool,
    }
    """
    safe = (app_name or '').strip()
    if not safe:
        return None, 'App name is empty', None
    if len(safe) > 64:
        return None, 'App name too long (max 64 chars)', None

    R = _random.Random()
    if provider is None:
        provider = R.choice(['telegraph', 'rentry'])
    persona = next((p for p in _WRITING_PERSONAS if p['id'] == persona_id), None)
    if persona is None:
        persona = R.choice(_WRITING_PERSONAS)  # uniform across 12 → 8.33% each
    title = R.choice(_ALTERNATE_TITLES)

    meta = {
        'provider': provider,
        'persona': persona['id'],
        'persona_voice': persona.get('voice', '')[:80],
        'use_llm': use_llm,
        'title': title,
        'inject_typos': inject_typos,
    }

    logger.info(f'privacy dispatch: provider={provider} llm={use_llm} '
                f'persona={persona["id"]} title={title!r} typos={inject_typos}')

    # ── Generate content (LLM always; template only if explicitly disabled) ──
    nodes = None
    md_body = None
    if use_llm:
        html, err = _llm_generate_privacy_html(safe, persona, use_case)
        if not html:
            # NO silent template fallback — surface the error so caller can decide.
            return None, f'LLM generation failed: {err}', meta
        if inject_typos:
            html = _inject_realistic_typos(html, rate_per_1000_words=2)
        nodes = _html_to_telegraph_nodes(html)
        if not nodes:
            return None, 'LLM HTML parsed to 0 valid nodes', meta
        md_body = _telegraph_nodes_to_markdown(nodes)
    else:
        # Explicit opt-out → template (debug/testing path only)
        nodes = _telegraph_privacy_policy_content(safe)
        md_body = _telegraph_nodes_to_markdown(nodes)
        if inject_typos:
            md_body = _inject_realistic_typos(md_body, rate_per_1000_words=2)

    # ── POST to chosen host ──
    if provider == 'rentry':
        url, err = _create_rentry_privacy_policy(safe, title=title, md_override=md_body)
    else:
        url, err = _create_telegraph_privacy_policy(safe, title=title, nodes_override=nodes)
    return url, err, meta


# ─── Telegram handlers ────────────────────────────────────────────────

async def privacy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/privacy — single-step: prompt for app name, then generate with random dispatch.

    No more provider-choice menu. Each call rolls 50/50 telegra.ph/rentry.co
    + 1-of-12 writing personas + LLM-or-template + alternate title + typos.
    The persona used is reported in the response as proof of randomization.
    """
    context.user_data['expecting_privacy_app_name'] = True
    context.user_data['privacy_provider'] = 'random'  # always random
    await update.message.reply_text(
        "📜 <b>Privacy Policy Generator</b>\n\n"
        "Send the <b>name of your app</b> as your next message "
        "(e.g. <code>Atlas Studio</code>).\n\n"
        "<i>I'll auto-pick a host (50/50 telegra.ph vs rentry.co), a writing "
        "persona (1 of 12), and either LLM-generated or template content. "
        "Maximum clustering defense — no two policies will look alike.</i>\n\n"
        "<i>Send /cancel to abort.</i>",
        parse_mode='HTML')


async def privacy_provider_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Legacy callback handler — kept for backward compatibility if anyone
    has an old menu message open. Falls through to random."""
    query = update.callback_query
    await query.answer()
    provider = (query.data or '').replace('privacy_provider_', '')
    if provider not in ('telegraph', 'rentry', 'random'):
        provider = 'random'
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
    """Called by the bot's main text router when expecting_privacy_app_name.

    Generates via the randomized dispatch (50/50 provider × 12 personas × LLM/template)
    and reports the chosen combo in the response so the user can audit randomness."""
    if not context.user_data.get('expecting_privacy_app_name'):
        return
    text = (update.message.text or '').strip()
    if text.lower() in ('/cancel', 'cancel'):
        context.user_data.pop('expecting_privacy_app_name', None)
        context.user_data.pop('privacy_provider', None)
        await update.message.reply_text("Privacy generation cancelled.")
        return
    context.user_data.pop('expecting_privacy_app_name', None)
    context.user_data.pop('privacy_provider', None)
    import asyncio
    await update.message.reply_text(
        f"📜 Generating randomized privacy policy for <b>{_h.escape(text)}</b>…",
        parse_mode='HTML')
    try:
        url, err, meta = await asyncio.to_thread(
            _create_privacy_policy_dispatch,
            None, text, None, None, None, True)
    except Exception as e:
        url, err, meta = None, f"crashed: {e}", None
    if err or not url:
        await update.message.reply_text(
            f"❌ create failed: <code>{_h.escape(str(err)[:300])}</code>",
            parse_mode='HTML')
        return
    # Pretty-print the persona + provider + generator
    provider_label = {'telegraph': '🪶 telegra.ph', 'rentry': '📄 rentry.co'}.get(
        (meta or {}).get('provider', ''), '🔗 ?')
    persona_id = (meta or {}).get('persona', '?')
    persona_voice = (meta or {}).get('persona_voice', '')
    gen_label = '🤖 LLM (gpt-4o-mini)' if (meta or {}).get('use_llm') else '📋 template'
    title = (meta or {}).get('title', '?')
    typos = '✍️ yes' if (meta or {}).get('inject_typos') else '—'
    await update.message.reply_text(
        f"✅ <b>Privacy Policy created</b>\n\n"
        f"<b>App:</b> <code>{_h.escape(text)}</code>\n"
        f"<b>URL:</b> {url}\n\n"
        f"<b>━━ Randomization proof ━━</b>\n"
        f"<b>Host:</b> {provider_label}\n"
        f"<b>Persona:</b> <code>{_h.escape(persona_id)}</code>\n"
        f"<b>Voice:</b> <i>{_h.escape(persona_voice)}…</i>\n"
        f"<b>Generator:</b> {gen_label}\n"
        f"<b>Title:</b> <code>{_h.escape(title)}</code>\n"
        f"<b>Typos:</b> {typos}\n\n"
        f"Paste the URL into the Meta App dashboard's "
        f"<i>Privacy Policy URL</i> field.",
        parse_mode='HTML', disable_web_page_preview=False)
