import os
import json
import datetime
import secrets
import time
import re
import hashlib

from flask import Flask, render_template, request, send_from_directory, redirect, url_for, flash, session, Response, jsonify
from werkzeug.utils import secure_filename

from auth_simple import require_admin, require_nanny
from config import admin_ids
from telegram_notify import send_message
from telegram_auth import validate_webapp_init_data, TelegramAuthError

from models import db, Nanny, Lead, User, Shift, Client, NannyBlock


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
    app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev_secret_change_me')

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

    BASE_DIR = os.path.dirname(__file__)

    # Default hourly rates (VND) — can be overridden per lead
    DEFAULT_CLIENT_RATE_VND = 130_000
    DEFAULT_NANNY_RATE_VND  = 110_000

    app.config['DATA_DIR'] = os.path.join(BASE_DIR, 'data')
    app.config['UPLOAD_DIR'] = os.path.join(BASE_DIR, 'uploads')
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

    os.makedirs(app.config['DATA_DIR'], exist_ok=True)
    os.makedirs(app.config['UPLOAD_DIR'], exist_ok=True)

    app.jinja_env.globals['current_year'] = datetime.datetime.utcnow().year
    # Cache-busting version for static assets (update on deploy)
    _static_ver = os.environ.get('APP_VERSION') or str(int(time.time() // 86400))
    app.jinja_env.globals['static_ver'] = _static_ver

    def nanny_photo_src(photo: str | None) -> str:
        if not photo:
            return url_for('static', filename='img/nanny_placeholder.jpg')
        photo = str(photo)
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

    @app.after_request
    def security_headers(resp):
        # Prevent MIME sniffing
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        # Allow framing only from same origin (admin panel, etc.)
        # Telegram Mini App embeds via t.me — don't block that
        # resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        # Referrer policy
        resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        # Permissions policy — disable unused features
        resp.headers.setdefault('Permissions-Policy', 'geolocation=(), microphone=(), camera=()')
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
    ASSIGNMENTS_FILE = os.path.join(app.config['DATA_DIR'], 'assignments.json')
    RECEIPTS_FILE = os.path.join(app.config['DATA_DIR'], 'receipts.json')

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

    def _safe_send_message(chat_id: int | str | None, text: str) -> bool:
        if not chat_id:
            return False
        try:
            send_message(chat_id, text)
            return True
        except Exception as e:
            try:
                app.logger.warning("Telegram notify failed for %s: %s", chat_id, e)
            except Exception:
                pass
            return False

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
        return [
            {
                'id': 'rev-anna',
                'author': 'Анна',
                'role': 'Мама Миши, 3 года',
                'stars': 5,
                'text': 'Нужно было срочно на пару часов — оформила заявку, и уже через 10 минут мне подтвердили няню. Очень бережно к ребёнку, будем обращаться ещё.',
                'created_at': datetime.datetime.utcnow().isoformat(),
            },
            {
                'id': 'rev-igor',
                'author': 'Игорь',
                'role': 'Папа Софии, 4 года',
                'stars': 5,
                'text': 'Понравилось, что можно выбрать даты и время прямо в календаре. Админ быстро всё согласовал, няня приехала вовремя, всё аккуратно и спокойно.',
                'created_at': datetime.datetime.utcnow().isoformat(),
            },
            {
                'id': 'rev-marina',
                'author': 'Марина',
                'role': 'Мама Лёвы, 1 год',
                'stars': 5,
                'text': 'У нас малыш 1 год — переживали. Няня сразу нашла подход, помогла с режимом, поиграла и уложила спать без слёз. Спасибо за сервис!',
                'created_at': datetime.datetime.utcnow().isoformat(),
            },
        ]

    def load_reviews():
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
            }
            if review['id'] != item.get('id') or review['author'] != item.get('author') or review['role'] != item.get('role') or review['stars'] != item.get('stars'):
                changed = True
            reviews.append(review)

        reviews.sort(key=lambda x: x.get('created_at') or '', reverse=True)
        if changed:
            _write_json(REVIEWS_FILE, reviews)
        return reviews

    def save_reviews(reviews: list[dict]):
        _write_json(REVIEWS_FILE, reviews)

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
        reviews = load_reviews()
        return render_template('index.html', nannies_preview=nannies_preview, reviews=reviews)

    
    @app.route('/app')
    def tg_entry():
        # Canonical entry for Telegram Mini App (BotFather URL should point here)
        return render_template('tg_entry.html')

    @app.route('/nanny/login')
    def nanny_login():
        return render_template('nanny_login.html')

    @app.route('/admin/login')
    def admin_login():
        return render_template('admin_login.html')

    @app.route('/nanny/app')
    @require_nanny
    def nanny_app():
        return render_template('nanny_app.html')

    @app.route('/nanny')
    @require_nanny
    def nanny_home():
        return render_template('nanny_app.html')


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

        # Upsert user in DB (only in SQL mode)
        # NOTE: Role assignment is minimal for now.
        role = 'admin' if telegram_user_id in admin_ids() else 'client'

        # If this Telegram user is linked as nanny, assign nanny role
        if role != 'admin':
            if use_sql:
                try:
                    n = Nanny.query.filter_by(telegram_user_id=telegram_user_id).first()
                    if n:
                        role = 'nanny'
                except Exception:
                    pass
            else:
                # JSON mode: check nannies.json
                nannies_list = _read_json(NANNIES_FILE, [])
                for _n in nannies_list:
                    if str(_n.get('telegram_user_id') or '') == str(telegram_user_id):
                        role = 'nanny'
                        break
        if use_sql:
            u = User.query.filter_by(telegram_user_id=telegram_user_id).first()
            if not u:
                u = User(telegram_user_id=telegram_user_id, role=role)
                db.session.add(u)
            u.username = user_obj.get('username')
            u.display_name = (user_obj.get('first_name') or '')
            # keep role=admin if allowlisted
            if role == 'admin':
                u.role = 'admin'
            db.session.commit()

        auth_token = _make_auth_token(role, telegram_user_id)

        # For client role: find their LK token if they already have a lead
        lk_url = None
        if role == 'client':
            leads_list = _read_json(LEADS_FILE, [])
            tg_id_str = str(telegram_user_id)
            tg_username = user_obj.get('username', '')
            for lead in leads_list:
                lead_tg = str(lead.get('telegram_user_id') or '')
                lead_uname = str(lead.get('telegram_username') or '').lstrip('@')
                if (lead_tg and lead_tg == tg_id_str) or \
                   (tg_username and lead_uname and lead_uname.lower() == tg_username.lower()):
                    # /client/<token> is the correct route
                    if lead.get('token'):
                        lk_url = f"/client/{lead['token']}"
                        break

        return {
            'ok': True,
            'telegram_user_id': telegram_user_id,
            'telegram_username': user_obj.get('username'),
            'telegram_display_name': (user_obj.get('first_name') or ''),
            'role': role,
            'auth_token': auth_token,
            'lk_url': lk_url,
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
        note = (data.get('note') or '').strip() or None

        # Basic validation: date must be YYYY-MM-DD
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
            return {'error': 'invalid date'}, 400
        if (start and not re.match(r'^\d{2}:\d{2}$', start)) or (end and not re.match(r'^\d{2}:\d{2}$', end)):
            return {'error': 'invalid time'}, 400

        b = NannyBlock(nanny_id=nanny.id, date=date, start=start, end=end, note=note, kind='dayoff')
        db.session.add(b)
        db.session.commit()
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
        note = (data.get('note') or '').strip() or None

        if not actual_start or not actual_end:
            return {'error': 'Заполните начало и конец'}, 400

        s.nanny_actual_start = actual_start
        s.nanny_actual_end = actual_end
        s.nanny_actual_note = note
        s.status = 'waiting_client'
        db.session.commit()

        # Notify client if we know their telegram_user_id
        client = Client.query.get(s.client_id) if s.client_id else None
        if client and client.telegram_user_id:
            try:
                send_message(
                    int(client.telegram_user_id),
                    f"✅ Няня отправила факт по смене {s.date} {s.planned_start or ''}-{s.planned_end or ''}.\nПожалуйста, подтвердите в приложении."
                )
            except Exception:
                pass

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
        note = (data.get('note') or '').strip() or None
        if not actual_start or not actual_end:
            return {'error': 'Заполните начало и конец'}, 400

        s.client_actual_start = actual_start
        s.client_actual_end = actual_end

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
                send_message(int(nanny.telegram_user_id), f"📝 Клиент отправил факт по смене {s.date}. Итог зафиксирует админ.")
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

    def _notify_admins(text: str):
        ids = admin_ids()
        if not ids:
            return
        for tid in ids:
            _safe_send_message(tid, text)

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
        parent_name = (data.get('parent_name') or '').strip()[:100]
        telegram = (data.get('telegram') or '').strip()[:100]
        child_name = (data.get('child_name') or '').strip()[:100]
        child_age = (data.get('child_age') or '').strip()[:20]
        notes = (data.get('notes') or '').strip()[:1000]
        meeting_date_raw = data.get('meeting_date')
        work_dates_raw = data.get('work_dates') or {}

        if not parent_name or not telegram or not child_name or not child_age:
            return {'error': 'Заполните обязательные поля.'}, 400

        # Validate meeting_date
        _d_re = r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
        meeting_date = meeting_date_raw if (
            isinstance(meeting_date_raw, str) and re.match(_d_re, meeting_date_raw)
        ) else None

        # Validate work_dates: YYYY-MM-DD keys only, max 60 entries
        def _val_work_dates(raw):
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

        work_dates = _val_work_dates(work_dates_raw)
        token = secrets.token_urlsafe(16)

        if use_sql:
            db.session.add(
                Lead(
                    token=token,
                    parent_name=parent_name,
                    telegram=telegram,
                    child_name=child_name,
                    child_age=child_age,
                    notes=notes,
                    meeting_date=meeting_date,
                    work_dates=work_dates or {},
                    documents={'receipts': {}},
                )
            )
            db.session.commit()
        else:
            lead = {
                'token': token,
                'parent_name': parent_name,
                'telegram': telegram,
                'child_name': child_name,
                'child_age': child_age,
                'notes': notes,
                'meeting_date': meeting_date,
                'work_dates': work_dates,
                'assigned_nanny_id': None,
                'client_rate_per_hour': DEFAULT_CLIENT_RATE_VND,
                'nanny_rate_per_hour': DEFAULT_NANNY_RATE_VND,
                'submitted_at': datetime.datetime.utcnow().isoformat(),
                'documents': {'receipts': {}},
            }

            leads = load_leads()
            leads.insert(0, lead)
            save_leads(leads)

        lk_url = request.host_url.rstrip('/') + '/client/' + token

        _notify_admins(
            "🆕 Новая заявка\n"
            f"Родитель: {parent_name}\n"
            f"Telegram: {telegram}\n"
            f"Ребёнок: {child_name}, {child_age}\n"
            f"ЛК: {lk_url}"
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
        if lead.get('assigned_nanny_id'):
            nanny = next((n for n in nannies if n.get('id') == lead.get('assigned_nanny_id')), None)
        return render_template('client_portal.html', lead=lead, nanny=nanny)

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
            lead.meeting_date = meeting_date
            lead.work_dates = work_dates
            db.session.commit()
            return {'ok': True}

        leads = load_leads()
        lead = next((x for x in leads if x.get('token') == token), None)
        if not lead:
            return {'error': 'ЛК не найден'}, 404
        lead['meeting_date'] = meeting_date
        lead['work_dates'] = work_dates
        save_leads(leads)
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
        if not safe_name:
            return {'error': 'Недопустимое имя файла'}, 400
        filename = f"{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{safe_name}"
        file.save(os.path.join(app.config['UPLOAD_DIR'], filename))

        if use_sql:
            lead_row = Lead.query.filter_by(token=token).first()
            if not lead_row:
                return {'error': 'ЛК не найден'}, 404
            docs = (lead_row.documents or {})
            docs.setdefault('receipts', {}).setdefault(date_str, []).append(filename)
            lead_row.documents = docs
            db.session.commit()
        else:
            leads = load_leads()
            lead = next((x for x in leads if x.get('token') == token), None)
            if not lead:
                return {'error': 'ЛК не найден'}, 404
            lead.setdefault('documents', {}).setdefault('receipts', {}).setdefault(date_str, []).append(filename)
            save_leads(leads)

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

    @app.route('/nanny/portal/<portal_token>')
    def nanny_portal(portal_token: str):
        nannies = load_nannies()
        nanny = next((n for n in nannies if n.get('portal_token') == portal_token), None)
        if not nanny:
            return 'Ссылка недействительна', 404
        # Set session so blocks API can verify identity
        session['nanny_portal_token'] = portal_token
        session.permanent = True
        leads = load_leads()
        # Clients assigned to this nanny
        clients = [l for l in leads if l.get('assigned_nanny_id') == nanny.get('id')]
        # Build event list with nanny_rate so JS can compute earnings per shift
        events_with_rate = []
        for l in clients:
            rate = l.get('nanny_rate_per_hour') or DEFAULT_NANNY_RATE_VND
            for d, info in (l.get('work_dates') or {}).items():
                events_with_rate.append({
                    'date': d,
                    'child_name': l.get('child_name'),
                    'child_age': l.get('child_age'),
                    'client_token': l.get('token'),
                    'time': (info or {}).get('time') if isinstance(info, dict) else None,
                    'nanny_rate': rate,
                })
        events_with_rate.sort(key=lambda x: x.get('date') or '')
        today = datetime.datetime.utcnow().date().isoformat()
        return render_template('nanny_portal_public.html', nanny=nanny, clients=clients,
                               events=events_with_rate, today=today,
                               default_nanny_rate=DEFAULT_NANNY_RATE_VND)

    @app.route('/nanny/<portal_token>')
    def nanny_profile(portal_token: str):
        """Public nanny profile (opens from the main page cards)."""
        nannies = load_nannies()
        nanny = next((n for n in nannies if n.get('portal_token') == portal_token), None)
        if not nanny:
            return 'Няня не найдена', 404
        return render_template('nanny_profile.html', nanny=nanny)

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
        comment = (request.form.get('comment') or '').strip()
        file = request.files.get('file')
        if not client_token or not date_str or not file or not file.filename:
            return {'error': 'Заполните client_token, date и выберите файл'}, 400

        leads = load_leads()
        lead = next((x for x in leads if x.get('token') == client_token), None)
        if not lead:
            return {'error': 'Клиент не найден (token неверный)'}, 404

        # MIME type validation — only images and PDF allowed
        ALLOWED_MIME_PREFIXES = ('image/jpeg', 'image/png', 'image/gif', 'image/webp', 'application/pdf')
        file_mime = file.mimetype or ''
        if not any(file_mime.startswith(m) for m in ALLOWED_MIME_PREFIXES):
            return {'error': f'Недопустимый тип файла: {file_mime}. Разрешены: изображения и PDF.'}, 400

        safe_name = secure_filename(file.filename)
        if not safe_name:
            return {'error': 'Недопустимое имя файла'}, 400
        filename = f"{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{safe_name}"
        file.save(os.path.join(app.config['UPLOAD_DIR'], filename))

        lead.setdefault('documents', {}).setdefault('receipts', {}).setdefault(date_str, []).append(filename)
        # Optional metadata (comment) without breaking existing receipt list
        lead.setdefault('documents', {}).setdefault('receipt_meta', {}).setdefault(date_str, {})[filename] = {
            'comment': comment,
            'uploaded_at': datetime.datetime.utcnow().isoformat(),
            'nanny_id': nanny.get('id'),
        }
        save_leads(leads)
        return {'ok': True, 'filename': filename}

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
            # No-SQL mode: not supported
            return {'error': 'SQL mode required'}, 400

    @app.route('/api/nanny/<portal_token>/blocks', methods=['POST'])
    def api_nanny_public_blocks_create(portal_token: str):
        if not use_sql:
            return {'error': 'SQL mode required'}, 400

        # Security: caller must be authenticated as this nanny via session
        session_token = session.get('nanny_portal_token') or ''
        if session_token != portal_token:
            return {'error': 'Forbidden'}, 403

        nanny = Nanny.query.filter_by(portal_token=portal_token).first()
        if not nanny:
            return {'error': 'invalid token'}, 404

        data = request.get_json(silent=True) or {}
        date = (data.get('date') or '').strip()
        start = (data.get('start') or '').strip() or None
        end = (data.get('end') or '').strip() or None
        note = (data.get('note') or '').strip() or None

        if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
            return {'error': 'invalid date'}, 400
        if (start and not re.match(r'^\d{2}:\d{2}$', start)) or (end and not re.match(r'^\d{2}:\d{2}$', end)):
            return {'error': 'invalid time'}, 400

        b = NannyBlock(nanny_id=nanny.id, date=date, start=start, end=end, note=note, kind='dayoff')
        db.session.add(b)
        db.session.commit()
        return {'ok': True, 'id': b.id}

    @app.route('/api/nanny/<portal_token>/blocks/<int:block_id>', methods=['DELETE'])
    def api_nanny_public_blocks_delete(portal_token: str, block_id: int):
        if not use_sql:
            return {'error': 'SQL mode required'}, 400

        # Security: caller must be authenticated as this nanny via session
        session_token = session.get('nanny_portal_token') or ''
        if session_token != portal_token:
            return {'error': 'Forbidden'}, 403

        nanny = Nanny.query.filter_by(portal_token=portal_token).first()
        if not nanny:
            return {'error': 'invalid token'}, 404

        b = NannyBlock.query.filter_by(id=block_id, nanny_id=nanny.id, kind='dayoff').first()
        if not b:
            return {'error': 'not found'}, 404

        db.session.delete(b)
        db.session.commit()
        return {'ok': True}



    @app.route('/admin')
    @require_admin
    def admin():
        # Admin panel (protected)
        leads = load_leads()
        nannies = load_nannies()
        # Build calendar events from leads (show BOTH assigned and unassigned).
        # Unassigned leads are still important and should be visible in the calendar.
        events = []
        for l in leads:
            for d, info in (l.get('work_dates') or {}).items():
                events.append({
                    'date': d,
                    'nanny_id': l.get('assigned_nanny_id'),  # may be null
                    'child_name': l.get('child_name'),
                    'child_age': l.get('child_age'),
                    'token': l.get('token'),
                    'time': (info or {}).get('time') if isinstance(info, dict) else None,
                })
        events.sort(key=lambda x: x.get('date') or '')

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
        crm = {
            'leads_total': len(leads),
            'assigned_total': assigned_count,
            'unassigned_total': unassigned_count,
            'work_dates_total': work_dates_total,
            'reviews_total': len(reviews),
            'shifts_total': len(shift_rows),
        }

        return render_template(
            'admin_simple.html',
            leads=leads,
            nannies=nannies,
            events=events,
            clients=clients,
            shifts=shift_rows,
            reviews=reviews,
            crm=crm,
        )

    @app.route('/admin/nanny/save', methods=['POST'])
    @require_admin
    def admin_nanny_save():
        """Create or update a nanny from the admin panel."""
        if use_sql:
            nanny_id_raw = (request.form.get('id') or '').strip() or None
            name = (request.form.get('name') or '').strip()
            exp_short = (request.form.get('exp_short') or '').strip() or None
            bio = (request.form.get('bio') or '').strip() or None
            photo = (request.form.get('photo') or '').strip() or None
            photo_file = request.files.get('photo_file')
            if photo_file and getattr(photo_file, 'filename', ''):
                fn = secure_filename(photo_file.filename)
                if fn:
                    fn = f"nanny_{int(time.time())}_{fn}"
                    photo_file.save(os.path.join(app.config['UPLOAD_DIR'], fn))
                    # store as logical path, rendered by nanny_photo_src()
                    photo = f"uploads/{fn}"
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
        name = (request.form.get('name') or '').strip()
        age = (request.form.get('age') or '').strip()
        exp_short = (request.form.get('exp_short') or '').strip()
        bio = (request.form.get('bio') or '').strip()
        photo = (request.form.get('photo') or '').strip()
        tid_raw_json = (request.form.get('telegram_user_id') or '').strip() or None
        # Photo upload support in JSON mode
        photo_file_json = request.files.get('photo_file')
        if photo_file_json and getattr(photo_file_json, 'filename', ''):
            fn_json = secure_filename(photo_file_json.filename)
            if fn_json:
                fn_json = f"nanny_{int(time.time())}_{fn_json}"
                photo_file_json.save(os.path.join(app.config['UPLOAD_DIR'], fn_json))
                photo = f"uploads/{fn_json}"

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

        parent_name = (request.form.get('parent_name') or '').strip()
        child_name = (request.form.get('child_name') or '').strip() or None
        child_age = (request.form.get('child_age') or '').strip() or None
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
        if nanny and nanny.telegram_user_id:
            _safe_send_message(
                int(nanny.telegram_user_id),
                f"🆕 Новая смена: {date} {planned_start}-{planned_end}.\nОткройте /nanny/app чтобы отправить факт после смены."
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

            client = Client.query.get(client_id)
            if seconds > 0 and seconds < 2 * 3600 and s.reminder_sent_at is None:
                msg = f"⏰ Напоминание: смена через менее чем 2 часа.\n{date} {planned_start}-{planned_end}"
                if client and client.telegram_user_id:
                    try:
                        send_message(int(client.telegram_user_id), msg)
                    except Exception:
                        pass
                if nanny and nanny.telegram_user_id:
                    try:
                        send_message(int(nanny.telegram_user_id), msg)
                    except Exception:
                        pass
                s.reminder_sent_at = datetime.datetime.utcnow()
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
            try:
                send_message(int(nanny.telegram_user_id), text)
            except Exception:
                pass
        if client and client.telegram_user_id:
            try:
                send_message(int(client.telegram_user_id), text)
            except Exception:
                pass

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
                        f"ЛК: {request.host_url.rstrip('/')}/nanny/app"
                    )
                client_chat_id = _extract_chat_id(lead_row.telegram)
                if client_chat_id:
                    _safe_send_message(
                        client_chat_id,
                        "✅ По вашей заявке назначена няня.\n"
                        f"Даты: {dates_text}\n"
                        f"ЛК: {request.host_url.rstrip('/')}/client/{lead_row.token}"
                    )

            flash('Няня назначена', 'success')
            return redirect(url_for('admin'))

        leads = load_leads()
        lead = next((x for x in leads if x.get('token') == token), None)
        if not lead:
            flash('Заявка не найдена', 'error')
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
                    f"ЛК: {request.host_url.rstrip('/')}/nanny/portal/{selected_nanny.get('portal_token')}"
                )
            client_chat_id = _extract_chat_id(lead.get('telegram'))
            if client_chat_id:
                _safe_send_message(
                    client_chat_id,
                    "✅ По вашей заявке назначена няня.\n"
                    f"Даты: {dates_text}\n"
                    f"ЛК: {request.host_url.rstrip('/')}/client/{lead.get('token')}"
                )

        flash('Няня назначена', 'success')
        return redirect(url_for('admin'))

    @app.route('/admin/review/save', methods=['POST'])
    @require_admin
    def admin_review_save():
        reviews = load_reviews()
        review_id = (request.form.get('id') or '').strip()
        author = (request.form.get('author') or '').strip() or 'Родитель'
        role = (request.form.get('role') or '').strip()
        text_value = (request.form.get('text') or '').strip()
        try:
            stars = max(1, min(5, int((request.form.get('stars') or '5').strip())))
        except Exception:
            stars = 5

        if not text_value:
            flash('Текст отзыва обязателен', 'error')
            return redirect(url_for('admin'))

        existing = next((r for r in reviews if r.get('id') == review_id), None) if review_id else None
        if existing:
            existing['author'] = author
            existing['role'] = role
            existing['stars'] = stars
            existing['text'] = text_value
            flash('Отзыв обновлён', 'success')
        else:
            reviews.insert(0, {
                'id': _legacy_token('rev-', f"{author}-{time.time()}"),
                'author': author,
                'role': role,
                'stars': stars,
                'text': text_value,
                'created_at': datetime.datetime.utcnow().isoformat(),
            })
            flash('Отзыв добавлен', 'success')

        save_reviews(reviews)
        return redirect(url_for('admin'))

    @app.route('/admin/review/delete', methods=['POST'])
    @require_admin
    def admin_review_delete():
        review_id = (request.form.get('id') or '').strip()
        reviews = load_reviews()
        new_reviews = [r for r in reviews if r.get('id') != review_id]
        if len(new_reviews) == len(reviews):
            flash('Отзыв не найден', 'error')
        else:
            save_reviews(new_reviews)
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
        if secret:
            provided = request.args.get('secret') or request.headers.get('X-Cron-Secret')
            if provided != secret:
                return {'error': 'forbidden'}, 403

        from zoneinfo import ZoneInfo

        tzname = os.environ.get('SHIFT_TZ') or 'Asia/Ho_Chi_Minh'
        tz = ZoneInfo(tzname)
        now_dt = datetime.datetime.now(tz)

        # Window: shifts starting between (2h) and (2h + 5m)
        win_start = now_dt + datetime.timedelta(hours=2)
        win_end = win_start + datetime.timedelta(minutes=5)

        sent = 0

        # IMPORTANT: avoid scanning the whole table every 5 minutes.
        # Since date/time are stored as strings (YYYY-MM-DD, HH:MM), we can still
        # filter efficiently by (date, planned_start) with proper indexes.
        def _query_window(dt_from: datetime.datetime, dt_to: datetime.datetime):
            date_from = dt_from.date().isoformat()
            date_to = dt_to.date().isoformat()
            t_from = dt_from.strftime('%H:%M')
            t_to = dt_to.strftime('%H:%M')

            base = Shift.query.filter(
                Shift.reminder_sent_at.is_(None),
                Shift.planned_start.isnot(None),
                Shift.status != 'cancelled',
            )

            if date_from == date_to:
                return base.filter(
                    Shift.date == date_from,
                    Shift.planned_start >= t_from,
                    Shift.planned_start < t_to,
                )

            # Window crosses midnight (rare, but possible)
            q1 = base.filter(
                Shift.date == date_from,
                Shift.planned_start >= t_from,
            )
            q2 = base.filter(
                Shift.date == date_to,
                Shift.planned_start < t_to,
            )
            return q1.union_all(q2)

        rows = _query_window(win_start, win_end).all()
        for s in rows:

            nanny = Nanny.query.get(s.nanny_id) if s.nanny_id else None
            client = Client.query.get(s.client_id) if s.client_id else None
            msg = f"⏰ Напоминание: смена через 2 часа.\n{s.date} {s.planned_start}-{s.planned_end or ''}"

            if client and client.telegram_user_id:
                try:
                    send_message(int(client.telegram_user_id), msg)
                except Exception:
                    pass
            if nanny and nanny.telegram_user_id:
                try:
                    send_message(int(nanny.telegram_user_id), msg)
                except Exception:
                    pass

            s.reminder_sent_at = datetime.datetime.utcnow()
            sent += 1

        if sent:
            db.session.commit()

        return {'ok': True, 'sent': sent, 'tz': tzname}

    @app.route('/uploads/<path:filename>')
    def uploads(filename):
        # Whitelist allowed extensions to prevent serving unexpected files
        allowed_exts = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.pdf', '.mp4', '.mov', '.webm'}
        _, ext = os.path.splitext(filename.lower())
        if ext not in allowed_exts:
            return jsonify({'error': 'forbidden'}), 403
        return send_from_directory(app.config['UPLOAD_DIR'], filename)


    # ── ERROR HANDLERS ─────────────────────────────────────────────────────
    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'not found'}), 404
        return render_template('404.html'), 404

    @app.errorhandler(500)
    def server_error(e):
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
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Файл слишком большой. Максимум 16 МБ.'}), 413
        return render_template('404.html'), 413

    @app.errorhandler(429)
    def too_many_requests(e):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Слишком много запросов. Попробуйте позже.'}), 429
        return render_template('404.html'), 429

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
            "Disallow: /api/",
            "Sitemap: " + request.url_root.rstrip('/') + "/sitemap.xml",
        ]
        return Response("\n".join(lines) + "\n", mimetype="text/plain")

    ARTICLES_FILE = os.path.join(app.config['DATA_DIR'], 'articles.json')

    @app.route('/sitemap.xml')
    def sitemap_xml():
        import html as _html
        base = request.url_root.rstrip('/')
        articles = _read_json(ARTICLES_FILE, [])
        nannies_list = load_nannies()
        today = datetime.datetime.utcnow().strftime('%Y-%m-%d')

        entries = [
            {'loc': base + '/', 'priority': '1.0', 'changefreq': 'weekly', 'lastmod': today},
            {'loc': base + '/blog', 'priority': '0.8', 'changefreq': 'weekly', 'lastmod': today},
            {'loc': base + '/faq', 'priority': '0.7', 'changefreq': 'monthly', 'lastmod': today},
        ]
        for a in articles:
            if a.get('published') and a.get('slug'):
                lastmod = (a.get('updated_at') or a.get('created_at') or today)[:10]
                entries.append({
                    'loc': base + '/blog/' + a['slug'],
                    'priority': '0.7',
                    'changefreq': 'monthly',
                    'lastmod': lastmod,
                })
        for n in nannies_list:
            if n.get('portal_token'):
                entries.append({
                    'loc': base + '/nanny/' + _html.escape(str(n['portal_token'])),
                    'priority': '0.6',
                    'changefreq': 'monthly',
                    'lastmod': today,
                })

        xml = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for e in entries:
            xml.append(
                f"<url><loc>{_html.escape(e['loc'])}</loc>"
                f"<lastmod>{e['lastmod']}</lastmod>"
                f"<changefreq>{e['changefreq']}</changefreq>"
                f"<priority>{e['priority']}</priority></url>"
            )
        xml.append("</urlset>")
        return Response("\n".join(xml), mimetype="application/xml")

    # ── ARTICLES (public) ──────────────────────────────────────────────────
    def _articles_published():
        arts = _read_json(ARTICLES_FILE, [])
        return [a for a in arts if a.get('published')]



    @app.route('/api/admin/lead/<token>/rates', methods=['POST'])
    @require_admin
    def api_admin_lead_rates(token: str):
        """Update client_rate_per_hour and nanny_rate_per_hour for a lead."""
        data = request.get_json(force=True) or {}
        try:
            cr = int(data.get('client_rate_per_hour') or 0)
            nr = int(data.get('nanny_rate_per_hour') or 0)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid rates'}), 400
        if cr < 0 or nr < 0:
            return jsonify({'error': 'rates must be non-negative'}), 400

        if use_sql:
            lead_row = Lead.query.filter_by(token=token).first()
            if not lead_row:
                return jsonify({'error': 'not found'}), 404
            if cr: lead_row.client_rate_per_hour = cr
            if nr: lead_row.nanny_rate_per_hour = nr
            db.session.commit()
        else:
            leads = load_leads()
            lead = next((x for x in leads if x.get('token') == token), None)
            if not lead:
                return jsonify({'error': 'not found'}), 404
            if cr: lead['client_rate_per_hour'] = cr
            if nr: lead['nanny_rate_per_hour'] = nr
            save_leads(leads)
        return jsonify({'ok': True})

    @app.route('/faq')
    def faq():
        return render_template('faq.html')

    @app.route('/blog')
    def blog():
        articles = sorted(_articles_published(),
                          key=lambda a: a.get('created_at', ''), reverse=True)
        return render_template('blog.html', articles=articles)

    @app.route('/blog/<slug>')
    def article(slug):
        arts = _read_json(ARTICLES_FILE, [])
        art = next((a for a in arts if a.get('slug') == slug and a.get('published')), None)
        if not art:
            return render_template('404.html'), 404
        # prev/next for navigation
        published = sorted(_articles_published(),
                           key=lambda a: a.get('created_at', ''), reverse=True)
        idx = next((i for i, a in enumerate(published) if a['slug'] == slug), None)
        prev_art = published[idx + 1] if idx is not None and idx + 1 < len(published) else None
        next_art = published[idx - 1] if idx is not None and idx > 0 else None
        return render_template('article.html', art=art, prev_art=prev_art, next_art=next_art)

    # ── ARTICLES API (admin) ────────────────────────────────────────────────
    @app.route('/api/admin/articles', methods=['GET'])
    @require_admin
    def api_articles_list():
        arts = _read_json(ARTICLES_FILE, [])
        arts = sorted(arts, key=lambda a: a.get('created_at', ''), reverse=True)
        return jsonify(arts)

    @app.route('/api/admin/articles', methods=['POST'])
    @require_admin
    def api_article_create():
        data = request.get_json(force=True) or {}
        import uuid as _uuid
        slug = (data.get('slug') or '').strip()
        if not slug:
            # auto-generate from title
            import re as _re
            raw = (data.get('title') or 'article').lower()
            slug = _re.sub(r'[^a-z0-9а-яёa-z]+', '-', raw).strip('-')[:80]
            slug = slug + '-' + str(int(time.time()))[-5:]
        arts = _read_json(ARTICLES_FILE, [])
        # slug uniqueness
        if any(a.get('slug') == slug for a in arts):
            slug = slug + '-' + str(int(time.time()))[-4:]
        now = time.strftime('%Y-%m-%dT%H:%M:%S')
        art = {
            'id': str(_uuid.uuid4()),
            'slug': slug,
            'title': (data.get('title') or '').strip(),
            'excerpt': (data.get('excerpt') or '').strip(),
            'body': _sanitize_html(data.get('body') or ''),
            'cover_url': (data.get('cover_url') or '').strip(),
            'video_url': (data.get('video_url') or '').strip(),
            'video_file': (data.get('video_file') or '').strip(),
            'published': bool(data.get('published', True)),
            'created_at': now,
            'updated_at': now,
            'seo_title': (data.get('seo_title') or '').strip(),
            'seo_description': (data.get('seo_description') or '').strip(),
            'seo_keywords': (data.get('seo_keywords') or '').strip(),
        }
        arts.append(art)
        _write_json(ARTICLES_FILE, arts)
        return jsonify({'ok': True, 'article': art})

    @app.route('/api/admin/articles/<art_id>', methods=['PUT'])
    @require_admin
    def api_article_update(art_id):
        data = request.get_json(force=True) or {}
        arts = _read_json(ARTICLES_FILE, [])
        art = next((a for a in arts if a['id'] == art_id), None)
        if not art:
            return jsonify({'error': 'not found'}), 404
        fields = ['title','excerpt','body','cover_url','video_url','video_file',
                  'published','seo_title','seo_description','seo_keywords','slug']
        for f in fields:
            if f in data:
                if f == 'body':
                    art[f] = _sanitize_html(data[f])
                else:
                    art[f] = data[f]
        art['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        _write_json(ARTICLES_FILE, arts)
        return jsonify({'ok': True, 'article': art})

    @app.route('/api/admin/articles/<art_id>', methods=['DELETE'])
    @require_admin
    def api_article_delete(art_id):
        arts = _read_json(ARTICLES_FILE, [])
        arts = [a for a in arts if a['id'] != art_id]
        _write_json(ARTICLES_FILE, arts)
        return jsonify({'ok': True})

    @app.route('/api/admin/articles/upload-cover', methods=['POST'])
    @require_admin
    def api_article_upload_cover():
        f = request.files.get('file')
        if not f:
            return jsonify({'error': 'no file'}), 400
        import uuid as _uuid
        ext = (f.filename or 'img').rsplit('.', 1)[-1].lower()
        if ext not in ('jpg', 'jpeg', 'png', 'gif', 'webp'):
            return jsonify({'error': 'bad ext'}), 400
        folder = os.path.join(app.config.get('UPLOAD_FOLDER', 'uploads'), 'articles')
        os.makedirs(folder, exist_ok=True)
        fname = str(_uuid.uuid4()) + '.' + ext
        fpath = os.path.join(folder, fname)
        f.save(fpath)
        url = '/uploads/articles/' + fname
        return jsonify({'ok': True, 'url': url})

    @app.route('/api/admin/articles/upload-video', methods=['POST'])
    @require_admin
    def api_article_upload_video():
        f = request.files.get('file')
        if not f:
            return jsonify({'error': 'no file'}), 400
        import uuid as _uuid
        ext = (f.filename or 'vid').rsplit('.', 1)[-1].lower()
        if ext not in ('mp4', 'mov', 'webm', 'avi'):
            return jsonify({'error': 'bad ext'}), 400
        folder = os.path.join(app.config.get('UPLOAD_FOLDER', 'uploads'), 'articles')
        os.makedirs(folder, exist_ok=True)
        fname = str(_uuid.uuid4()) + '.' + ext
        fpath = os.path.join(folder, fname)
        f.save(fpath)
        url = '/uploads/articles/' + fname
        return jsonify({'ok': True, 'url': url})

    # ── PUBLIC API: latest articles (for blocks on index/lk) ───────────────
    @app.route('/api/articles/latest')
    def api_articles_latest():
        arts = sorted(_articles_published(),
                      key=lambda a: a.get('created_at', ''), reverse=True)[:3]
        resp = jsonify([{
            'slug': a['slug'], 'title': a['title'],
            'excerpt': a.get('excerpt',''), 'cover_url': a.get('cover_url',''),
            'created_at': a.get('created_at','')
        } for a in arts])
        resp.headers['Cache-Control'] = 'public, max-age=60, stale-while-revalidate=300'
        return resp

    return app


if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)),
            debug=os.environ.get('FLASK_DEBUG') == '1')
