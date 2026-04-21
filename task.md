# Task: Audit & Fix нanny-portal

## Done
- [x] Cloned repo, ran app locally
- [x] Tested all routes (200/302/400/404)
- [x] Audited security, SEO, design, functionality, Mini App
- [x] Fixed: CSP header + frame-ancestors for Telegram
- [x] Fixed: HSTS header in production
- [x] Fixed: FLASK_SECRET_KEY warning/error in production
- [x] Fixed: /cron/remind_2h — blocked without CRON_SECRET
- [x] Fixed: sitemap/robots.txt — use SITE_URL env var
- [x] Written AUDIT.md with top-10 priorities

## In Progress
- [ ] Fix Schema.org ratingValue calculation
- [ ] Add date validation on server in /api/lead
- [ ] Add lazy loading to nanny images
- [ ] FAQ Schema markup
- [ ] Add SITE_URL to .env.example
- [ ] Mini App fallback error screen in tg_webapp.js

## Pending
- [ ] CSRF protection (complex, needs flask-wtf)
- [ ] File upload extension validation
- [ ] railway.json with cron config
