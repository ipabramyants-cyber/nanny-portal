// Telegram WebApp — авторизация при открытии Mini App
(function () {
  if (!window.Telegram || !window.Telegram.WebApp) {
    window.__TG_AUTH__ = { ok: false, error: 'Telegram WebApp не найден' };
    return;
  }

  var tg = window.Telegram.WebApp;
  tg.ready();
  tg.expand();

  var initData = tg.initData || '';
  if (!initData) {
    window.__TG_AUTH__ = { ok: false, error: 'initData пустой — откройте через бота' };
    return;
  }

  // Авторизуемся на сервере
  fetch('/api/auth/telegram', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ init_data: initData }),
    credentials: 'include',
  })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
    .then(function (res) {
      if (res.ok) {
        window.__TG_AUTH__ = Object.assign({ ok: true }, res.data);
      } else {
        window.__TG_AUTH__ = { ok: false, error: (res.data && res.data.error) || 'Ошибка авторизации' };
      }
    })
    .catch(function (e) {
      window.__TG_AUTH__ = { ok: false, error: 'Сеть: ' + e.message };
    });
})();
