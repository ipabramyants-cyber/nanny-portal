// Splash loader — показывает анимацию логотипа при загрузке
(function () {
  var splash = document.getElementById('splash');
  if (!splash) return;

  function hideSplash() {
    splash.classList.add('hide');
    setTimeout(function () {
      splash.style.display = 'none';
    }, 400);
  }

  // Минимум 1.4 сек показа анимации, потом ждём load
  var minTimer = setTimeout(function () {
    if (document.readyState === 'complete') {
      hideSplash();
    } else {
      window.addEventListener('load', hideSplash, { once: true });
    }
  }, 1400);

  window.addEventListener('load', function () {
    // Если страница загрузилась раньше — ждём минимальный таймер
    // (minTimer сам вызовет hideSplash)
  }, { once: true });
})();
