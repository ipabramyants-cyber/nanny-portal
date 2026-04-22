// Service Worker — Няня на час
// Cache-first for static assets, network-first for HTML pages

const CACHE = 'nanny-v1';
const STATIC = [
  '/static/css/style.css',
  '/static/js/calendar.js',
  '/static/js/loader.js',
  '/static/js/tg_webapp.js',
  '/static/img/logo.png',
  '/static/img/logo.webp',
  '/static/img/nanny_placeholder.webp',
  '/static/site.webmanifest',
  '/offline.html',
];

// Install: pre-cache static assets
self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(STATIC)).then(() => self.skipWaiting())
  );
});

// Activate: clean old caches
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Fetch strategy:
// - /static/*  → cache-first
// - /uploads/* → cache-first
// - HTML pages → network-first with offline fallback
// - /api/*     → network only (never cache)
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // Skip non-GET and cross-origin
  if (e.request.method !== 'GET') return;
  if (url.origin !== location.origin) return;

  // API — always network
  if (url.pathname.startsWith('/api/')) return;

  // Static/uploads — cache first
  if (url.pathname.startsWith('/static/') || url.pathname.startsWith('/uploads/')) {
    e.respondWith(
      caches.match(e.request).then((cached) => {
        if (cached) return cached;
        return fetch(e.request).then((res) => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(CACHE).then((c) => c.put(e.request, clone));
          }
          return res;
        });
      })
    );
    return;
  }

  // HTML pages — network first, offline fallback
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok) {
          const clone = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, clone));
        }
        return res;
      })
      .catch(() =>
        caches.match(e.request).then((cached) => cached || caches.match('/offline.html'))
      )
  );
});
