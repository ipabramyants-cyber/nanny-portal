# Nanny Portal - Task

## Status: Setting up

## Project
Flask app - "Няня на час" (nanny agency) - Nha Trang, Vietnam
- Main site (index.html)
- Client cabinet (/lk/<token>)
- Nanny portal (/nanny/*)
- Admin panel (/admin)
- Telegram Mini App entry (/app)
- JSON storage (data/) or SQL (via STORAGE=sql + DATABASE_URL)

## Missing files to create
- [x] templates/ (copied)
- [ ] auth_simple.py - require_admin, require_nanny decorators
- [ ] config.py - admin_ids()
- [ ] telegram_notify.py - send_message()
- [ ] telegram_auth.py - validate_webapp_init_data, TelegramAuthError
- [ ] models.py - db, Nanny, Lead, User, Shift, Client, NannyBlock
- [ ] requirements.txt
- [ ] .env.example
- [ ] run.py / wsgi.py
- [ ] Dockerfile
- [ ] static/img/nanny_placeholder.jpg

## Todo
1. Create missing python modules
2. Install deps
3. Create .env with dummy values
4. Launch & test
5. Fix any bugs
6. Write deployment guide
