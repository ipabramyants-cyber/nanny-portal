// Telegram WebApp — авторизация при открытии Mini App
(function () {
  if (!window.Telegram || !window.Telegram.WebApp) {
    window.__TG_AUTH__ = { ok: false, error: 'Telegram WebApp не найден' };
    _showTgError('Откройте приложение через бота в Telegram');
    return;
  }

  var tg = window.Telegram.WebApp;
  tg.ready();
  tg.expand();

  var initData = tg.initData || '';
  if (!initData) {
    window.__TG_AUTH__ = { ok: false, error: 'initData пустой — откройте через бота' };
    _showTgError('Пожалуйста, откройте приложение через бота @nanny_nya_trang_bot');
    return;
  }

  // Авторизуемся на сервере
  var _retries = 0;
  function _doAuth() {
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
          _hideTgError();
        } else {
          window.__TG_AUTH__ = { ok: false, error: (res.data && res.data.error) || 'Ошибка авторизации' };
          _showTgError((res.data && res.data.error) || 'Ошибка авторизации', true);
        }
      })
      .catch(function (e) {
        window.__TG_AUTH__ = { ok: false, error: 'Сеть: ' + e.message };
        if (_retries < 2) {
          _retries++;
          setTimeout(_doAuth, 2000); // retry after 2s
        } else {
          _showTgError('Нет соединения с сервером. Попробуйте позже.', true);
        }
      });
  }

  _doAuth();

  function _showTgError(msg, showRetry) {
    var existing = document.getElementById('__tg_auth_error__');
    if (existing) existing.remove();
    var el = document.createElement('div');
    el.id = '__tg_auth_error__';
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(255,255,255,.97);z-index:99999;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;text-align:center;font-family:sans-serif;';
    el.innerHTML = '<div style="font-size:48px;margin-bottom:16px;">⚠️</div>' +
      '<div style="font-size:16px;color:#333;margin-bottom:24px;max-width:280px;">' + msg + '</div>' +
      (showRetry ? '<button onclick="location.reload()" style="background:#016b82;color:#fff;border:none;padding:12px 28px;border-radius:24px;font-size:15px;cursor:pointer;">Повторить</button>' : '');
    document.body.appendChild(el);
  }

  function _hideTgError() {
    var el = document.getElementById('__tg_auth_error__');
    if (el) el.remove();
  }
})();
