"""
telegram_auth.py — Validate Telegram WebApp initData.
See: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
import hashlib
import hmac
import urllib.parse
import time


class TelegramAuthError(Exception):
    pass


def validate_webapp_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict:
    """
    Validate Telegram Mini App initData string.

    Returns parsed key-value dict on success.
    Raises TelegramAuthError on failure.

    max_age: max allowed age of initData in seconds (default 24h).
             Set to 0 to disable age check (useful in dev).
    """
    if not init_data:
        raise TelegramAuthError('initData is empty')

    # Parse the URL-encoded string
    params = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))

    # Extract and remove hash
    received_hash = params.pop('hash', None)
    if not received_hash:
        raise TelegramAuthError('Missing hash in initData')

    # Build data-check-string: sorted key=value pairs joined by \n
    data_check_string = '\n'.join(
        f'{k}={v}' for k, v in sorted(params.items())
    )

    # Compute secret key: HMAC-SHA256(bot_token, "WebAppData")
    secret_key = hmac.new(
        b'WebAppData',
        bot_token.encode('utf-8'),
        hashlib.sha256,
    ).digest()

    # Compute expected hash
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise TelegramAuthError('Invalid hash — data tampered or wrong bot token')

    # Check age
    if max_age > 0:
        auth_date = int(params.get('auth_date') or 0)
        if auth_date == 0:
            raise TelegramAuthError('Missing auth_date')
        age = int(time.time()) - auth_date
        if age > max_age:
            raise TelegramAuthError(f'initData expired ({age}s > {max_age}s)')

    return params
