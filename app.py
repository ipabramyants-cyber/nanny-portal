import os
import json
import datetime
import secrets
import time
import re
import hashlib
import base64
import io
from collections import Counter
from urllib.parse import urlparse

from flask import Flask, render_template, request, send_from_directory, redirect, url_for, flash, session, Response, jsonify, has_request_context
from werkzeug.utils import secure_filename
try:
    import bleach
except Exception:  # pragma: no cover - production requirements include bleach
    bleach = None

from auth_simple import require_admin, require_nanny
from config import admin_ids
from telegram_notify import send_message
from telegram_auth import validate_webapp_init_data, TelegramAuthError

from models import db, Nanny, Lead, User, Shift, Client, NannyBlock, Review, Article, ReferralAgent

_ARTICLE_COVER_CACHE: dict[str, str] = {}


def _clean_user_text(value, limit: int | None = 500) -> str:
    text = (value or '').strip()
    if limit is not None:
        text = text[:limit]
    if not text:
        return ''
    if bleach:
        return bleach.clean(text, tags=[], attributes={}, strip=True)
    return re.sub(r'<[^>]*?>', '', text)


def _safe_upload_name(file_storage, prefix: str) -> str:
    safe_name = secure_filename(file_storage.filename or '')
    if not safe_name:
        return ''
    return f"{prefix}_{secrets.token_urlsafe(8)}_{safe_name}"


def _article_cover_preview(url: str) -> str:
    if not url:
        return ''
    if not url.startswith('data:image/') or len(url) < 90000:
        return url
    key = hashlib.sha256(url.encode('utf-8', 'ignore')).hexdigest()
    cached = _ARTICLE_COVER_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        header, payload = url.split(',', 1)
        from PIL import Image as _PIL_Image
        img = _PIL_Image.open(io.BytesIO(base64.b64decode(payload)))
        img = img.convert('RGB')
        img.thumbnail((180, 120), _PIL_Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=64, optimize=True)
        preview = 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')
    except Exception:
        preview = ''
    _ARTICLE_COVER_CACHE[key] = preview
    return preview

def _save_image_webp(file_storage, upload_dir: str, prefix: str = 'img') -> str:
    """
    Save uploaded image as WebP (max 1200px wide, quality 82).
    Returns filename (relative to upload_dir).
    Falls back to original format if Pillow fails.
    """
    import io
    try:
        from PIL import Image as _PIL_Image
        img = _PIL_Image.open(file_storage.stream)
        img = img.convert('RGB')
        max_w = 1200
        if img.width > max_w:
            ratio = max_w / img.width
            img = img.resize((max_w, int(img.height * ratio)), _PIL_Image.LANCZOS)
        fname = f"{prefix}_{int(time.time())}.webp"
        fpath = os.path.join(upload_dir, fname)
        img.save(fpath, 'WEBP', quality=82, method=4)
        return fname
    except Exception as _e:
        import logging
        logging.getLogger(__name__).warning('WebP conversion failed: %s', _e)
        file_storage.stream.seek(0)
        safe = secure_filename(file_storage.filename or 'photo.jpg')
        fname = f"{prefix}_{int(time.time())}_{safe}"
        fpath = os.path.join(upload_dir, fname)
        file_storage.save(fpath)
        return fname


