#!/usr/bin/env python3
"""
migrate_to_pg.py — однократная миграция JSON-данных в PostgreSQL.

Использование:
    DATABASE_URL=postgresql://... python3 migrate_to_pg.py

Безопасно запускать повторно: пропускает уже существующие записи.
"""
import os, json, sys, datetime, secrets

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    print('ERROR: DATABASE_URL не задан')
    sys.exit(1)

# Railway отдаёт URL с postgres:// — SQLAlchemy 1.4+ требует postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

os.environ['DATABASE_URL'] = DATABASE_URL
os.environ['STORAGE'] = 'sql'

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, 'data')

from models import db, Nanny, Lead
from flask import Flask

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

with app.app_context():
    print('Создаём таблицы если не существуют...')
    db.create_all()
    print('OK')

    # ── Нянь ──────────────────────────────────────────────────
    nannies_file = os.path.join(DATA_DIR, 'nannies.json')
    if os.path.exists(nannies_file):
        with open(nannies_file) as f:
            nannies_raw = json.load(f)
        print(f'\nМигрируем нянь: {len(nannies_raw)} записей...')
        migrated = 0
        for n in nannies_raw:
            token = n.get('portal_token') or secrets.token_urlsafe(12)
            existing = Nanny.query.filter_by(portal_token=token).first()
            if existing:
                print(f'  skip (уже есть): {n.get("name")} / {token}')
                continue
            tg_id = n.get('telegram_user_id')
            if tg_id:
                try:
                    tg_id = int(str(tg_id).strip())
                except Exception:
                    tg_id = None
            age = n.get('age')
            if age is not None:
                age = str(age)
            nanny = Nanny(
                name=str(n.get('name') or 'Без имени'),
                age=age,
                exp_short=n.get('exp_short') or '',
                bio=n.get('bio') or '',
                photo=n.get('photo') or '',
                telegram_user_id=tg_id,
                portal_token=token,
                is_active=n.get('status', 'active') != 'inactive',
            )
            db.session.add(nanny)
            migrated += 1
            print(f'  + {nanny.name}')
        db.session.commit()
        print(f'Нянь добавлено: {migrated}')
    else:
        print('nannies.json не найден — пропускаем')

    # ── Лиды ──────────────────────────────────────────────────
    leads_file = os.path.join(DATA_DIR, 'leads.json')
    if os.path.exists(leads_file):
        with open(leads_file) as f:
            leads_raw = json.load(f)
        print(f'\nМигрируем лиды: {len(leads_raw)} записей...')
        migrated = 0
        for l in leads_raw:
            token = l.get('token')
            if not token:
                continue
            if Lead.query.filter_by(token=token).first():
                print(f'  skip (уже есть): {token}')
                continue

            # resolve assigned_nanny_id by portal_token
            nanny_id = None
            assigned_tok = l.get('assigned_nanny_id')
            if assigned_tok:
                nanny = Nanny.query.filter_by(portal_token=str(assigned_tok)).first()
                if not nanny:
                    # try by name (old JSON used string id like 'anna')
                    nanny = Nanny.query.filter(
                        Nanny.portal_token.like(f'%{assigned_tok}%')
                    ).first()
                if nanny:
                    nanny_id = nanny.id

            submitted_raw = l.get('submitted_at')
            try:
                submitted_at = datetime.datetime.fromisoformat(submitted_raw) if submitted_raw else datetime.datetime.utcnow()
            except Exception:
                submitted_at = datetime.datetime.utcnow()

            lead = Lead(
                token=token,
                parent_name=l.get('parent_name') or '',
                phone=l.get('phone') or '',
                telegram=l.get('telegram') or '',
                child_name=l.get('child_name') or '',
                child_age=l.get('child_age') or '',
                notes=l.get('notes') or '',
                status=l.get('status') or 'new',
                work_dates=l.get('work_dates') or {},
                meeting_date=l.get('meeting_date'),
                documents=l.get('documents') or {'receipts': {}},
                assigned_nanny_id=nanny_id,
                telegram_user_id=l.get('telegram_user_id'),
                submitted_at=submitted_at,
            )
            db.session.add(lead)
            migrated += 1
            print(f'  + {lead.parent_name or lead.token}')
        db.session.commit()
        print(f'Лидов добавлено: {migrated}')
    else:
        print('leads.json не найден — пропускаем')

    # ── Итог ──────────────────────────────────────────────────
    print(f'\n=== Готово ===')
    print(f'Нянь в БД:  {Nanny.query.count()}')
    print(f'Лидов в БД: {Lead.query.count()}')
