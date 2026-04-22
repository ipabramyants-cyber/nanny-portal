(function () {
  // Calendar widget:
  // - Choose mode: meeting (single date) or work (multiple dates)
  // - Left click: set date (and optionally time)
  // - Right click / tap remove icon: remove selection
  // - Meeting dates = purple; Work dates = green
  // - Past dates = disabled (grayed, not clickable)
  // - Works both on landing and on client LK pages.

  const calEl = document.getElementById('calendar');
  if (!calEl) return;

  const isLeadPage = !!document.getElementById('leadForm');
  const isClientLK = !!(window.__LK__ && window.__LK__.token);
  const hasControls = !!(document.getElementById('modeMeeting') || document.getElementById('modeWork') || document.getElementById('saveBtn'));
  if (!isLeadPage && !isClientLK && !hasControls) return;

  const monthLabel = document.getElementById('monthLabel');
  const prevBtn    = document.getElementById('prevMonth');
  const nextBtn    = document.getElementById('nextMonth');
  const modeMeetingBtn = document.getElementById('modeMeeting');
  const modeWorkBtn    = document.getElementById('modeWork');
  const saveBtn        = document.getElementById('saveBtn');
  const form           = document.getElementById('leadForm');

  const initial  = window.__LK__ || {};

  let viewDate   = new Date();
  viewDate.setDate(1);

  let mode        = 'work';
  let meetingDate = initial.meeting_date || null;
  let workDates   = initial.work_dates   || {};

  // Today's YYYY-MM-DD for past-date blocking
  const pad     = (n) => (n < 10 ? '0' + n : '' + n);
  const ymd     = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const todayKey = ymd(new Date());

  // ── Touch / swipe support for month navigation ──────────────
  let _touchStartX = null;
  calEl.addEventListener('touchstart', (e) => {
    _touchStartX = e.touches[0].clientX;
  }, { passive: true });
  calEl.addEventListener('touchend', (e) => {
    if (_touchStartX === null) return;
    const dx = e.changedTouches[0].clientX - _touchStartX;
    _touchStartX = null;
    if (Math.abs(dx) < 40) return;
    if (dx < 0) navigateMonth(1);
    else         navigateMonth(-1);
  }, { passive: true });

  function navigateMonth(delta) {
    const direction = delta > 0 ? 'left' : 'right';
    calEl.classList.add('cal-slide-out-' + direction);
    setTimeout(() => {
      viewDate = new Date(viewDate.getFullYear(), viewDate.getMonth() + delta, 1);
      render();
      calEl.classList.remove('cal-slide-out-left', 'cal-slide-out-right');
      calEl.classList.add('cal-slide-in-' + (direction === 'left' ? 'right' : 'left'));
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          calEl.classList.remove('cal-slide-in-left', 'cal-slide-in-right');
        });
      });
    }, 160);
  }

  function setMode(newMode) {
    mode = newMode;
    if (modeMeetingBtn) modeMeetingBtn.classList.toggle('active', mode === 'meeting');
    if (modeWorkBtn)    modeWorkBtn.classList.toggle('active', mode === 'work');
  }

  if (modeMeetingBtn) modeMeetingBtn.addEventListener('click', () => setMode('meeting'));
  if (modeWorkBtn)    modeWorkBtn.addEventListener('click', () => setMode('work'));
  if (prevBtn)        prevBtn.addEventListener('click', () => navigateMonth(-1));
  if (nextBtn)        nextBtn.addEventListener('click', () => navigateMonth(1));

  // ── "Today" button ──────────────────────────────────────────
  const todayBtn = document.getElementById('todayBtn');
  if (todayBtn) {
    todayBtn.addEventListener('click', () => {
      const today = new Date();
      if (viewDate.getFullYear() !== today.getFullYear() || viewDate.getMonth() !== today.getMonth()) {
        viewDate = new Date(today.getFullYear(), today.getMonth(), 1);
        render();
      }
    });
  }

  const monthNames = ['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'];

  function render() {
    const year  = viewDate.getFullYear();
    const month = viewDate.getMonth();
    if (monthLabel) monthLabel.textContent = `${monthNames[month]} ${year}`;

    // Update "Today" button visibility
    if (todayBtn) {
      const today = new Date();
      todayBtn.style.display = (year === today.getFullYear() && month === today.getMonth()) ? 'none' : 'inline-flex';
    }

    const firstDay    = new Date(year, month, 1);
    const startDow    = (firstDay.getDay() + 6) % 7;
    const daysInMonth = new Date(year, month + 1, 0).getDate();

    calEl.innerHTML = '';

    // DOW header
    const dowRow = document.createElement('div');
    dowRow.className = 'cal-row cal-dow';
    ['Пн','Вт','Ср','Чт','Пт','Сб','Вс'].forEach((d) => {
      const c = document.createElement('div');
      c.className = 'cal-cell dow';
      c.textContent = d;
      dowRow.appendChild(c);
    });
    calEl.appendChild(dowRow);

    let day = 1;
    for (let r = 0; r < 6; r++) {
      if (day > daysInMonth) break;
      const row = document.createElement('div');
      row.className = 'cal-row';

      for (let c = 0; c < 7; c++) {
        const cell = document.createElement('div');
        cell.className = 'cal-cell';

        if (r === 0 && c < startDow) {
          cell.classList.add('muted');
          row.appendChild(cell);
          continue;
        }
        if (day > daysInMonth) {
          cell.classList.add('muted');
          row.appendChild(cell);
          continue;
        }

        const dateObj = new Date(year, month, day);
        const key = ymd(dateObj);
        const isPast = key < todayKey;
        const isToday = key === todayKey;

        const num = document.createElement('div');
        num.className = 'cal-num';
        num.textContent = String(day);
        cell.appendChild(num);

        // ── Past dates — disabled ──────────────────────────────
        if (isPast) {
          cell.classList.add('muted', 'past');
          row.appendChild(cell);
          day++;
          continue;
        }

        // ── Today ─────────────────────────────────────────────
        if (isToday) cell.classList.add('today');

        // ── Selected states ───────────────────────────────────
        if (meetingDate === key) cell.classList.add('meeting');
        if (workDates[key]) {
          cell.classList.add('work');
          if (workDates[key].time) {
            const t = document.createElement('div');
            t.className = 'cal-time';
            t.textContent = workDates[key].time;
            cell.appendChild(t);
          }
        }

        // ── Left click ────────────────────────────────────────
        cell.addEventListener('click', async (e) => {
          e.preventDefault();
          if (mode === 'meeting') {
            meetingDate = key;
            render();
          } else {
            if (!workDates[key]) workDates[key] = {};
            const existing = String(workDates[key].time || '');
            const picked = await _pickTime(existing);
            if (picked !== null) workDates[key].time = picked;
            render();
          }
        });

        // ── Right click / long press → remove ─────────────────
        cell.addEventListener('contextmenu', (e) => {
          e.preventDefault();
          _removeDate(key, cell);
        });

        row.appendChild(cell);
        day++;
      }
      calEl.appendChild(row);
    }
  }

  function _removeDate(key, cell) {
    if (!cell) {
      render();
      return;
    }
    // Flash red animation before removing
    cell.classList.add('cal-removing');
    setTimeout(() => {
      if (meetingDate === key) meetingDate = null;
      if (workDates[key]) delete workDates[key];
      render();
    }, 260);
  }

  // ── Time picker bottom sheet ───────────────────────────────
  async function _pickTime(existingRange) {
    const m   = (existingRange || '').match(/^(\d{2}):(\d{2})\s*-\s*(\d{2}):(\d{2})$/);
    const sh0 = m ? m[1] : '10';
    const sm0 = m ? m[2] : '00';
    const eh0 = m ? m[3] : '14';
    const em0 = m ? m[4] : '00';

    function hoursOpts(sel) {
      let s = '';
      for (let h = 6; h <= 23; h++) {
        const v = String(h).padStart(2, '0');
        s += `<option value="${v}"${v === sel ? ' selected' : ''}>${v}</option>`;
      }
      return s;
    }
    function minsOpts(sel) {
      return ['00', '15', '30', '45'].map(v =>
        `<option value="${v}"${v === sel ? ' selected' : ''}>${v}</option>`
      ).join('');
    }

    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;flex-direction:column;justify-content:flex-end;background:rgba(0,0,0,.5);backdrop-filter:blur(2px);';

    const sheet = document.createElement('div');
    sheet.style.cssText = 'background:#fff;border-radius:24px 24px 0 0;padding:20px 20px 36px;box-shadow:0 -4px 40px rgba(0,0,0,.18);max-height:85vh;overflow-y:auto;';
    sheet.innerHTML = `
      <div style="width:40px;height:4px;border-radius:4px;background:rgba(0,0,0,.12);margin:0 auto 20px;"></div>
      <div style="font-weight:900;font-size:18px;margin-bottom:20px;color:#016b82;text-align:center;">⏰ Время работы</div>
      <div style="display:grid;grid-template-columns:1fr auto 1fr;gap:8px;align-items:end;margin-bottom:20px;">
        <div>
          <div style="font-size:12px;font-weight:700;color:#6a8f9b;margin-bottom:8px;text-align:center;text-transform:uppercase;letter-spacing:.05em;">Начало</div>
          <div style="display:flex;gap:4px;align-items:center;">
            <select id="__cal_sh" style="flex:1;padding:12px 4px;border:2px solid #e2e8f0;border-radius:12px;font-size:18px;font-weight:800;background:#f8fafc;-webkit-appearance:none;text-align:center;cursor:pointer;">${hoursOpts(sh0)}</select>
            <span style="font-weight:900;font-size:20px;color:#016b82;">:</span>
            <select id="__cal_sm" style="flex:1;padding:12px 4px;border:2px solid #e2e8f0;border-radius:12px;font-size:18px;font-weight:800;background:#f8fafc;-webkit-appearance:none;text-align:center;cursor:pointer;">${minsOpts(sm0)}</select>
          </div>
        </div>
        <div style="padding-bottom:12px;color:#bbb;font-size:22px;font-weight:300;text-align:center;">→</div>
        <div>
          <div style="font-size:12px;font-weight:700;color:#6a8f9b;margin-bottom:8px;text-align:center;text-transform:uppercase;letter-spacing:.05em;">Конец</div>
          <div style="display:flex;gap:4px;align-items:center;">
            <select id="__cal_eh" style="flex:1;padding:12px 4px;border:2px solid #e2e8f0;border-radius:12px;font-size:18px;font-weight:800;background:#f8fafc;-webkit-appearance:none;text-align:center;cursor:pointer;">${hoursOpts(eh0)}</select>
            <span style="font-weight:900;font-size:20px;color:#016b82;">:</span>
            <select id="__cal_em" style="flex:1;padding:12px 4px;border:2px solid #e2e8f0;border-radius:12px;font-size:18px;font-weight:800;background:#f8fafc;-webkit-appearance:none;text-align:center;cursor:pointer;">${minsOpts(em0)}</select>
          </div>
        </div>
      </div>
      <div id="__cal_preview" style="text-align:center;font-size:15px;font-weight:700;color:#016b82;margin-bottom:8px;min-height:24px;"></div>
      <div id="__cal_err" style="color:#e53e3e;font-size:13px;min-height:18px;margin-bottom:12px;text-align:center;"></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
        <button id="__cal_cancel" style="padding:14px;border:2px solid #e2e8f0;border-radius:14px;font-size:15px;font-weight:700;background:#f8fafc;cursor:pointer;transition:background .15s;">Отмена</button>
        <button id="__cal_ok" style="padding:14px;border:none;border-radius:14px;font-size:15px;font-weight:700;background:linear-gradient(135deg,#016b82,#0ab5d4);color:#fff;cursor:pointer;transition:opacity .15s;">Сохранить</button>
      </div>
    `;

    overlay.appendChild(sheet);
    document.body.appendChild(overlay);

    // Animate in
    sheet.style.transform = 'translateY(100%)';
    sheet.style.transition = 'transform .3s cubic-bezier(.32,.72,0,1)';
    requestAnimationFrame(() => { sheet.style.transform = 'translateY(0)'; });

    // Live preview
    function updatePreview() {
      const sh = sheet.querySelector('#__cal_sh').value;
      const sm = sheet.querySelector('#__cal_sm').value;
      const eh = sheet.querySelector('#__cal_eh').value;
      const em = sheet.querySelector('#__cal_em').value;
      const startMins = parseInt(sh) * 60 + parseInt(sm);
      const endMins   = parseInt(eh) * 60 + parseInt(em);
      const diff = endMins - startMins;
      if (diff > 0) {
        const hrs = Math.floor(diff / 60);
        const mins = diff % 60;
        const dur = hrs > 0 ? (mins > 0 ? `${hrs}ч ${mins}м` : `${hrs}ч`) : `${mins}м`;
        sheet.querySelector('#__cal_preview').textContent = `${sh}:${sm} — ${eh}:${em} · ${dur}`;
        sheet.querySelector('#__cal_err').textContent = '';
      } else {
        sheet.querySelector('#__cal_preview').textContent = '';
      }
    }
    ['__cal_sh','__cal_sm','__cal_eh','__cal_em'].forEach(id =>
      sheet.querySelector('#' + id).addEventListener('change', updatePreview)
    );
    updatePreview();

    return await new Promise((resolve) => {
      function close(val) {
        sheet.style.transform = 'translateY(100%)';
        setTimeout(() => { try { overlay.remove(); } catch (e) {} }, 300);
        resolve(val);
      }

      overlay.addEventListener('click', (e) => { if (e.target === overlay) close(null); });
      sheet.querySelector('#__cal_cancel').addEventListener('click', () => close(null));
      sheet.querySelector('#__cal_ok').addEventListener('click', () => {
        const sh = sheet.querySelector('#__cal_sh').value;
        const sm = sheet.querySelector('#__cal_sm').value;
        const eh = sheet.querySelector('#__cal_eh').value;
        const em = sheet.querySelector('#__cal_em').value;
        const startMins = parseInt(sh) * 60 + parseInt(sm);
        const endMins   = parseInt(eh) * 60 + parseInt(em);
        if (endMins <= startMins) {
          sheet.querySelector('#__cal_err').textContent = '⚠ Конец должен быть позже начала';
          return;
        }
        close(`${sh}:${sm}-${eh}:${em}`);
      });
    });
  }

  // ── Save button ────────────────────────────────────────────
  if (saveBtn) {
    saveBtn.addEventListener('click', async () => {
      if (form) {
        const fd = new FormData(form);
        const payload = {
          parent_name:  fd.get('parent_name'),
          telegram:     fd.get('telegram'),
          child_name:   fd.get('child_name'),
          child_age:    fd.get('child_age'),
          notes:        fd.get('notes'),
          meeting_date: meetingDate,
          work_dates:   workDates,
        };

        saveBtn.disabled = true;
        saveBtn.textContent = 'Отправляем…';

        try {
          const res  = await fetch('/api/lead', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          });
          const data = await res.json();
          if (!res.ok) {
            alert(data.error || 'Ошибка сохранения');
            saveBtn.disabled = false;
            saveBtn.textContent = '✉ Отправить заявку';
            return;
          }
          _showSuccessScreen(data.lk_url || null);
        } catch (e) {
          alert('Ошибка сети. Попробуйте ещё раз.');
          saveBtn.disabled = false;
          saveBtn.textContent = '✉ Отправить заявку';
        }
        return;
      }

      if (window.__LK__ && window.__LK__.token) {
        saveBtn.disabled = true;
        saveBtn.textContent = 'Сохраняем…';
        try {
          const res  = await fetch(`/api/client/${window.__LK__.token}/update`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ meeting_date: meetingDate, work_dates: workDates }),
          });
          const data = await res.json();
          if (!res.ok) {
            alert(data.error || 'Ошибка');
          } else {
            saveBtn.textContent = '✓ Сохранено!';
            saveBtn.style.background = '#22c55e';
            setTimeout(() => {
              saveBtn.disabled = false;
              saveBtn.textContent = 'Сохранить изменения';
              saveBtn.style.background = '';
            }, 2000);
            return;
          }
        } catch (e) {
          alert('Ошибка сети.');
        }
        saveBtn.disabled = false;
        saveBtn.textContent = 'Сохранить изменения';
      }
    });
  }

  render();

  // ── Success screen ──────────────────────────────────────────
  function _showSuccessScreen(lkUrl) {
    const formPanel = document.querySelector('.panel--lead');
    const calPanel  = document.querySelector('.panel--calendar');
    if (formPanel) formPanel.style.display = 'none';
    if (calPanel)  calPanel.style.display  = 'none';

    const wrap = document.createElement('div');
    wrap.style.cssText = 'grid-column:1/-1;display:flex;justify-content:center;padding:16px 0 32px;';
    wrap.innerHTML = `
      <div style="background:#fff;border-radius:24px;padding:40px 28px 36px;max-width:480px;width:100%;text-align:center;box-shadow:0 8px 48px rgba(1,107,130,.13);animation:fade-up .45s both;">
        <div style="font-size:72px;margin-bottom:16px;animation:badge-bounce .6s both;">🎉</div>
        <h2 style="font-size:24px;font-weight:900;color:#016b82;margin-bottom:10px;">Заявка принята!</h2>
        <p style="color:#555;font-size:15px;line-height:1.6;margin-bottom:24px;">
          Мы уже подбираем для вас няню.<br>
          Ответим в Telegram в течение <strong>15 минут</strong>.
        </p>
        ${lkUrl ? `
          <a href="${lkUrl}" class="btn primary" style="display:flex;align-items:center;justify-content:center;gap:8px;text-decoration:none;margin-bottom:12px;width:100%;">
            📋 Открыть личный кабинет
          </a>
          <p style="font-size:12px;color:#999;">Сохраните ссылку — в ней ваше расписание и смены</p>
        ` : `
          <div style="font-size:13px;color:#888;background:#f0f9fc;padding:14px 18px;border-radius:14px;line-height:1.5;">
            📬 Ссылка на личный кабинет придёт в Telegram
          </div>
        `}
      </div>
    `;

    const heroGrid = document.querySelector('.hero-grid');
    if (heroGrid) {
      heroGrid.appendChild(wrap);
      wrap.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } else {
      if (lkUrl) window.location.href = lkUrl;
      else alert('Заявка принята! Ожидайте сообщения в Telegram.');
    }
  }

})();
