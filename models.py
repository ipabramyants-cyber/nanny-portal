"""
models.py — SQLAlchemy models (used only when STORAGE=sql or DATABASE_URL is set).
In JSON mode (default) these classes are imported but never instantiated.
"""
import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Nanny(db.Model):
    __tablename__ = 'nannies'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    age = db.Column(db.String(20), nullable=True)
    exp_short = db.Column(db.String(200), nullable=True)
    bio = db.Column(db.Text, nullable=True)
    photo = db.Column(db.String(500), nullable=True)
    telegram_user_id = db.Column(db.BigInteger, nullable=True)
    portal_token = db.Column(db.String(100), unique=True, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)

    shifts = db.relationship('Shift', backref='nanny', lazy='dynamic')
    blocks = db.relationship('NannyBlock', backref='nanny', lazy='dynamic')


class Lead(db.Model):
    __tablename__ = 'leads'

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False)
    parent_name = db.Column(db.String(200), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    email = db.Column(db.String(200), nullable=True)
    telegram = db.Column(db.String(200), nullable=True)   # @username or numeric id
    child_name = db.Column(db.String(200), nullable=True)
    child_age = db.Column(db.String(50), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), default='new', nullable=False)
    work_dates = db.Column(db.JSON, nullable=True, default=dict)
    meeting_date = db.Column(db.String(20), nullable=True)
    documents = db.Column(db.JSON, nullable=True, default=dict)
    assigned_nanny_id = db.Column(db.Integer, db.ForeignKey('nannies.id'), nullable=True)
    telegram_user_id = db.Column(db.BigInteger, nullable=True)
    submitted_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)

    assigned_nanny = db.relationship('Nanny', foreign_keys=[assigned_nanny_id])


class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    telegram_user_id = db.Column(db.BigInteger, unique=True, nullable=False)
    telegram_username = db.Column(db.String(200), nullable=True)
    display_name = db.Column(db.String(200), nullable=True)
    role = db.Column(db.String(50), default='client', nullable=False)  # admin | nanny | client
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)
    last_seen_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)


class Client(db.Model):
    __tablename__ = 'clients'

    id = db.Column(db.Integer, primary_key=True)
    parent_name = db.Column(db.String(200), nullable=False)
    child_name = db.Column(db.String(200), nullable=True)
    child_age = db.Column(db.String(50), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    telegram_user_id = db.Column(db.BigInteger, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)

    shifts = db.relationship('Shift', backref='client', lazy='dynamic')


class Shift(db.Model):
    __tablename__ = 'shifts'

    id = db.Column(db.Integer, primary_key=True)
    nanny_id = db.Column(db.Integer, db.ForeignKey('nannies.id'), nullable=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=True)
    date = db.Column(db.String(10), nullable=False)          # YYYY-MM-DD
    planned_start = db.Column(db.String(5), nullable=True)   # HH:MM
    planned_end = db.Column(db.String(5), nullable=True)     # HH:MM
    status = db.Column(db.String(50), default='assigned', nullable=False)
    # assigned | waiting_client | confirmed | dispute | resolved

    nanny_actual_start = db.Column(db.String(5), nullable=True)
    nanny_actual_end = db.Column(db.String(5), nullable=True)
    client_actual_start = db.Column(db.String(5), nullable=True)
    client_actual_end = db.Column(db.String(5), nullable=True)
    resolved_start = db.Column(db.String(5), nullable=True)
    resolved_end = db.Column(db.String(5), nullable=True)

    nanny_rate_per_hour = db.Column(db.Integer, nullable=True)    # VND per hour
    client_rate_per_hour = db.Column(db.Integer, nullable=True)   # VND per hour

    notes = db.Column(db.Text, nullable=True)
    reminder_sent_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)


class NannyBlock(db.Model):
    __tablename__ = 'nanny_blocks'

    id = db.Column(db.Integer, primary_key=True)
    nanny_id = db.Column(db.Integer, db.ForeignKey('nannies.id'), nullable=False)
    date = db.Column(db.String(10), nullable=False)   # YYYY-MM-DD
    start = db.Column(db.String(5), nullable=True)    # HH:MM
    end = db.Column(db.String(5), nullable=True)      # HH:MM
    note = db.Column(db.String(500), nullable=True)
    kind = db.Column(db.String(50), default='dayoff', nullable=False)  # dayoff | busy
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)
