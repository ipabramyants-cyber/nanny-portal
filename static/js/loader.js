// Splash loader: short branded intro with a safe fallback timeout.
(function () {
  var splash = document.getElementById('splash');
  if (!splash) return;

  var hidden = false;
  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var minDelay = reduceMotion ? 120 : 950;
  var maxDelay = reduceMotion ? 800 : 2800;

  function hideSplash() {
    if (hidden) return;
    hidden = true;
    splash.classList.add('hide');
    setTimeout(function () {
      splash.style.display = 'none';
      splash.setAttribute('aria-hidden', 'true');
    }, reduceMotion ? 80 : 460);
  }

  setTimeout(function () {
    if (document.readyState === 'complete') {
      hideSplash();
    } else {
      window.addEventListener('load', hideSplash, { once: true });
    }
  }, minDelay);

  setTimeout(hideSplash, maxDelay);
})();
