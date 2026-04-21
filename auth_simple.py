"""
auth_simple.py — Simple session-based auth decorators.

Admin: authenticated via Telegram WebApp initData (/api/auth/telegram).
       session['telegram_user_id'] must be in admin_ids().
       Also supports ADMIN_TOKEN env var for Basic Auth (dev/testing).

Nanny: authenticated via Telegram WebApp initData, role='nanny'.
       Or session['nanny_portal_token'] is set (set by /nanny/<token> route).
"""
import os
import functools
from flask import session, redirect, url_for, request, Response
from config import admin_ids


def _check_basic_auth() -> bool:
    """Allow access via Basic Auth using ADMIN_TOKEN env var (for testing)."""
    token = os.environ.get('ADMIN_TOKEN', '')
    if not token:
        return False
    auth = request.authorization
    if auth and auth.password == token:
        return True
    # Also accept token as Bearer
    hdr = request.headers.get('Authorization', '')
    if hdr.startswith('Bearer ') and hdr[7:] == token:
        return True
    # Dev mode: ?dev_token=xxx in URL
    if os.environ.get('FLASK_DEBUG') == '1':
        if request.args.get('dev_token') == token or request.cookies.get('dev_token') == token:
            return True
    return False


def require_admin(f):
    """Decorator: require admin session or Basic Auth."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        # Check Basic Auth (dev/CLI access)
        if _check_basic_auth():
            return f(*args, **kwargs)
        # Check Telegram session
        tg_id = session.get('telegram_user_id')
        if tg_id and tg_id in admin_ids():
            return f(*args, **kwargs)
        # Not authenticated → redirect to admin login
        return redirect(url_for('admin_login'))
    return decorated


def require_nanny(f):
    """Decorator: require nanny session."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        # Check Basic Auth (dev/CLI access)
        if _check_basic_auth():
            return f(*args, **kwargs)
        # Check nanny portal token in session
        if session.get('nanny_portal_token'):
            return f(*args, **kwargs)
        # Check Telegram session with nanny role
        if session.get('role') == 'nanny':
            return f(*args, **kwargs)
        # Not authenticated → redirect to nanny login
        return redirect(url_for('nanny_login'))
    return decorated
