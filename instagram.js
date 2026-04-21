(function () {
  const grid = document.getElementById('igGrid');
  if (!grid) return;

  // Demo cards with real beach/nanny photos from static folder
  const items = [
    { img: '/static/img/baby-blue-sunglasses.jpg',   caption: 'Малышка на пляже ☀️' },
    { img: '/static/img/baby-floatie-pool.jpg',      caption: 'Плавание с поплавком 🏊' },
    { img: '/static/img/boy-lemonade-beach.jpg',     caption: 'Лимонад на море 🍋' },
    { img: '/static/img/girl-pink-glasses-pool.jpg', caption: 'Бассейн — лучший друг 💦' },
    { img: '/static/img/girl-sunscreen-beach.jpg',   caption: 'Защита от солнца 🧴' },
    { img: '/static/img/mom-daughter-beach.jpg',     caption: 'Прогулка по берегу 🌊' },
  ];

  grid.innerHTML = '';
  items.forEach((item) => {
    const a = document.createElement('a');
    a.href = 'https://www.instagram.com/nanny_nya_trang/';
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.className = 'ig-card';
    a.setAttribute('aria-label', item.caption);

    const img = document.createElement('img');
    img.src = item.img;
    img.alt = item.caption;
    img.loading = 'lazy';
    img.onerror = function() {
      this.parentElement.style.display = 'none';
    };

    const body = document.createElement('div');
    body.className = 'ig-body';
    body.textContent = item.caption;

    a.appendChild(img);
    a.appendChild(body);
    grid.appendChild(a);
  });
})();
