"""
telegram_notify.py — Send Telegram messages via Bot API.
Set TELEGRAM_BOT_TOKEN env var.
"""
import os
import urllib.request
import urllib.parse
import json
import logging

logger = logging.getLogger(__name__)


def send_message(
    chat_id: int | str,
    text: str,
    parse_mode: str | None = None,
    reply_markup: dict | None = None,
) -> dict:
    """
    Send a message to a Telegram chat.
    Returns the Telegram API response dict.
    Raises RuntimeError on failure.
    """
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    if not token:
        logger.warning('TELEGRAM_BOT_TOKEN not set — skipping Telegram notification')
        return {}

    url = f'https://api.telegram.org/bot{token}/sendMessage'
    payload = {
        'chat_id': chat_id,
        'text': text,
        'disable_web_page_preview': True,
    }
    if parse_mode:
        payload['parse_mode'] = parse_mode
    if reply_markup:
        payload['reply_markup'] = reply_markup
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        logger.error('Telegram sendMessage error for chat_id=%s: %s', chat_id, e)
        raise RuntimeError(f'Telegram API error: {e}') from e
