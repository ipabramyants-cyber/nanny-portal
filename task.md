# Task: Fix all UX/Design/Security issues in nanny-portal

## Status: IN PROGRESS

## Priority fixes:

### 🔴 CRITICAL
- [x] Audit done
- [ ] Calendar: block past dates (JS + CSS)
- [ ] Calendar: fix .cal-time 9px → 11px bold
- [ ] Calendar: fix prev/next button contrast (2.45:1 → 4.5+)
- [ ] Calendar: animate month switch (slide/fade)
- [ ] Calendar: add legend (colors meaning)
- [ ] Calendar: add "Today" button
- [ ] Calendar: right-click delete animation (flash/shake)

### 🟡 IMPORTANT
- [ ] /nanny/login page — proper design with instructions
- [ ] /client/app page — proper design with instructions
- [ ] FAQ Schema.org markup
- [ ] Blog: add loading="lazy" to images
- [ ] Heading hierarchy: h1→h3 fix (add h2)
- [ ] Type scale: reduce 16 sizes → 7
- [ ] Font families: remove Georgia
- [ ] Mobile: hamburger menu
- [ ] Reviews carousel: add prev/next arrows
- [ ] Contrast fixes for .hint text and h3

### 🟢 POLISH
- [ ] Success screen confetti/emoji
- [ ] Today button in calendar
- [ ] Swipe gesture for calendar months
- [ ] Skeleton loaders
- [ ] Nanny profile back button
- [ ] Empty state for articles
- [ ] Tariffs: move prices to config

## Files to edit:
- static/js/calendar.js
- static/css/style.css
- templates/nanny_login.html
- templates/client_app.html
- templates/faq.html
- templates/base.html
- templates/blog.html
- templates/index.html
