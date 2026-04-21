"""
auth_simple.py — Session-based auth decorators.

Admin: authenticated via Telegram WebApp initData (/api/auth/telegram).
       session['telegram_user_id'] must be in admin_ids().
       Fallback: ?_t=<auth_token> (short-lived token from /api/auth/telegram).
       Dev: Basic Auth with ADMIN_TOKEN or ?dev_token=ADMIN_TOKEN.

Nanny: same flow, role='nanny' in session.
"""
import os
import functools
from flask import session, redirect, url_for, request, g
from config import admin_ids


def _check_basic_auth() -> bool:
    """Allow access via Basic Auth / dev_token (dev only)."""
    token = os.environ.get('ADMIN_TOKEN', '')
    if not token:
        return False
    auth = request.authorization
    if auth and auth.password == token:
        return True
    hdr = request.headers.get('Authorization', '')
    if hdr.startswith('Bearer ') and hdr[7:] == token:
        return True
    if os.environ.get('FLASK_DEBUG') == '1':
        if request.args.get('dev_token') == token or request.cookies.get('dev_token') == token:
            return True
    return False


def _try_auth_token() -> str | None:
    """
    Check ?_t=<token> query param.
    If valid, promote session and return role.
    Imports _validate_auth_token from app module (set after app is created).
    """
    t = request.args.get('_t', '')
    if not t:
        return None
    try:
        from flask import current_app
        validate = current_app.config.get('_validate_auth_token')
        if not validate:
            return None
        entry = validate(t)
        if not entry:
            return None
        # Hydrate session
        session.permanent = True
        session['telegram_user_id'] = entry['telegram_user_id']
        session['role'] = entry['role']
        return entry['role']
    except Exception:
        return None


def _session_role() -> str | None:
    """Return role from session if authenticated."""
    tg_id = session.get('telegram_user_id')
    if not tg_id:
        return None
    # Explicit role stored in session
    role = session.get('role')
    if role:
        return role
    # Fallback: check if admin by ID
    if tg_id in admin_ids():
        return 'admin'
    return 'client'


def require_admin(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if _check_basic_auth():
            return f(*args, **kwargs)
        # Try short-lived token from URL
        role = _try_auth_token()
        if role == 'admin':
            return f(*args, **kwargs)
        # Check session
        tg_id = session.get('telegram_user_id')
        if tg_id and tg_id in admin_ids():
            return f(*args, **kwargs)
        if session.get('role') == 'admin':
            return f(*args, **kwargs)
        return redirect(url_for('admin_login'))
    return decorated


def require_nanny(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if _check_basic_auth():
            return f(*args, **kwargs)
        role = _try_auth_token()
        if role in ('nanny', 'admin'):
            return f(*args, **kwargs)
        if session.get('nanny_portal_token'):
            return f(*args, **kwargs)
        if session.get('role') in ('nanny', 'admin'):
            return f(*args, **kwargs)
        return redirect(url_for('nanny_login'))
    return decorated