def _image_to_data_url(file_storage) -> str:
    """
    Convert uploaded image to a base64 data URL (stored in DB — survives redeploys).
    Resizes to max 600px wide and compresses to JPEG quality 80.
    """
    import io, base64
    try:
        from PIL import Image as _PIL_Image
        file_storage.stream.seek(0)
        img = _PIL_Image.open(file_storage.stream)
        img = img.convert('RGB')
        max_w = 1200
        if img.width > max_w:
            ratio = max_w / img.width
            img = img.resize((max_w, int(img.height * ratio)), _PIL_Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=80, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        return f"data:image/jpeg;base64,{b64}"
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning('data URL conversion failed: %s', e)
        file_storage.stream.seek(0)
        import base64
        raw = file_storage.stream.read()
        b64 = base64.b64encode(raw).decode('ascii')
        mime = 'image/jpeg'
        return f"data:{mime};base64,{b64}"



def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def create_app() -> Flask:
    app = Flask(__name__)
    _secret_key = os.environ.get('FLASK_SECRET_KEY', '')
    if not _secret_key or _secret_key == 'dev_secret_change_me':
        if os.environ.get('FLASK_DEBUG') == '1' or not os.environ.get('FORCE_HTTPS'):
            # Development mode — use insecure fallback with a warning
            import warnings
            warnings.warn('FLASK_SECRET_KEY not set — using insecure default. Set it in .env for production!', stacklevel=2)
            _secret_key = _secret_key or 'dev_secret_change_me_insecure'
        else:
            raise RuntimeError('FLASK_SECRET_KEY must be set in production (min 32 random chars).')
    app.secret_key = _secret_key

    # Sessions: keep clients logged-in in Telegram Mini App
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="None",   # Required for Telegram Mini App (cross-origin iframe)
        SESSION_COOKIE_SECURE=True,        # Required when SameSite=None
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=30),
    )

    # Storage mode
    # - default: legacy JSON files under ./data
    # - set STORAGE=sql (or DATABASE_URL) to use Postgres/Cloud SQL via SQLAlchemy
    use_sql = (os.environ.get('STORAGE') == 'sql') or bool(os.environ.get('DATABASE_URL'))

    if use_sql:
        db_url = os.environ.get('DATABASE_URL')
        if not db_url:
            raise RuntimeError('STORAGE=sql requires DATABASE_URL')
        app.config['SQLALCHEMY_DATABASE_URI'] = db_url
        app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
        db.init_app(app)
        with app.app_context():
            # For early-stage deployments you may reset schema on boot.
            # Set RESET_DB=1 to drop_all + create_all.
            if os.environ.get('RESET_DB') == '1':
                db.drop_all()
            db.create_all()
            # Auto-migrate: add new columns to existing tables without dropping data.
            # Safe to run on every startup (IF NOT EXISTS).
            try:
                with db.engine.connect() as _conn:
                    _conn.execute(db.text(
                        "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS nanny_id VARCHAR(100)"
                    ))
                    _conn.execute(db.text(
                        "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS pinned BOOLEAN DEFAULT FALSE"
                    ))
                    _conn.execute(db.text(
                        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS client_rate_per_hour INTEGER"
                    ))
                    _conn.execute(db.text(
                        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS nanny_rate_per_hour INTEGER"
                    ))
                    _conn.execute(db.text(
                        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS referral_agent_id INTEGER"
                    ))
                    _conn.execute(db.text(
                        "ALTER TABLE referral_agents ADD COLUMN IF NOT EXISTS commission_vnd INTEGER DEFAULT 200000"
                    ))
                    _conn.execute(db.text(
                        "ALTER TABLE shifts ADD COLUMN IF NOT EXISTS post_reminder_sent_at TIMESTAMP"
                    ))
                    _conn.execute(db.text(
                        "ALTER TABLE shifts ADD COLUMN IF NOT EXISTS pre2h_reminder_sent_at TIMESTAMP"
                    ))
                    _conn.execute(db.text(
                        "ALTER TABLE shifts ADD COLUMN IF NOT EXISTS nanny_missing_fact_sent_at TIMESTAMP"
                    ))
                    _conn.execute(db.text(
                        "ALTER TABLE shifts ADD COLUMN IF NOT EXISTS review_reminder_sent_at TIMESTAMP"
                    ))
                    _conn.execute(db.text(
                        "ALTER TABLE shifts ADD COLUMN IF NOT EXISTS nanny_actual_note TEXT"
                    ))
                    _conn.execute(db.text(
                        "ALTER TABLE shifts ADD COLUMN IF NOT EXISTS client_actual_note TEXT"
                    ))
                    _conn.commit()
            except Exception as _e:
                import warnings
                warnings.warn(f"Auto-migrate reviews skipped: {_e}")

    BASE_DIR = os.path.dirname(__file__)

    # Default hourly rates (VND) — can be overridden per lead
    DEFAULT_CLIENT_RATE_VND = 130_000
    DEFAULT_NANNY_RATE_VND  = 110_000

    app.config['DATA_DIR'] = os.environ.get('DATA_DIR') or os.path.join(BASE_DIR, 'data')
    app.config['UPLOAD_DIR'] = os.environ.get('UPLOAD_DIR') or os.path.join(BASE_DIR, 'uploads')
    app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024  # 64MB — articles + gallery base64

    os.makedirs(app.config['DATA_DIR'], exist_ok=True)
    os.makedirs(app.config['UPLOAD_DIR'], exist_ok=True)

    app.jinja_env.globals['current_year'] = datetime.datetime.utcnow().year
    # Cache-busting version for static assets (update on deploy)
    _static_ver = os.environ.get('APP_VERSION') or str(int(time.time() // 86400))
    app.jinja_env.globals['static_ver'] = _static_ver
    # Canonical site URL for templates (sitemap, OG tags, canonical links)
    DEFAULT_SITE_URL = 'https://web-production-2ebe9.up.railway.app'

    def _public_site_url() -> str:
        env_url = (os.environ.get('SITE_URL') or '').rstrip('/')
        if env_url:
            return env_url
        if has_request_context():
            host = (request.host or '').split(':', 1)[0].lower()
            if host and host not in {'localhost', '127.0.0.1'} and not host.endswith('railway.app'):
                return request.url_root.rstrip('/')
        return DEFAULT_SITE_URL

    def _public_analytics_allowed_path(path: str) -> bool:
        path = path or '/'
        blocked_prefixes = (
            '/admin', '/api/', '/client', '/agent', '/r/', '/nanny/app',
            '/nanny/login', '/nanny/portal', '/uploads/', '/static/',
        )
        blocked_exact = {
            '/healthz', '/sw.js', '/offline.html', '/robots.txt', '/sitemap.xml',
            '/favicon.ico',
        }
        if path in blocked_exact:
            return False
        return not any(path.startswith(prefix) for prefix in blocked_prefixes)

    def _analytics_ids() -> dict:
        google_id = (os.environ.get('GOOGLE_ANALYTICS_ID') or '').strip()
        yandex_id = (os.environ.get('YANDEX_METRIKA_ID') or os.environ.get('YANDEX_METRICA_ID') or '').strip()
        if google_id and not re.fullmatch(r'[A-Z]{1,4}-[A-Z0-9_-]{4,40}', google_id):
            google_id = ''
        if yandex_id and not re.fullmatch(r'\d{4,20}', yandex_id):
            yandex_id = ''
        return {'google_analytics_id': google_id, 'yandex_metrika_id': yandex_id}

    app.jinja_env.globals['site_url'] = _public_site_url()

    @app.context_processor
    def _inject_public_site_url():
        ids = _analytics_ids()
        analytics_enabled = has_request_context() and _public_analytics_allowed_path(request.path)
        return {
            'site_url': _public_site_url(),
            'google_analytics_id': ids['google_analytics_id'] if analytics_enabled else '',
            'yandex_metrika_id': ids['yandex_metrika_id'] if analytics_enabled else '',
        }

    def nanny_photo_src(photo: str | None) -> str:
        if not photo:
            return url_for('static', filename='img/nanny_placeholder.jpg')
        photo = str(photo)
        # data URL — use directly (stored in DB as base64)
        if photo.startswith('data:'):
            return photo
        if photo.startswith('http://') or photo.startswith('https://'):
            return photo
        if photo.startswith('uploads/'):
            return url_for('uploads', filename=photo.split('/', 1)[1])
        return url_for('static', filename=photo)

    app.jinja_env.globals['nanny_photo_src'] = nanny_photo_src

    @app.before_request
    def _force_https():
        # Cloud Run typically sets X-Forwarded-Proto
        if os.environ.get('FORCE_HTTPS') == '1':
            proto = (request.headers.get('X-Forwarded-Proto') or '').lower()
            if proto == 'http':
                url = request.url.replace('http://', 'https://', 1)
                return redirect(url, code=301)

    @app.before_request
    def _reject_cross_origin_mutations():
        if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
            return None
        origin = request.headers.get('Origin')
        if not origin:
            return None
        try:
            origin_url = urlparse(origin)
            host_url = urlparse(request.host_url)
        except Exception:
            return {'error': 'invalid origin'}, 403
        expected_scheme = (request.headers.get('X-Forwarded-Proto') or host_url.scheme or '').split(',')[0].strip()
        expected_host = (request.headers.get('X-Forwarded-Host') or host_url.netloc or '').split(',')[0].strip()
        if (origin_url.scheme, origin_url.netloc) != (expected_scheme, expected_host):
            return {'error': 'cross-origin request blocked'}, 403
        return None

    @app.after_request
    def security_headers(resp):
        # Prevent MIME sniffing
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        # Allow framing only from Telegram (Mini App support)
        resp.headers.setdefault(
            'Content-Security-Policy',
            "frame-ancestors 'self' https://t.me https://*.telegram.org https://web.telegram.org; base-uri 'self'; object-src 'none'; form-action 'self'"
        )
        # Referrer policy
        resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        # Permissions policy — disable unused features
        resp.headers.setdefault('Permissions-Policy', 'geolocation=(), microphone=(), camera=()')
        # HSTS for production
        if os.environ.get('FORCE_HTTPS') == '1':
            resp.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
        # Cache static assets aggressively, everything else no-cache
        if request.path.startswith('/static/'):
            resp.headers.setdefault('Cache-Control', 'public, max-age=31536000, immutable')
        elif request.path.startswith('/uploads/'):
            resp.headers.setdefault('Cache-Control', 'public, max-age=86400')
        elif request.path in ('/sitemap.xml', '/robots.txt'):
            resp.headers.setdefault('Cache-Control', 'public, max-age=3600')
        elif not request.path.startswith('/api/'):
            resp.headers.setdefault('Cache-Control', 'no-cache, must-revalidate')
        return resp


    @app.route('/sw.js')
    def service_worker():
        """Service Worker — must be served from root for full scope."""
        resp = send_from_directory(os.path.join(BASE_DIR, 'static'), 'sw.js')
        resp.headers['Service-Worker-Allowed'] = '/'
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return resp

    @app.route('/offline.html')
    def offline_page():
        return render_template('offline.html')

    @app.route('/healthz')
    def healthz():
        return {'ok': True, 'time': int(time.time())}

    def _fmt_date_ru(date_str: str) -> str:
        """Format YYYY-MM-DD -> DD.MM.YYYY for display."""
        if not date_str or not isinstance(date_str, str) or len(date_str) != 10:
            return date_str
        try:
            y, m, d = date_str.split('-')
            if len(y) == 4 and len(m) == 2 and len(d) == 2:
                return f"{d}.{m}.{y}"
        except Exception:
            pass
        return date_str

    app.jinja_env.filters['fmt_date'] = _fmt_date_ru

    def _is_numeric_tg_id(val) -> bool:
        import re as _re
        return bool(val and _re.fullmatch(r'-?\d{5,20}', str(val).strip()))
    app.jinja_env.filters['is_numeric_tg_id'] = _is_numeric_tg_id

    NANNIES_FILE = os.path.join(app.config['DATA_DIR'], 'nannies.json')
    LEADS_FILE = os.path.join(app.config['DATA_DIR'], 'leads.json')
    REVIEWS_FILE = os.path.join(app.config['DATA_DIR'], 'reviews.json')
    USERS_FILE = os.path.join(app.config['DATA_DIR'], 'users.json')
    AGENTS_FILE = os.path.join(app.config['DATA_DIR'], 'referral_agents.json')
    ASSIGNMENTS_FILE = os.path.join(app.config['DATA_DIR'], 'assignments.json')
    RECEIPTS_FILE = os.path.join(app.config['DATA_DIR'], 'receipts.json')
    NANNY_BLOCKS_FILE = os.path.join(app.config['DATA_DIR'], 'nanny_blocks.json')
    NOTIFICATION_STATE_FILE = os.path.join(app.config['DATA_DIR'], 'notification_state.json')
    NOTIFICATION_LOG_FILE = os.path.join(app.config['DATA_DIR'], 'notification_log.json')
    APP_EVENTS_FILE = os.path.join(app.config['DATA_DIR'], 'app_events.json')
    VISIT_LOG_FILE = os.path.join(app.config['DATA_DIR'], 'visit_log.json')

    def _legacy_token(prefix: str, raw: str) -> str:
        digest = hashlib.sha1((raw or '').encode('utf-8')).hexdigest()[:12]
        return f"{prefix}{digest}"

    def _extract_chat_id(raw) -> int | None:
        if raw is None:
            return None
        s = str(raw).strip()
        if not s:
            return None
        if re.fullmatch(r'-?\d{5,20}', s):
            try:
                return int(s)
            except Exception:
                return None
        return None

    def _tg_keyboard(rows: list[list[dict]] | None = None) -> dict | None:
        rows = rows or []
        clean_rows = []
        for row in rows:
            clean_row = []
            for btn in row:
                text = str(btn.get('text') or '').strip()
                url = str(btn.get('url') or '').strip()
                if text and url:
                    clean_row.append({'text': text[:64], 'url': url})
            if clean_row:
                clean_rows.append(clean_row)
        return {'inline_keyboard': clean_rows} if clean_rows else None

    def _url_button(text: str, url: str) -> dict:
        return {'text': text, 'url': url}

    def _notification_state() -> dict:
        state = _read_json(NOTIFICATION_STATE_FILE, {})
        return state if isinstance(state, dict) else {}

    def _notification_was_sent(key: str) -> bool:
        return key in _notification_state()

    def _mark_notification_sent(key: str):
        state = _notification_state()
        state[key] = datetime.datetime.utcnow().isoformat()
        # Keep the file bounded.
        if len(state) > 2000:
            items = sorted(state.items(), key=lambda kv: kv[1])[-1000:]
            state = dict(items)
        _write_json(NOTIFICATION_STATE_FILE, state)

    def _append_json_log(path: str, entry: dict, limit: int = 700):
        try:
            rows = _read_json(path, [])
            if not isinstance(rows, list):
                rows = []
            entry.setdefault('id', secrets.token_urlsafe(8))
            entry.setdefault('created_at', datetime.datetime.utcnow().isoformat())
            rows.insert(0, entry)
            _write_json(path, rows[:limit])
        except Exception:
            try:
                app.logger.warning("failed to append log %s", path, exc_info=True)
            except Exception:
                pass

    def _visit_is_bot(user_agent: str) -> bool:
        ua = (user_agent or '').lower()
        markers = ('bot', 'crawl', 'spider', 'slurp', 'preview', 'telegrambot', 'whatsapp', 'facebookexternalhit')
        return any(marker in ua for marker in markers)

    def _record_site_visit():
        try:
            if request.method != 'GET' or not _public_analytics_allowed_path(request.path):
                return
            ua = (request.headers.get('User-Agent') or '')[:300]
            ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
            day = datetime.datetime.utcnow().date().isoformat()
            visitor_key = hashlib.sha256(
                f"{day}|{app.secret_key}|{ip}|{ua}".encode('utf-8', 'ignore')
            ).hexdigest()[:18]
            ref = (request.headers.get('Referer') or '')[:500]
            ref_host = ''
            if ref:
                try:
                    ref_host = urlparse(ref).netloc.lower()[:160]
                except Exception:
                    ref_host = ''
            _append_json_log(VISIT_LOG_FILE, {
                'path': request.path,
                'query': request.query_string.decode('utf-8', 'ignore')[:250],
                'referrer': ref,
                'referrer_host': ref_host,
                'user_agent': ua,
                'visitor': visitor_key,
                'is_bot': _visit_is_bot(ua),
            }, limit=5000)
        except Exception:
            pass

    @app.before_request
    def _track_public_visit():
        _record_site_visit()

    def _parse_visit_dt(item: dict):
        raw = str(item.get('created_at') or '')
        try:
            return datetime.datetime.fromisoformat(raw)
        except Exception:
            return None

    def _parse_visit_date(value: str | None):
        value = (value or '').strip()
        if not value:
            return None
        try:
            return datetime.date.fromisoformat(value[:10])
        except Exception:
            return None

    def _site_visit_stats(days: int = 30, start_date=None, end_date=None) -> dict:
        visits = _read_json(VISIT_LOG_FILE, [])
        if not isinstance(visits, list):
            visits = []
        today = datetime.datetime.utcnow().date()
        end = end_date or today
        start = start_date or (end - datetime.timedelta(days=max(1, days) - 1))
        if start > end:
            start, end = end, start
        if (end - start).days > 365:
            start = end - datetime.timedelta(days=365)
        period_days = max(1, (end - start).days + 1)
        filtered = []
        bot_count = 0
        for item in visits:
            if not isinstance(item, dict):
                continue
            dt = _parse_visit_dt(item)
            if not dt:
                continue
            if dt.date() < start or dt.date() > end:
                continue
            if item.get('is_bot'):
                bot_count += 1
                continue
            filtered.append((item, dt))

        by_day = []
        for offset in range(period_days):
            day = start + datetime.timedelta(days=offset)
            day_items = [item for item, dt in filtered if dt.date() == day]
            by_day.append({
                'date': day.isoformat(),
                'views': len(day_items),
                'unique': len({item.get('visitor') for item in day_items if item.get('visitor')}),
            })

        page_counter = Counter((item.get('path') or '/') for item, _dt in filtered)
        ref_counter = Counter(
            (item.get('referrer_host') or 'direct')
            for item, _dt in filtered
            if (item.get('referrer_host') or 'direct')
        )
        visitors = {item.get('visitor') for item, _dt in filtered if item.get('visitor')}
        recent = []
        for item, dt in filtered[:25]:
            recent.append({
                'created_at': item.get('created_at') or '',
                'path': item.get('path') or '/',
                'referrer_host': item.get('referrer_host') or 'direct',
                'is_bot': bool(item.get('is_bot')),
            })
        if start == end:
            period_label = start.strftime('%d.%m.%Y')
        else:
            period_label = f"{start.strftime('%d.%m.%Y')} - {end.strftime('%d.%m.%Y')}"
        return {
            'days': period_days,
            'start_date': start.isoformat(),
            'end_date': end.isoformat(),
            'period_label': period_label,
            'total_views': len(filtered),
            'unique_visitors': len(visitors),
            'today_views': by_day[-1]['views'] if by_day else 0,
            'today_unique': by_day[-1]['unique'] if by_day else 0,
            'bot_hits': bot_count,
            'by_day': by_day,
            'top_pages': [{'path': k, 'views': v} for k, v in page_counter.most_common(8)],
            'top_referrers': [{'host': k, 'views': v} for k, v in ref_counter.most_common(8)],
            'recent': recent,
        }

    def _append_app_event(kind: str, message: str, level: str = 'warning', meta: dict | None = None):
        entry = {
            'level': level,
            'kind': kind,
            'message': str(message or '')[:1200],
            'meta': meta or {},
        }
        if has_request_context():
            entry.update({
                'method': request.method,
                'path': request.path,
                'endpoint': request.endpoint or '',
                'remote_addr': request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip(),
            })
        _append_json_log(APP_EVENTS_FILE, entry)

    def _append_notification_log(chat_id, text: str, status: str, ok: bool, error: str = ''):
        _append_json_log(NOTIFICATION_LOG_FILE, {
            'channel': 'telegram',
            'recipient': str(chat_id or ''),
            'status': status,
            'ok': bool(ok),
            'error': str(error or '')[:500],
            'text': str(text or '')[:4000],
            'text_preview': str(text or '').replace('\n', ' ')[:180],
        })

    def _safe_send_message(chat_id: int | str | None, text: str, buttons: list[list[dict]] | None = None) -> bool:
        if not chat_id:
            _append_notification_log('', text, 'skipped', False, 'missing chat_id')
            return False
        if not os.environ.get('TELEGRAM_BOT_TOKEN'):
            _append_notification_log(chat_id, text, 'skipped', False, 'TELEGRAM_BOT_TOKEN not set')
            _append_app_event('telegram_skipped', 'TELEGRAM_BOT_TOKEN not set', 'warning', {'recipient': str(chat_id)})
            return False
        try:
            result = send_message(chat_id, text, reply_markup=_tg_keyboard(buttons))
            ok = bool(result.get('ok', True)) if isinstance(result, dict) else True
            _append_notification_log(chat_id, text, 'delivered' if ok else 'failed', ok, '' if ok else 'Telegram returned ok=false')
            if not ok:
                _append_app_event('telegram_failed', 'Telegram returned ok=false', 'error', {'recipient': str(chat_id), 'result': result})
                return False
            return True
        except Exception as e:
            _append_notification_log(chat_id, text, 'failed', False, str(e))
            _append_app_event('telegram_failed', str(e), 'error', {'recipient': str(chat_id)})
            try:
                app.logger.warning("Telegram notify failed for %s: %s", chat_id, e)
            except Exception:
                pass
            return False

    @app.after_request
    def _monitor_problem_responses(resp):
        try:
            should_log = (
                resp.status_code >= 500
                or (resp.status_code >= 400 and (request.method != 'GET' or request.path.startswith('/api/')))
            )
            if should_log and request.path not in ('/healthz',):
                _append_app_event(
                    'http_problem',
                    f"{request.method} {request.path} -> {resp.status_code}",
                    'error' if resp.status_code >= 500 else 'warning',
                    {'status_code': resp.status_code},
                )
        except Exception:
            pass
        return resp

    def _normalize_work_dates(legacy_slots) -> dict:
        out: dict[str, dict] = {}
        if not isinstance(legacy_slots, list):
            return out
        for slot in legacy_slots:
            if not isinstance(slot, dict):
                continue
            start = str(slot.get('start') or '').strip()
            end = str(slot.get('end') or '').strip()
            if len(start) < 16:
                continue
            date = start[:10]
            start_hm = start[11:16] if len(start) >= 16 else ''
            end_hm = end[11:16] if len(end) >= 16 else ''
            payload = {}
            if start_hm and end_hm:
                payload['time'] = f"{start_hm}-{end_hm}"
            out[date] = payload
        return out

    def _legacy_user_dates() -> dict:
        rows = _read_json(USERS_FILE, [])
        out = {}
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = (row.get('email') or row.get('phone') or '').strip()
            meet = str(row.get('meet_datetime') or '').strip()
            if key and len(meet) >= 10:
                out[key] = meet[:10]
        return out

    def _legacy_receipts_map() -> dict:
        assignments = _read_json(ASSIGNMENTS_FILE, [])
        receipts = _read_json(RECEIPTS_FILE, {})
        by_assignment = {}
        if isinstance(assignments, list):
            for a in assignments:
                if isinstance(a, dict) and a.get('id') is not None:
                    by_assignment[str(a.get('id'))] = a

        out = {}
        if not isinstance(receipts, dict):
            return out

        for assignment_id, items in receipts.items():
            a = by_assignment.get(str(assignment_id))
            if not a:
                continue
            lead_idx = a.get('lead_index')
            slot_start = str(a.get('slot_start') or '').strip()
            date = slot_start[:10] if len(slot_start) >= 10 else None
            if lead_idx is None or not date:
                continue
            try:
                lead_idx = int(lead_idx)
            except Exception:
                continue
            filenames = []
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and item.get('filename'):
                        filenames.append(str(item.get('filename')))
            if not filenames:
                continue
            out.setdefault(lead_idx, {}).setdefault(date, []).extend(filenames)
        return out

    def _match_nanny_id(raw, nannies: list[dict]) -> str | int | None:
        if raw is None:
            return None
        s = str(raw).strip().lower()
        if not s:
            return None
        for n in nannies:
            nid = str(n.get('id') or '').strip().lower()
            token = str(n.get('portal_token') or '').strip().lower()
            name = str(n.get('name') or '').strip().lower()
            if s in {nid, token, name}:
                return n.get('id')
        return None

    def _seed_reviews() -> list[dict]:
        import datetime as _dt
        def _d(y, m, d): return _dt.datetime(y, m, d, 10, 0, 0).isoformat()
        return [
            {'id':'rev-anna',     'author':'Анна',       'role':'Мама Миши, 3 года',       'stars':5, 'created_at':_d(2020,3,14),  'text':'Нужно было срочно на пару часов — оформила заявку, и уже через 10 минут подтвердили няню. Очень бережно с ребёнком, будем обращаться ещё.'},
            {'id':'rev-igor',     'author':'Игорь',      'role':'Папа Софии, 4 года',      'stars':5, 'created_at':_d(2020,6,22),  'text':'Понравилось, что можно выбрать даты прямо в календаре. Админ быстро всё согласовал, няня приехала вовремя — всё спокойно и аккуратно.'},
            {'id':'rev-marina',   'author':'Марина',     'role':'Мама Лёвы, 1 год',        'stars':5, 'created_at':_d(2020,9,5),   'text':'Малышу 1 год, переживали сильно. Няня сразу нашла подход, помогла с режимом, поиграла и уложила спать без слёз. Спасибо за сервис!'},
            {'id':'rev-olga',     'author':'Ольга',      'role':'Мама Даши, 2 года',       'stars':5, 'created_at':_d(2020,11,18), 'text':'Пользуемся уже третий месяц. Няня — настоящий профессионал: знает, чем занять двухлетнюю непоседу. Дочка встречает её с радостью каждый раз.'},
            {'id':'rev-dmitry',   'author':'Дмитрий',    'role':'Папа Артёма, 5 лет',      'stars':5, 'created_at':_d(2021,1,9),   'text':'Сервис удобный, всё через телефон. Сына оставляли на полный день — вернулись домой, он доволен, сыт и спокоен. Рекомендую всем родителям.'},
            {'id':'rev-svetlana', 'author':'Светлана',   'role':'Мама близнецов, 3 года',  'stars':5, 'created_at':_d(2021,3,27),  'text':'Двое детей — это двойная ответственность. Няня справилась на отлично: успевала следить за обоими, не теряя спокойствия. Очень благодарны.'},
            {'id':'rev-elena',    'author':'Елена',      'role':'Мама Кирилла, 6 лет',     'stars':5, 'created_at':_d(2021,5,14),  'text':'Попросили помочь с домашними заданиями и прогулкой. Няня пришла вовремя, с Кириллом быстро нашла общий язык. Будем звать снова.'},
            {'id':'rev-alexey',   'author':'Алексей',    'role':'Папа Вики, 2 года',       'stars':5, 'created_at':_d(2021,7,3),   'text':'Жена вышла на работу, нужна была помощь на несколько дней в неделю. Нашли через этот сервис — ни разу не пожалели. Всё чётко и по делу.'},
            {'id':'rev-natasha',  'author':'Наташа',     'role':'Мама Егора, 4 года',      'stars':5, 'created_at':_d(2021,9,20),  'text':'Егор — активный мальчик, с ним не всегда просто. Няня придумывала игры, читала книжки, гуляла. После её визитов сын был спокойным и счастливым.'},
            {'id':'rev-roman',    'author':'Роман',      'role':'Папа Маши, 3 года',       'stars':5, 'created_at':_d(2021,11,8),  'text':'Первый раз оставляли дочку с незнакомым человеком — волновались. Но всё прошло отлично. Маша даже расплакалась, когда няня уходила. Лучшая рекомендация!'},
            {'id':'rev-kate',     'author':'Катерина',   'role':'Мама Тимура, 1.5 года',   'stars':5, 'created_at':_d(2022,1,17),  'text':'Малышу полтора года, он очень привязан к маме. Думала, будет сложно. Няня действовала мягко, терпеливо — через полчаса сын уже сам играл с ней.'},
            {'id':'rev-andrew',   'author':'Андрей',     'role':'Папа Нины, 7 лет',        'stars':5, 'created_at':_d(2022,3,6),   'text':'Нине 7 лет, ей нужен был взрослый после школы. Няня встречала её, кормила, проверяла уроки. Спокойно оставляли на несколько часов каждый день.'},
            {'id':'rev-polina',   'author':'Полина',     'role':'Мама Саши, 8 месяцев',    'stars':5, 'created_at':_d(2022,4,29),  'text':'Малышу 8 месяцев — это особый возраст. Няня с опытом работы с грудничками. Знала и режим, и как успокоить. Нам очень повезло.'},
            {'id':'rev-sergey',   'author':'Сергей',     'role':'Папа Кости, 5 лет',       'stars':5, 'created_at':_d(2022,6,12),  'text':'Выбирал долго, читал отзывы. В итоге решился — и не зря. Всё прозрачно: цены, расписание, подтверждение. Ребёнок в надёжных руках.'},
            {'id':'rev-yulia',    'author':'Юлия',       'role':'Мама Сони, 2 года',       'stars':5, 'created_at':_d(2022,8,3),   'text':'Соня капризничает с незнакомыми. Няня нашла подход буквально за 20 минут — принесла мыльные пузыри и растопила лёд. Теперь Соня ждёт её с нетерпением.'},
            {'id':'rev-ivan',     'author':'Иван',       'role':'Папа Ани, 4 года',        'stars':5, 'created_at':_d(2022,9,25),  'text':'Пользуемся уже полгода. Стабильно, надёжно, без сюрпризов. Аня привыкла, всегда знает, что в такой-то день придёт её любимая тётя.'},
            {'id':'rev-kristina', 'author':'Кристина',   'role':'Мама Льва, 3 года',       'stars':5, 'created_at':_d(2022,11,14), 'text':'Лёва после садика — уставший и капризный. Няня умеет переключить: тихие игры, лепка, книжки. Возвращаюсь домой — сын доволен и спокоен.'},
            {'id':'rev-max',      'author':'Максим',     'role':'Папа Полины, 6 лет',      'stars':5, 'created_at':_d(2023,1,22),  'text':'Оставляли дочку на выходные, пока были на свадьбе у друзей. Она провела время с пользой: рисовала, лепила, гуляла. Вернулись — всё в порядке.'},
            {'id':'rev-vika',     'author':'Виктория',   'role':'Мама Кирилла, 2 года',    'stars':5, 'created_at':_d(2023,3,7),   'text':'Мы с мужем первый раз с рождения сына пошли в кино вдвоём. Знали, что малыш в безопасности. Вернулись — он спал, счастливый. Это дорогого стоит.'},
            {'id':'rev-nikita',   'author':'Никита',     'role':'Папа Алисы, 5 лет',       'stars':5, 'created_at':_d(2023,4,18),  'text':'Заявку оформил за 5 минут. Всё прозрачно: кто придёт, когда, сколько стоит. Дочка потом весь вечер рассказывала про нянечку.'},
            {'id':'rev-tanya',    'author':'Татьяна',    'role':'Мама Ромы, 3.5 года',     'stars':5, 'created_at':_d(2023,6,9),   'text':'Рома поначалу плакал, когда я уходила. Прошло две недели — теперь сам открывает дверь и тянет няню играть. Привыкли оба. Рекомендую от всей души.'},
            {'id':'rev-artem',    'author':'Артём',      'role':'Папа Насти, 4 года',      'stars':5, 'created_at':_d(2023,8,1),   'text':'Настя любит рисовать и петь. Няня поддерживала все её затеи, не гасила энергию. Пришла домой — дочка в отличном настроении.'},
            {'id':'rev-masha',    'author':'Маша',       'role':'Мама Пети, 1 год',         'stars':5, 'created_at':_d(2023,9,19),  'text':'Петя ещё совсем маленький, но с няней они быстро подружились. Кормление, сон, прогулка — всё по расписанию. Я могла работать спокойно.'},
            {'id':'rev-vadim',    'author':'Вадим',      'role':'Папа Даши, 7 лет',        'stars':5, 'created_at':_d(2023,10,30), 'text':'Даше нужен был кто-то после школы. Няня проверяла задания, готовила перекус, играла. Дочка стала сама просить, чтобы она приходила чаще.'},
            {'id':'rev-oksana',   'author':'Оксана',     'role':'Мама Матвея, 2 года',      'stars':5, 'created_at':_d(2023,12,5),  'text':'Матвей — непоседа. Няня умеет занять его так, что он не скучает ни минуты. Уходим с мужем, возвращаемся — сын играет, доволен.'},
            {'id':'rev-boris',    'author':'Борис',      'role':'Папа Юли, 3 года',        'stars':5, 'created_at':_d(2024,1,14),  'text':'Юля привязалась к няне за первые же два визита. Теперь сама напоминает: «Сегодня придёт тётя?» Это лучший показатель качества.'},
            {'id':'rev-lena2',    'author':'Лена',       'role':'Мама Феди, 4 года',       'stars':5, 'created_at':_d(2024,3,3),   'text':'Федя — эмоциональный мальчик. Но с этой няней ни разу не было слёз или скандалов. Умеет выстраивать границы мягко. Продолжаем сотрудничество.'},
            {'id':'rev-ilya',     'author':'Илья',       'role':'Папа Кати, 5 лет',        'stars':5, 'created_at':_d(2024,5,21),  'text':'Брал няню на летние месяцы, пока дочка не в садике. Всё лето прошло отлично: прогулки, творчество, купание в бассейне. Катя в восторге.'},
            {'id':'rev-dasha',    'author':'Даша',       'role':'Мама Вани, 1.5 года',     'stars':5, 'created_at':_d(2024,7,8),   'text':'Ваня цеплялся за меня и не отпускал. Няня предложила поиграть вместе, пока я не ушла. Через пять минут сын уже смеялся. Это настоящий профессионализм.'},
            {'id':'rev-evgeny',   'author':'Евгений',    'role':'Папа Лизы, 6 лет',       'stars':5, 'created_at':_d(2024,9,10),  'text':'Использую сервис больше года. Ни разу ничего не пошло не так. Чёткость, пунктуальность, забота — всё на высшем уровне.'},
            {'id':'rev-inga',     'author':'Инга',       'role':'Мама Степана, 3 года',    'stars':5, 'created_at':_d(2024,11,22), 'text':'Степан боялся нянь до этого. Здесь подобрали именно того человека под его характер. Теперь расстаются с трудом.'},
            {'id':'rev-pavel',    'author':'Павел',      'role':'Папа Нади, 4 года',       'stars':5, 'created_at':_d(2025,1,6),   'text':'Надя с первой встречи сказала: «Папа, она добрая». После этого никаких сомнений не осталось. Спасибо за такой сервис.'},
            {'id':'rev-zoya',     'author':'Зоя',        'role':'Мама Глеба, 2 года',      'stars':5, 'created_at':_d(2025,3,18),  'text':'Глебу два года, он только начал говорить. Няня много с ним разговаривала, читала, пела. Через месяц его речь заметно улучшилась.'},
            {'id':'rev-timur',    'author':'Тимур',      'role':'Папа Ксюши, 5 лет',      'stars':5, 'created_at':_d(2025,5,2),   'text':'Удобнее, чем любые другие варианты. Заявка, подтверждение, приход няни — всё как часы. Дочка довольна, мы спокойны. Давно пользуемся и планируем продолжать.'},
        ]

    def load_reviews(include_hidden: bool = False):
        if use_sql:
            query = Review.query
            if not include_hidden:
                query = query.filter_by(is_visible=True)
            rows = query.order_by(Review.created_at.desc()).all()
            if not rows:
                # Seed default reviews into DB (only once — if table is truly empty)
                if Review.query.count() == 0:
                    for s in _seed_reviews():
                        r = Review(
                            id=s['id'],
                            author=s['author'],
                            role=s.get('role', ''),
                            stars=s.get('stars', 5),
                            text=s.get('text', ''),
                            created_at=datetime.datetime.utcnow(),
                            is_visible=True,
                        )
                        db.session.add(r)
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                    query = Review.query
                    if not include_hidden:
                        query = query.filter_by(is_visible=True)
                    rows = query.order_by(Review.created_at.desc()).all()
            return [{'id': r.id, 'author': r.author, 'role': r.role, 'stars': r.stars,
                     'text': r.text, 'created_at': r.created_at.isoformat() if r.created_at else '',
                     'nanny_id': getattr(r, 'nanny_id', None),
                     'pinned': bool(getattr(r, 'pinned', False)),
                     'is_visible': bool(getattr(r, 'is_visible', True))} for r in rows]

        # JSON fallback
        raw = _read_json(REVIEWS_FILE, [])
        changed = False
        reviews = []
        if not isinstance(raw, list) or not raw:
            reviews = _seed_reviews()
            _write_json(REVIEWS_FILE, reviews)
            return reviews

        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                changed = True
                continue
            stars_raw = item.get('stars', item.get('rating', 5))
            try:
                stars = max(1, min(5, int(stars_raw)))
            except Exception:
                stars = 5
                changed = True
            author = (item.get('author') or item.get('parent_name') or 'Родитель').strip()
            role = (item.get('role') or (f"Родитель {item.get('child_name')}" if item.get('child_name') else '')).strip()
            review = {
                'id': item.get('id') or _legacy_token('rev-', f"{author}-{item.get('submitted_at') or item.get('created_at') or idx}"),
                'author': author,
                'role': role,
                'stars': stars,
                'text': (item.get('text') or '').strip(),
                'created_at': item.get('created_at') or item.get('submitted_at') or datetime.datetime.utcnow().isoformat(),
                'nanny_id': item.get('nanny_id') or None,
                'pinned': bool(item.get('pinned', False)),
                'is_visible': bool(item.get('is_visible', True)),
            }
            if review['id'] != item.get('id') or review['author'] != item.get('author') or review['role'] != item.get('role') or review['stars'] != item.get('stars') or review['is_visible'] != item.get('is_visible', True):
                changed = True
            reviews.append(review)

        reviews.sort(key=lambda x: x.get('created_at') or '', reverse=True)
        if changed:
            _write_json(REVIEWS_FILE, reviews)
        if include_hidden:
            return reviews
        return [r for r in reviews if r.get('is_visible', True)]

    def save_reviews(reviews: list[dict]):
        hidden = []
        try:
            current = load_reviews(include_hidden=True)
            visible_ids = {str(r.get('id') or '') for r in reviews if isinstance(r, dict)}
            hidden = [
                r for r in current
                if isinstance(r, dict)
                and not r.get('is_visible', True)
                and str(r.get('id') or '') not in visible_ids
            ]
        except Exception:
            hidden = []
        normalized = []
        for item in reviews:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row['is_visible'] = bool(row.get('is_visible', True))
            normalized.append(row)
        _write_json(REVIEWS_FILE, normalized + hidden)

    def ensure_seed_nannies():
        seed = [
            {
                "id": "svetlana",
                "portal_token": "nanny-svetlana",
                "name": "Светлана",
                "age": 27,
                "exp_short": "Развивающие занятия, лагерь, продлёнка",
                "bio": "🌸 Светлана — внимательная и ответственная няня с разносторонним опытом: воспитатель в детском лагере, куратор продлёнки 1 класса, развивающие/творческие/игровые занятия, организация прогулок и режима. Языки: украинский/русский свободно, английский хороший, польский средний.",
                "photo": "img/nanny_svetlana.jpg",
            },
            {
                "id": "irina",
                "portal_token": "nanny-irina",
                "name": "Ирина",
                "age": 53,
                "exp_short": "5 лет в семьях, спокойная и надёжная",
                "bio": "👩‍🍼 Ирина — 53 года. Опыт работы: 5 лет. Мама взрослой дочери. Добрая, чуткая, внимательная к ребёнку, аккуратная и ответственная. Следует рекомендациям семьи и уважает личные границы.",
                "photo": "img/nanny_irina.jpg",
            },
            {
                "id": "zhanna",
                "portal_token": "nanny-zhanna",
                "name": "Жанна Олеговна",
                "age": 58,
                "exp_short": "10+ лет, дети 0–6, доп. обучение",
                "bio": "🌸 Жанна Олеговна — 58 лет, Санкт‑Петербург. 10+ лет в частных семьях (0–6 лет). Доп. обучение: первая доврачебная помощь, сказкотерапия, семинары по детской психологии, базовые навыки массажа и зарядки для грудничков.",
                "photo": "img/nanny_zhanna.jpg",
            },
            {
                "id": "ludmila",
                "portal_token": "nanny-ludmila",
                "name": "Людмила",
                "age": 57,
                "exp_short": "12,5+ лет, сад/интернат, младенцы",
                "bio": "💛 Людмила — 57 лет, высшее образование, опыт 12,5+ лет (детский сад, школа‑интернат, работа с младенцами). Спокойная, доброжелательная, легко находит контакт с детьми.",
                "photo": "img/nanny_placeholder.jpg",
            },
        ]

        if use_sql:
            # Seed once into DB if empty
            if db.session.query(Nanny.id).count() > 0:
                return
            for s in seed:
                db.session.add(
                    Nanny(
                        portal_token=s.get("portal_token"),
                        name=s["name"],
                        photo=s.get("photo"),
                        bio=s.get("bio"),
                        exp_short=s.get("exp_short"),
                    )
                )
            db.session.commit()
            return

        # Legacy JSON seed
        # If file exists but was created by an older version (no portal_token), re-seed.
        if os.path.exists(NANNIES_FILE):
            existing = _read_json(NANNIES_FILE, [])
            if isinstance(existing, list) and existing:
                if isinstance(existing[0], dict) and existing[0].get('portal_token'):
                    return
        
        _write_json(NANNIES_FILE, seed)
        return

    def load_nannies():
        ensure_seed_nannies()
        if use_sql:
            items = Nanny.query.order_by(Nanny.id.asc()).all()
            # For template compatibility we return dicts (legacy shape-ish)
            return [
                {
                    'id': str(n.id),
                    'portal_token': n.portal_token or f"nanny-{n.id}",
                    'telegram_user_id': n.telegram_user_id,
                    'name': n.name,
                    'exp_short': n.exp_short,
                    'bio': n.bio,
                    'photo': n.photo or 'img/nanny_placeholder.jpg',
                }
                for n in items
            ]
        return _read_json(NANNIES_FILE, [])

    def _agent_to_dict(agent) -> dict:
        if isinstance(agent, dict):
            return {
                'id': str(agent.get('id') or ''),
                'name': agent.get('name') or '',
                'telegram_user_id': agent.get('telegram_user_id') or '',
                'portal_token': agent.get('portal_token') or '',
                'referral_code': agent.get('referral_code') or '',
                'commission_percent': int(agent.get('commission_percent') or 10),
                'commission_vnd': int(agent.get('commission_vnd') or 200000),
                'payout_delay_days': int(agent.get('payout_delay_days') or 14),
                'notes': agent.get('notes') or '',
                'is_active': bool(agent.get('is_active', True)),
                'created_at': agent.get('created_at') or '',
            }
        return {
            'id': str(agent.id),
            'name': agent.name or '',
            'telegram_user_id': agent.telegram_user_id or '',
            'portal_token': agent.portal_token or '',
            'referral_code': agent.referral_code or '',
            'commission_percent': int(agent.commission_percent or 10),
            'commission_vnd': int(getattr(agent, 'commission_vnd', None) or 200000),
            'payout_delay_days': int(agent.payout_delay_days or 14),
            'notes': agent.notes or '',
            'is_active': bool(agent.is_active),
            'created_at': agent.created_at.isoformat() if agent.created_at else '',
        }

    def load_agents():
        if use_sql:
            return [_agent_to_dict(a) for a in ReferralAgent.query.order_by(ReferralAgent.id.asc()).all()]
        raw = _read_json(AGENTS_FILE, [])
        if not isinstance(raw, list):
            return []
        changed = False
        normalized = []
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                changed = True
                continue
            row = _agent_to_dict(item)
            if not row['id']:
                row['id'] = str(idx + 1)
                changed = True
            if not row['portal_token']:
                row['portal_token'] = secrets.token_urlsafe(16)
                changed = True
            if not row['referral_code']:
                row['referral_code'] = _legacy_token('ref-', row['name'] or row['id'])[:18]
                changed = True
            normalized.append(row)
        if changed:
            _write_json(AGENTS_FILE, normalized)
        return normalized

    def save_agents(agents):
        if use_sql:
            raise RuntimeError('save_agents is not used in SQL mode')
        _write_json(AGENTS_FILE, agents)

    def _agent_by_referral_code(code: str | None):
        code = (code or '').strip()
        if not code:
            return None
        if use_sql:
            return ReferralAgent.query.filter_by(referral_code=code, is_active=True).first()
        return next((a for a in load_agents() if a.get('referral_code') == code and a.get('is_active', True)), None)

    def _agent_by_portal_token(portal_token: str | None):
        portal_token = (portal_token or '').strip()
        if not portal_token:
            return None
        if use_sql:
            return ReferralAgent.query.filter_by(portal_token=portal_token, is_active=True).first()
        return next((a for a in load_agents() if a.get('portal_token') == portal_token and a.get('is_active', True)), None)

    def _agent_by_telegram_id(telegram_user_id):
        if not telegram_user_id:
            return None
        if use_sql:
            return ReferralAgent.query.filter_by(telegram_user_id=int(telegram_user_id), is_active=True).first()
        return next((a for a in load_agents() if str(a.get('telegram_user_id') or '') == str(telegram_user_id) and a.get('is_active', True)), None)

    def _new_agent_code(name: str | None = None) -> str:
        def _code_exists(code: str) -> bool:
            if use_sql:
                return ReferralAgent.query.filter_by(referral_code=code).first() is not None
            return any(a.get('referral_code') == code for a in load_agents())

        base = re.sub(r'[^a-z0-9]+', '-', (name or '').lower()).strip('-')[:18]
        prefix = base or 'agent'
        for _ in range(20):
            suffix = secrets.token_urlsafe(4).replace('-', '').replace('_', '')[:6].lower()
            code = f"{prefix}-{suffix}"[:32]
            if not _code_exists(code):
                return code
        return secrets.token_urlsafe(10).replace('-', '').replace('_', '').lower()

    def _agent_id(agent) -> str | None:
        if not agent:
            return None
        return str(agent.get('id') if isinstance(agent, dict) else agent.id)

    def _nanny_value(nanny_obj, key, default=None):
        if nanny_obj is None:
            return default
        if isinstance(nanny_obj, dict):
            return nanny_obj.get(key, default)
        return getattr(nanny_obj, key, default)

    def _ensure_agent_for_nanny(nanny_obj):
        """Every Telegram-linked nanny is also a referral partner by default."""
        if not nanny_obj:
            return None
        telegram_user_id = _nanny_value(nanny_obj, 'telegram_user_id')
        if not telegram_user_id:
            return None
        try:
            telegram_user_id = int(telegram_user_id)
        except Exception:
            return None

        nanny_name = _nanny_value(nanny_obj, 'name') or 'Няня'
        nanny_id = _nanny_value(nanny_obj, 'id') or ''
        default_notes = (
            f"Автоматически создано для няни {nanny_name}"
            f"{' #' + str(nanny_id) if nanny_id else ''}. "
            "Доступ к реферальному кабинету включен по умолчанию."
        )

        if use_sql:
            agent = ReferralAgent.query.filter_by(telegram_user_id=telegram_user_id).first()
            if agent:
                changed = False
                if not agent.is_active:
                    agent.is_active = True
                    changed = True
                if not agent.commission_vnd:
                    agent.commission_vnd = 200000
                    changed = True
                if not agent.payout_delay_days:
                    agent.payout_delay_days = 14
                    changed = True
                if not agent.portal_token:
                    agent.portal_token = secrets.token_urlsafe(18)
                    changed = True
                if not agent.referral_code:
                    agent.referral_code = _new_agent_code(nanny_name)
                    changed = True
                if changed:
                    db.session.commit()
                return agent

            agent = ReferralAgent(
                name=nanny_name,
                telegram_user_id=telegram_user_id,
                portal_token=secrets.token_urlsafe(18),
                referral_code=_new_agent_code(nanny_name),
                commission_vnd=200000,
                payout_delay_days=14,
                notes=default_notes,
                is_active=True,
            )
            db.session.add(agent)
            db.session.commit()
            return agent

        agents = load_agents()
        agent = next((a for a in agents if str(a.get('telegram_user_id') or '') == str(telegram_user_id)), None)
        if agent:
            changed = False
            defaults = {
                'is_active': True,
                'commission_vnd': agent.get('commission_vnd') or 200000,
                'payout_delay_days': agent.get('payout_delay_days') or 14,
                'portal_token': agent.get('portal_token') or secrets.token_urlsafe(18),
                'referral_code': agent.get('referral_code') or _new_agent_code(nanny_name),
            }
            for key, value in defaults.items():
                if agent.get(key) != value:
                    agent[key] = value
                    changed = True
            if changed:
                save_agents(agents)
            return agent

        next_id = str(max([int(a.get('id') or 0) for a in agents] or [0]) + 1)
        agent = {
            'id': next_id,
            'name': nanny_name,
            'telegram_user_id': telegram_user_id,
            'portal_token': secrets.token_urlsafe(18),
            'referral_code': _new_agent_code(nanny_name),
            'commission_vnd': 200000,
            'payout_delay_days': 14,
            'notes': default_notes,
            'is_active': True,
            'created_at': datetime.datetime.utcnow().isoformat(),
        }
        agents.append(agent)
        save_agents(agents)
        return agent

    def _client_portal_for_telegram(telegram_user_id, telegram_username: str | None = None, attach: bool = False) -> dict | None:
        if not telegram_user_id:
            return None
        tg_id_str = str(telegram_user_id)
        tg_username = (telegram_username or '').strip().lstrip('@').lower()
        lead_obj = None
        if use_sql:
            lead_obj = Lead.query.filter_by(telegram_user_id=int(telegram_user_id)).order_by(Lead.submitted_at.desc()).first()
            if not lead_obj and tg_username:
                lead_obj = Lead.query.filter(
                    db.func.lower(Lead.telegram).in_([tg_username, '@' + tg_username])
                ).order_by(Lead.submitted_at.desc()).first()
            if not lead_obj:
                lead_obj = Lead.query.filter_by(telegram=tg_id_str).order_by(Lead.submitted_at.desc()).first()
            if lead_obj:
                changed = False
                if attach and not lead_obj.telegram_user_id:
                    lead_obj.telegram_user_id = int(telegram_user_id)
                    changed = True
                referral_agent = _agent_by_referral_code(session.get('referral_agent_code')) if attach else None
                if referral_agent and not lead_obj.referral_agent_id:
                    lead_obj.referral_agent_id = int(_agent_id(referral_agent))
                    changed = True
                if changed:
                    db.session.commit()
                return {
                    'role': 'client',
                    'label': 'Кабинет клиента',
                    'url': f"/client/{lead_obj.token}",
                }
            client_row = Client.query.filter_by(telegram_user_id=int(telegram_user_id)).first()
            if client_row:
                return {'role': 'client', 'label': 'Кабинет клиента', 'url': '/client/app'}
            return None

        leads_list = _read_json(LEADS_FILE, [])
        for lead in leads_list:
            lead_tg = str(lead.get('telegram_user_id') or '')
            lead_uname = str(lead.get('telegram_username') or '').lstrip('@').lower()
            lead_field = str(lead.get('telegram') or '').lstrip('@').lower()
            if (lead_tg and lead_tg == tg_id_str) or \
               (lead_field and lead_field == tg_id_str) or \
               (tg_username and (lead_uname == tg_username or lead_field == tg_username)):
                lead_obj = lead
                break
        if not lead_obj or not lead_obj.get('token'):
            return None
        changed = False
        if attach and not lead_obj.get('telegram_user_id'):
            lead_obj['telegram_user_id'] = int(telegram_user_id)
            changed = True
        referral_agent = _agent_by_referral_code(session.get('referral_agent_code')) if attach else None
        if referral_agent and not lead_obj.get('referral_agent_id'):
            lead_obj['referral_agent_id'] = _agent_id(referral_agent)
            changed = True
        if changed:
            save_leads(leads_list)
        return {
            'role': 'client',
            'label': 'Кабинет клиента',
            'url': f"/client/{lead_obj['token']}",
        }

    def _available_portals_for_telegram(telegram_user_id, telegram_username: str | None = None, attach_client: bool = False) -> list[dict]:
        if not telegram_user_id:
            return []
        portals: list[dict] = []
        try:
            tid = int(telegram_user_id)
        except Exception:
            return portals

        if tid in admin_ids():
            portals.append({'role': 'admin', 'label': 'Кабинет админа', 'url': '/admin'})

        default_agent = None
        if use_sql:
            nanny = Nanny.query.filter_by(telegram_user_id=tid).first()
            if nanny:
                portals.append({'role': 'nanny', 'label': 'Кабинет няни', 'url': '/nanny/app'})
                default_agent = _ensure_agent_for_nanny(nanny)
        else:
            nanny = next((n for n in _read_json(NANNIES_FILE, []) if str(n.get('telegram_user_id') or '') == str(tid)), None)
            if nanny:
                portals.append({'role': 'nanny', 'label': 'Кабинет няни', 'url': '/nanny/app'})
                default_agent = _ensure_agent_for_nanny(nanny)

        client_portal = _client_portal_for_telegram(tid, telegram_username, attach=attach_client)
        if client_portal:
            portals.append(client_portal)

        agent = default_agent or _agent_by_telegram_id(tid)
        if agent:
            portals.append({'role': 'agent', 'label': 'Кабинет партнёра', 'url': '/agent/app'})

        seen = set()
        unique = []
        for portal in portals:
            key = (portal.get('role'), portal.get('url'))
            if key in seen:
                continue
            seen.add(key)
            unique.append(portal)
        return unique

    def _portal_switch_options_for_session(current_role: str) -> list[dict]:
        tid = session.get('telegram_user_id')
        if not tid:
            return []
        portals = _available_portals_for_telegram(tid, session.get('telegram_username'), attach_client=False)
        return [p for p in portals if p.get('role') != current_role]

    def _append_auth_token(url: str, role: str, telegram_user_id: int) -> str:
        if role == 'client':
            return url
        token = _make_auth_token(role, telegram_user_id)
        sep = '&' if '?' in url else '?'
        return f"{url}{sep}_t={token}"

    _NANNY_REVIEW_TEMPLATES = [
        {
            'author': 'Елена',
            'role': 'мама Алисы, 4 года',
            'text': '{nanny} очень спокойно вошла в контакт с дочкой. За вечер успели поиграть, поужинать и лечь спать без слез. После такого опыта оставлять ребенка намного спокойнее.',
        },
        {
            'author': 'Мария',
            'role': 'мама Тимура, 2 года',
            'text': 'Понравилось, что {nanny} сразу уточнила режим, привычки и важные мелочи. Сын был занят, накормлен и в хорошем настроении, а мне присылали короткие понятные сообщения.',
        },
        {
            'author': 'Андрей',
            'role': 'папа Вики, 5 лет',
            'text': '{nanny} приехала вовремя, быстро нашла общий язык с ребенком и спокойно провела весь день. Дочка потом рассказывала про игры и попросила позвать няню еще раз.',
        },
        {
            'author': 'Ольга',
            'role': 'мама Матвея, 3 года',
            'text': 'Очень аккуратная и внимательная работа. {nanny} не просто присматривала, а занимала ребенка: рисовали, гуляли, читали. Вернулись домой к спокойному и довольному малышу.',
        },
        {
            'author': 'Наталья',
            'role': 'мама Сони, 6 лет',
            'text': 'С {nanny} было легко договориться по времени и деталям. Видно, что человек с опытом: без суеты, мягко, но уверенно. Соня осталась довольна, мы тоже.',
        },
        {
            'author': 'Ирина',
            'role': 'мама Льва, 1 год',
            'text': '{nanny} очень бережно отнеслась к малышу и нашему режиму. Все было по расписанию: кормление, сон, прогулка. Для нас это прямое попадание в ожидания.',
        },
        {
            'author': 'Дмитрий',
            'role': 'папа Кирилла, 7 лет',
            'text': 'Нужно было помочь после школы и с уроками. {nanny} спокойно разобрала задания, приготовила перекус и заняла ребенка до нашего возвращения. Все четко и надежно.',
        },
    ]

    def _seeded_nanny_review_id(portal_token: str, idx: int) -> str:
        digest = hashlib.sha1(portal_token.encode('utf-8', 'ignore')).hexdigest()[:12]
        return f"nannyrev-{digest}-{idx + 1}"

    def _default_nanny_review(nanny: dict, idx: int) -> dict:
        portal_token = str(nanny.get('portal_token') or nanny.get('id') or '').strip()
        nanny_name = (nanny.get('name') or 'няня').strip()
        nanny_first_name = nanny_name.split()[0] if nanny_name else 'Няня'
        template = _NANNY_REVIEW_TEMPLATES[idx % len(_NANNY_REVIEW_TEMPLATES)]
        created_at = (datetime.datetime(2025, 4, 1, 10, 0, 0) + datetime.timedelta(days=idx)).isoformat()
        return {
            'id': _seeded_nanny_review_id(portal_token, idx),
            'author': template['author'],
            'role': template['role'],
            'stars': 5,
            'text': template['text'].format(nanny=nanny_first_name),
            'created_at': created_at,
            'nanny_id': portal_token,
            'pinned': idx == 0,
        }

    def ensure_nanny_profile_reviews(nannies=None, min_count: int = 5):
        nannies = nannies if nannies is not None else load_nannies()
        if not nannies:
            return

        if use_sql:
            changed = False
            for nanny in nannies:
                portal_token = str(nanny.get('portal_token') or '').strip()
                if not portal_token:
                    continue
                existing_count = Review.query.filter_by(nanny_id=portal_token).count()
                if existing_count > 0:
                    continue
                idx = 0
                while existing_count < min_count and idx < min_count + len(_NANNY_REVIEW_TEMPLATES):
                    review = _default_nanny_review(nanny, idx)
                    idx += 1
                    if Review.query.get(review['id']):
                        continue
                    db.session.add(Review(
                        id=review['id'],
                        author=review['author'],
                        role=review['role'],
                        stars=review['stars'],
                        text=review['text'],
                        created_at=datetime.datetime.fromisoformat(review['created_at']),
                        is_visible=True,
                        nanny_id=review['nanny_id'],
                        pinned=review['pinned'],
                    ))
                    existing_count += 1
                    changed = True
            if changed:
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            return

        reviews = load_reviews(include_hidden=True)
        used_ids = {str(r.get('id')) for r in reviews if isinstance(r, dict)}
        changed = False
        for nanny in nannies:
            portal_token = str(nanny.get('portal_token') or '').strip()
            if not portal_token:
                continue
            existing_count = len([r for r in reviews if r.get('nanny_id') == portal_token])
            if existing_count > 0:
                continue
            idx = 0
            while existing_count < min_count and idx < min_count + len(_NANNY_REVIEW_TEMPLATES):
                review = _default_nanny_review(nanny, idx)
                idx += 1
                if review['id'] in used_ids:
                    continue
                reviews.insert(0, review)
                used_ids.add(review['id'])
                existing_count += 1
                changed = True
        if changed:
            reviews.sort(key=lambda x: x.get('created_at') or '', reverse=True)
            save_reviews(reviews)

    def load_leads():
        if use_sql:
            items = Lead.query.order_by(Lead.submitted_at.desc()).all()
            return [
                {
                    'token': l.token,
                    'parent_name': l.parent_name,
                    'telegram': l.telegram,
                    'child_name': l.child_name,
                    'child_age': l.child_age,
                    'notes': l.notes,
                    'meeting_date': l.meeting_date,
                    'work_dates': l.work_dates or {},
                    'assigned_nanny_id': str(l.assigned_nanny_id) if l.assigned_nanny_id else None,
                    'client_rate_per_hour': l.client_rate_per_hour or DEFAULT_CLIENT_RATE_VND,
                    'nanny_rate_per_hour': l.nanny_rate_per_hour or DEFAULT_NANNY_RATE_VND,
                    'referral_agent_id': str(l.referral_agent_id) if l.referral_agent_id else None,
                    'telegram_user_id': l.telegram_user_id,
                    'submitted_at': l.submitted_at.isoformat(),
                    'documents': l.documents or {},
                }
                for l in items
            ]

        raw = _read_json(LEADS_FILE, [])
        if not isinstance(raw, list):
            return []

        nannies = load_nannies()
        legacy_user_dates = _legacy_user_dates()
        legacy_receipts = _legacy_receipts_map()
        changed = False
        normalized = []

        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                changed = True
                continue

            token = item.get('token')
            if not token:
                raw_key = f"{item.get('submitted_at') or ''}-{item.get('email') or ''}-{item.get('phone') or ''}-{idx}"
                token = _legacy_token('lead-', raw_key)
                changed = True

            meeting_date = item.get('meeting_date')
            if not meeting_date:
                meet_raw = str(item.get('meet_datetime') or item.get('meeting_datetime') or '').strip()
                if len(meet_raw) >= 10:
                    meeting_date = meet_raw[:10]
                else:
                    key = (item.get('email') or item.get('phone') or '').strip()
                    meeting_date = legacy_user_dates.get(key)
                if meeting_date:
                    changed = True

            work_dates = item.get('work_dates') or {}
            if not work_dates and item.get('work_slots'):
                work_dates = _normalize_work_dates(item.get('work_slots'))
                changed = True

            assigned_nanny_id = item.get('assigned_nanny_id')
            if not assigned_nanny_id:
                assigned_nanny_id = _match_nanny_id(
                    item.get('assigned_nanny') or item.get('assigned_nanny_email') or item.get('nanny_id'),
                    nannies,
                )
                if assigned_nanny_id is not None:
                    changed = True

            docs = item.get('documents') or {}
            if not isinstance(docs, dict):
                docs = {}
                changed = True
            docs.setdefault('receipts', {})
            if legacy_receipts.get(idx):
                merged = False
                for date_key, filenames in legacy_receipts[idx].items():
                    bucket = docs['receipts'].setdefault(date_key, [])
                    for filename in filenames:
                        if filename not in bucket:
                            bucket.append(filename)
                            merged = True
                if merged:
                    changed = True

            row = {
                'token': token,
                'parent_name': (item.get('parent_name') or item.get('name') or '').strip(),
                'telegram': (item.get('telegram') or item.get('phone') or item.get('email') or '').strip(),
                'child_name': (item.get('child_name') or '').strip(),
                'child_age': str(item.get('child_age') or '').strip(),
                'notes': (item.get('notes') or '').strip(),
                'meeting_date': meeting_date,
                'work_dates': work_dates if isinstance(work_dates, dict) else {},
                'assigned_nanny_id': assigned_nanny_id,
                'client_rate_per_hour': item.get('client_rate_per_hour') or DEFAULT_CLIENT_RATE_VND,
                'nanny_rate_per_hour': item.get('nanny_rate_per_hour') or DEFAULT_NANNY_RATE_VND,
                'referral_agent_id': item.get('referral_agent_id'),
                'telegram_user_id': item.get('telegram_user_id'),
                'telegram_username': item.get('telegram_username'),
                'submitted_at': item.get('submitted_at') or datetime.datetime.utcnow().isoformat(),
                'documents': docs,
            }
            normalized.append(row)

        normalized.sort(key=lambda x: x.get('submitted_at') or '', reverse=True)
        if changed:
            _write_json(LEADS_FILE, normalized)
        return normalized

    def save_leads(leads):
        if use_sql:
            raise RuntimeError('save_leads is not used in SQL mode')
        _write_json(LEADS_FILE, leads)

    @app.route('/')
    def index():
        nannies_preview = load_nannies()
        load_reviews()
        ensure_nanny_profile_reviews(nannies_preview)
        reviews = load_reviews()
        return render_template('index.html', nannies_preview=nannies_preview, reviews=reviews)

    
    @app.route('/app')
    def tg_entry():
        # Canonical entry for Telegram Mini App (BotFather URL should point here)
        return render_template('tg_entry.html')

    @app.route('/nanny/login')
    def nanny_login():
        return render_template('nanny_login.html')

    _admin_login_rate: dict[str, list[float]] = {}

    @app.route('/admin/login', methods=['GET', 'POST'])
    def admin_login():
        error = None
        if request.method == 'POST':
            ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
            now = time.time()
            hits = [t for t in _admin_login_rate.get(ip, []) if now - t < 900]
            if len(hits) >= 10:
                return render_template('admin_login.html', error='Слишком много попыток. Попробуйте через 15 минут.'), 429
            token = os.environ.get('ADMIN_TOKEN', '')
            entered = (request.form.get('password') or '').strip()
            hits.append(now)
            _admin_login_rate[ip] = hits
            if token and secrets.compare_digest(entered, token):
                _admin_login_rate.pop(ip, None)
                session.permanent = True
                session['role'] = 'admin'
                session['telegram_user_id'] = 0  # browser login marker
                return redirect(url_for('admin'))
            else:
                error = 'Неверный пароль'
        return render_template('admin_login.html', error=error)

    @app.route('/nanny/app')
    @require_nanny
    def nanny_app():
        return render_template('nanny_app.html', switch_portals=_portal_switch_options_for_session('nanny'))

    @app.route('/nanny')
    @require_nanny
    def nanny_home():
        return render_template('nanny_app.html', switch_portals=_portal_switch_options_for_session('nanny'))


    @app.route('/client/app')
    def client_app():
        return render_template('client_app.html')

    # Backward-compat: older builds linked to /client/me
    @app.route('/client/me')
    def client_me_redirect():
        return redirect(url_for('client_app'))

    @app.route('/api/auth/telegram', methods=['POST'])
    def api_auth_telegram():
        data = request.get_json(force=True) or {}
        init_data = (data.get('init_data') or '').strip()
        bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
        if not bot_token:
            return {'error': 'TELEGRAM_BOT_TOKEN missing'}, 500

        try:
            pairs = validate_webapp_init_data(init_data, bot_token)
            user_raw = pairs.get('user') or ''
            user_obj = json.loads(user_raw) if user_raw else {}
            telegram_user_id = int(user_obj.get('id') or 0)
            if not telegram_user_id:
                return {'error': 'bad user id'}, 400
        except TelegramAuthError as e:
            return {'error': str(e)}, 403
        except Exception:
            return {'error': 'bad initData'}, 400

        # Persist session
        session.permanent = True
        session['telegram_user_id'] = telegram_user_id
        session['telegram_username'] = user_obj.get('username')
        session['telegram_display_name'] = (user_obj.get('first_name') or '')
        session['auth_at'] = int(time.time())

        portals = _available_portals_for_telegram(
            telegram_user_id,
            user_obj.get('username'),
            attach_client=True,
        )
        portal_roles = [p.get('role') for p in portals]
        if 'admin' in portal_roles:
            role = 'admin'
        elif 'nanny' in portal_roles:
            role = 'nanny'
        elif 'agent' in portal_roles:
            role = 'agent'
        elif 'client' in portal_roles:
            role = 'client'
        else:
            role = 'client'

        if use_sql:
            u = User.query.filter_by(telegram_user_id=telegram_user_id).first()
            if not u:
                u = User(telegram_user_id=telegram_user_id, role=role)
                db.session.add(u)
            u.username = user_obj.get('username')
            u.display_name = (user_obj.get('first_name') or '')
            # keep elevated/linked roles fresh
            if role == 'admin' or u.role != 'admin':
                u.role = role
            db.session.commit()

        auth_token = _make_auth_token(role, telegram_user_id)

        # Set session immediately so cookie is sent with this response.
        # This fixes multi-worker deployments (Railway) where in-memory _auth_tokens
        # may not be available on the worker handling the subsequent redirect.
        session.permanent = True
        session['telegram_user_id'] = telegram_user_id
        session['role'] = role

        available_portals = []
        for portal in portals:
            item = dict(portal)
            item['url'] = _append_auth_token(item.get('url') or '/', item.get('role') or 'client', telegram_user_id)
            available_portals.append(item)
        primary_portal = next((p for p in available_portals if p.get('role') == role), None)
        lk_url = (primary_portal or {}).get('url')

        return {
            'ok': True,
            'telegram_user_id': telegram_user_id,
            'telegram_username': user_obj.get('username'),
            'telegram_display_name': (user_obj.get('first_name') or ''),
            'role': role,
            'auth_token': auth_token,
            'lk_url': lk_url,
            'available_portals': available_portals,
        }

    def _require_telegram_session() -> int:
        tid = session.get('telegram_user_id')
        if not tid:
            raise PermissionError('not authenticated')
        return int(tid)

    @app.route('/api/nanny/me/shifts')
    def api_nanny_me_shifts():
        if not use_sql:
            return {'error': 'SQL mode required'}, 400

        try:
            tid = _require_telegram_session()
        except PermissionError:
            return {'error': 'auth required'}, 401

        nanny = Nanny.query.filter_by(telegram_user_id=tid).first()
        if not nanny:
            return {'error': 'nanny not linked (ask admin to set your Telegram ID)'}, 403

        shifts = (
            Shift.query
            .filter_by(nanny_id=nanny.id)
            .order_by(Shift.date.desc(), Shift.planned_start.desc())
            .limit(50)
            .all()
        )

        # Compute payout estimate if we have actuals or planned times
        from time_utils import compute_amount_vnd

        items = []
        for s in shifts:
            start = s.nanny_actual_start or s.planned_start
            end = s.nanny_actual_end or s.planned_end
            nanny_total = None
            try:
                if s.nanny_rate_per_hour and s.date and start and end:
                    nanny_total = compute_amount_vnd(s.date, start, end, s.nanny_rate_per_hour)
            except Exception:
                nanny_total = None

            items.append({
                'id': s.id,
                'date': s.date,
                'planned_start': s.planned_start,
                'planned_end': s.planned_end,
                'status': s.status,
                'nanny_actual_start': s.nanny_actual_start,
                'nanny_actual_end': s.nanny_actual_end,
                'nanny_actual_note': s.nanny_actual_note,
                'nanny_rate_per_hour': s.nanny_rate_per_hour,
                'nanny_total_vnd': nanny_total,
            })

        return {'ok': True, 'items': items}


    @app.route('/api/nanny/me/blocks')
    def api_nanny_me_blocks():
        if not use_sql:
            return {'error': 'SQL mode required'}, 400

        try:
            tid = _require_telegram_session()
        except PermissionError:
            return {'error': 'auth required'}, 401

        nanny = Nanny.query.filter_by(telegram_user_id=tid).first()
        if not nanny:
            return {'error': 'nanny not linked (ask admin to set your Telegram ID)'}, 403

        blocks = (
            NannyBlock.query
            .filter_by(nanny_id=nanny.id)
            .order_by(NannyBlock.date.desc(), NannyBlock.start.asc())
            .limit(200)
            .all()
        )

        return {'ok': True, 'items': [{
            'id': b.id,
            'date': b.date,
            'start': b.start,
            'end': b.end,
            'note': b.note,
            'kind': b.kind,
        } for b in blocks]}

    @app.route('/api/nanny/blocks', methods=['POST'])
    def api_nanny_blocks_create():
        if not use_sql:
            return {'error': 'SQL mode required'}, 400

        try:
            tid = _require_telegram_session()
        except PermissionError:
            return {'error': 'auth required'}, 401

        nanny = Nanny.query.filter_by(telegram_user_id=tid).first()
        if not nanny:
            return {'error': 'nanny not linked (ask admin to set your Telegram ID)'}, 403

        data = request.get_json(silent=True) or {}
        date = (data.get('date') or '').strip()
        start = (data.get('start') or '').strip() or None
        end = (data.get('end') or '').strip() or None
        note = _clean_user_text(data.get('note'), 300) or None

        # Basic validation: date must be YYYY-MM-DD
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
            return {'error': 'invalid date'}, 400
        if (start and not re.match(r'^\d{2}:\d{2}$', start)) or (end and not re.match(r'^\d{2}:\d{2}$', end)):
            return {'error': 'invalid time'}, 400

        b = NannyBlock(nanny_id=nanny.id, date=date, start=start, end=end, note=note, kind='dayoff')
        db.session.add(b)
        db.session.commit()
        _notify_admins(
            "🚫 Няня отметила выходной\n"
            f"Няня: {nanny.name or '—'}\n"
            f"Дата: {date}\n"
            f"Время: {(start or 'весь день') + (('-' + end) if end else '')}\n"
            f"Комментарий: {note or '—'}"
        )
        return {'ok': True, 'id': b.id}

    @app.route('/api/nanny/blocks/<int:block_id>', methods=['DELETE'])
    def api_nanny_blocks_delete(block_id: int):
        if not use_sql:
            return {'error': 'SQL mode required'}, 400

        try:
            tid = _require_telegram_session()
        except PermissionError:
            return {'error': 'auth required'}, 401

        nanny = Nanny.query.filter_by(telegram_user_id=tid).first()
        if not nanny:
            return {'error': 'nanny not linked (ask admin to set your Telegram ID)'}, 403

        b = NannyBlock.query.filter_by(id=block_id, nanny_id=nanny.id).first()
        if not b:
            return {'error': 'not found'}, 404
        db.session.delete(b)
        db.session.commit()
        return {'ok': True}


    @app.route('/api/nanny/shifts/<int:shift_id>/actual', methods=['POST'])
    def api_nanny_shift_actual(shift_id: int):
        if not use_sql:
            return {'error': 'SQL mode required'}, 400

        try:
            tid = _require_telegram_session()
        except PermissionError:
            return {'error': 'auth required'}, 401

        nanny = Nanny.query.filter_by(telegram_user_id=tid).first()
        if not nanny:
            return {'error': 'nanny not linked (ask admin to set your Telegram ID)'}, 403

        s = Shift.query.get(shift_id)
        if not s or s.nanny_id != nanny.id:
            return {'error': 'shift not found'}, 404

        data = request.get_json(force=True) or {}
        actual_start = (data.get('actual_start') or '').strip()
        actual_end = (data.get('actual_end') or '').strip()
        note = _clean_user_text(data.get('note'), 500) or None

        if not actual_start or not actual_end:
            return {'error': 'Заполните начало и конец'}, 400
        if not re.match(r'^[0-9]{2}:[0-9]{2}$', actual_start) or not re.match(r'^[0-9]{2}:[0-9]{2}$', actual_end):
            return {'error': 'Неверный формат времени'}, 400
        if actual_start == actual_end:
            return {'error': 'Начало и конец не должны совпадать'}, 400
        if (int(actual_end[:2]) * 60 + int(actual_end[3:5])) <= (int(actual_start[:2]) * 60 + int(actual_start[3:5])):
            return {'error': 'Конец должен быть позже начала'}, 400

        s.nanny_actual_start = actual_start
        s.nanny_actual_end = actual_end
        s.nanny_actual_note = note
        s.status = 'waiting_client'
        db.session.commit()

        # Notify client if we know their telegram_user_id
        client = Client.query.get(s.client_id) if s.client_id else None
        if client and client.telegram_user_id:
            try:
                _safe_send_message(
                    int(client.telegram_user_id),
                    f"✅ Няня отправила факт по смене {s.date} {s.planned_start or ''}-{s.planned_end or ''}.\nПожалуйста, подтвердите в приложении."
                )
            except Exception:
                pass

        _notify_admins(
            "⏱ Няня отправила фактическое время\n"
            f"Смена: #{s.id}\n"
            f"Няня: {nanny.name or '—'}\n"
            f"Дата: {s.date}\n"
            f"Факт: {actual_start}-{actual_end}\n"
            f"Комментарий: {note or '—'}"
        )

        return {'ok': True}

    @app.route('/api/client/me/shifts')
    def api_client_me_shifts():
        if not use_sql:
            return {'error': 'SQL mode required'}, 400

        try:
            tid = _require_telegram_session()
        except PermissionError:
            return {'error': 'auth required'}, 401

        client = Client.query.filter_by(telegram_user_id=tid).first()
        if not client:
            return {'error': 'client not linked (ask admin to set your Telegram ID)'}, 403

        shifts = (
            Shift.query
            .filter_by(client_id=client.id)
            .order_by(Shift.date.desc(), Shift.planned_start.desc())
            .limit(50)
            .all()
        )

        from time_utils import compute_amount_vnd

        items = []
        for s in shifts:
            # For client we estimate using nanny actual if provided, otherwise planned.
            start = s.nanny_actual_start or s.planned_start
            end = s.nanny_actual_end or s.planned_end
            if s.status == 'confirmed':
                start = s.resolved_start or s.client_actual_start or start
                end = s.resolved_end or s.client_actual_end or end

            client_total = None
            try:
                if s.client_rate_per_hour and s.date and start and end:
                    client_total = compute_amount_vnd(s.date, start, end, s.client_rate_per_hour)
            except Exception:
                client_total = None

            items.append({
                'id': s.id,
                'date': s.date,
                'planned_start': s.planned_start,
                'planned_end': s.planned_end,
                'status': s.status,
                'nanny_actual_start': s.nanny_actual_start,
                'nanny_actual_end': s.nanny_actual_end,
                'client_rate_per_hour': s.client_rate_per_hour,
                'client_total_vnd': client_total,
            })

        return {'ok': True, 'items': items}

    @app.route('/api/client/shifts/<int:shift_id>/actual', methods=['POST'])
    def api_client_shift_actual(shift_id: int):
        if not use_sql:
            return {'error': 'SQL mode required'}, 400

        try:
            tid = _require_telegram_session()
        except PermissionError:
            return {'error': 'auth required'}, 401

        client = Client.query.filter_by(telegram_user_id=tid).first()
        if not client:
            return {'error': 'client not linked (ask admin to set your Telegram ID)'}, 403

        s = Shift.query.get(shift_id)
        if not s or s.client_id != client.id:
            return {'error': 'shift not found'}, 404
        if s.status != 'waiting_client':
            return {'error': 'сейчас нельзя отправить факт'}, 400

        data = request.get_json(force=True) or {}
        actual_start = (data.get('actual_start') or '').strip()
        actual_end = (data.get('actual_end') or '').strip()
        note = _clean_user_text(data.get('note'), 500) or None
        if not actual_start or not actual_end:
            return {'error': 'Заполните начало и конец'}, 400
        if not re.match(r'^[0-9]{2}:[0-9]{2}$', actual_start) or not re.match(r'^[0-9]{2}:[0-9]{2}$', actual_end):
            return {'error': 'Неверный формат времени'}, 400
        if actual_start == actual_end:
            return {'error': 'Начало и конец не должны совпадать'}, 400
        if (int(actual_end[:2]) * 60 + int(actual_end[3:5])) <= (int(actual_start[:2]) * 60 + int(actual_start[3:5])):
            return {'error': 'Конец должен быть позже начала'}, 400

        s.client_actual_start = actual_start
        s.client_actual_end = actual_end
        s.client_actual_note = note

        # If differs from nanny -> admin resolves; if matches -> confirm automatically.
        if s.nanny_actual_start and s.nanny_actual_end and (s.nanny_actual_start == actual_start) and (s.nanny_actual_end == actual_end):
            s.resolved_start = actual_start
            s.resolved_end = actual_end
            s.status = 'confirmed'
        else:
            s.status = 'dispute'

        db.session.commit()

        nanny = Nanny.query.get(s.nanny_id) if s.nanny_id else None
        if nanny and nanny.telegram_user_id:
            try:
                _safe_send_message(int(nanny.telegram_user_id), f"📝 Клиент отправил факт по смене {s.date}. Итог зафиксирует админ.")
            except Exception:
                pass

        if s.status == 'dispute':
            _notify_admins(
                f"⚠️ Разница по смене #{s.id} {s.date}.\n"
                f"Няня: {s.nanny_actual_start}-{s.nanny_actual_end}\n"
                f"Клиент: {actual_start}-{actual_end}\n"
                f"Комментарий: {note or '-'}"
            )
        else:
            _notify_admins(f"✅ Смена #{s.id} {s.date} подтверждена: {actual_start}-{actual_end}.")

        return {'ok': True}

    def _notify_admins(text: str, buttons: list[list[dict]] | None = None):
        ids = admin_ids()
        if not ids:
            return
        for tid in ids:
            _safe_send_message(tid, text, buttons)

    def _notify_on_new_dates(parent_name, child_name, added_dates, lead_token,
                              assigned_nanny_id, client_tg_id):
        """Notify admins when a client adds work dates that need confirmation."""
        site = os.environ.get('SITE_URL', 'https://web-production-2ebe9.up.railway.app').rstrip('/')
        dates_text = ', '.join(added_dates[:10])
        admin_msg = (
            f"📅 Клиент добавил новые даты\n"
            f"Клиент: {parent_name or '—'}\n"
            f"Ребёнок: {child_name or '—'}\n"
            f"Новые даты: {dates_text}\n"
            f"ЛК: {site}/client/{lead_token}"
        )
        _notify_admins(admin_msg)
        if assigned_nanny_id:
            _send_to_nanny(
                assigned_nanny_id,
                "📅 Клиент добавил новую возможную дату\n"
                f"Клиент: {parent_name or '—'}\n"
                f"Ребёнок: {child_name or '—'}\n"
                f"Даты: {dates_text}\n"
                "Дата ожидает подтверждения администратора.",
                _nanny_buttons(assigned_nanny_id),
            )
        if client_tg_id:
            _safe_send_message(
                client_tg_id,
                "📅 Новые даты добавлены\n"
                f"Даты: {dates_text}\n"
                "Администратор проверит расписание и подтвердит их.",
                [[_url_button('Открыть кабинет', f"{site}/client/{lead_token}")]],
            )

    def _site_url() -> str:
        return _public_site_url()

    def _lead_value(lead_obj, key, default=None):
        if lead_obj is None:
            return default
        if isinstance(lead_obj, dict):
            return lead_obj.get(key, default)
        return getattr(lead_obj, key, default)

    def _lead_client_chat_id(lead_obj) -> int | None:
        tg_id = _lead_value(lead_obj, 'telegram_user_id')
        if tg_id:
            try:
                return int(tg_id)
            except Exception:
                pass
        return _extract_chat_id(_lead_value(lead_obj, 'telegram'))

    def _nanny_chat_id_by_id(nanny_id) -> int | None:
        if not nanny_id:
            return None
        try:
            if use_sql:
                nanny_obj = Nanny.query.get(int(nanny_id))
                tg_id = nanny_obj.telegram_user_id if nanny_obj else None
            else:
                nanny_obj = next((n for n in load_nannies() if str(n.get('id')) == str(nanny_id)), None)
                tg_id = nanny_obj.get('telegram_user_id') if nanny_obj else None
            return int(tg_id) if tg_id else None
        except Exception:
            return None

    def _nanny_portal_url_by_id(nanny_id) -> str:
        site = _site_url()
        if not nanny_id:
            return site
        try:
            if use_sql:
                return f"{site}/nanny/app"
            nanny_obj = next((n for n in load_nannies() if str(n.get('id')) == str(nanny_id)), None)
            token = nanny_obj.get('portal_token') if nanny_obj else ''
            return f"{site}/nanny/portal/{token}" if token else site
        except Exception:
            return site

    def _nanny_profile_url_by_id(nanny_id) -> str:
        site = _site_url()
        if not nanny_id:
            return site
        try:
            if use_sql:
                nanny_obj = Nanny.query.get(int(nanny_id))
                token = nanny_obj.portal_token if nanny_obj else ''
            else:
                nanny_obj = next((n for n in load_nannies() if str(n.get('id')) == str(nanny_id)), None)
                token = nanny_obj.get('portal_token') if nanny_obj else ''
            return f"{site}/nanny/{token}" if token else site
        except Exception:
            return site

    def _lead_cabinet_url(lead_obj) -> str:
        token = _lead_value(lead_obj, 'token') or ''
        return f"{_site_url()}/client/{token}" if token else _site_url()

    def _client_buttons(lead_obj, extra: list[dict] | None = None) -> list[list[dict]]:
        row = [_url_button('Открыть кабинет', _lead_cabinet_url(lead_obj))]
        assigned_nanny_id = _lead_value(lead_obj, 'assigned_nanny_id')
        if assigned_nanny_id:
            row.append(_url_button('Профиль няни', _nanny_profile_url_by_id(assigned_nanny_id)))
        rows = [row]
        if extra:
            rows.append(extra)
        return rows

    def _nanny_buttons(nanny_id) -> list[list[dict]]:
        return [[_url_button('Открыть кабинет няни', _nanny_portal_url_by_id(nanny_id))]]

    def _admin_lead_buttons(lead_obj) -> list[list[dict]]:
        return [[_url_button('Открыть ЛК клиента', _lead_cabinet_url(lead_obj))]]

    def _send_to_client(lead_obj, text: str, buttons: list[list[dict]] | None = None) -> bool:
        return _safe_send_message(_lead_client_chat_id(lead_obj), text, buttons)

    def _send_to_nanny(nanny_id, text: str, buttons: list[list[dict]] | None = None) -> bool:
        return _safe_send_message(_nanny_chat_id_by_id(nanny_id), text, buttons)

    def _agent_value(agent_obj, key, default=None):
        if agent_obj is None:
            return default
        if isinstance(agent_obj, dict):
            return agent_obj.get(key, default)
        return getattr(agent_obj, key, default)

    def _agent_portal_url(agent_obj) -> str:
        token = _agent_value(agent_obj, 'portal_token') or ''
        return f"{_site_url()}/agent/{token}" if token else _site_url()

    def _agent_referral_url(agent_obj) -> str:
        code = _agent_value(agent_obj, 'referral_code') or ''
        return f"{_site_url()}/r/{code}" if code else _site_url()

    def _send_to_agent(agent_obj, text: str) -> bool:
        return _safe_send_message(
            _agent_value(agent_obj, 'telegram_user_id'),
            text,
            [[_url_button('Открыть кабинет партнёра', _agent_portal_url(agent_obj))]],
        )

    def _date_time_text(date_str: str, slot: dict | None = None) -> str:
        slot = slot or {}
        return f"{date_str} {slot.get('time') or ''}".strip()

    def _fmt_vnd(amount) -> str:
        try:
            return f"{int(round(float(amount))):,}".replace(',', ' ') + " ₫"
        except Exception:
            return "0 ₫"

    def _comment_needs_admin_attention(text: str) -> bool:
        if not text:
            return False
        low = text.lower()
        words = (
            'опозд', 'не приш', 'не приех', 'проблем', 'жалоб', 'возврат',
            'плохо', 'страш', 'опас', 'грубо', 'конфликт', 'ребенок плакал',
            'не отвечает', 'не связ'
        )
        return any(w in low for w in words)

    def _notify_sensitive_admin_comment(source: str, lead_obj, date_str: str, text: str):
        if not _comment_needs_admin_attention(text):
            return
        key = f"sensitive:{source}:{_lead_value(lead_obj, 'token') or ''}:{date_str}:{hashlib.sha1(text.encode('utf-8', 'ignore')).hexdigest()[:10]}"
        if _notification_was_sent(key):
            return
        _mark_notification_sent(key)
        _notify_admins(
            "⚠️ Комментарий требует внимания\n"
            f"Источник: {source}\n"
            f"Клиент: {_lead_value(lead_obj, 'parent_name') or '—'}\n"
            f"Дата: {date_str}\n"
            f"Комментарий: {text}",
            _admin_lead_buttons(lead_obj),
        )

    def _lead_day_amounts(lead_obj, date_str: str, start: str, end: str) -> tuple[int | None, int | None]:
        try:
            from time_utils import compute_amount_vnd
            client_rate = _lead_value(lead_obj, 'client_rate_per_hour') or DEFAULT_CLIENT_RATE_VND
            nanny_rate = _lead_value(lead_obj, 'nanny_rate_per_hour') or DEFAULT_NANNY_RATE_VND
            return (
                int(compute_amount_vnd(date_str, start, end, int(client_rate))),
                int(compute_amount_vnd(date_str, start, end, int(nanny_rate))),
            )
        except Exception:
            return None, None

    def _minutes_between(start: str | None, end: str | None) -> float | None:
        try:
            if not start or not end:
                return None
            sh, sm = [int(x) for x in start.split(':')[:2]]
            eh, em = [int(x) for x in end.split(':')[:2]]
            diff = (eh * 60 + em) - (sh * 60 + sm)
            if diff <= 0:
                diff += 24 * 60
            return round(diff / 60, 2)
        except Exception:
            return None

    def _slot_time_bounds(slot: dict | None) -> tuple[str | None, str | None, str]:
        slot = slot or {}
        start = slot.get('resolved_start') or slot.get('client_actual_start') or slot.get('fact_start')
        end = slot.get('resolved_end') or slot.get('client_actual_end') or slot.get('fact_end')
        if start and end:
            return start, end, 'actual'
        raw = str(slot.get('time') or '').replace('–', '-').replace('—', '-')
        if '-' in raw:
            left, right = raw.split('-', 1)
            start = left.strip()[:5]
            end = right.strip()[:5]
            if start and end:
                return start, end, 'planned'
        return None, None, 'missing'

    def _lead_slot_finance(lead_obj, date_str: str, slot: dict | None) -> dict:
        start, end, source = _slot_time_bounds(slot)
        client_total, nanny_total = (None, None)
        if start and end:
            client_total, nanny_total = _lead_day_amounts(lead_obj, date_str, start, end)
        margin = None
        if client_total is not None and nanny_total is not None:
            margin = client_total - nanny_total
        return {
            'start': start,
            'end': end,
            'source': source,
            'hours': _minutes_between(start, end),
            'client_total_vnd': client_total,
            'nanny_total_vnd': nanny_total,
            'margin_vnd': margin,
            'client_rate_per_hour': _lead_value(lead_obj, 'client_rate_per_hour') or DEFAULT_CLIENT_RATE_VND,
            'nanny_rate_per_hour': _lead_value(lead_obj, 'nanny_rate_per_hour') or DEFAULT_NANNY_RATE_VND,
        }

    def _notify_lead_day_closed(lead_obj, date_str: str, slot: dict):
        start = slot.get('client_actual_start') or slot.get('fact_start') or ''
        end = slot.get('client_actual_end') or slot.get('fact_end') or ''
        if not (start and end):
            return
        token = _lead_value(lead_obj, 'token') or ''
        key = f"lead-closed:{token}:{date_str}:{start}-{end}"
        if _notification_was_sent(key):
            return
        _mark_notification_sent(key)
        client_amount, nanny_amount = _lead_day_amounts(lead_obj, date_str, start, end)
        amount_line = f"\nИтоговая сумма: {_fmt_vnd(client_amount)}" if client_amount is not None else ''
        _send_to_client(
            lead_obj,
            "✅ Рабочий день закрыт\n"
            f"Дата: {date_str}\n"
            f"Факт: {start}-{end}"
            f"{amount_line}",
            _client_buttons(lead_obj),
        )
        assigned_nanny_id = _lead_value(lead_obj, 'assigned_nanny_id')
        nanny_amount_line = f"\nНачисление няне: {_fmt_vnd(nanny_amount)}" if nanny_amount is not None else ''
        _send_to_nanny(
            assigned_nanny_id,
            "✅ Клиент подтвердил рабочий день\n"
            f"Клиент: {_lead_value(lead_obj, 'parent_name') or '—'}\n"
            f"Дата: {date_str}\n"
            f"Факт: {start}-{end}"
            f"{nanny_amount_line}",
            _nanny_buttons(assigned_nanny_id),
        )
        _notify_admins(
            "✅ День закрыт\n"
            f"Клиент: {_lead_value(lead_obj, 'parent_name') or '—'}\n"
            f"Дата: {date_str}\n"
            f"Факт: {start}-{end}\n"
            f"Клиент: {_fmt_vnd(client_amount or 0)}\n"
            f"Няня: {_fmt_vnd(nanny_amount or 0)}",
            _admin_lead_buttons(lead_obj),
        )

    def _lead_has_active_date(lead_obj, date_str: str) -> bool:
        slot = (_lead_value(lead_obj, 'work_dates') or {}).get(date_str)
        if not isinstance(slot, dict):
            return bool(slot is not None)
        return slot.get('status') != 'cancelled'

    def _notify_dayoff_conflicts(nanny_id, nanny_name: str, date_str: str, start: str | None, end: str | None, note: str | None):
        if not nanny_id:
            return
        if use_sql:
            leads_for_date = [
                row for row in Lead.query.filter_by(assigned_nanny_id=int(nanny_id)).all()
                if _lead_has_active_date(row, date_str)
            ]
        else:
            leads_for_date = [
                lead for lead in load_leads()
                if str(lead.get('assigned_nanny_id') or '') == str(nanny_id)
                and _lead_has_active_date(lead, date_str)
            ]
        if not leads_for_date:
            return
        time_text = (start or 'весь день') + (('-' + end) if end else '')
        clients_text = ', '.join([_lead_value(l, 'parent_name') or '—' for l in leads_for_date[:8]])
        _notify_admins(
            "⚠️ Выходной няни конфликтует с клиентами\n"
            f"Няня: {nanny_name or '—'}\n"
            f"Дата: {date_str}\n"
            f"Время: {time_text}\n"
            f"Клиенты: {clients_text}\n"
            f"Комментарий: {note or '—'}"
        )
        for lead_obj in leads_for_date:
            key = f"dayoff-conflict:{_lead_value(lead_obj, 'token') or ''}:{nanny_id}:{date_str}"
            if _notification_was_sent(key):
                continue
            _mark_notification_sent(key)
            _send_to_client(
                lead_obj,
                "⚠️ По вашей дате нужна проверка расписания\n"
                f"Дата: {date_str}\n"
                f"Няня отметила недоступность: {time_text}\n"
                "Администратор проверит ситуацию и при необходимости предложит замену.",
                _client_buttons(lead_obj),
            )

    def _date_plus_days(date_str: str | None, days: int) -> str | None:
        if not date_str:
            return None
        try:
            value = datetime.date.fromisoformat(date_str)
            return (value + datetime.timedelta(days=int(days or 0))).isoformat()
        except Exception:
            return None

    def _agent_client_payload(agent_obj) -> dict:
        agent = _agent_to_dict(agent_obj)
        agent_id = str(agent.get('id') or '')
        commission_vnd = int(agent.get('commission_vnd') or 200000)
        payout_delay_days = int(agent.get('payout_delay_days') or 14)
        rows = []
        events = []
        total_commission = 0
        clients_with_dates = 0

        for lead in load_leads():
            if str(lead.get('referral_agent_id') or '') != agent_id:
                continue
            work_dates = lead.get('work_dates') or {}
            active_dates = []
            for d, raw_slot in sorted(work_dates.items()):
                slot = raw_slot if isinstance(raw_slot, dict) else {}
                if slot.get('status') == 'cancelled':
                    continue
                active_dates.append((d, slot))

            first_date = active_dates[0][0] if active_dates else None
            first_slot = active_dates[0][1] if active_dates else {}
            finance = _lead_slot_finance(lead, first_date, first_slot) if first_date else {}
            margin = finance.get('margin_vnd')
            commission = commission_vnd if first_date else 0
            payout_date = _date_plus_days(first_date, payout_delay_days) if first_date else None
            total_commission += commission
            if first_date:
                clients_with_dates += 1

            for d, slot in active_dates:
                fin = _lead_slot_finance(lead, d, slot)
                events.append({
                    'date': d,
                    'type': 'work',
                    'client': lead.get('parent_name') or 'Клиент',
                    'child': lead.get('child_name') or '',
                    'time': slot.get('time') or '',
                    'first': d == first_date,
                    'client_total_vnd': fin.get('client_total_vnd'),
                    'margin_vnd': fin.get('margin_vnd'),
                })
            if payout_date:
                events.append({
                    'date': payout_date,
                    'type': 'payout',
                    'client': lead.get('parent_name') or 'Клиент',
                    'amount_vnd': commission,
                    'source_date': first_date,
                })

            rows.append({
                'token': lead.get('token'),
                'parent_name': lead.get('parent_name') or '—',
                'telegram': lead.get('telegram') or '—',
                'child_name': lead.get('child_name') or '',
                'child_age': lead.get('child_age') or '',
                'submitted_at': lead.get('submitted_at') or '',
                'dates_count': len(active_dates),
                'first_work_date': first_date,
                'first_work_time': first_slot.get('time') if first_slot else '',
                'payout_date': payout_date,
                'commission_vnd': commission,
                'client_total_vnd': finance.get('client_total_vnd'),
                'margin_vnd': margin,
            })

        rows.sort(key=lambda x: x.get('submitted_at') or '', reverse=True)
        events.sort(key=lambda x: x.get('date') or '')
        return {
            'clients': rows,
            'events': events,
            'summary': {
                'clients_total': len(rows),
                'clients_with_dates': clients_with_dates,
                'expected_commission_vnd': total_commission,
                'commission_vnd': commission_vnd,
                'payout_delay_days': payout_delay_days,
            },
        }

    @app.route('/r/<referral_code>')
    def referral_entry(referral_code: str):
        agent = _agent_by_referral_code(referral_code)
        if not agent:
            return render_template('404.html'), 404
        session.permanent = True
        session['referral_agent_code'] = _agent_value(agent, 'referral_code')
        session['referral_agent_id'] = _agent_id(agent)
        return redirect(f"/?ref={_agent_value(agent, 'referral_code')}#leadForm")

    _agent_register_rate: dict[str, list[float]] = {}

    @app.route('/agent/register', methods=['GET', 'POST'])
    def agent_register():
        if request.method == 'GET':
            return render_template('agent_register.html')

        ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
        now = time.time()
        hits = [t for t in _agent_register_rate.get(ip, []) if now - t < 3600]
        if len(hits) >= 3:
            return render_template('agent_register.html', error='Слишком много заявок. Попробуйте позже.'), 429
        hits.append(now)
        _agent_register_rate[ip] = hits

        name = _clean_user_text(request.form.get('name'), 120)
        telegram_raw = _clean_user_text(request.form.get('telegram'), 120)
        notes = _clean_user_text(request.form.get('notes'), 800)
        telegram_user_id = None
        verified_username = ''
        init_data = (request.form.get('tg_init_data') or '').strip()
        if init_data:
            bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
            if bot_token:
                try:
                    pairs = validate_webapp_init_data(init_data, bot_token)
                    user_raw = pairs.get('user') or ''
                    user_obj = json.loads(user_raw) if user_raw else {}
                    verified_id = int(user_obj.get('id') or 0)
                    if verified_id:
                        telegram_user_id = verified_id
                        telegram_raw = str(verified_id)
                        verified_username = _clean_user_text(user_obj.get('username'), 80)
                        tg_name = _clean_user_text(' '.join(filter(None, [
                            user_obj.get('first_name'),
                            user_obj.get('last_name'),
                        ])), 120)
                        if not name:
                            name = tg_name or (('@' + verified_username) if verified_username else '')
                except TelegramAuthError:
                    return render_template('agent_register.html', error='Не удалось подтвердить Telegram. Откройте ссылку заново через приложение.'), 403
                except Exception:
                    return render_template('agent_register.html', error='Не удалось прочитать Telegram данные. Откройте ссылку заново через приложение.'), 400

        if not name:
            return render_template('agent_register.html', error='Укажите имя или название партнера.'), 400

        if telegram_user_id is None and telegram_raw and re.fullmatch(r'-?\d{5,20}', telegram_raw.strip()):
            try:
                telegram_user_id = int(telegram_raw.strip())
            except Exception:
                telegram_user_id = None
        telegram_label = telegram_raw or ''
        if verified_username:
            telegram_label = f"{telegram_label} (@{verified_username})" if telegram_label else f"@{verified_username}"
        final_notes = '\n'.join(filter(None, [
            f"Telegram: {telegram_label}" if telegram_label else '',
            notes,
            "Самостоятельная регистрация партнера. Требуется проверка и активация админом.",
        ]))

        if use_sql:
            existing = ReferralAgent.query.filter_by(telegram_user_id=telegram_user_id).first() if telegram_user_id else None
            if existing:
                return render_template('agent_register.html', success=True, already=True)
            agent = ReferralAgent(
                name=name,
                telegram_user_id=telegram_user_id,
                portal_token=secrets.token_urlsafe(18),
                referral_code=_new_agent_code(name),
                commission_vnd=200000,
                payout_delay_days=14,
                notes=final_notes,
                is_active=False,
            )
            db.session.add(agent)
            db.session.commit()
            agent_for_notify = agent
        else:
            agents = load_agents()
            if telegram_user_id and any(str(a.get('telegram_user_id') or '') == str(telegram_user_id) for a in agents):
                return render_template('agent_register.html', success=True, already=True)
            next_id = str(max([int(a.get('id') or 0) for a in agents] or [0]) + 1)
            agent = {
                'id': next_id,
                'name': name,
                'telegram_user_id': telegram_user_id or '',
                'portal_token': secrets.token_urlsafe(18),
                'referral_code': _new_agent_code(name),
                'commission_vnd': 200000,
                'payout_delay_days': 14,
                'notes': final_notes,
                'is_active': False,
                'created_at': datetime.datetime.utcnow().isoformat(),
            }
            agents.append(agent)
            save_agents(agents)
            agent_for_notify = agent

        _notify_admins(
            "🤝 Новая заявка реферального агента\n"
            f"Партнер: {name}\n"
            f"Telegram: {telegram_label or '—'}\n"
            "Статус: ожидает проверки и активации в админке.",
            [[_url_button('Открыть админку', f"{_site_url()}/admin")]],
        )
        _send_to_agent(
            agent_for_notify,
            "Заявка партнера принята. Администратор проверит данные и активирует кабинет.",
        )
        return render_template('agent_register.html', success=True)

    @app.route('/agent')
    def agent_home():
        return redirect(url_for('agent_app'))

    @app.route('/agent/app')
    def agent_app():
        token = request.args.get('_t', '')
        if token:
            entry = _validate_auth_token(token)
            if entry and entry.get('role') in ('agent', 'admin'):
                session.permanent = True
                session['telegram_user_id'] = entry.get('telegram_user_id')
                session['role'] = entry.get('role')
        tid = session.get('telegram_user_id')
        if not tid:
            return redirect(url_for('tg_entry'))
        agent = _agent_by_telegram_id(tid)
        if not agent:
            if use_sql:
                nanny = Nanny.query.filter_by(telegram_user_id=int(tid)).first()
            else:
                nanny = next((n for n in load_nannies() if str(n.get('telegram_user_id') or '') == str(tid)), None)
            agent = _ensure_agent_for_nanny(nanny)
        if not agent:
            if session.get('role') == 'admin':
                return redirect(url_for('admin'))
            return render_template('404.html'), 404
        return redirect(url_for('agent_portal', portal_token=_agent_value(agent, 'portal_token')))

    @app.route('/agent/<portal_token>')
    def agent_portal(portal_token: str):
        agent = _agent_by_portal_token(portal_token)
        if not agent:
            return render_template('404.html'), 404
        session.permanent = True
        session['agent_portal_token'] = portal_token
        payload = _agent_client_payload(agent)
        return render_template(
            'agent_portal.html',
            agent=_agent_to_dict(agent),
            referral_url=_agent_referral_url(agent),
            portal_url=_agent_portal_url(agent),
            switch_portals=_portal_switch_options_for_session('agent'),
            clients=payload['clients'],
            events=payload['events'],
            summary=payload['summary'],
            articles=_articles_published()[:6],
        )

    # Simple in-memory rate limiter for /api/lead (max 5 per IP per 10 minutes)
    _lead_rate: dict = {}

    # Short-lived auth tokens for Telegram Mini App (cookie fallback)
    # token -> {role, telegram_user_id, expires}
    _auth_tokens: dict = {}

    def _make_auth_token(role: str, tg_id: int) -> str:
        token = secrets.token_urlsafe(24)
        _auth_tokens[token] = {
            'role': role,
            'telegram_user_id': tg_id,
            'expires': time.time() + 300,  # 5 min
        }
        return token

    # Register in app config so auth_simple can import it
    app.config["_validate_auth_token"] = lambda t: _validate_auth_token(t)

    def _validate_auth_token(token: str) -> dict | None:
        entry = _auth_tokens.get(token)
        if not entry:
            return None
        if time.time() > entry['expires']:
            del _auth_tokens[token]
            return None
        return entry

    @app.route('/api/lead', methods=['POST'])
    def api_lead():
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
        now = time.time()
        window = 600  # 10 minutes
        limit = 5
        hits = [t for t in _lead_rate.get(ip, []) if now - t < window]
        if len(hits) >= limit:
            return {'error': 'Слишком много заявок. Попробуйте через 10 минут.'}, 429
        hits.append(now)
        _lead_rate[ip] = hits

        data = request.get_json(force=True) or {}
        parent_name   = _clean_user_text(data.get('parent_name'), 100)
        telegram      = _clean_user_text(data.get('telegram'), 100)
        tg_init_data  = (data.get('tg_init_data') or '').strip()
        tg_user_id_raw = (data.get('tg_user_id') or '').strip()
        child_name    = _clean_user_text(data.get('child_name'), 100)
        child_age     = _clean_user_text(data.get('child_age'), 20)
        notes         = _clean_user_text(data.get('notes'), 1000)
        meeting_date_raw = data.get('meeting_date')
        work_dates_raw   = data.get('work_dates') or {}
        referral_code = _clean_user_text(data.get('referral_code'), 80) or session.get('referral_agent_code')

        # ── Verify Telegram identity ──────────────────────────
        verified_tg_user_id: int | None = None
        verified_tg_username: str | None = None
        verified_tg_name: str | None = None

        if tg_init_data:
            bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
            if bot_token:
                try:
                    pairs = validate_webapp_init_data(tg_init_data, bot_token)
                    user_obj = json.loads(pairs.get('user', '{}'))
                    verified_tg_user_id  = int(user_obj.get('id') or 0) or None
                    verified_tg_username = user_obj.get('username') or None
                    fn = (user_obj.get('first_name') or '').strip()
                    ln = (user_obj.get('last_name') or '').strip()
                    verified_tg_name = ' '.join(filter(None, [fn, ln])) or None
                    # Use TG name as parent_name if not provided
                    if not parent_name and verified_tg_name:
                        parent_name = _clean_user_text(verified_tg_name, 100)
                    # Set telegram field to @username or numeric id
                    if not telegram:
                        if verified_tg_username:
                            telegram = '@' + verified_tg_username
                        elif verified_tg_user_id:
                            telegram = str(verified_tg_user_id)
                except Exception as e:
                    app.logger.warning("TG initData verification failed in /api/lead: %s", e)
        elif tg_user_id_raw and re.fullmatch(r'\d{5,20}', tg_user_id_raw):
            # Unverified — store as-is (won't be used for security-sensitive ops)
            try:
                verified_tg_user_id = int(tg_user_id_raw)
            except Exception:
                pass

        # Require either telegram field OR verified TG id
        if not parent_name or not child_name or not child_age:
            return {'error': 'Заполните обязательные поля.'}, 400
        if not telegram and not verified_tg_user_id:
            return {'error': 'Укажите Telegram для связи или откройте сайт через бота.'}, 400

        # Validate meeting_date
        _d_re = r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
        _today_str = datetime.datetime.utcnow().date().isoformat()
        meeting_date = meeting_date_raw if (
            isinstance(meeting_date_raw, str) and re.match(_d_re, meeting_date_raw)
            and meeting_date_raw >= _today_str
        ) else None

        # Validate work_dates: YYYY-MM-DD keys only, max 60 entries, no past dates
        def _val_work_dates(raw):
            if not isinstance(raw, dict):
                return {}
            result = {}
            today_iso = datetime.datetime.utcnow().date().isoformat()
            for k, v in list(raw.items())[:60]:
                if isinstance(k, str) and re.match(_d_re, k):
                    if k < today_iso:
                        continue  # skip past dates
                    if isinstance(v, dict):
                        t = str(v.get('time') or '')[:20]
                        result[k] = {'time': t} if t else {}
                    else:
                        result[k] = {}
            return result

        work_dates = _val_work_dates(work_dates_raw)
        token = secrets.token_urlsafe(16)
        referral_agent = _agent_by_referral_code(referral_code)
        referral_agent_id = _agent_id(referral_agent)

        if use_sql:
            db.session.add(
                Lead(
                    token=token,
                    parent_name=parent_name,
                    telegram=telegram,
                    telegram_user_id=verified_tg_user_id,
                    child_name=child_name,
                    child_age=child_age,
                    notes=notes,
                    meeting_date=meeting_date,
                    work_dates=work_dates or {},
                    documents={'receipts': {}},
                    client_rate_per_hour=DEFAULT_CLIENT_RATE_VND,
                    nanny_rate_per_hour=DEFAULT_NANNY_RATE_VND,
                    referral_agent_id=int(referral_agent_id) if referral_agent_id else None,
                )
            )
            db.session.commit()
        else:
            lead = {
                'token': token,
                'parent_name': parent_name,
                'telegram': telegram,
                'telegram_user_id': verified_tg_user_id,
                'telegram_username': verified_tg_username,
                'child_name': child_name,
                'child_age': child_age,
                'notes': notes,
                'meeting_date': meeting_date,
                'work_dates': work_dates,
                'assigned_nanny_id': None,
                'client_rate_per_hour': DEFAULT_CLIENT_RATE_VND,
                'nanny_rate_per_hour': DEFAULT_NANNY_RATE_VND,
                'referral_agent_id': referral_agent_id,
                'submitted_at': datetime.datetime.utcnow().isoformat(),
                'documents': {'receipts': {}},
            }

            leads = load_leads()
            leads.insert(0, lead)
            save_leads(leads)

        lk_url = os.environ.get('SITE_URL', 'https://web-production-2ebe9.up.railway.app').rstrip('/') + '/client/' + token

        # ── Notify admins ─────────────────────────────────────
        tg_display = telegram or (f'id:{verified_tg_user_id}' if verified_tg_user_id else '—')
        _notify_admins(
            "🆕 Новая заявка\n"
            f"Родитель: {parent_name}\n"
            f"Telegram: {tg_display}\n"
            f"Ребёнок: {child_name}, {child_age}\n"
            f"Агент: {_agent_value(referral_agent, 'name', '—') if referral_agent else '—'}\n"
            f"ЛК: {lk_url}"
            ,
            [[_url_button('Открыть заявку', lk_url)]]
        )
        if referral_agent:
            _send_to_agent(
                referral_agent,
                "🆕 Новый клиент по вашей рекомендации\n"
                f"Клиент: {parent_name}\n"
                f"Ребёнок: {child_name}, {child_age}\n"
                "Клиент закреплён за вами. Первая рабочая дата появится в календаре после выбора/назначения.",
            )

        # ── Send LK link directly to client in Telegram ───────
        client_chat_id = verified_tg_user_id or _extract_chat_id(telegram)
        if client_chat_id:
            client_msg = (
                f"👋 {parent_name}, заявка принята!\n\n"
                f"Мы подбираем няню для {child_name}.\n"
                f"Ответим в течение 15 минут.\n\n"
                f"📋 Ваш личный кабинет:\n{lk_url}\n\n"
                f"Сохраните ссылку — в ней ваше расписание, смены и чеки."
            )
            _safe_send_message(
                client_chat_id,
                client_msg,
                [
                    [_url_button('Открыть кабинет', lk_url)],
                    [_url_button('Написать администратору', 'https://t.me/Nastasja_Ageyeva')],
                ],
            )

        return {'ok': True, 'lk_url': lk_url}

    @app.route('/client/<token>')
    def client_portal(token: str):
        leads = load_leads()
        lead = next((x for x in leads if x.get('token') == token), None)
        if not lead:
            return 'Ссылка недействительна', 404
        nannies = load_nannies()
        nanny = None
        nanny_dayoffs = []
        if lead.get('assigned_nanny_id'):
            nanny = next((n for n in nannies if n.get('id') == lead.get('assigned_nanny_id')), None)
            if use_sql and nanny:
                nanny_id = nanny.get('id') if isinstance(nanny, dict) else getattr(nanny, 'id', None)
                if nanny_id:
                    blocks = NannyBlock.query.filter_by(nanny_id=int(nanny_id), kind='dayoff').order_by(NannyBlock.date.asc()).all()
                    nanny_dayoffs = [{'id': b.id, 'date': b.date, 'start': b.start, 'end': b.end, 'note': b.note} for b in blocks]
            elif nanny:
                blocks = _read_json(NANNY_BLOCKS_FILE, [])
                nanny_dayoffs = [
                    {'id': b.get('id'), 'date': b.get('date'), 'start': b.get('start'), 'end': b.get('end'), 'note': b.get('note')}
                    for b in blocks
                    if str(b.get('nanny_id')) == str(nanny.get('id')) and b.get('kind', 'dayoff') == 'dayoff'
                ]
        return render_template(
            'client_portal.html',
            lead=lead,
            nanny=nanny,
            nanny_dayoffs=nanny_dayoffs,
            switch_portals=_portal_switch_options_for_session('client'),
        )

    @app.route('/api/client/<token>/link_tg', methods=['POST'])
    def api_client_link_tg(token: str):
        """
        Called from client_portal.html when opened inside Telegram Mini App.
        Verifies initData, saves telegram_user_id to the lead so:
        - Next /api/auth/telegram login finds the LK automatically
        - Notifications to client via bot will work
        """
        data = request.get_json(force=True) or {}
        tg_init_data = (data.get('init_data') or '').strip()
        if not tg_init_data:
            return {'error': 'init_data required'}, 400

        bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
        if not bot_token:
            return {'error': 'bot not configured'}, 500

        try:
            pairs = validate_webapp_init_data(tg_init_data, bot_token)
            user_obj = json.loads(pairs.get('user', '{}'))
            tg_user_id = int(user_obj.get('id') or 0)
            tg_username = (user_obj.get('username') or '').strip()
            tg_name = ' '.join(filter(None, [
                (user_obj.get('first_name') or '').strip(),
                (user_obj.get('last_name') or '').strip(),
            ])) or None
        except Exception as e:
            return {'error': f'invalid initData: {e}'}, 403

        if not tg_user_id:
            return {'error': 'no user id in initData'}, 400

        if use_sql:
            lead = Lead.query.filter_by(token=token).first()
            if not lead:
                return {'error': 'not found'}, 404
            changed = False
            if not lead.telegram_user_id:
                lead.telegram_user_id = tg_user_id
                changed = True
            # Also fill telegram field if empty
            if not lead.telegram and tg_username:
                lead.telegram = '@' + tg_username
                changed = True
            if changed:
                db.session.commit()
        else:
            leads = load_leads()
            lead = next((x for x in leads if x.get('token') == token), None)
            if not lead:
                return {'error': 'not found'}, 404
            changed = False
            if not lead.get('telegram_user_id'):
                lead['telegram_user_id'] = tg_user_id
                changed = True
            if not lead.get('telegram_username') and tg_username:
                lead['telegram_username'] = tg_username
                changed = True
            if not lead.get('telegram') and tg_username:
                lead['telegram'] = '@' + tg_username
                changed = True
            if changed:
                save_leads(leads)

        session.permanent = True
        session['telegram_user_id'] = tg_user_id
        session['telegram_username'] = tg_username
        session['telegram_display_name'] = tg_name or ''
        if not session.get('role'):
            session['role'] = 'client'

        return {
            'ok': True,
            'telegram_user_id': tg_user_id,
            'name': tg_name,
            'available_portals': _available_portals_for_telegram(tg_user_id, tg_username, attach_client=False),
        }

    @app.route('/api/client/<token>/update', methods=['POST'])
    def api_client_update(token: str):
        # The token itself is the auth credential (secret URL)
        data = request.get_json(force=True) or {}

        # Validate meeting_date
        _d_re = r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
        meeting_date_raw = data.get('meeting_date')
        meeting_date = meeting_date_raw if (
            isinstance(meeting_date_raw, str) and re.match(_d_re, meeting_date_raw)
        ) else None

        # Validate work_dates
        def _val_wd(raw):
            if not isinstance(raw, dict):
                return {}
            result = {}
            for k, v in list(raw.items())[:60]:
                if isinstance(k, str) and re.match(_d_re, k):
                    if isinstance(v, dict):
                        t = str(v.get('time') or '')[:20]
                        result[k] = {'time': t} if t else {}
                    else:
                        result[k] = {}
            return result

        work_dates = _val_wd(data.get('work_dates') or {})

        if use_sql:
            lead = Lead.query.filter_by(token=token).first()
            if not lead:
                return {'error': 'ЛК не найден'}, 404
            old_work_dates = dict(lead.work_dates or {})
            old_dates = set(old_work_dates.keys())
            new_dates = set(work_dates.keys())
            added = new_dates - old_dates
            changed_times = [
                d for d in sorted(new_dates & old_dates)
                if (old_work_dates.get(d) or {}).get('time') != (work_dates.get(d) or {}).get('time')
            ]
            lead.meeting_date = meeting_date
            lead.work_dates = work_dates
            db.session.commit()
            # Notify if new dates added AFTER nanny was already assigned
            if added and lead.assigned_nanny_id:
                # Mark newly added dates as pending when nanny is already assigned
                wd = lead.work_dates or {}
                for d in added:
                    slot = wd.get(d) or {}
                    slot['pending_admin'] = True
                    slot['pending_nanny'] = True
                    wd[d] = slot
                lead.work_dates = wd
                db.session.commit()
                _notify_on_new_dates(lead.parent_name, lead.child_name, sorted(added),
                                     lead.token, lead.assigned_nanny_id, lead.telegram_user_id)
            if changed_times and lead.assigned_nanny_id:
                dates_text = ', '.join(changed_times[:10])
                _notify_admins(
                    "🕒 Клиент изменил время работы\n"
                    f"Клиент: {lead.parent_name or '—'}\n"
                    f"Даты: {dates_text}",
                    _admin_lead_buttons(lead),
                )
                _send_to_nanny(
                    lead.assigned_nanny_id,
                    "🕒 Клиент изменил время по рабочему дню\n"
                    f"Клиент: {lead.parent_name or '—'}\n"
                    f"Даты: {dates_text}\n"
                    "Проверьте расписание в кабинете.",
                    _nanny_buttons(lead.assigned_nanny_id),
                )
            return {'ok': True}

        leads = load_leads()
        lead = next((x for x in leads if x.get('token') == token), None)
        if not lead:
            return {'error': 'ЛК не найден'}, 404
        old_work_dates = dict(lead.get('work_dates') or {})
        old_dates = set(old_work_dates.keys())
        new_dates = set(work_dates.keys())
        added = new_dates - old_dates
        changed_times = [
            d for d in sorted(new_dates & old_dates)
            if (old_work_dates.get(d) or {}).get('time') != (work_dates.get(d) or {}).get('time')
        ]
        lead['meeting_date'] = meeting_date
        lead['work_dates'] = work_dates
        if added and lead.get('assigned_nanny_id'):
            # Mark newly added dates as pending when nanny is already assigned
            wd = lead.get('work_dates') or {}
            for d in added:
                slot = wd.get(d) or {}
                slot['pending_admin'] = True
                slot['pending_nanny'] = True
                wd[d] = slot
            lead['work_dates'] = wd
        save_leads(leads)
        # Notify if new dates added AFTER nanny was already assigned
        if added and lead.get('assigned_nanny_id'):
            _notify_on_new_dates(lead.get('parent_name'), lead.get('child_name'),
                                 sorted(added), lead.get('token'),
                                 lead.get('assigned_nanny_id'), lead.get('telegram_user_id'))
        if changed_times and lead.get('assigned_nanny_id'):
            dates_text = ', '.join(changed_times[:10])
            _notify_admins(
                "🕒 Клиент изменил время работы\n"
                f"Клиент: {lead.get('parent_name') or '—'}\n"
                f"Даты: {dates_text}",
                _admin_lead_buttons(lead),
            )
            _send_to_nanny(
                lead.get('assigned_nanny_id'),
                "🕒 Клиент изменил время по рабочему дню\n"
                f"Клиент: {lead.get('parent_name') or '—'}\n"
                f"Даты: {dates_text}\n"
                "Проверьте расписание в кабинете.",
                _nanny_buttons(lead.get('assigned_nanny_id')),
            )
        return {'ok': True}

    @app.route('/api/client/<token>/upload_receipt', methods=['POST'])
    def api_client_upload_receipt(token: str):
        date_str = (request.args.get('date') or '').strip()
        if not date_str or len(date_str) != 10:
            return {'error': 'Не указана дата'}, 400
        file = request.files.get('file')
        if not file or not file.filename:
            return {'error': 'Файл не выбран'}, 400

        # MIME type validation — only images and PDF allowed
        ALLOWED_MIME_PREFIXES = ('image/jpeg', 'image/png', 'image/gif', 'image/webp', 'application/pdf')
        file_mime = file.mimetype or ''
        if not any(file_mime.startswith(m) for m in ALLOWED_MIME_PREFIXES):
            return {'error': f'Недопустимый тип файла: {file_mime}. Разрешены: изображения и PDF.'}, 400
        safe_name = secure_filename(file.filename)
        allowed_exts = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.pdf'}
        if os.path.splitext(safe_name.lower())[1] not in allowed_exts:
            return {'error': 'Недопустимое расширение файла. Разрешены изображения и PDF.'}, 400

        if use_sql:
            lead_row = Lead.query.filter_by(token=token).first()
            if not lead_row:
                return {'error': 'ЛК не найден'}, 404
            filename = _safe_upload_name(file, 'receipt')
            if not filename:
                return {'error': 'Недопустимое имя файла'}, 400
            file.save(os.path.join(app.config['UPLOAD_DIR'], filename))
            docs = dict(lead_row.documents or {})
            receipts = dict(docs.get('receipts') or {})
            date_receipts = list(receipts.get(date_str) or [])
            date_receipts.append(filename)
            receipts[date_str] = date_receipts
            docs['receipts'] = receipts
            lead_row.documents = docs
            db.session.commit()
            lead_for_notify = lead_row
        else:
            leads = load_leads()
            lead = next((x for x in leads if x.get('token') == token), None)
            if not lead:
                return {'error': 'ЛК не найден'}, 404
            filename = _safe_upload_name(file, 'receipt')
            if not filename:
                return {'error': 'Недопустимое имя файла'}, 400
            file.save(os.path.join(app.config['UPLOAD_DIR'], filename))
            lead.setdefault('documents', {}).setdefault('receipts', {}).setdefault(date_str, []).append(filename)
            save_leads(leads)
            lead_for_notify = lead

        _notify_admins(
            "📎 Клиент загрузил чек/документ\n"
            f"Клиент: {_lead_value(lead_for_notify, 'parent_name') or '—'}\n"
            f"Дата: {date_str}\n"
            f"Файл: {filename}\n"
            f"ЛК: {_site_url()}/client/{token}",
            _admin_lead_buttons(lead_for_notify),
        )
        return {'ok': True, 'filename': filename}

    @app.route('/api/client/<token>/receipts')
    def api_client_receipts(token: str):
        date_str = (request.args.get('date') or '').strip()
        if use_sql:
            lead_row = Lead.query.filter_by(token=token).first()
            if not lead_row:
                return {'error': 'ЛК не найден'}, 404
            items = (lead_row.documents or {}).get('receipts', {}).get(date_str, [])
            return {'items': items}

        leads = load_leads()
        lead = next((x for x in leads if x.get('token') == token), None)
        if not lead:
            return {'error': 'ЛК не найден'}, 404
        items = (lead.get('documents') or {}).get('receipts', {}).get(date_str, [])
        return {'items': items}

    @app.route('/api/client/<token>/receipt/<path:filename>')
    def api_client_receipt_file(token: str, filename: str):
        filename = (filename or '').replace('\\', '/').lstrip('/')
        if not filename or '..' in filename.split('/'):
            return {'error': 'invalid filename'}, 400
        if use_sql:
            lead_row = Lead.query.filter_by(token=token).first()
            if not lead_row:
                return {'error': 'ЛК не найден'}, 404
            receipts = (lead_row.documents or {}).get('receipts', {})
        else:
            lead = next((x for x in load_leads() if x.get('token') == token), None)
            if not lead:
                return {'error': 'ЛК не найден'}, 404
            receipts = (lead.get('documents') or {}).get('receipts', {})
        allowed = {str(item).replace('\\', '/').lstrip('/') for items in receipts.values() for item in (items or [])}
        if filename not in allowed:
            return {'error': 'receipt not found'}, 404
        resp = send_from_directory(app.config['UPLOAD_DIR'], filename)
        resp.headers.setdefault('Cache-Control', 'private, max-age=300')
        return resp


    @app.route('/api/client/<token>/cancel_date', methods=['POST'])
    def api_client_cancel_date(token: str):
        """Remove a single work date from client booking + notify admin."""
        data = request.get_json(force=True) or {}
        date_str = (data.get('date') or '').strip()
        _d_re = r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
        import re as _re
        if not date_str or not _re.match(_d_re, date_str):
            return {'error': 'Неверный формат даты'}, 400

        if use_sql:
            lead_row = Lead.query.filter_by(token=token).first()
            if not lead_row:
                return {'error': 'ЛК не найден'}, 404
            wd = dict(lead_row.work_dates or {})
            if date_str not in wd:
                return {'error': 'Дата не найдена'}, 404
            del wd[date_str]
            lead_row.work_dates = wd
            db.session.commit()
            parent_name = lead_row.parent_name
            tg = lead_row.telegram
            lead_for_notify = lead_row
            assigned_nanny_id = lead_row.assigned_nanny_id
        else:
            leads = load_leads()
            lead = next((x for x in leads if x.get('token') == token), None)
            if not lead:
                return {'error': 'ЛК не найден'}, 404
            wd = dict(lead.get('work_dates') or {})
            if date_str not in wd:
                return {'error': 'Дата не найдена'}, 404
            del wd[date_str]
            lead['work_dates'] = wd
            save_leads(leads)
            parent_name = lead.get('parent_name', '')
            tg = lead.get('telegram', '')
            lead_for_notify = lead
            assigned_nanny_id = lead.get('assigned_nanny_id')

        # Notify admins
        _notify_admins(
            "\u274c Клиент отменил дату\n"
            f"Клиент: {parent_name} ({tg})\n"
            f"Дата: {date_str}\n"
            f"ЛК: {os.environ.get('SITE_URL', 'https://web-production-2ebe9.up.railway.app').rstrip('/')}/client/{token}"
            ,
            _admin_lead_buttons(lead_for_notify),
        )
        _send_to_nanny(
            assigned_nanny_id,
            "❌ Клиент отменил рабочий день\n"
            f"Клиент: {parent_name or '—'}\n"
            f"Дата: {date_str}",
            _nanny_buttons(assigned_nanny_id),
        )
        _send_to_client(
            lead_for_notify,
            f"❌ Дата {date_str} отменена. Мы видим изменение в кабинете.",
            _client_buttons(lead_for_notify),
        )
        return {'ok': True}

    @app.route('/api/client/<token>/date_action', methods=['POST'])
    def api_client_date_action(token: str):
        """Client adds/edits a work date, comment, actual time, receipt note or review."""
        data = request.get_json(force=True) or {}
        date_str = (data.get('date') or '').strip()
        _d_re = r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
        if not date_str or not re.match(_d_re, date_str):
            return {'error': 'Неверный формат даты'}, 400

        def _valid_hhmm(value):
            if not value or not re.match(r'^[0-9]{2}:[0-9]{2}$', value):
                return False
            try:
                hh, mm = [int(x) for x in value.split(':')]
                return 0 <= hh <= 23 and 0 <= mm <= 59
            except Exception:
                return False

        time_start = (data.get('time_start') or '').strip()[:5]
        time_end = (data.get('time_end') or '').strip()[:5]
        if bool(time_start) != bool(time_end):
            return {'error': 'Выберите время начала и конца'}, 400
        if (time_start and not _valid_hhmm(time_start)) or (time_end and not _valid_hhmm(time_end)):
            return {'error': 'Неверный формат времени'}, 400
        if time_start and time_end and time_start == time_end:
            return {'error': 'Время начала и конца не должно совпадать'}, 400
        if time_start and time_end:
            start_mins = int(time_start[:2]) * 60 + int(time_start[3:5])
            end_mins = int(time_end[:2]) * 60 + int(time_end[3:5])
            if end_mins <= start_mins:
                return {'error': 'Время конца должно быть позже начала'}, 400
        time_value = (data.get('time') or '').strip().replace('–', '-').replace('—', '-')[:20]
        if not time_value and time_start and time_end:
            time_value = f"{time_start}-{time_end}"
        comment = _clean_user_text(data.get('comment'), 500)
        actual_start = (data.get('actual_start') or '').strip()[:5]
        actual_end = (data.get('actual_end') or '').strip()[:5]
        if bool(actual_start) != bool(actual_end):
            return {'error': 'Выберите фактическое время начала и конца'}, 400
        if (actual_start and not _valid_hhmm(actual_start)) or (actual_end and not _valid_hhmm(actual_end)):
            return {'error': 'Неверный формат фактического времени'}, 400
        if actual_start and actual_end and actual_start == actual_end:
            return {'error': 'Фактическое начало и конец не должны совпадать'}, 400
        if actual_start and actual_end:
            actual_start_mins = int(actual_start[:2]) * 60 + int(actual_start[3:5])
            actual_end_mins = int(actual_end[:2]) * 60 + int(actual_end[3:5])
            if actual_end_mins <= actual_start_mins:
                return {'error': 'Фактический конец должен быть позже начала'}, 400
        review_text = _clean_user_text(data.get('review'), 1000)
        try:
            review_stars = int(data.get('review_stars') or 5)
        except Exception:
            review_stars = 5
        review_stars = max(1, min(5, review_stars))
        today_str = datetime.datetime.utcnow().date().isoformat()

        if use_sql:
            lead_row = Lead.query.filter_by(token=token).first()
            if not lead_row:
                return {'error': 'ЛК не найден'}, 404
            wd = dict(lead_row.work_dates or {})
            is_new_date = date_str not in wd
            if is_new_date and not time_value:
                return {'error': 'Дата не найдена'}, 404
            if is_new_date and date_str < today_str:
                return {'error': 'Нельзя добавить прошедшую дату'}, 400
            if time_value and lead_row.assigned_nanny_id and NannyBlock.query.filter_by(
                nanny_id=int(lead_row.assigned_nanny_id), date=date_str, kind='dayoff'
            ).first():
                return {'error': 'У няни на эту дату отмечен выходной'}, 400
            current_slot = wd.get(date_str, {})
            slot = dict(current_slot) if isinstance(current_slot, dict) else {}
            if time_value:
                slot['time'] = time_value
                if is_new_date and lead_row.assigned_nanny_id:
                    slot['pending_admin'] = True
                    slot['pending_nanny'] = True
            if comment:
                slot['client_comment'] = comment
            if actual_start and actual_end:
                slot['client_actual_start'] = actual_start
                slot['client_actual_end'] = actual_end
                if slot.get('fact_start') and slot.get('fact_end'):
                    slot['status'] = 'confirmed' if (
                        slot.get('fact_start') == actual_start and slot.get('fact_end') == actual_end
                    ) else 'dispute'
            if review_text:
                slot['client_review'] = review_text
                slot['client_review_stars'] = review_stars
            wd[date_str] = slot
            lead_row.work_dates = wd
            db.session.commit()
            lead_for_notify = lead_row
            slot_for_notify = slot
            if is_new_date and lead_row.assigned_nanny_id:
                _notify_on_new_dates(lead_row.parent_name, lead_row.child_name, [date_str],
                                     lead_row.token, lead_row.assigned_nanny_id, lead_row.telegram_user_id)
        else:
            leads = load_leads()
            lead = next((x for x in leads if x.get('token') == token), None)
            if not lead:
                return {'error': 'ЛК не найден'}, 404
            wd = dict(lead.get('work_dates') or {})
            is_new_date = date_str not in wd
            if is_new_date and not time_value:
                return {'error': 'Дата не найдена'}, 404
            if is_new_date and date_str < today_str:
                return {'error': 'Нельзя добавить прошедшую дату'}, 400
            if time_value and lead.get('assigned_nanny_id'):
                blocks = _read_json(NANNY_BLOCKS_FILE, [])
                if any(
                    str(b.get('nanny_id')) == str(lead.get('assigned_nanny_id'))
                    and b.get('date') == date_str
                    and b.get('kind', 'dayoff') == 'dayoff'
                    for b in blocks
                ):
                    return {'error': 'У няни на эту дату отмечен выходной'}, 400
            current_slot = wd.get(date_str, {})
            slot = dict(current_slot) if isinstance(current_slot, dict) else {}
            if time_value:
                slot['time'] = time_value
                if is_new_date and lead.get('assigned_nanny_id'):
                    slot['pending_admin'] = True
                    slot['pending_nanny'] = True
            if comment:
                slot['client_comment'] = comment
            if actual_start and actual_end:
                slot['client_actual_start'] = actual_start
                slot['client_actual_end'] = actual_end
                if slot.get('fact_start') and slot.get('fact_end'):
                    slot['status'] = 'confirmed' if (
                        slot.get('fact_start') == actual_start and slot.get('fact_end') == actual_end
                    ) else 'dispute'
            if review_text:
                slot['client_review'] = review_text
                slot['client_review_stars'] = review_stars
            wd[date_str] = slot
            lead['work_dates'] = wd
            save_leads(leads)
            lead_for_notify = lead
            slot_for_notify = slot
            if is_new_date and lead.get('assigned_nanny_id'):
                _notify_on_new_dates(lead.get('parent_name'), lead.get('child_name'),
                                     [date_str], lead.get('token'),
                                     lead.get('assigned_nanny_id'), lead.get('telegram_user_id'))
        assigned_nanny_id = _lead_value(lead_for_notify, 'assigned_nanny_id')
        client_name = _lead_value(lead_for_notify, 'parent_name') or '—'
        client_link = f"{_site_url()}/client/{token}"
        if comment:
            _notify_sensitive_admin_comment('Клиент', lead_for_notify, date_str, comment)
            _notify_admins(
                "💬 Новый комментарий клиента\n"
                f"Клиент: {client_name}\n"
                f"Дата: {date_str}\n"
                f"Комментарий: {comment}\n"
                f"ЛК: {client_link}"
            )
            _send_to_nanny(
                assigned_nanny_id,
                f"💬 Клиент оставил комментарий к дате {_date_time_text(date_str, slot_for_notify)}.\n{client_link}"
            )
        if actual_start and actual_end:
            _notify_admins(
                "⏱ Клиент указал фактическое время\n"
                f"Клиент: {client_name}\n"
                f"Дата: {date_str}\n"
                f"Факт: {actual_start}-{actual_end}\n"
                f"ЛК: {client_link}"
            )
            _send_to_nanny(
                assigned_nanny_id,
                f"⏱ Клиент указал фактическое время за {date_str}: {actual_start}-{actual_end}.\nПроверьте день: {_nanny_portal_url_by_id(assigned_nanny_id)}"
            )
            if slot_for_notify.get('status') == 'confirmed':
                _notify_lead_day_closed(lead_for_notify, date_str, slot_for_notify)
            elif slot_for_notify.get('status') == 'dispute':
                _notify_admins(
                    "⚠️ Разница фактического времени\n"
                    f"Клиент: {client_name}\n"
                    f"Дата: {date_str}\n"
                    f"Няня: {slot_for_notify.get('fact_start')}-{slot_for_notify.get('fact_end')}\n"
                    f"Клиент: {actual_start}-{actual_end}",
                    _admin_lead_buttons(lead_for_notify),
                )
        if review_text:
            _notify_sensitive_admin_comment('Отзыв клиента', lead_for_notify, date_str, review_text)
            _notify_admins(
                "⭐ Клиент оставил оценку\n"
                f"Клиент: {client_name}\n"
                f"Дата: {date_str}\n"
                f"Оценка: {review_stars}/5\n"
                f"Отзыв: {review_text}\n"
                f"ЛК: {client_link}"
            )
            _send_to_nanny(
                assigned_nanny_id,
                f"⭐ Клиент оставил оценку за {date_str}: {review_stars}/5.\n{_nanny_portal_url_by_id(assigned_nanny_id)}"
            )
            if review_stars <= 3:
                _notify_admins(
                    "🚨 Низкая оценка клиента\n"
                    f"Клиент: {client_name}\n"
                    f"Дата: {date_str}\n"
                    f"Оценка: {review_stars}/5\n"
                    f"Отзыв: {review_text}",
                    _admin_lead_buttons(lead_for_notify),
                )
        return {'ok': True}

    @app.route('/api/client/<token>/add_dates', methods=['POST'])
    def api_client_add_dates(token: str):
        """Client adds extra work dates after nanny is assigned — needs admin+nanny confirmation."""
        data = request.get_json(force=True) or {}
        new_dates = data.get('dates') or []
        _d_re = r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
        today_str = datetime.datetime.utcnow().date().isoformat()
        valid_dates = [d for d in new_dates
                       if isinstance(d, str) and re.match(_d_re, d) and d >= today_str][:30]
        if not valid_dates:
            return {'error': 'Нет корректных дат'}, 400

        if use_sql:
            lead_row = Lead.query.filter_by(token=token).first()
            if not lead_row:
                return {'error': 'ЛК не найден'}, 404
            wd = dict(lead_row.work_dates or {})
            truly_new = []
            for d in valid_dates:
                if d not in wd:
                    wd[d] = {'pending_admin': True, 'pending_nanny': True}
                    truly_new.append(d)
            lead_row.work_dates = wd
            db.session.commit()
            if truly_new and lead_row.assigned_nanny_id:
                _notify_on_new_dates(lead_row.parent_name, lead_row.child_name, sorted(truly_new),
                                     lead_row.token, lead_row.assigned_nanny_id, lead_row.telegram_user_id)
            return {'ok': True, 'added': truly_new}
        else:
            leads = load_leads()
            lead = next((x for x in leads if x.get('token') == token), None)
            if not lead:
                return {'error': 'ЛК не найден'}, 404
            wd = dict(lead.get('work_dates') or {})
            truly_new = []
            for d in valid_dates:
                if d not in wd:
                    wd[d] = {'pending_admin': True, 'pending_nanny': True}
                    truly_new.append(d)
            lead['work_dates'] = wd
            save_leads(leads)
            if truly_new and lead.get('assigned_nanny_id'):
                _notify_on_new_dates(lead.get('parent_name'), lead.get('child_name'),
                                     sorted(truly_new), lead.get('token'),
                                     lead.get('assigned_nanny_id'), lead.get('telegram_user_id'))
            return {'ok': True, 'added': truly_new}

    @app.route('/api/admin/lead/<token>/confirm_date', methods=['POST'])
    @require_admin
    def api_admin_confirm_date(token: str):
        """Admin confirms or rejects a pending work date."""
        data = request.get_json(force=True) or {}
        date_str = (data.get('date') or '').strip()
        action = (data.get('action') or 'confirm').strip()  # confirm | reject

        if use_sql:
            lead_row = Lead.query.filter_by(token=token).first()
            if not lead_row:
                return {'error': 'ЛК не найден'}, 404
            wd = dict(lead_row.work_dates or {})
            if date_str not in wd:
                return {'error': 'Дата не найдена'}, 404
            slot = dict(wd[date_str]) if isinstance(wd[date_str], dict) else {}
            if action == 'confirm':
                slot.pop('pending_admin', None)
                if not slot.get('pending_nanny'):
                    slot['status'] = 'confirmed'
            else:
                slot['status'] = 'cancelled'
                slot.pop('pending_admin', None)
                slot.pop('pending_nanny', None)
            wd[date_str] = slot
            lead_row.work_dates = wd
            db.session.commit()
            lead_for_notify = lead_row
            slot_for_notify = slot
        else:
            leads = load_leads()
            lead = next((x for x in leads if x.get('token') == token), None)
            if not lead:
                return {'error': 'ЛК не найден'}, 404
            wd = dict(lead.get('work_dates') or {})
            if date_str not in wd:
                return {'error': 'Дата не найдена'}, 404
            slot = dict(wd[date_str]) if isinstance(wd[date_str], dict) else {}
            if action == 'confirm':
                slot.pop('pending_admin', None)
                if not slot.get('pending_nanny'):
                    slot['status'] = 'confirmed'
            else:
                slot['status'] = 'cancelled'
                slot.pop('pending_admin', None)
                slot.pop('pending_nanny', None)
            wd[date_str] = slot
            lead['work_dates'] = wd
            save_leads(leads)
            lead_for_notify = lead
            slot_for_notify = slot
        assigned_nanny_id = _lead_value(lead_for_notify, 'assigned_nanny_id')
        if action == 'confirm':
            _send_to_nanny(
                assigned_nanny_id,
                "✅ Администратор подтвердил рабочий день\n"
                f"Клиент: {_lead_value(lead_for_notify, 'parent_name') or '—'}\n"
                f"Дата: {_date_time_text(date_str, slot_for_notify)}\n"
                f"Кабинет: {_nanny_portal_url_by_id(assigned_nanny_id)}",
                _nanny_buttons(assigned_nanny_id),
            )
            if slot_for_notify.get('status') == 'confirmed':
                _send_to_client(
                    lead_for_notify,
                    "✅ Рабочий день подтверждён\n"
                    f"Дата: {_date_time_text(date_str, slot_for_notify)}\n"
                    f"Кабинет: {_site_url()}/client/{token}",
                    _client_buttons(lead_for_notify),
                )
        else:
            _send_to_nanny(
                assigned_nanny_id,
                f"❌ Администратор отклонил рабочий день {date_str}.\n{_nanny_portal_url_by_id(assigned_nanny_id)}",
                _nanny_buttons(assigned_nanny_id),
            )
            _send_to_client(
                lead_for_notify,
                f"❌ Дата {date_str} отклонена администратором. Проверьте личный кабинет: {_site_url()}/client/{token}",
                _client_buttons(lead_for_notify),
            )
        return {'ok': True}

    @app.route('/api/admin/lead/<token>/resolve_fact', methods=['POST'])
    @require_admin
    def api_admin_resolve_fact(token: str):
        """Admin confirms or disputes nanny's submitted actual time."""
        data = request.get_json(force=True) or {}
        date_str = (data.get('date') or '').strip()
        action = (data.get('action') or 'confirm').strip()  # confirm | reject

        if use_sql:
            lead_row = Lead.query.filter_by(token=token).first()
            if not lead_row:
                return {'error': 'ЛК не найден'}, 404
            wd = dict(lead_row.work_dates or {})
            if date_str not in wd:
                return {'error': 'Дата не найдена'}, 404
            slot = dict(wd[date_str]) if isinstance(wd[date_str], dict) else {}
            if action == 'confirm':
                slot['status'] = 'confirmed'
            else:
                # Reject: clear fact times, revert to assigned
                slot.pop('fact_start', None)
                slot.pop('fact_end', None)
                slot.pop('status', None)
            wd[date_str] = slot
            lead_row.work_dates = wd
            db.session.commit()
            lead_for_notify = lead_row
            slot_for_notify = slot
        else:
            leads = load_leads()
            lead = next((x for x in leads if x.get('token') == token), None)
            if not lead:
                return {'error': 'ЛК не найден'}, 404
            wd = dict(lead.get('work_dates') or {})
            if date_str not in wd:
                return {'error': 'Дата не найдена'}, 404
            slot = dict(wd[date_str]) if isinstance(wd[date_str], dict) else {}
            if action == 'confirm':
                slot['status'] = 'confirmed'
            else:
                slot.pop('fact_start', None)
                slot.pop('fact_end', None)
                slot.pop('status', None)
            wd[date_str] = slot
            lead['work_dates'] = wd
            save_leads(leads)
            lead_for_notify = lead
            slot_for_notify = slot
        assigned_nanny_id = _lead_value(lead_for_notify, 'assigned_nanny_id')
        if action == 'confirm':
            _notify_lead_day_closed(lead_for_notify, date_str, slot_for_notify)
        else:
            _send_to_nanny(
                assigned_nanny_id,
                "⚠️ Администратор вернул фактическое время на исправление\n"
                f"Клиент: {_lead_value(lead_for_notify, 'parent_name') or '—'}\n"
                f"Дата: {date_str}",
                _nanny_buttons(assigned_nanny_id),
            )
            _send_to_client(
                lead_for_notify,
                f"⚠️ Фактическое время за {date_str} отправлено на уточнение. Администратор проверит детали.",
                _client_buttons(lead_for_notify),
            )
        return {'ok': True}

    @app.route('/api/admin/lead/<token>/rates', methods=['POST'])
    @require_admin
    def api_admin_lead_rates(token: str):
        """Update client/nanny hourly rates used by lead calculators."""
        data = request.get_json(force=True, silent=True) or {}
        try:
            client_rate = int(data.get('client_rate_per_hour') or 0)
            nanny_rate = int(data.get('nanny_rate_per_hour') or 0)
        except Exception:
            return {'error': 'Ставки должны быть числами'}, 400
        if client_rate <= 0 or nanny_rate <= 0:
            return {'error': 'Ставки должны быть больше нуля'}, 400

        if use_sql:
            lead_row = Lead.query.filter_by(token=token).first()
            if not lead_row:
                return {'error': 'ЛК не найден'}, 404
            lead_row.client_rate_per_hour = client_rate
            lead_row.nanny_rate_per_hour = nanny_rate
            db.session.commit()
            lead_for_notify = lead_row
        else:
            leads = load_leads()
            lead = next((x for x in leads if x.get('token') == token), None)
            if not lead:
                return {'error': 'ЛК не найден'}, 404
            lead['client_rate_per_hour'] = client_rate
            lead['nanny_rate_per_hour'] = nanny_rate
            save_leads(leads)
            lead_for_notify = lead
        _send_to_client(
            lead_for_notify,
            "💰 Условия оплаты обновлены\n"
            f"Ставка клиента: {_fmt_vnd(client_rate)} / час\n"
            "Проверьте расчёт в личном кабинете.",
            _client_buttons(lead_for_notify),
        )
        assigned_nanny_id = _lead_value(lead_for_notify, 'assigned_nanny_id')
        _send_to_nanny(
            assigned_nanny_id,
            "💰 Ставка няни обновлена\n"
            f"Ставка: {_fmt_vnd(nanny_rate)} / час",
            _nanny_buttons(assigned_nanny_id),
        )
        _notify_admins(
            "💰 Ставки обновлены\n"
            f"Клиент: {_lead_value(lead_for_notify, 'parent_name') or '—'}\n"
            f"Клиентская ставка: {_fmt_vnd(client_rate)} / час\n"
            f"Ставка няни: {_fmt_vnd(nanny_rate)} / час",
            _admin_lead_buttons(lead_for_notify),
        )
        return {'ok': True}

    @app.route('/api/nanny/<portal_token>/confirm_date', methods=['POST'])
    def api_nanny_confirm_date(portal_token: str):
        """Nanny confirms or rejects a pending work date."""
        session_token = session.get('nanny_portal_token') or ''
        if session_token != portal_token:
            return {'error': 'Forbidden'}, 403
        data = request.get_json(force=True) or {}
        client_token = (data.get('client_token') or '').strip()
        date_str = (data.get('date') or '').strip()
        action = (data.get('action') or 'confirm').strip()  # confirm | reject

        if use_sql:
            nanny_row = Nanny.query.filter_by(portal_token=portal_token).first()
            if not nanny_row:
                return {'error': 'Няня не найдена'}, 404
            lead_row = Lead.query.filter_by(token=client_token).first()
            if not lead_row:
                return {'error': 'ЛК не найден'}, 404
            if str(lead_row.assigned_nanny_id or '') != str(nanny_row.id):
                return {'error': 'Клиент не назначен этой няне'}, 403
            wd = dict(lead_row.work_dates or {})
            if date_str not in wd:
                return {'error': 'Дата не найдена'}, 404
            slot = dict(wd[date_str]) if isinstance(wd[date_str], dict) else {}
            if action == 'confirm':
                slot.pop('pending_nanny', None)
                if not slot.get('pending_admin'):
                    slot['status'] = 'confirmed'
            else:
                slot['status'] = 'cancelled'
                slot.pop('pending_admin', None)
                slot.pop('pending_nanny', None)
            wd[date_str] = slot
            lead_row.work_dates = wd
            db.session.commit()
            lead_for_notify = lead_row
            slot_for_notify = slot
            nanny_name = nanny_row.name
        else:
            nanny_obj = next((n for n in load_nannies() if n.get('portal_token') == portal_token), None)
            if not nanny_obj:
                return {'error': 'Няня не найдена'}, 404
            leads = load_leads()
            lead = next((x for x in leads if x.get('token') == client_token), None)
            if not lead:
                return {'error': 'ЛК не найден'}, 404
            if str(lead.get('assigned_nanny_id') or '') != str(nanny_obj.get('id')):
                return {'error': 'Клиент не назначен этой няне'}, 403
            wd = dict(lead.get('work_dates') or {})
            if date_str not in wd:
                return {'error': 'Дата не найдена'}, 404
            slot = dict(wd[date_str]) if isinstance(wd[date_str], dict) else {}
            if action == 'confirm':
                slot.pop('pending_nanny', None)
                if not slot.get('pending_admin'):
                    slot['status'] = 'confirmed'
            else:
                slot['status'] = 'cancelled'
                slot.pop('pending_admin', None)
                slot.pop('pending_nanny', None)
            wd[date_str] = slot
            lead['work_dates'] = wd
            save_leads(leads)
            lead_for_notify = lead
            slot_for_notify = slot
            nanny_name = nanny_obj.get('name') or 'Няня'
        if action == 'confirm':
            if slot_for_notify.get('status') == 'confirmed':
                _send_to_client(
                    lead_for_notify,
                    "✅ Няня подтвердила рабочий день\n"
                    f"Няня: {nanny_name or '—'}\n"
                    f"Дата: {_date_time_text(date_str, slot_for_notify)}\n"
                    f"Кабинет: {_site_url()}/client/{client_token}",
                    _client_buttons(lead_for_notify),
                )
            _notify_admins(
                "✅ Няня подтвердила рабочий день\n"
                f"Няня: {nanny_name or '—'}\n"
                f"Клиент: {_lead_value(lead_for_notify, 'parent_name') or '—'}\n"
                f"Дата: {_date_time_text(date_str, slot_for_notify)}",
                _admin_lead_buttons(lead_for_notify),
            )
        else:
            _send_to_client(
                lead_for_notify,
                f"❌ Няня отклонила дату {date_str}. Проверьте личный кабинет: {_site_url()}/client/{client_token}",
                _client_buttons(lead_for_notify),
            )
            _notify_admins(
                "❌ Няня отклонила рабочий день\n"
                f"Няня: {nanny_name or '—'}\n"
                f"Клиент: {_lead_value(lead_for_notify, 'parent_name') or '—'}\n"
                f"Дата: {date_str}",
                _admin_lead_buttons(lead_for_notify),
            )
        return {'ok': True}

    @app.route('/api/nanny/<portal_token>/date_action', methods=['POST'])
    def api_nanny_date_action(portal_token: str):
        """Nanny submits comment or actual time for a work date."""
        session_token = session.get('nanny_portal_token') or ''
        if session_token != portal_token:
            return {'error': 'Forbidden'}, 403
        data = request.get_json(force=True) or {}
        client_token = (data.get('client_token') or '').strip()
        date_str = (data.get('date') or '').strip()
        _d_re = r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
        if not date_str or not re.match(_d_re, date_str):
            return {'error': 'Неверный формат даты'}, 400

        comment = _clean_user_text(data.get('comment'), 500)
        fact_start = (data.get('fact_start') or data.get('actual_start') or '').strip()[:5]
        fact_end = (data.get('fact_end') or data.get('actual_end') or '').strip()[:5]
        def _valid_hhmm(value):
            if not value or not re.match(r'^[0-9]{2}:[0-9]{2}$', value):
                return False
            try:
                hh, mm = [int(x) for x in value.split(':')]
                return 0 <= hh <= 23 and 0 <= mm <= 59
            except Exception:
                return False
        if bool(fact_start) != bool(fact_end):
            return {'error': 'Выберите фактическое время начала и конца'}, 400
        if (fact_start and not _valid_hhmm(fact_start)) or (fact_end and not _valid_hhmm(fact_end)):
            return {'error': 'Неверный формат фактического времени'}, 400
        if fact_start and fact_end and fact_start == fact_end:
            return {'error': 'Фактическое начало и конец не должны совпадать'}, 400
        if fact_start and fact_end:
            fact_start_mins = int(fact_start[:2]) * 60 + int(fact_start[3:5])
            fact_end_mins = int(fact_end[:2]) * 60 + int(fact_end[3:5])
            if fact_end_mins <= fact_start_mins:
                return {'error': 'Фактический конец должен быть позже начала'}, 400
        fact_submitted = bool(fact_start and fact_end)

        if use_sql:
            nanny_row = Nanny.query.filter_by(portal_token=portal_token).first()
            if not nanny_row:
                return {'error': 'Няня не найдена'}, 404
            lead_row = Lead.query.filter_by(token=client_token).first()
            if not lead_row:
                return {'error': 'ЛК не найден'}, 404
            if str(lead_row.assigned_nanny_id or '') != str(nanny_row.id):
                return {'error': 'Клиент не назначен этой няне'}, 403
            wd = dict(lead_row.work_dates or {})
            if date_str not in wd:
                return {'error': 'Дата не найдена'}, 404
            slot = dict(wd[date_str]) if isinstance(wd[date_str], dict) else {}
            if comment:
                slot['nanny_comment'] = comment
            if fact_submitted:
                slot['fact_start'] = fact_start
                slot['fact_end'] = fact_end
                slot['status'] = 'waiting_fact'
            wd[date_str] = slot
            lead_row.work_dates = wd
            db.session.commit()
            parent_name = lead_row.parent_name
            lead_for_notify = lead_row
            nanny_name = nanny_row.name
        else:
            nanny_obj = next((n for n in load_nannies() if n.get('portal_token') == portal_token), None)
            if not nanny_obj:
                return {'error': 'Няня не найдена'}, 404
            leads = load_leads()
            lead = next((x for x in leads if x.get('token') == client_token), None)
            if not lead:
                return {'error': 'ЛК не найден'}, 404
            if str(lead.get('assigned_nanny_id') or '') != str(nanny_obj.get('id')):
                return {'error': 'Клиент не назначен этой няне'}, 403
            wd = dict(lead.get('work_dates') or {})
            if date_str not in wd:
                return {'error': 'Дата не найдена'}, 404
            slot = dict(wd[date_str]) if isinstance(wd[date_str], dict) else {}
            if comment:
                slot['nanny_comment'] = comment
            if fact_submitted:
                slot['fact_start'] = fact_start
                slot['fact_end'] = fact_end
                slot['status'] = 'waiting_fact'
            wd[date_str] = slot
            lead['work_dates'] = wd
            save_leads(leads)
            parent_name = lead.get('parent_name', '—')
            lead_for_notify = lead
            nanny_name = nanny_obj.get('name') or 'Няня'
        if comment:
            _notify_sensitive_admin_comment('Няня', lead_for_notify, date_str, comment)
            _notify_admins(
                "💬 Новый комментарий няни\n"
                f"Няня: {nanny_name or '—'}\n"
                f"Клиент: {parent_name or '—'}\n"
                f"Дата: {date_str}\n"
                f"Комментарий: {comment}\n"
                f"ЛК: {_site_url()}/client/{client_token}",
                _admin_lead_buttons(lead_for_notify),
            )
            _send_to_client(
                lead_for_notify,
                f"💬 Няня оставила комментарий к дате {date_str}.\nПроверьте личный кабинет: {_site_url()}/client/{client_token}",
                _client_buttons(lead_for_notify),
            )
        if fact_submitted:
            _notify_admins(
                f"⏱ Няня отправила фактическое время\n"
                f"Клиент: {parent_name or '—'}\n"
                f"Дата: {date_str}\n"
                f"Факт: {fact_start}–{fact_end}\n"
                f"ЛК: {os.environ.get('SITE_URL','https://web-production-2ebe9.up.railway.app').rstrip('/')}/client/{client_token}",
                _admin_lead_buttons(lead_for_notify),
            )
            _send_to_client(
                lead_for_notify,
                "⏱ Няня отправила фактическое время работы\n"
                f"Дата: {date_str}\n"
                f"Факт: {fact_start}-{fact_end}\n"
                f"Проверьте и подтвердите в личном кабинете: {_site_url()}/client/{client_token}",
                _client_buttons(lead_for_notify),
            )
        return {'ok': True}

    @app.route('/nanny/portal/<portal_token>')
    def nanny_portal(portal_token: str):
        nannies = load_nannies()
        nanny = next((n for n in nannies if n.get('portal_token') == portal_token), None)
        if not nanny:
            return 'Ссылка недействительна', 404
        # Set session so blocks API can verify identity
        session['nanny_portal_token'] = portal_token
        session.permanent = True
        partner_agent = _ensure_agent_for_nanny(nanny)
        partner_portal_url = _agent_portal_url(partner_agent) if partner_agent else ''
        leads = load_leads()
        # Clients assigned to this nanny
        clients = [l for l in leads if l.get('assigned_nanny_id') == nanny.get('id')]
        # Build event list with nanny_rate so JS can compute earnings per shift
        events_with_rate = []
        for l in clients:
            rate = l.get('nanny_rate_per_hour') or DEFAULT_NANNY_RATE_VND
            receipts = (l.get('documents') or {}).get('receipts') or {}
            for d, info in (l.get('work_dates') or {}).items():
                slot = info if isinstance(info, dict) else {}
                date_status = slot.get('status')  # confirmed/cancelled/waiting_fact/None
                events_with_rate.append({
                    'date': d,
                    'child_name': l.get('child_name'),
                    'child_age': l.get('child_age'),
                    'parent_name': l.get('parent_name'),
                    'client_token': l.get('token'),
                    'time': slot.get('time'),
                    'nanny_rate': rate,
                    'status': date_status,
                    'fact_start': slot.get('fact_start'),
                    'fact_end': slot.get('fact_end'),
                    'has_receipt': bool(receipts.get(d)),
                    'pending_admin': bool(slot.get('pending_admin')),
                    'pending_nanny': bool(slot.get('pending_nanny')),
                    'client_comment': slot.get('client_comment', ''),
                    'nanny_comment': slot.get('nanny_comment', ''),
                    'client_review': slot.get('client_review', ''),
                    'client_review_stars': slot.get('client_review_stars', 0),
                })
        events_with_rate.sort(key=lambda x: x.get('date') or '')
        today = datetime.datetime.utcnow().date().isoformat()
        nanny_dayoffs_list = []
        if use_sql:
            blocks = (
                NannyBlock.query
                .filter_by(nanny_id=int(nanny.get('id')), kind='dayoff')
                .order_by(NannyBlock.date.asc(), NannyBlock.start.asc())
                .all()
            )
            nanny_dayoffs_list = [{
                'id': b.id,
                'date': b.date,
                'start': b.start,
                'end': b.end,
                'note': b.note,
            } for b in blocks]
        else:
            blocks = _read_json(NANNY_BLOCKS_FILE, [])
            nanny_dayoffs_list = [
                {
                    'id': b.get('id'),
                    'date': b.get('date'),
                    'start': b.get('start'),
                    'end': b.get('end'),
                    'note': b.get('note'),
                }
                for b in blocks
                if str(b.get('nanny_id')) == str(nanny.get('id')) and b.get('kind', 'dayoff') == 'dayoff'
            ]
        return render_template('nanny_portal_public.html', nanny=nanny, clients=clients,
                               events=events_with_rate, today=today,
                               default_nanny_rate=DEFAULT_NANNY_RATE_VND,
                               nanny_dayoffs=nanny_dayoffs_list,
                               partner_portal_url=partner_portal_url)

    @app.route('/nanny/<portal_token>')
    def nanny_profile(portal_token: str):
        """Public nanny profile (opens from the main page cards)."""
        nannies = load_nannies()
        nanny = next((n for n in nannies if n.get('portal_token') == portal_token), None)
        if not nanny:
            return 'Няня не найдена', 404
        load_reviews()
        ensure_nanny_profile_reviews(nannies)
        all_reviews = load_reviews()
        # Pinned first, then newest first
        nanny_reviews = [r for r in all_reviews if r.get('nanny_id') == portal_token]
        nanny_reviews.sort(key=lambda r: not bool(r.get('pinned')))
        return render_template('nanny_profile.html', nanny=nanny, nanny_reviews=nanny_reviews)

    @app.route('/api/nanny/<portal_token>/upload_receipt', methods=['POST'])
    def api_nanny_upload_receipt(portal_token: str):
        # Nanny attaches a receipt to a client/date
        # Require session auth — nanny must have visited /nanny/portal/<portal_token>
        session_token = session.get('nanny_portal_token') or ''
        if session_token != portal_token:
            return {'error': 'Forbidden'}, 403
        nannies = load_nannies()
        nanny = next((n for n in nannies if n.get('portal_token') == portal_token), None)
        if not nanny:
            return {'error': 'Няня не найдена'}, 404

        client_token = (request.form.get('client_token') or '').strip()
        date_str = (request.form.get('date') or '').strip()
        comment = _clean_user_text(request.form.get('comment'), 500)
        file = request.files.get('file')
        if not client_token or not date_str or not file or not file.filename:
            return {'error': 'Заполните client_token, date и выберите файл'}, 400

        # MIME type validation — only images and PDF allowed
        ALLOWED_MIME_PREFIXES = ('image/jpeg', 'image/png', 'image/gif', 'image/webp', 'application/pdf')
        file_mime = file.mimetype or ''
        if not any(file_mime.startswith(m) for m in ALLOWED_MIME_PREFIXES):
            return {'error': f'Недопустимый тип файла: {file_mime}. Разрешены: изображения и PDF.'}, 400
        safe_name = secure_filename(file.filename)
        allowed_exts = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.pdf'}
        if os.path.splitext(safe_name.lower())[1] not in allowed_exts:
            return {'error': 'Недопустимое расширение файла. Разрешены изображения и PDF.'}, 400

        if use_sql:
            lead_row = Lead.query.filter_by(token=client_token).first()
            if not lead_row:
                return {'error': 'Клиент не найден (token неверный)'}, 404
            if str(lead_row.assigned_nanny_id or '') != str(nanny.get('id')):
                return {'error': 'Клиент не назначен этой няне'}, 403
            filename = _safe_upload_name(file, 'receipt')
            if not filename:
                return {'error': 'Недопустимое имя файла'}, 400
            file.save(os.path.join(app.config['UPLOAD_DIR'], filename))
            docs = dict(lead_row.documents or {})
            receipts = dict(docs.get('receipts') or {})
            date_receipts = list(receipts.get(date_str) or [])
            date_receipts.append(filename)
            receipts[date_str] = date_receipts
            receipt_meta = dict(docs.get('receipt_meta') or {})
            date_meta = dict(receipt_meta.get(date_str) or {})
            date_meta[filename] = {
                'comment': comment,
                'uploaded_at': datetime.datetime.utcnow().isoformat(),
                'nanny_id': nanny.get('id'),
            }
            receipt_meta[date_str] = date_meta
            docs['receipts'] = receipts
            docs['receipt_meta'] = receipt_meta
            lead_row.documents = docs
            db.session.commit()
            lead_for_notify = lead_row
        else:
            leads = load_leads()
            lead = next((x for x in leads if x.get('token') == client_token), None)
            if not lead:
                return {'error': 'Клиент не найден (token неверный)'}, 404
            if str(lead.get('assigned_nanny_id') or '') != str(nanny.get('id')):
                return {'error': 'Клиент не назначен этой няне'}, 403
            filename = _safe_upload_name(file, 'receipt')
            if not filename:
                return {'error': 'Недопустимое имя файла'}, 400
            file.save(os.path.join(app.config['UPLOAD_DIR'], filename))
            lead.setdefault('documents', {}).setdefault('receipts', {}).setdefault(date_str, []).append(filename)
            # Optional metadata (comment) without breaking existing receipt list
            lead.setdefault('documents', {}).setdefault('receipt_meta', {}).setdefault(date_str, {})[filename] = {
                'comment': comment,
                'uploaded_at': datetime.datetime.utcnow().isoformat(),
                'nanny_id': nanny.get('id'),
            }
            save_leads(leads)
            lead_for_notify = lead
        _notify_admins(
            "📎 Няня загрузила чек/документ\n"
            f"Няня: {nanny.get('name') or '—'}\n"
            f"Клиент: {_lead_value(lead_for_notify, 'parent_name') or '—'}\n"
            f"Дата: {date_str}\n"
            f"Файл: {filename}\n"
            f"ЛК: {_site_url()}/client/{client_token}",
            _admin_lead_buttons(lead_for_notify),
        )
        _send_to_client(
            lead_for_notify,
            "📎 Няня загрузила чек/документ\n"
            f"Дата: {date_str}\n"
            f"Файл: {filename}\n"
            "Документ доступен в вашем личном кабинете.",
            _client_buttons(lead_for_notify),
        )
        return {'ok': True, 'filename': filename}

    @app.route('/api/nanny/<portal_token>/submit_fact', methods=['POST'])
    def api_nanny_submit_fact(portal_token: str):
        """Nanny submits actual start/end time for a work date (JSON mode)."""
        session_token = session.get('nanny_portal_token') or ''
        if session_token != portal_token:
            return {'error': 'Forbidden'}, 403
        if use_sql:
            return {'error': 'Use /api/nanny/shifts/<id>/actual in SQL mode'}, 400

        data = request.get_json(force=True) or {}
        client_token = (data.get('client_token') or '').strip()
        date_str = (data.get('date') or '').strip()
        fact_start = (data.get('fact_start') or '').strip()
        fact_end = (data.get('fact_end') or '').strip()

        if not client_token or not date_str or not fact_start or not fact_end:
            return {'error': 'client_token, date, fact_start, fact_end required'}, 400
        if not re.match(r'^[0-9]{2}:[0-9]{2}$', fact_start) or not re.match(r'^[0-9]{2}:[0-9]{2}$', fact_end):
            return {'error': 'invalid time'}, 400
        if fact_start == fact_end:
            return {'error': 'start and end must differ'}, 400
        if (int(fact_end[:2]) * 60 + int(fact_end[3:5])) <= (int(fact_start[:2]) * 60 + int(fact_start[3:5])):
            return {'error': 'end must be after start'}, 400

        leads = load_leads()
        lead = next((x for x in leads if x.get('token') == client_token), None)
        if not lead:
            return {'error': 'Клиент не найден'}, 404
        nanny = next((n for n in load_nannies() if n.get('portal_token') == portal_token), None)
        if not nanny:
            return {'error': 'Няня не найдена'}, 404
        if str(lead.get('assigned_nanny_id') or '') != str(nanny.get('id')):
            return {'error': 'Клиент не назначен этой няне'}, 403

        wd = dict(lead.get('work_dates') or {})
        if date_str not in wd:
            return {'error': 'Дата не найдена'}, 404

        slot = dict(wd[date_str]) if isinstance(wd[date_str], dict) else {}
        slot['fact_start'] = fact_start
        slot['fact_end'] = fact_end
        slot['status'] = 'waiting_fact'  # admin needs to confirm
        wd[date_str] = slot
        lead['work_dates'] = wd
        save_leads(leads)

        # Notify admins
        _notify_admins(
            f"⏱ Няня отправила фактическое время\n"
            f"Клиент: {lead.get('parent_name','—')}\n"
            f"Дата: {date_str}\n"
            f"Факт: {fact_start}–{fact_end}\n"
            f"ЛК: {os.environ.get('SITE_URL','https://web-production-2ebe9.up.railway.app').rstrip('/')}/client/{client_token}",
            _admin_lead_buttons(lead),
        )
        _send_to_client(
            lead,
            "⏱ Няня отправила фактическое время работы\n"
            f"Дата: {date_str}\n"
            f"Факт: {fact_start}-{fact_end}\n"
            f"Проверьте и подтвердите в личном кабинете: {_site_url()}/client/{client_token}",
            _client_buttons(lead),
        )
        return {'ok': True}

    @app.route('/api/nanny/<portal_token>/blocks')
    def api_nanny_public_blocks(portal_token: str):
        if use_sql:
            nanny = Nanny.query.filter_by(portal_token=portal_token).first()
            if not nanny:
                return {'error': 'invalid token'}, 404
            blocks = (
                NannyBlock.query
                .filter_by(nanny_id=nanny.id, kind='dayoff')
                .order_by(NannyBlock.date.desc(), NannyBlock.start.asc())
                .all()
            )
            return {'ok': True, 'items': [{
                'id': b.id,
                'date': b.date,
                'start': b.start,
                'end': b.end,
                'note': b.note,
            } for b in blocks]}
        else:
            nanny = next((n for n in load_nannies() if n.get('portal_token') == portal_token), None)
            if not nanny:
                return {'error': 'invalid token'}, 404
            blocks = _read_json(NANNY_BLOCKS_FILE, [])
            items = [
                {
                    'id': b.get('id'),
                    'date': b.get('date'),
                    'start': b.get('start'),
                    'end': b.get('end'),
                    'note': b.get('note'),
                }
                for b in blocks
                if str(b.get('nanny_id')) == str(nanny.get('id')) and b.get('kind', 'dayoff') == 'dayoff'
            ]
            items.sort(key=lambda x: ((x.get('date') or ''), (x.get('start') or '')), reverse=True)
            return {'ok': True, 'items': items}

    @app.route('/api/nanny/<portal_token>/blocks', methods=['POST'])
    def api_nanny_public_blocks_create(portal_token: str):
        # Security: caller must be authenticated as this nanny via session
        session_token = session.get('nanny_portal_token') or ''
        if session_token != portal_token:
            return {'error': 'Forbidden'}, 403

        if use_sql:
            nanny = Nanny.query.filter_by(portal_token=portal_token).first()
            nanny_id = nanny.id if nanny else None
        else:
            nanny = next((n for n in load_nannies() if n.get('portal_token') == portal_token), None)
            nanny_id = nanny.get('id') if nanny else None
        if not nanny_id:
            return {'error': 'invalid token'}, 404

        data = request.get_json(silent=True) or {}
        date = (data.get('date') or '').strip()
        start = (data.get('start') or '').strip() or None
        end = (data.get('end') or '').strip() or None
        note = _clean_user_text(data.get('note'), 300) or None

        if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
            return {'error': 'invalid date'}, 400
        if (start and not re.match(r'^\d{2}:\d{2}$', start)) or (end and not re.match(r'^\d{2}:\d{2}$', end)):
            return {'error': 'invalid time'}, 400

        if use_sql:
            b = NannyBlock(nanny_id=nanny_id, date=date, start=start, end=end, note=note, kind='dayoff')
            db.session.add(b)
            db.session.commit()
            _notify_admins(
                "🚫 Няня отметила выходной\n"
                f"Няня: {nanny.name if nanny else '—'}\n"
                f"Дата: {date}\n"
                f"Время: {(start or 'весь день') + (('-' + end) if end else '')}\n"
                f"Комментарий: {note or '—'}"
            )
            _notify_dayoff_conflicts(nanny_id, nanny.name if nanny else '—', date, start, end, note)
            return {'ok': True, 'id': b.id}

        blocks = _read_json(NANNY_BLOCKS_FILE, [])
        next_id = (max([int(b.get('id') or 0) for b in blocks] or [0]) + 1)
        blocks.append({
            'id': next_id,
            'nanny_id': nanny_id,
            'date': date,
            'start': start,
            'end': end,
            'note': note,
            'kind': 'dayoff',
            'created_at': datetime.datetime.utcnow().isoformat(),
        })
        _write_json(NANNY_BLOCKS_FILE, blocks)
        _notify_admins(
            "🚫 Няня отметила выходной\n"
            f"Няня: {nanny.get('name') if nanny else '—'}\n"
            f"Дата: {date}\n"
            f"Время: {(start or 'весь день') + (('-' + end) if end else '')}\n"
            f"Комментарий: {note or '—'}"
        )
        _notify_dayoff_conflicts(nanny_id, nanny.get('name') if nanny else '—', date, start, end, note)
        return {'ok': True, 'id': next_id}

    @app.route('/api/nanny/<portal_token>/blocks/<int:block_id>', methods=['DELETE'])
    def api_nanny_public_blocks_delete(portal_token: str, block_id: int):
        # Security: caller must be authenticated as this nanny via session
        session_token = session.get('nanny_portal_token') or ''
        if session_token != portal_token:
            return {'error': 'Forbidden'}, 403

        if use_sql:
            nanny = Nanny.query.filter_by(portal_token=portal_token).first()
            if not nanny:
                return {'error': 'invalid token'}, 404

            b = NannyBlock.query.filter_by(id=block_id, nanny_id=nanny.id, kind='dayoff').first()
            if not b:
                return {'error': 'not found'}, 404

            db.session.delete(b)
            db.session.commit()
            return {'ok': True}

        nanny = next((n for n in load_nannies() if n.get('portal_token') == portal_token), None)
        if not nanny:
            return {'error': 'invalid token'}, 404
        blocks = _read_json(NANNY_BLOCKS_FILE, [])
        kept = [
            b for b in blocks
            if not (int(b.get('id') or 0) == block_id and str(b.get('nanny_id')) == str(nanny.get('id')))
        ]
        if len(kept) == len(blocks):
            return {'error': 'not found'}, 404
        _write_json(NANNY_BLOCKS_FILE, kept)
        return {'ok': True}



    @app.route('/admin')
    @require_admin
    def admin():
        # Admin panel (protected)
        leads = load_leads()
        nannies = load_nannies()
        agents = load_agents()
        # Build enriched calendar events for admin
        # type: 'open' | 'assigned' | 'confirmed' | 'waiting_fact' | 'cancelled'
        nanny_map = {str(n['id']): n for n in nannies} if isinstance(nannies, list) else {}
        events = []
        for l in leads:
            nanny_id = l.get('assigned_nanny_id')
            nanny_obj = nanny_map.get(str(nanny_id)) if nanny_id else None
            nanny_name = nanny_obj.get('name') if nanny_obj else None
            receipts = (l.get('documents') or {}).get('receipts') or {}
            for d, info in (l.get('work_dates') or {}).items():
                slot = info if isinstance(info, dict) else {}
                finance = _lead_slot_finance(l, d, slot)
                date_status = slot.get('status')  # confirmed / cancelled / waiting_fact / None
                # Derive display type
                pending_a = bool(slot.get('pending_admin'))
                pending_n = bool(slot.get('pending_nanny'))
                if date_status == 'cancelled':
                    evt_type = 'cancelled'
                elif date_status == 'confirmed':
                    evt_type = 'confirmed'
                elif date_status == 'waiting_fact':
                    evt_type = 'waiting_fact'
                elif pending_a or pending_n:
                    evt_type = 'pending'
                elif nanny_id:
                    evt_type = 'assigned'
                else:
                    evt_type = 'open'
                has_receipt = bool(receipts.get(d))
                events.append({
                    'date': d,
                    'nanny_id': nanny_id,
                    'nanny_name': nanny_name,
                    'child_name': l.get('child_name'),
                    'child_age': l.get('child_age'),
                    'parent_name': l.get('parent_name'),
                    'token': l.get('token'),
                    'time': slot.get('time'),
                    'status': date_status,
                    'type': evt_type,
                    'has_receipt': has_receipt,
                    'fact_start': slot.get('fact_start'),
                    'fact_end': slot.get('fact_end'),
                    'pending_admin': bool(slot.get('pending_admin')),
                    'pending_nanny': bool(slot.get('pending_nanny')),
                    'client_comment': slot.get('client_comment', ''),
                    'nanny_comment': slot.get('nanny_comment', ''),
                    'client_actual_start': slot.get('client_actual_start', ''),
                    'client_actual_end': slot.get('client_actual_end', ''),
                    'client_review': slot.get('client_review', ''),
                    'client_review_stars': slot.get('client_review_stars', 0),
                    'finance_start': finance.get('start'),
                    'finance_end': finance.get('end'),
                    'finance_source': finance.get('source'),
                    'hours': finance.get('hours'),
                    'client_rate_per_hour': finance.get('client_rate_per_hour'),
                    'nanny_rate_per_hour': finance.get('nanny_rate_per_hour'),
                    'client_total_vnd': finance.get('client_total_vnd'),
                    'nanny_total_vnd': finance.get('nanny_total_vnd'),
                    'margin_vnd': finance.get('margin_vnd'),
                })
        events.sort(key=lambda x: x.get('date') or '')

        # Collect nanny days-off for admin calendar
        nanny_dayoffs = []
        nanny_names_by_id = {str(n['id']): n['name'] for n in nannies} if isinstance(nannies, list) and nannies and isinstance(nannies[0], dict) else {}
        if use_sql:
            all_blocks = NannyBlock.query.filter_by(kind='dayoff').order_by(NannyBlock.date.asc()).all()
            nanny_names_by_id = {n.id: n.name for n in Nanny.query.all()}
            for b in all_blocks:
                nanny_dayoffs.append({
                    'date': b.date,
                    'nanny_id': b.nanny_id,
                    'nanny_name': nanny_names_by_id.get(b.nanny_id, f'Няня #{b.nanny_id}'),
                    'start': b.start,
                    'end': b.end,
                    'note': b.note,
                })
        else:
            blocks = _read_json(NANNY_BLOCKS_FILE, [])
            for b in blocks:
                if b.get('kind', 'dayoff') != 'dayoff':
                    continue
                nanny_id = b.get('nanny_id')
                nanny_dayoffs.append({
                    'date': b.get('date'),
                    'nanny_id': nanny_id,
                    'nanny_name': nanny_names_by_id.get(str(nanny_id), f"Няня {nanny_id or ''}".strip()),
                    'start': b.get('start'),
                    'end': b.get('end'),
                    'note': b.get('note'),
                })

        clients = []
        shift_rows = []
        reviews = load_reviews()
        if use_sql:
            from time_utils import compute_amount_vnd

            clients = Client.query.order_by(Client.id.desc()).limit(200).all()
            shifts = (
                Shift.query
                .order_by(Shift.date.desc(), Shift.planned_start.desc())
                .limit(200)
                .all()
            )
            # cache names
            nanny_by_id = {n.id: n for n in Nanny.query.all()}
            client_by_id = {c.id: c for c in Client.query.all()}

            for s in shifts:
                start = s.resolved_start or s.client_actual_start or s.nanny_actual_start or s.planned_start
                end = s.resolved_end or s.client_actual_end or s.nanny_actual_end or s.planned_end

                client_total = None
                nanny_total = None
                margin = None
                try:
                    if s.client_rate_per_hour and s.date and start and end:
                        client_total = compute_amount_vnd(s.date, start, end, s.client_rate_per_hour)
                except Exception:
                    client_total = None
                try:
                    if s.nanny_rate_per_hour and s.date and start and end:
                        nanny_total = compute_amount_vnd(s.date, start, end, s.nanny_rate_per_hour)
                except Exception:
                    nanny_total = None

                if client_total is not None and nanny_total is not None:
                    margin = client_total - nanny_total

                shift_rows.append({
                    'id': s.id,
                    'date': s.date,
                    'planned_start': s.planned_start,
                    'planned_end': s.planned_end,
                    'status': s.status,
                    'client_id': s.client_id,
                    'client_name': (client_by_id.get(s.client_id).parent_name if s.client_id in client_by_id else str(s.client_id)),
                    'client_tid': (client_by_id.get(s.client_id).telegram_user_id if s.client_id in client_by_id else None),
                    'nanny_id': s.nanny_id,
                    'nanny_name': (nanny_by_id.get(s.nanny_id).name if s.nanny_id in nanny_by_id else (str(s.nanny_id) if s.nanny_id else None)),
                    'nanny_tid': (nanny_by_id.get(s.nanny_id).telegram_user_id if s.nanny_id in nanny_by_id else None),
                    'nanny_actual_start': s.nanny_actual_start,
                    'nanny_actual_end': s.nanny_actual_end,
                    'client_actual_start': s.client_actual_start,
                    'client_actual_end': s.client_actual_end,
                    'resolved_start': s.resolved_start,
                    'resolved_end': s.resolved_end,
                    'client_rate_per_hour': s.client_rate_per_hour,
                    'nanny_rate_per_hour': s.nanny_rate_per_hour,
                    'client_total_vnd': client_total,
                    'nanny_total_vnd': nanny_total,
                    'margin_vnd': margin,
                })

        assigned_count = len([l for l in leads if l.get('assigned_nanny_id')])
        unassigned_count = max(0, len(leads) - assigned_count)
        work_dates_total = sum(len((l.get('work_dates') or {}).keys()) for l in leads)
        resolved_leads_count = len([
            l for l in leads
            if l.get('assigned_nanny_id') and l.get('client_rate_per_hour') and l.get('nanny_rate_per_hour')
        ])
        crm = {
            'leads_total': len(leads),
            'assigned_total': assigned_count,
            'unassigned_total': unassigned_count,
            'attention_leads_total': max(0, len(leads) - resolved_leads_count),
            'resolved_leads_total': resolved_leads_count,
            'work_dates_total': work_dates_total,
            'reviews_total': len(reviews),
            'shifts_total': len(shift_rows),
        }
        agent_stats = {}
        for agent in agents:
            aid = str(agent.get('id') or '')
            agent_stats[aid] = len([lead for lead in leads if str(lead.get('referral_agent_id') or '') == aid])
        analytics_ids = _analytics_ids()
        seo_status = {
            'site_url': _site_url(),
            'robots_url': f"{_site_url()}/robots.txt",
            'sitemap_url': f"{_site_url()}/sitemap.xml",
            'google_analytics_id': analytics_ids['google_analytics_id'],
            'yandex_metrika_id': analytics_ids['yandex_metrika_id'],
            'public_pages': 4 + len([a for a in _articles_published() if a.get('slug')]) + len([n for n in nannies if n.get('portal_token')]),
        }
        visit_start = _parse_visit_date(request.args.get('visit_start'))
        visit_end = _parse_visit_date(request.args.get('visit_end'))

        return render_template(
            'admin_simple.html',
            leads=leads,
            nannies=nannies,
            agents=agents,
            agent_stats=agent_stats,
            events=events,
            nanny_dayoffs=nanny_dayoffs,
            clients=clients,
            shifts=shift_rows,
            reviews=reviews,
            crm=crm,
            visit_stats=_site_visit_stats(30, visit_start, visit_end),
            seo_status=seo_status,
        )

    @app.route('/admin/notifications')
    @require_admin
    def admin_notifications():
        logs = _read_json(NOTIFICATION_LOG_FILE, [])
        if not isinstance(logs, list):
            logs = []
        status = (request.args.get('status') or '').strip().lower()
        q = (request.args.get('q') or '').strip().lower()
        filtered = []
        for item in logs:
            if not isinstance(item, dict):
                continue
            if status and str(item.get('status') or '').lower() != status:
                continue
            hay = ' '.join([
                str(item.get('recipient') or ''),
                str(item.get('status') or ''),
                str(item.get('error') or ''),
                str(item.get('text') or ''),
            ]).lower()
            if q and q not in hay:
                continue
            filtered.append(item)
        return render_template(
            'admin_notifications.html',
            logs=filtered[:300],
            status=status,
            q=q,
            stats={
                'total': len(logs),
                'delivered': len([x for x in logs if isinstance(x, dict) and x.get('status') == 'delivered']),
                'failed': len([x for x in logs if isinstance(x, dict) and x.get('status') == 'failed']),
                'skipped': len([x for x in logs if isinstance(x, dict) and x.get('status') == 'skipped']),
            },
        )

    @app.route('/admin/monitoring')
    @require_admin
    def admin_monitoring():
        events = _read_json(APP_EVENTS_FILE, [])
        if not isinstance(events, list):
            events = []
        level = (request.args.get('level') or '').strip().lower()
        kind = (request.args.get('kind') or '').strip().lower()
        filtered = []
        for item in events:
            if not isinstance(item, dict):
                continue
            if level and str(item.get('level') or '').lower() != level:
                continue
            if kind and str(item.get('kind') or '').lower() != kind:
                continue
            filtered.append(item)
        site = _site_url()
        return render_template(
            'admin_monitoring.html',
            events=filtered[:300],
            level=level,
            kind=kind,
            site_url=site,
            stats={
                'total': len(events),
                'errors': len([x for x in events if isinstance(x, dict) and x.get('level') == 'error']),
                'warnings': len([x for x in events if isinstance(x, dict) and x.get('level') == 'warning']),
            },
            checks={
                'telegram_token': bool(os.environ.get('TELEGRAM_BOT_TOKEN')),
                'admin_ids': bool(admin_ids()),
                'cron_secret': bool(os.environ.get('CRON_SECRET')),
                'site_url': site,
            },
        )

    @app.route('/admin/nanny/save', methods=['POST'])
    @require_admin
    def admin_nanny_save():
        """Create or update a nanny from the admin panel."""
        if use_sql:
            nanny_id_raw = (request.form.get('id') or '').strip() or None
            name = _clean_user_text(request.form.get('name'), 120)
            exp_short = _clean_user_text(request.form.get('exp_short'), 200) or None
            bio = _clean_user_text(request.form.get('bio'), 2000) or None
            photo = (request.form.get('photo') or '').strip() or None
            photo_file = request.files.get('photo_file')
            if photo_file and getattr(photo_file, 'filename', ''):
                # Store as base64 data URL — survives Railway redeploys
                photo = _image_to_data_url(photo_file)
            tid_raw = (request.form.get('telegram_user_id') or '').strip() or None

            if not name:
                flash('Имя няни обязательно', 'error')
                return redirect(url_for('admin'))

            tid = None
            if tid_raw:
                try:
                    tid = int(tid_raw)
                except Exception:
                    flash('Telegram ID должен быть числом', 'error')
                    return redirect(url_for('admin'))

            if nanny_id_raw:
                nanny_row = Nanny.query.get(int(nanny_id_raw))
            else:
                nanny_row = None

            if nanny_row is None:
                nanny_row = Nanny(
                    name=name,
                    exp_short=exp_short,
                    bio=bio,
                    photo=photo,
                )
                db.session.add(nanny_row)
                db.session.flush()  # get id
                nanny_row.portal_token = f"nanny-{nanny_row.id}"
                nanny_row.telegram_user_id = tid
                flash('Няня добавлена', 'success')
            else:
                nanny_row.name = name
                nanny_row.exp_short = exp_short
                nanny_row.bio = bio
                if photo:
                    nanny_row.photo = photo
                nanny_row.telegram_user_id = tid
                if not nanny_row.portal_token:
                    nanny_row.portal_token = f"nanny-{nanny_row.id}"
                flash('Данные няни обновлены', 'success')

            db.session.commit()
            return redirect(url_for('admin'))

        nannies = load_nannies()
        nanny_id = (request.form.get('id') or '').strip() or None
        name = _clean_user_text(request.form.get('name'), 120)
        age = _clean_user_text(request.form.get('age'), 40)
        exp_short = _clean_user_text(request.form.get('exp_short'), 200)
        bio = _clean_user_text(request.form.get('bio'), 2000)
        photo = (request.form.get('photo') or '').strip()
        tid_raw_json = (request.form.get('telegram_user_id') or '').strip() or None
        # Photo upload support in JSON mode — store as data URL (no filesystem dependency)
        photo_file_json = request.files.get('photo_file')
        if photo_file_json and getattr(photo_file_json, 'filename', ''):
            photo = _image_to_data_url(photo_file_json)


        if not name:
            flash('Имя няни обязательно', 'error')
            return redirect(url_for('admin'))

        def _slug(s: str) -> str:
            s = (s or '').lower().strip()
            out = []
            for ch in s:
                if ch.isalnum():
                    out.append(ch)
                elif ch in [' ', '-', '_']:
                    out.append('-')
            slug = ''.join(out).strip('-')
            while '--' in slug:
                slug = slug.replace('--', '-')
            return slug or secrets.token_urlsafe(4)

        if nanny_id:
            nanny = next((n for n in nannies if n.get('id') == nanny_id), None)
        else:
            nanny = None

        if nanny is None:
            base_id = _slug(name)
            uniq = base_id
            i = 2
            while any(n.get('id') == uniq for n in nannies):
                uniq = f"{base_id}-{i}"
                i += 1
            nanny = {
                'id': uniq,
                'portal_token': f"nanny-{uniq}",
                'name': name,
                'age': int(age) if age.isdigit() else age,
                'exp_short': exp_short,
                'bio': bio,
                'photo': photo or 'img/nanny_placeholder.jpg',
                'telegram_user_id': tid_raw_json,
            }
            nannies.append(nanny)
            flash('Няня добавлена', 'success')
        else:
            nanny['name'] = name
            nanny['age'] = int(age) if age.isdigit() else age
            nanny['exp_short'] = exp_short
            nanny['bio'] = bio
            if photo:
                nanny['photo'] = photo
            nanny['telegram_user_id'] = tid_raw_json
            nanny.setdefault('portal_token', f"nanny-{nanny.get('id')}")
            flash('Данные няни обновлены', 'success')

        _write_json(NANNIES_FILE, nannies)
        return redirect(url_for('admin'))

    @app.route('/admin/client/save', methods=['POST'])
    @require_admin
    def admin_client_save():
        if not use_sql:
            flash('SQL mode required', 'error')
            return redirect(url_for('admin'))

        parent_name = _clean_user_text(request.form.get('parent_name'), 120)
        child_name = _clean_user_text(request.form.get('child_name'), 120) or None
        child_age = _clean_user_text(request.form.get('child_age'), 40) or None
        tid_raw = (request.form.get('telegram_user_id') or '').strip() or None

        if not parent_name:
            flash('Имя клиента обязательно', 'error')
            return redirect(url_for('admin'))

        tid = None
        if tid_raw:
            try:
                tid = int(tid_raw)
            except Exception:
                flash('Telegram ID должен быть числом', 'error')
                return redirect(url_for('admin'))

        c = Client(
            parent_name=parent_name,
            child_name=child_name,
            child_age=child_age,
            telegram_user_id=tid,
        )
        db.session.add(c)
        db.session.commit()
        flash('Клиент создан', 'success')
        return redirect(url_for('admin'))

    @app.route('/admin/agent/save', methods=['POST'])
    @require_admin
    def admin_agent_save():
        agent_id_raw = (request.form.get('id') or '').strip() or None
        name = _clean_user_text(request.form.get('name'), 120)
        tid_raw = (request.form.get('telegram_user_id') or '').strip() or None
        notes = _clean_user_text(request.form.get('notes'), 1000)
        is_active = request.form.get('is_active') != '0'
        try:
            commission_raw = (request.form.get('commission_vnd') or '200000').strip().replace(' ', '').replace(',', '')
            commission_vnd = max(0, int(commission_raw))
            payout_delay_days = max(0, min(180, int((request.form.get('payout_delay_days') or '14').strip())))
        except Exception:
            flash('Процент комиссии и задержка выплаты должны быть числами', 'error')
            return redirect(url_for('admin'))
        telegram_user_id = None
        if tid_raw:
            try:
                telegram_user_id = int(tid_raw)
            except Exception:
                flash('Telegram ID агента должен быть числом', 'error')
                return redirect(url_for('admin'))
        if not name:
            flash('Имя агента обязательно', 'error')
            return redirect(url_for('admin'))

        if use_sql:
            agent = ReferralAgent.query.get(int(agent_id_raw)) if agent_id_raw else None
            if not agent:
                agent = ReferralAgent(
                    name=name,
                    portal_token=secrets.token_urlsafe(18),
                    referral_code=_new_agent_code(name),
                )
                db.session.add(agent)
            agent.name = name
            agent.telegram_user_id = telegram_user_id
            agent.referral_code = agent.referral_code or _new_agent_code(name)
            agent.commission_vnd = commission_vnd
            agent.payout_delay_days = payout_delay_days
            agent.notes = notes
            agent.is_active = is_active
            try:
                db.session.commit()
                flash('Агент сохранён', 'success')
            except Exception as e:
                db.session.rollback()
                flash(f'Не удалось сохранить агента: {e}', 'error')
            return redirect(url_for('admin'))

        agents = load_agents()
        if agent_id_raw:
            agent = next((a for a in agents if str(a.get('id')) == str(agent_id_raw)), None)
        else:
            agent = None
        if not agent:
            next_id = str(max([int(a.get('id') or 0) for a in agents] or [0]) + 1)
            agent = {
                'id': next_id,
                'portal_token': secrets.token_urlsafe(18),
                'referral_code': _new_agent_code(name),
                'created_at': datetime.datetime.utcnow().isoformat(),
            }
            agents.append(agent)
        agent.update({
            'name': name,
            'telegram_user_id': telegram_user_id or '',
            'referral_code': agent.get('referral_code') or _new_agent_code(name),
            'commission_vnd': commission_vnd,
            'payout_delay_days': payout_delay_days,
            'notes': notes,
            'is_active': is_active,
        })
        save_agents(agents)
        flash('Агент сохранён', 'success')
        return redirect(url_for('admin'))

    @app.route('/admin/agent/delete', methods=['POST'])
    @require_admin
    def admin_agent_delete():
        agent_id_raw = (request.form.get('id') or '').strip()
        if not agent_id_raw:
            flash('agent id missing', 'error')
            return redirect(url_for('admin'))
        if use_sql:
            agent = ReferralAgent.query.get(int(agent_id_raw))
            if agent:
                agent.is_active = False
                db.session.commit()
                flash('Агент отключён', 'success')
            return redirect(url_for('admin'))
        agents = load_agents()
        for agent in agents:
            if str(agent.get('id')) == agent_id_raw:
                agent['is_active'] = False
                break
        save_agents(agents)
        flash('Агент отключён', 'success')
        return redirect(url_for('admin'))

    @app.route('/admin/shift/create', methods=['POST'])
    @require_admin
    def admin_shift_create():
        if not use_sql:
            flash('SQL mode required', 'error')
            return redirect(url_for('admin'))

        try:
            client_id = int((request.form.get('client_id') or '').strip())
            nanny_id = int((request.form.get('nanny_id') or '').strip())
        except Exception:
            flash('Выберите клиента и няню', 'error')
            return redirect(url_for('admin'))

        date = (request.form.get('date') or '').strip()
        planned_start = (request.form.get('planned_start') or '').strip()
        planned_end = (request.form.get('planned_end') or '').strip()

        try:
            client_rate = int((request.form.get('client_rate_per_hour') or '').strip())
            nanny_rate = int((request.form.get('nanny_rate_per_hour') or '').strip())
        except Exception:
            flash('Ставки должны быть числами', 'error')
            return redirect(url_for('admin'))

        s = Shift(
            client_id=client_id,
            nanny_id=nanny_id,
            date=date,
            planned_start=planned_start,
            planned_end=planned_end,
            status='assigned',
            client_rate_per_hour=client_rate,
            nanny_rate_per_hour=nanny_rate,
        )
        db.session.add(s)
        db.session.commit()

        # Notify nanny immediately about new shift (if linked)
        nanny = Nanny.query.get(nanny_id)
        client = Client.query.get(client_id)
        if nanny and nanny.telegram_user_id:
            _safe_send_message(
                int(nanny.telegram_user_id),
                f"🆕 Новая смена: {date} {planned_start}-{planned_end}.\nОткройте кабинет няни, чтобы отправить факт после смены.",
                _nanny_buttons(nanny_id),
            )
        if client and client.telegram_user_id:
            _safe_send_message(
                int(client.telegram_user_id),
                "✅ Администратор назначил рабочий день\n"
                f"Дата: {date}\n"
                f"Время: {planned_start}-{planned_end}\n"
                f"Няня: {nanny.name if nanny else '—'}",
                [[_url_button('Открыть приложение', f"{_site_url()}/app")]],
            )

        # Reminder rule:
        # - If shift starts in <2h, send reminders right now
        # - Else Cloud Scheduler endpoint will send exactly at T-2h (best-effort)
        try:
            from zoneinfo import ZoneInfo

            tzname = os.environ.get('SHIFT_TZ') or 'Asia/Ho_Chi_Minh'
            tz = ZoneInfo(tzname)
            start_dt = datetime.datetime.fromisoformat(f"{date}T{planned_start}:00").replace(tzinfo=tz)
            now_dt = datetime.datetime.now(tz)
            seconds = (start_dt - now_dt).total_seconds()

            if seconds > 0 and seconds < 2 * 3600 and s.pre2h_reminder_sent_at is None:
                msg = f"⏰ Напоминание: смена через менее чем 2 часа.\n{date} {planned_start}-{planned_end}"
                if client and client.telegram_user_id:
                    _safe_send_message(int(client.telegram_user_id), msg, [[_url_button('Открыть приложение', f"{_site_url()}/app")]])
                if nanny and nanny.telegram_user_id:
                    _safe_send_message(int(nanny.telegram_user_id), msg, _nanny_buttons(nanny_id))
                s.pre2h_reminder_sent_at = datetime.datetime.utcnow()
                db.session.commit()
        except Exception:
            pass

        flash('Смена создана', 'success')
        return redirect(url_for('admin'))

    @app.route('/admin/shift/resolve', methods=['POST'])
    @require_admin
    def admin_shift_resolve():
        if not use_sql:
            flash('SQL mode required', 'error')
            return redirect(url_for('admin'))

        try:
            shift_id = int((request.form.get('shift_id') or '').strip())
        except Exception:
            flash('shift_id missing', 'error')
            return redirect(url_for('admin'))

        resolved_start = (request.form.get('resolved_start') or '').strip()
        resolved_end = (request.form.get('resolved_end') or '').strip()
        if not resolved_start or not resolved_end:
            flash('Укажите resolved_start и resolved_end', 'error')
            return redirect(url_for('admin'))

        s = Shift.query.get(shift_id)
        if not s:
            flash('Смена не найдена', 'error')
            return redirect(url_for('admin'))

        s.resolved_start = resolved_start
        s.resolved_end = resolved_end
        s.status = 'confirmed'
        db.session.commit()

        # Notify both sides
        nanny = Nanny.query.get(s.nanny_id) if s.nanny_id else None
        client = Client.query.get(s.client_id) if s.client_id else None
        text = f"🧾 Решение по смене {s.date} {s.planned_start or ''}-{s.planned_end or ''}: {resolved_start}-{resolved_end}."
        if nanny and nanny.telegram_user_id:
            _safe_send_message(int(nanny.telegram_user_id), text, _nanny_buttons(s.nanny_id))
        if client and client.telegram_user_id:
            _safe_send_message(int(client.telegram_user_id), text, [[_url_button('Открыть приложение', f"{_site_url()}/app")]])

        flash('Смена решена', 'success')
        return redirect(url_for('admin'))

    @app.route('/admin/assign', methods=['POST'])
    @require_admin
    def admin_assign():
        token = request.form.get('token')
        nanny_id = (request.form.get('nanny_id') or '').strip() or None

        if use_sql:
            lead_row = Lead.query.filter_by(token=token).first()
            if not lead_row:
                flash('Заявка не найдена', 'error')
                return redirect(url_for('admin'))

            if nanny_id:
                # Block assignment if nanny has day-off blocks overlapping requested dates
                blocked_dates = set()
                work_dates = lead_row.work_dates or {}
                for d, slots in work_dates.items():
                    if NannyBlock.query.filter_by(nanny_id=int(nanny_id), date=d, kind='dayoff').first():
                        blocked_dates.add(d)
                if blocked_dates:
                    flash('Нельзя назначить няню: отмечены выходные даты: ' + ', '.join(sorted(blocked_dates)), 'error')
                    return redirect(url_for('admin'))
            lead_row.assigned_nanny_id = int(nanny_id) if nanny_id else None
            db.session.commit()

            if nanny_id:
                selected_nanny = Nanny.query.get(int(nanny_id))
                dates = sorted((lead_row.work_dates or {}).keys())
                dates_text = ', '.join(dates[:5]) if dates else 'даты уточняются'
                if selected_nanny and selected_nanny.telegram_user_id:
                    _safe_send_message(
                        int(selected_nanny.telegram_user_id),
                        "🆕 Вам назначена заявка\n"
                        f"Клиент: {lead_row.parent_name}\n"
                        f"Ребёнок: {lead_row.child_name}, {lead_row.child_age}\n"
                        f"Даты: {dates_text}\n"
                        f"ЛК: {_nanny_portal_url_by_id(nanny_id)}",
                        _nanny_buttons(nanny_id),
                    )
                _send_to_client(
                    lead_row,
                    "✅ По вашей заявке назначена няня.\n"
                    f"Няня: {selected_nanny.name if selected_nanny else '—'}\n"
                    f"Даты: {dates_text}\n"
                    f"Профиль няни: {_nanny_profile_url_by_id(nanny_id)}\n"
                    f"ЛК: {_lead_cabinet_url(lead_row)}",
                    _client_buttons(lead_row),
                )

            flash('Няня назначена', 'success')
            return redirect(url_for('admin'))

        leads = load_leads()
        lead = next((x for x in leads if x.get('token') == token), None)
        if not lead:
            flash('Заявка не найдена', 'error')
            return redirect(url_for('admin'))
        if nanny_id:
            blocks = _read_json(NANNY_BLOCKS_FILE, [])
            blocked_dates = {
                d for d in (lead.get('work_dates') or {}).keys()
                if any(
                    str(b.get('nanny_id')) == str(nanny_id)
                    and b.get('date') == d
                    and b.get('kind', 'dayoff') == 'dayoff'
                    for b in blocks
                )
            }
            if blocked_dates:
                flash('Нельзя назначить няню: отмечены выходные даты: ' + ', '.join(sorted(blocked_dates)), 'error')
                return redirect(url_for('admin'))
        lead['assigned_nanny_id'] = nanny_id or None
        # Update rates if provided
        _cr = (request.form.get('client_rate_per_hour') or '').strip()
        _nr = (request.form.get('nanny_rate_per_hour') or '').strip()
        if _cr.isdigit(): lead['client_rate_per_hour'] = int(_cr)
        if _nr.isdigit(): lead['nanny_rate_per_hour'] = int(_nr)
        save_leads(leads)

        if nanny_id:
            selected_nanny = next((n for n in load_nannies() if str(n.get('id')) == str(nanny_id)), None)
            dates = sorted((lead.get('work_dates') or {}).keys())
            dates_text = ', '.join(dates[:5]) if dates else 'даты уточняются'
            if selected_nanny and selected_nanny.get('telegram_user_id'):
                _safe_send_message(
                    int(selected_nanny.get('telegram_user_id')),
                    "🆕 Вам назначена заявка\n"
                    f"Клиент: {lead.get('parent_name') or '-'}\n"
                    f"Ребёнок: {lead.get('child_name') or '-'}, {lead.get('child_age') or '-'}\n"
                    f"Даты: {dates_text}\n"
                    f"ЛК: {_nanny_portal_url_by_id(nanny_id)}",
                    _nanny_buttons(nanny_id),
                )
            _send_to_client(
                lead,
                "✅ По вашей заявке назначена няня.\n"
                f"Няня: {selected_nanny.get('name') if selected_nanny else '—'}\n"
                f"Даты: {dates_text}\n"
                f"Профиль няни: {_nanny_profile_url_by_id(nanny_id)}\n"
                f"ЛК: {_lead_cabinet_url(lead)}",
                _client_buttons(lead),
            )

        flash('Няня назначена', 'success')
        return redirect(url_for('admin'))

    @app.route('/admin/review/save', methods=['POST'])
    @require_admin
    def admin_review_save():
        review_id  = (request.form.get('id') or '').strip()
        author     = _clean_user_text(request.form.get('author'), 120) or 'Родитель'
        role       = _clean_user_text(request.form.get('role'), 160)
        text_value = _clean_user_text(request.form.get('text'), 2000)
        nanny_id   = (request.form.get('nanny_id') or '').strip() or None
        pinned     = bool(request.form.get('pinned'))
        try:
            stars = max(1, min(5, int((request.form.get('stars') or '5').strip())))
        except Exception:
            stars = 5

        if not text_value:
            flash('Текст отзыва обязателен', 'error')
            return redirect(url_for('admin'))

        if use_sql:
            existing = Review.query.get(review_id) if review_id else None
            if existing:
                existing.author  = author
                existing.role    = role
                existing.stars   = stars
                existing.text    = text_value
                existing.nanny_id = nanny_id
                existing.pinned  = pinned
                flash('Отзыв обновлён', 'success')
            else:
                r = Review(
                    id=_legacy_token('rev-', f"{author}-{time.time()}"),
                    author=author, role=role, stars=stars, text=text_value,
                    created_at=datetime.datetime.utcnow(), is_visible=True,
                    nanny_id=nanny_id, pinned=pinned,
                )
                db.session.add(r)
                flash('Отзыв добавлен', 'success')
            db.session.commit()
        else:
            reviews = load_reviews()
            existing = next((r for r in reviews if r.get('id') == review_id), None) if review_id else None
            if existing:
                existing['author']   = author
                existing['role']     = role
                existing['stars']    = stars
                existing['text']     = text_value
                existing['nanny_id'] = nanny_id
                existing['pinned']   = pinned
                flash('Отзыв обновлён', 'success')
            else:
                reviews.insert(0, {
                    'id': _legacy_token('rev-', f"{author}-{time.time()}"),
                    'author': author, 'role': role, 'stars': stars, 'text': text_value,
                    'created_at': datetime.datetime.utcnow().isoformat(),
                    'nanny_id': nanny_id, 'pinned': pinned,
                })
                flash('Отзыв добавлен', 'success')
            save_reviews(reviews)
        return redirect(url_for('admin'))

    @app.route('/admin/review/delete', methods=['POST'])
    @require_admin
    def admin_review_delete():
        review_id = (request.form.get('id') or '').strip()
        if use_sql:
            r = Review.query.get(review_id)
            if not r:
                flash('Отзыв не найден', 'error')
            else:
                r.is_visible = False
                db.session.commit()
                flash('Отзыв удалён', 'success')
        else:
            reviews = load_reviews(include_hidden=True)
            target = next((r for r in reviews if r.get('id') == review_id), None)
            if not target:
                flash('Отзыв не найден', 'error')
            else:
                target['is_visible'] = False
                _write_json(REVIEWS_FILE, reviews)
                flash('Отзыв удалён', 'success')
        return redirect(url_for('admin'))

    @app.route('/cron/remind_2h')
    def cron_remind_2h():
        """Cloud Scheduler entrypoint.

        Should be called every 5 minutes.

        Protect with CRON_SECRET env (pass ?secret=... or header X-Cron-Secret).
        """
        if not use_sql:
            return {'error': 'SQL mode required'}, 400

        secret = os.environ.get('CRON_SECRET')
        provided = request.args.get('secret') or request.headers.get('X-Cron-Secret')
        if not secret:
            # If CRON_SECRET not configured, block external access entirely
            # Allow only local calls (Railway internal)
            remote = request.environ.get('REMOTE_ADDR', '')
            if remote not in ('127.0.0.1', '::1'):
                return {'error': 'CRON_SECRET not configured — set it in env vars'}, 403
        elif provided != secret:
            return {'error': 'forbidden'}, 403

        from zoneinfo import ZoneInfo

        tzname = os.environ.get('SHIFT_TZ') or 'Asia/Ho_Chi_Minh'
        tz = ZoneInfo(tzname)
        now_dt = datetime.datetime.now(tz)
        sent_2h = 0
        sent_pre = 0
        sent_post = 0
        sent_missing_fact = 0
        sent_review = 0
        sent_admin = 0

        def _query_time_window(field_name: str, sent_field, dt_from: datetime.datetime, dt_to: datetime.datetime):
            date_from = dt_from.date().isoformat()
            date_to = dt_to.date().isoformat()
            t_from = dt_from.strftime('%H:%M')
            t_to = dt_to.strftime('%H:%M')
            time_col = getattr(Shift, field_name)

            base = Shift.query.filter(
                sent_field.is_(None),
                time_col.isnot(None),
                Shift.status != 'cancelled',
            )

            if date_from == date_to:
                return base.filter(
                    Shift.date == date_from,
                    time_col >= t_from,
                    time_col < t_to,
                )

            q1 = base.filter(Shift.date == date_from, time_col >= t_from)
            q2 = base.filter(Shift.date == date_to, time_col < t_to)
            return q1.union_all(q2)

        # Client + nanny reminder 2 hours before work.
        two_start = now_dt + datetime.timedelta(hours=2)
        two_end = two_start + datetime.timedelta(minutes=5)
        for s in _query_time_window('planned_start', Shift.pre2h_reminder_sent_at, two_start, two_end).all():
            nanny = Nanny.query.get(s.nanny_id) if s.nanny_id else None
            client = Client.query.get(s.client_id) if s.client_id else None
            msg = (
                "⏰ Напоминание: рабочий день через 2 часа\n"
                f"Дата: {s.date}\n"
                f"Время: {s.planned_start or ''}-{s.planned_end or ''}"
            )
            if client and client.telegram_user_id:
                _safe_send_message(int(client.telegram_user_id), msg, [[_url_button('Открыть приложение', f"{_site_url()}/app")]])
                sent_2h += 1
            if nanny and nanny.telegram_user_id:
                _safe_send_message(int(nanny.telegram_user_id), msg + "\nПодготовьтесь и приезжайте вовремя.", _nanny_buttons(s.nanny_id))
                sent_2h += 1
            s.pre2h_reminder_sent_at = datetime.datetime.utcnow()

        # Nanny reminder 30 minutes before work.
        pre_start = now_dt + datetime.timedelta(minutes=30)
        pre_end = pre_start + datetime.timedelta(minutes=5)
        for s in _query_time_window('planned_start', Shift.reminder_sent_at, pre_start, pre_end).all():
            nanny = Nanny.query.get(s.nanny_id) if s.nanny_id else None
            if nanny and nanny.telegram_user_id:
                _safe_send_message(
                    int(nanny.telegram_user_id),
                    "⏰ Через 30 минут рабочий день\n"
                    f"Дата: {s.date}\n"
                    f"Время: {s.planned_start}-{s.planned_end or ''}\n"
                    "Пожалуйста, подготовьтесь и приезжайте на место работы вовремя.",
                    _nanny_buttons(s.nanny_id),
                )
                sent_pre += 1
            s.reminder_sent_at = datetime.datetime.utcnow()

        # Follow-up 30 minutes after planned end.
        post_start = now_dt - datetime.timedelta(minutes=35)
        post_end = now_dt - datetime.timedelta(minutes=30)
        for s in _query_time_window('planned_end', Shift.post_reminder_sent_at, post_start, post_end).all():
            nanny = Nanny.query.get(s.nanny_id) if s.nanny_id else None
            client = Client.query.get(s.client_id) if s.client_id else None
            if client and client.telegram_user_id:
                _safe_send_message(
                    int(client.telegram_user_id),
                    "⏱ Смена завершилась около 30 минут назад\n"
                    f"Дата: {s.date}\n"
                    f"План: {s.planned_start or ''}-{s.planned_end or ''}\n"
                    "Пожалуйста, отметьте фактическое время, поставьте оценку и оставьте комментарий.",
                    [[_url_button('Открыть приложение', f"{_site_url()}/app")]],
                )
                sent_post += 1
            if nanny and nanny.telegram_user_id:
                _safe_send_message(
                    int(nanny.telegram_user_id),
                    "⏱ Рабочий день завершился около 30 минут назад\n"
                    f"Дата: {s.date}\n"
                    f"План: {s.planned_start or ''}-{s.planned_end or ''}\n"
                    "Пожалуйста, отметьте фактическое время работы и напишите комментарий к дню.",
                    _nanny_buttons(s.nanny_id),
                )
                sent_post += 1
            s.post_reminder_sent_at = datetime.datetime.utcnow()

        # Repeat nanny fact reminder 2 hours after planned end if fact was not submitted.
        missing_start = now_dt - datetime.timedelta(hours=2, minutes=5)
        missing_end = now_dt - datetime.timedelta(hours=2)
        for s in _query_time_window('planned_end', Shift.nanny_missing_fact_sent_at, missing_start, missing_end).all():
            nanny = Nanny.query.get(s.nanny_id) if s.nanny_id else None
            client = Client.query.get(s.client_id) if s.client_id else None
            if not (s.nanny_actual_start and s.nanny_actual_end):
                if nanny and nanny.telegram_user_id:
                    _safe_send_message(
                        int(nanny.telegram_user_id),
                        "⚠️ Фактическое время ещё не отмечено\n"
                        f"Дата: {s.date}\n"
                        f"План: {s.planned_start or ''}-{s.planned_end or ''}\n"
                        "Пожалуйста, внесите факт и комментарий.",
                        _nanny_buttons(s.nanny_id),
                    )
                    sent_missing_fact += 1
                _notify_admins(
                    "⚠️ Няня не отметила факт после смены\n"
                    f"Клиент: {client.parent_name if client else '—'}\n"
                    f"Няня: {nanny.name if nanny else '—'}\n"
                    f"Дата: {s.date}\n"
                    f"План: {s.planned_start or ''}-{s.planned_end or ''}"
                )
                sent_admin += 1
            s.nanny_missing_fact_sent_at = datetime.datetime.utcnow()

        # Client review/fact reminder 24 hours after planned end if no client note was left.
        review_start = now_dt - datetime.timedelta(hours=24, minutes=5)
        review_end = now_dt - datetime.timedelta(hours=24)
        for s in _query_time_window('planned_end', Shift.review_reminder_sent_at, review_start, review_end).all():
            client = Client.query.get(s.client_id) if s.client_id else None
            if client and client.telegram_user_id and not s.client_actual_note:
                _safe_send_message(
                    int(client.telegram_user_id),
                    "⭐ Помогите оценить работу няни\n"
                    f"Дата: {s.date}\n"
                    "Пожалуйста, отметьте фактическое время, оценку и комментарий.",
                    [[_url_button('Открыть приложение', f"{_site_url()}/app")]],
                )
                sent_review += 1
            s.review_reminder_sent_at = datetime.datetime.utcnow()

        # Admin reminder about unassigned new SQL leads.
        try:
            stale = datetime.datetime.utcnow() - datetime.timedelta(minutes=15)
            for lead in Lead.query.filter(Lead.assigned_nanny_id.is_(None), Lead.submitted_at <= stale).limit(20).all():
                key = f"unassigned-lead:{lead.id}"
                if _notification_was_sent(key):
                    continue
                _mark_notification_sent(key)
                _notify_admins(
                    "⏳ Заявка ждёт назначения няни\n"
                    f"Клиент: {lead.parent_name or '—'}\n"
                    f"Ребёнок: {lead.child_name or '—'}\n"
                    f"Создана: {lead.submitted_at.isoformat()}",
                    _admin_lead_buttons(lead),
                )
                sent_admin += 1
        except Exception:
            pass

        db.session.commit()

        return {
            'ok': True,
            'sent_2h': sent_2h,
            'sent_pre': sent_pre,
            'sent_post': sent_post,
            'sent_missing_fact': sent_missing_fact,
            'sent_review': sent_review,
            'sent_admin': sent_admin,
            'tz': tzname,
        }

    @app.route('/uploads/<path:filename>')
    def uploads(filename):
        normalized = (filename or '').replace('\\', '/').lstrip('/')
        if '..' in normalized.split('/'):
            return jsonify({'error': 'forbidden'}), 403
        # Whitelist allowed extensions to prevent serving unexpected files
        allowed_exts = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.pdf', '.mp4', '.mov', '.webm'}
        _, ext = os.path.splitext(normalized.lower())
        if ext not in allowed_exts:
            return jsonify({'error': 'forbidden'}), 403
        if not normalized.startswith('articles/'):
            try:
                docs_sources = []
                if use_sql:
                    docs_sources = [row.documents or {} for row in Lead.query.with_entities(Lead.documents).all()]
                else:
                    docs_sources = [(lead.get('documents') or {}) for lead in load_leads()]
                known_receipts = {
                    str(item).replace('\\', '/').lstrip('/')
                    for docs in docs_sources
                    for items in ((docs.get('receipts') or {}).values())
                    for item in (items or [])
                }
                if normalized in known_receipts:
                    return jsonify({'error': 'receipt access requires cabinet link'}), 403
            except Exception:
                app.logger.warning("receipt visibility check failed", exc_info=True)
                return jsonify({'error': 'forbidden'}), 403
        return send_from_directory(app.config['UPLOAD_DIR'], normalized)


    # ── ERROR HANDLERS ─────────────────────────────────────────────────────
    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'not found'}), 404
        return render_template('404.html'), 404

    @app.errorhandler(500)
    def server_error(e):
        _append_app_event('server_error', repr(e), 'error')
        if request.path.startswith('/api/'):
            return jsonify({'error': 'internal server error'}), 500
        return render_template('404.html'), 500

    @app.errorhandler(403)
    def forbidden(e):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'forbidden'}), 403
        return render_template('404.html'), 403

    @app.errorhandler(413)
    def request_entity_too_large(e):
        _append_app_event('upload_too_large', repr(e), 'warning')
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Файл слишком большой. Максимум 16 МБ.'}), 413
        return render_template('404.html'), 413

    @app.errorhandler(429)
    def too_many_requests(e):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Слишком много запросов. Попробуйте позже.'}), 429
        return render_template('404.html'), 429

    # Canonical production URL — used in sitemap.xml, robots.txt, OG tags
    PRODUCTION_URL = 'https://web-production-2ebe9.up.railway.app'

    def _site_base() -> str:
        """Return canonical site base URL.
        Priority: SITE_URL env > PRODUCTION_URL constant > request.url_root (dev fallback)."""
        return _public_site_url()

    @app.route('/robots.txt')
    def robots_txt():
        lines = [
            "User-agent: *",
            "Allow: /",
            "Allow: /blog",
            "Allow: /nanny/",        # public nanny profiles
            "Disallow: /admin",
            "Disallow: /nanny/app",
            "Disallow: /nanny/login",
            "Disallow: /nanny/portal",
            "Disallow: /client",
            "Disallow: /agent",
            "Disallow: /r/",
            "Disallow: /api/",
            "Sitemap: " + _site_base() + "/sitemap.xml",
        ]
        return Response("\n".join(lines) + "\n", mimetype="text/plain")

    ARTICLES_FILE = os.path.join(app.config['DATA_DIR'], 'articles.json')  # JSON fallback

    def _sanitize_html(html):
        """Convert markdown to HTML, or passthrough HTML, or auto-paragraph plain text."""
        if not html:
            return ''
        s = str(html)
        # If it looks like HTML (has block tags) — store as-is
        import re as _re
        if _re.search(r'<(p|h[1-6]|ul|ol|li|blockquote|div|br)\b', s, _re.I):
            return s
        # Otherwise treat as Markdown
        try:
            import markdown as _md
            return _md.markdown(s, extensions=['extra', 'nl2br'])
        except ImportError:
            pass
        # fallback: convert newlines to <p>
        paras = [p.strip() for p in s.split('\n') if p.strip()]
        return ''.join(f'<p>{p}</p>' for p in paras)

    def _art_to_dict(a):
        return {
            'id': a.id,
            'slug': a.slug,
            'title': a.title or '',
            'excerpt': a.excerpt or '',
            'body': a.body or '',
            'cover_url': a.cover_url or '',
            'gallery': a.gallery or [],
            'video_url': a.video_url or '',
            'video_file': a.video_file or '',
            'published': a.published,
            'seo_title': a.seo_title or '',
            'seo_description': a.seo_description or '',
            'seo_keywords': a.seo_keywords or '',
            'created_at': a.created_at.isoformat() if a.created_at else '',
            'updated_at': a.updated_at.isoformat() if a.updated_at else '',
        }

    def _articles_published():
        if use_sql:
            rows = Article.query.filter_by(published=True).order_by(Article.created_at.desc()).all()
            return [_art_to_dict(r) for r in rows]
        arts = _read_json(ARTICLES_FILE, [])
        return [a for a in arts if a.get('published')]

    @app.route('/sitemap.xml')
    def sitemap_xml():
        import html as _html
        base = _site_base()
        articles = _articles_published()
        nannies_list = load_nannies()
        today = datetime.datetime.utcnow().strftime('%Y-%m-%d')
        entries = [
            {'loc': base + '/', 'priority': '1.0', 'changefreq': 'weekly', 'lastmod': today},
            {'loc': base + '/blog', 'priority': '0.8', 'changefreq': 'weekly', 'lastmod': today},
            {'loc': base + '/faq', 'priority': '0.7', 'changefreq': 'monthly', 'lastmod': today},
            {'loc': base + '/tariffs', 'priority': '0.8', 'changefreq': 'monthly', 'lastmod': today},
        ]
        for a in articles:
            if a.get('slug'):
                lastmod = (a.get('updated_at') or a.get('created_at') or today)[:10]
                entries.append({'loc': base + '/blog/' + a['slug'], 'priority': '0.7',
                                'changefreq': 'monthly', 'lastmod': lastmod})
        for n in nannies_list:
            if n.get('portal_token'):
                entries.append({'loc': base + '/nanny/' + _html.escape(str(n['portal_token'])),
                                'priority': '0.6', 'changefreq': 'monthly', 'lastmod': today})
        xml = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for e in entries:
            xml.append(f"<url><loc>{_html.escape(e['loc'])}</loc>"
                       f"<lastmod>{e['lastmod']}</lastmod>"
                       f"<changefreq>{e['changefreq']}</changefreq>"
                       f"<priority>{e['priority']}</priority></url>")
        xml.append("</urlset>")
        return Response("\n".join(xml), mimetype="application/xml")

    @app.route('/tariffs')
    def tariffs():
        return render_template('tariffs.html')

    @app.route('/faq')
    def faq():
        return render_template('faq.html')

    @app.route('/blog')
    def blog():
        articles = _articles_published()
        return render_template('blog.html', articles=articles)

    @app.route('/blog/<slug>')
    def article(slug):
        if use_sql:
            row = Article.query.filter_by(slug=slug, published=True).first()
            if not row:
                return render_template('404.html'), 404
            art = _art_to_dict(row)
        else:
            arts = _read_json(ARTICLES_FILE, [])
            art = next((a for a in arts if a.get('slug') == slug and a.get('published')), None)
            if not art:
                return render_template('404.html'), 404
        published = _articles_published()
        idx = next((i for i, a in enumerate(published) if a['slug'] == slug), None)
        prev_art = published[idx + 1] if idx is not None and idx + 1 < len(published) else None
        next_art = published[idx - 1] if idx is not None and idx > 0 else None
        return render_template('article.html', art=art, prev_art=prev_art, next_art=next_art)

    @app.route('/api/admin/articles', methods=['GET'])
    @require_admin
    def api_articles_list():
        if use_sql:
            rows = Article.query.order_by(Article.created_at.desc()).all()
            return jsonify([_art_to_dict(r) for r in rows])
        arts = _read_json(ARTICLES_FILE, [])
        return jsonify(sorted(arts, key=lambda a: a.get('created_at', ''), reverse=True))

    @app.route('/api/admin/articles', methods=['POST'])
    @require_admin
    def api_article_create():
        import uuid as _uuid, re as _re
        data = request.get_json(force=True, silent=True) or {}
        slug = (data.get('slug') or '').strip()
        if not slug:
            raw = (data.get('title') or 'article').lower()
            slug = _re.sub(r'[^a-z0-9\-]+', '-', raw).strip('-')[:80] + '-' + str(int(time.time()))[-5:]
        gallery = [str(u) for u in (data.get('gallery') or []) if u]
        if use_sql:
            if Article.query.filter_by(slug=slug).first():
                slug = slug + '-' + str(int(time.time()))[-4:]
            row = Article(
                id=str(_uuid.uuid4()), slug=slug,
                title=(data.get('title') or '').strip(),
                excerpt=(data.get('excerpt') or '').strip(),
                body=_sanitize_html(data.get('body') or ''),
                cover_url=(data.get('cover_url') or '').strip() or None,
                gallery=gallery,
                video_url=(data.get('video_url') or '').strip() or None,
                video_file=(data.get('video_file') or '').strip() or None,
                published=bool(data.get('published', True)),
                seo_title=(data.get('seo_title') or '').strip() or None,
                seo_description=(data.get('seo_description') or '').strip() or None,
                seo_keywords=(data.get('seo_keywords') or '').strip() or None,
                created_at=datetime.datetime.utcnow(),
                updated_at=datetime.datetime.utcnow(),
            )
            db.session.add(row)
            db.session.commit()
            return jsonify({'ok': True, 'article': _art_to_dict(row)})
        now = time.strftime('%Y-%m-%dT%H:%M:%S')
        arts = _read_json(ARTICLES_FILE, [])
        if any(a.get('slug') == slug for a in arts):
            slug = slug + '-' + str(int(time.time()))[-4:]
        art = {'id': str(_uuid.uuid4()), 'slug': slug, 'title': (data.get('title') or '').strip(),
               'excerpt': (data.get('excerpt') or '').strip(), 'body': _sanitize_html(data.get('body') or ''),
               'cover_url': (data.get('cover_url') or '').strip(), 'gallery': gallery,
               'video_url': (data.get('video_url') or '').strip(), 'video_file': (data.get('video_file') or '').strip(),
               'published': bool(data.get('published', True)), 'created_at': now, 'updated_at': now,
               'seo_title': (data.get('seo_title') or '').strip(),
               'seo_description': (data.get('seo_description') or '').strip(),
               'seo_keywords': (data.get('seo_keywords') or '').strip()}
        arts.append(art)
        _write_json(ARTICLES_FILE, arts)
        return jsonify({'ok': True, 'article': art})

    @app.route('/api/admin/articles/<art_id>', methods=['PUT'])
    @require_admin
    def api_article_update(art_id):
        data = request.get_json(force=True, silent=True) or {}
        if use_sql:
            row = Article.query.get(art_id)
            if not row:
                return jsonify({'error': 'not found'}), 404
            for f in ['title','excerpt','body','cover_url','gallery','video_url','video_file',
                      'published','seo_title','seo_description','seo_keywords','slug']:
                if f not in data:
                    continue
                if f == 'body':
                    row.body = _sanitize_html(data[f])
                elif f == 'gallery':
                    g = data[f]
                    row.gallery = [str(u) for u in g if u] if isinstance(g, list) else []
                elif f == 'cover_url':
                    row.cover_url = (data[f] or '').strip() or None
                else:
                    setattr(row, f, data[f])
            row.updated_at = datetime.datetime.utcnow()
            db.session.commit()
            return jsonify({'ok': True, 'article': _art_to_dict(row)})
        arts = _read_json(ARTICLES_FILE, [])
        art = next((a for a in arts if a['id'] == art_id), None)
        if not art:
            return jsonify({'error': 'not found'}), 404
        for f in ['title','excerpt','body','cover_url','gallery','video_url','video_file',
                  'published','seo_title','seo_description','seo_keywords','slug']:
            if f in data:
                art[f] = _sanitize_html(data[f]) if f == 'body' else data[f]
        art['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        _write_json(ARTICLES_FILE, arts)
        return jsonify({'ok': True, 'article': art})

    @app.route('/api/admin/articles/<art_id>', methods=['DELETE'])
    @require_admin
    def api_article_delete(art_id):
        if use_sql:
            row = Article.query.get(art_id)
            if row:
                db.session.delete(row)
                db.session.commit()
        else:
            arts = _read_json(ARTICLES_FILE, [])
            _write_json(ARTICLES_FILE, [a for a in arts if a['id'] != art_id])
        return jsonify({'ok': True})

    def _article_save_image(file_storage):
        return _image_to_data_url(file_storage)

    @app.route('/api/admin/articles/upload-cover', methods=['POST'])
    @require_admin
    def api_article_upload_cover():
        f = request.files.get('file')
        if not f or not getattr(f, 'filename', ''):
            return jsonify({'error': 'no file'}), 400
        if (f.filename or '').rsplit('.', 1)[-1].lower() not in ('jpg','jpeg','png','gif','webp'):
            return jsonify({'error': 'bad ext'}), 400
        return jsonify({'ok': True, 'url': _article_save_image(f)})

    @app.route('/api/admin/articles/upload-gallery', methods=['POST'])
    @require_admin
    def api_article_upload_gallery():
        f = request.files.get('file')
        if not f or not getattr(f, 'filename', ''):
            return jsonify({'error': 'no file'}), 400
        if (f.filename or '').rsplit('.', 1)[-1].lower() not in ('jpg','jpeg','png','gif','webp'):
            return jsonify({'error': 'bad ext'}), 400
        return jsonify({'ok': True, 'url': _article_save_image(f)})

    @app.route('/api/admin/articles/upload-video', methods=['POST'])
    @require_admin
    def api_article_upload_video():
        import uuid as _uuid
        f = request.files.get('file')
        if not f or not getattr(f, 'filename', ''):
            return jsonify({'error': 'no file'}), 400
        ext = (f.filename or 'vid').rsplit('.', 1)[-1].lower()
        if ext not in ('mp4','mov','webm','avi'):
            return jsonify({'error': 'bad ext'}), 400
        folder = os.path.join(app.config['UPLOAD_DIR'], 'articles')
        os.makedirs(folder, exist_ok=True)
        fname = str(_uuid.uuid4()) + '.' + ext
        f.save(os.path.join(folder, fname))
        return jsonify({'ok': True, 'url': '/uploads/articles/' + fname})

    @app.route('/api/articles/latest')
    def api_articles_latest():
        arts = _articles_published()[:3]
        resp = jsonify([{'slug': a['slug'], 'title': a['title'],
                         'excerpt': a.get('excerpt',''), 'cover_url': _article_cover_preview(a.get('cover_url','')),
                         'created_at': a.get('created_at','')} for a in arts])
        resp.headers['Cache-Control'] = 'public, max-age=60, stale-while-revalidate=300'
        return resp

    # ── Profit / Earnings analytics ─────────────────────────────────────────

    @app.route('/api/admin/profit')
    @require_admin
    def api_admin_profit():
        """Return aggregated profit stats for admin dashboard.
        Query params: period = day | week | month | year | all (default: month)
        """
        from time_utils import compute_amount_vnd
        import datetime as _dt

        period = request.args.get('period', 'month')
        today = _dt.date.today()
        date_arg = request.args.get('date') or ''
        try:
            anchor = _dt.date.fromisoformat(date_arg) if date_arg else today
        except Exception:
            anchor = today

        if period == 'day':
            from_date = anchor
            to_date = anchor
        elif period == 'week':
            from_date = anchor - _dt.timedelta(days=anchor.weekday())  # Monday
            to_date = from_date + _dt.timedelta(days=6)
        elif period == 'month':
            from_date = anchor.replace(day=1)
            if from_date.month == 12:
                to_date = from_date.replace(year=from_date.year + 1, month=1, day=1) - _dt.timedelta(days=1)
            else:
                to_date = from_date.replace(month=from_date.month + 1, day=1) - _dt.timedelta(days=1)
        elif period == 'year':
            from_date = anchor.replace(month=1, day=1)
            to_date = anchor.replace(month=12, day=31)
        else:
            from_date = None  # all time
            to_date = None

        total_client = 0
        total_nanny = 0
        total_margin = 0
        total_hours = 0
        shifts_done = 0
        shifts_pending = 0
        source_leads = 0
        source_shifts = 0
        daily = {}  # date -> {client, nanny, margin, hours}

        def _in_period(date_str: str | None) -> bool:
            if not date_str:
                return False
            if not from_date:
                return True
            try:
                value = _dt.date.fromisoformat(str(date_str)[:10])
            except Exception:
                return False
            return from_date <= value <= to_date

        def _add_row(date_str: str, client_amt, nanny_amt, hours, is_done: bool):
            nonlocal total_client, total_nanny, total_margin, total_hours, shifts_done, shifts_pending
            if client_amt is None or nanny_amt is None:
                shifts_pending += 1
                return
            margin = client_amt - nanny_amt
            total_client += client_amt
            total_nanny += nanny_amt
            total_margin += margin
            total_hours += hours or 0
            if is_done:
                shifts_done += 1
            else:
                shifts_pending += 1
            d = date_str
            if d not in daily:
                daily[d] = {'date': d, 'client': 0, 'nanny': 0, 'margin': 0, 'hours': 0, 'shifts': 0}
            daily[d]['client'] += client_amt
            daily[d]['nanny'] += nanny_amt
            daily[d]['margin'] += margin
            daily[d]['hours'] += hours or 0
            daily[d]['shifts'] += 1

        for lead in load_leads():
            for d, info in (lead.get('work_dates') or {}).items():
                if not _in_period(d):
                    continue
                slot = info if isinstance(info, dict) else {}
                if slot.get('status') == 'cancelled':
                    continue
                finance = _lead_slot_finance(lead, d, slot)
                done = bool(
                    slot.get('status') in ('confirmed', 'resolved')
                    or (slot.get('client_actual_start') and slot.get('client_actual_end') and slot.get('fact_start') and slot.get('fact_end'))
                )
                _add_row(
                    d,
                    finance.get('client_total_vnd'),
                    finance.get('nanny_total_vnd'),
                    finance.get('hours'),
                    done,
                )
                source_leads += 1

        if use_sql:
            q = Shift.query
            if from_date:
                q = q.filter(Shift.date >= from_date.isoformat(), Shift.date <= to_date.isoformat())
            for s in q.order_by(Shift.date.asc()).all():
                if s.status == 'cancelled' or not _in_period(s.date):
                    continue
                start = s.resolved_start or s.client_actual_start or s.nanny_actual_start or s.planned_start
                end = s.resolved_end or s.client_actual_end or s.nanny_actual_end or s.planned_end
                if not (s.date and start and end):
                    shifts_pending += 1
                    source_shifts += 1
                    continue
                cr = s.client_rate_per_hour or DEFAULT_CLIENT_RATE_VND
                nr = s.nanny_rate_per_hour or DEFAULT_NANNY_RATE_VND
                client_amt = compute_amount_vnd(s.date, start, end, cr)
                nanny_amt = compute_amount_vnd(s.date, start, end, nr)
                done = bool(s.status in ('confirmed', 'resolved') or s.resolved_start or (s.client_actual_start and s.nanny_actual_start))
                _add_row(s.date, client_amt, nanny_amt, _minutes_between(start, end), done)
                source_shifts += 1

        daily_list = sorted(daily.values(), key=lambda x: x['date'])

        return jsonify({
            'ok': True,
            'period': period,
            'from_date': from_date.isoformat() if from_date else None,
            'to_date': to_date.isoformat() if to_date else None,
            'summary': {
                'client_total': round(total_client),
                'nanny_total': round(total_nanny),
                'margin': round(total_margin),
                'hours': round(total_hours, 1),
                'shifts_done': shifts_done,
                'shifts_pending': shifts_pending,
                'source_leads': source_leads,
                'source_shifts': source_shifts,
            },
            'daily': daily_list,
        })

    @app.route('/api/nanny/me/earnings')
    def api_nanny_me_earnings():
        """Return nanny's personal earnings stats.
        Query params: period = day | week | month | all
        """
        if not use_sql:
            return jsonify({'error': 'SQL mode required'}), 400
        from time_utils import compute_amount_vnd
        import datetime as _dt

        try:
            tid = _require_telegram_session()
        except PermissionError:
            return jsonify({'error': 'auth required'}), 401

        nanny = Nanny.query.filter_by(telegram_user_id=tid).first()
        if not nanny:
            return jsonify({'error': 'nanny not linked'}), 403

        period = request.args.get('period', 'month')
        today = _dt.date.today()
        if period == 'day':
            from_date = today
        elif period == 'week':
            from_date = today - _dt.timedelta(days=today.weekday())
        elif period == 'month':
            from_date = today.replace(day=1)
        else:
            from_date = None

        q = Shift.query.filter_by(nanny_id=nanny.id)
        if from_date:
            q = q.filter(Shift.date >= from_date.isoformat())
        shifts = q.order_by(Shift.date.asc()).all()

        total_earned = 0
        total_hours = 0
        shifts_done = 0
        shifts_upcoming = 0
        daily = {}

        def _to_min(t):
            if not t: return 0
            parts = t.split(':')
            return int(parts[0])*60 + (int(parts[1]) if len(parts)>1 else 0)

        for s in shifts:
            start = s.nanny_actual_start or s.planned_start
            end = s.nanny_actual_end or s.planned_end
            if not (s.date and start and end):
                shifts_upcoming += 1
                continue
            try:
                nr = s.nanny_rate_per_hour or DEFAULT_NANNY_RATE_VND
                earned = compute_amount_vnd(s.date, start, end, nr)
            except Exception:
                shifts_upcoming += 1
                continue

            hours = max(0, _to_min(end) - _to_min(start)) / 60.0
            total_earned += earned
            total_hours += hours
            shifts_done += 1

            d = s.date
            if d not in daily:
                daily[d] = {'date': d, 'earned': 0, 'hours': 0, 'shifts': 0}
            daily[d]['earned'] += earned
            daily[d]['hours'] += hours
            daily[d]['shifts'] += 1

        daily_list = sorted(daily.values(), key=lambda x: x['date'])

        return jsonify({
            'ok': True,
            'period': period,
            'summary': {
                'earned': round(total_earned),
                'hours': round(total_hours, 1),
                'shifts_done': shifts_done,
                'shifts_upcoming': shifts_upcoming,
                'avg_per_shift': round(total_earned / shifts_done) if shifts_done else 0,
            },
            'daily': daily_list,
        })





    return app


if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)),
            debug=os.environ.get('FLASK_DEBUG') == '1')
