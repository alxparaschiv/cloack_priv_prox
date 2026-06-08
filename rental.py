"""/rental_instagram + /rental_facebook — one-shot 7-day SMS rentals from TextVerified.

Mirrors the rental call master_account_create.py uses for the Meta Dev pipeline,
but exposed as bare Telegram commands so the user can grab a phone number on
demand without launching the full account-creation pipeline.

Each command:
  - POST /api/pub/v2/reservations/rental
    { duration: 'sevenDay', isRenewable: False, numberType: 'mobile',
      serviceName: 'instagram' | 'facebook', capability: 'sms',
      allowBackOrderReservations: False, alwaysOn: True }
  - Polls the sale, extracts the rental_id, fetches the phone number
  - Replies in TG with rental_id + phone in copy-pasteable format

No inputs needed — just type the command, get the number back.
"""
import time
import logging

from telegram import Update
from telegram.ext import ContextTypes

from textverified_client import _client as tv_client

logger = logging.getLogger(__name__)


def _rent_seven_day(service_name):
    """Rent a 7-day non-renewable mobile SMS number for `service_name`
    ('instagram' or 'facebook'). Returns (rental_id, phone_e164, err)."""
    cli = tv_client()
    try:
        sale = cli._request('POST', '/api/pub/v2/reservations/rental', json={
            'allowBackOrderReservations': False,
            'alwaysOn': True,
            'duration': 'sevenDay',
            'isRenewable': False,
            'numberType': 'mobile',
            'serviceName': service_name,
            'capability': 'sms',
        })
        sale_id = sale['href'].rstrip('/').split('/')[-1]
    except Exception as e:
        return None, None, f"reservation POST err: {type(e).__name__}: {e}"

    # TextVerified provisions the rental asynchronously — sale → reservations is
    # populated within a few seconds. Poll briefly.
    rental_id = None
    for _ in range(8):
        time.sleep(2)
        try:
            sale_data = cli._request('GET', f'/api/pub/v2/sales/{sale_id}')
            reservations = sale_data.get('reservations') or []
            if reservations:
                rental_id = reservations[0].get('id')
                if rental_id:
                    break
        except Exception as e:
            logger.warning(f"[rental] sale poll err: {e}")
    if not rental_id:
        return None, None, f"sale {sale_id} never produced a rental id within 16s"

    try:
        detail = cli._request('GET',
            f'/api/pub/v2/reservations/rental/nonrenewable/{rental_id}')
        phone = detail.get('phoneNumber') or detail.get('number') or ''
    except Exception as e:
        return rental_id, None, f"rental detail GET err: {type(e).__name__}: {e}"

    if not phone:
        return rental_id, None, "rental created but phoneNumber field was empty"
    return rental_id, phone, None


def _balance_str():
    """Best-effort TextVerified balance fetch — returns '$X.XX' or '?' on err."""
    try:
        bal = tv_client().balance()
        return f"${bal:.2f}"
    except Exception as e:
        logger.warning(f"[rental] balance fetch err: {e}")
        return "?"


async def _do_rental(update: Update, service_label: str, service_api: str):
    """Shared handler for both /rental_instagram and /rental_facebook."""
    await update.message.reply_text(
        f"📱 renting a fresh 7-day {service_label} number from TextVerified…",
        parse_mode='Markdown')
    rental_id, phone, err = _rent_seven_day(service_api)
    if err:
        # Even on failure, surface the current balance so the user can see if
        # it's a funds issue.
        await update.message.reply_text(
            f"❌ rental failed: `{err}`\n"
            f"💰 TextVerified balance: `{_balance_str()}`",
            parse_mode='Markdown')
        return
    # Format phone for both contexts: with + sign (E.164) + 10-digit (US local)
    phone_e164 = phone if phone.startswith('+') else f'+{phone}'
    digits = ''.join(c for c in phone if c.isdigit())
    phone_10 = digits[-10:] if len(digits) >= 10 else digits
    # Pull the post-rental balance so the user knows how much is left to spend.
    balance = _balance_str()
    await update.message.reply_text(
        f"✅ *{service_label} rental ready* (balance left: `{balance}`)\n\n"
        f"📞 Phone (E.164): `{phone_e164}`\n"
        f"📞 Phone (10-digit): `{phone_10}`\n"
        f"🪪 Rental ID: `{rental_id}`\n"
        f"⏳ Duration: 7 days, non-renewable\n"
        f"💰 TextVerified balance after rental: `{balance}`\n\n"
        f"_Use /sms with this number to fetch the SMS code when it arrives._",
        parse_mode='Markdown')


async def rental_instagram_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_rental(update, 'Instagram', 'instagram')


async def rental_facebook_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_rental(update, 'Facebook', 'facebook')
