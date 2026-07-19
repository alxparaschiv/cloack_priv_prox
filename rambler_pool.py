"""/rambler_login — dispense one (or more) Rambler login(s) from the pool
(rambler_pool.txt on Drive). Each dispensed login is CONSUMED (written back out of
the pool) so it can never be handed out twice or reused by /account_pack. Distinct
from /rambler, which fetches the verification CODE for a login you already have.
"""
import asyncio
import html
import logging

from telegram import Update
from telegram.ext import ContextTypes

import fb_poster_registry as R

logger = logging.getLogger('rambler_pool')

_MAX = 20


async def rambler_login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    count = 1
    if args:
        try:
            count = max(1, min(_MAX, int(''.join(ch for ch in args[0] if ch.isdigit()))))
        except ValueError:
            count = 1
    res = await asyncio.to_thread(R.dispense_ramblers, count)
    if not res.get('ok'):
        await update.message.reply_text(f"❌ {res.get('err', 'could not read the pool')}")
        return
    logins = res.get('logins') or []
    if not logins:
        await update.message.reply_text(
            "📭 The Rambler pool is empty. Add lines (one <code>email:password</code> "
            "per line) to <code>rambler_pool.txt</code> on Drive.", parse_mode='HTML')
        return
    left = res.get('remaining')
    head = (f"🔑 <b>Rambler login{'s' if len(logins) > 1 else ''}</b>"
            + (f" — <b>{left}</b> left in the pool" if left is not None else "") + ":\n")
    rows = []
    for email, pw in logins:
        rows.append(f"• <code>{html.escape(email)}:{html.escape(pw)}</code>")
    tail = "\n\n<i>Paste a line into /rambler to fetch its verification code.</i>"
    await update.message.reply_text(head + "\n".join(rows) + tail, parse_mode='HTML')
