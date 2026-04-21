// Splash loader
(function() {
  window.addEventListener('load', function() {
    var splash = document.getElementById('splash');
    if (splash) {
      setTimeout(function() {
        splash.style.opacity = '0';
        splash.style.transition = 'opacity 0.4s';
        setTimeout(function() { splash.style.display = 'none'; }, 400);
      }, 600);
    }
  });
})();
