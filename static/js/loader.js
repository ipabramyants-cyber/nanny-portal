// Splash loader: short branded intro with a safe fallback timeout.
(function () {
  var splash = document.getElementById('splash');
  if (!splash) return;

  var hidden = false;
  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var minDelay = reduceMotion ? 80 : 320;
  var maxDelay = reduceMotion ? 420 : 1200;

  function hideSplash() {
    if (hidden) return;
    if (splash.dataset.keepVisible === '1') {
      setTimeout(hideSplash, 180);
      return;
    }
    hidden = true;
    splash.classList.add('hide');
    setTimeout(function () {
      splash.style.display = 'none';
      splash.setAttribute('aria-hidden', 'true');
    }, reduceMotion ? 60 : 220);
  }

  setTimeout(function () {
    if (document.readyState !== 'loading') {
      hideSplash();
    } else {
      document.addEventListener('DOMContentLoaded', hideSplash, { once: true });
    }
  }, minDelay);

  setTimeout(hideSplash, maxDelay);
})();
