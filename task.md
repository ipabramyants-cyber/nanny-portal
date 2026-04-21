# Nanny App — Current Sprint

## Status
- [x] app.py syntax fixed (line 2104)
- [x] DEFAULT_CLIENT_RATE_VND / DEFAULT_NANNY_RATE_VND constants added inside create_app()
- [x] Syntax verified OK

## TODO
- [ ] faq.html — create with FAQPage schema.org
- [ ] admin_simple.html — inline rate editor per lead (JS fetch /api/admin/lead/<token>/rates), calculator block
- [ ] client_portal.html — client calculator (hours × client_rate)
- [ ] nanny_portal_public.html — nanny earnings calculator (hours × nanny_rate)
- [ ] index.html — tariffs section with <picture> tag
- [ ] git commit + push

## Notes
- lead dict has client_rate_per_hour, nanny_rate_per_hour (int VND)
- work_dates is dict keyed by ISO date with {start, end, ...} per date
- Admin sees nanny_rate + margin. Client/nanny see only their own rate.
- rates API: POST /api/admin/lead/<token>/rates {client_rate_per_hour, nanny_rate_per_hour}
