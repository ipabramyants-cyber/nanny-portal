(function () {
  // Calendar widget:
  // - Choose mode: meeting (single date) or work (multiple dates)
  // - Left click: set date (and optionally time)
  // - Right click: remove selection
  // - Meeting dates = purple; Work dates = green
  // - Works both on landing and on client LK pages.

  const calEl = document.getElementById('calendar');
  if (!calEl) return;

  // This widget is ONLY for:
  // - Landing page lead form (date selection)
  // - Client LK page (update booking)
  // Other pages (admin calendar, nanny portal calendar) use their own JS and also
  // contain #calendar, so we must not attach here.
  const isLeadPage = !!document.getElementById('leadForm');
  const isClientLK = !!(window.__LK__ && window.__LK__.token);
  const hasControls = !!(document.getElementById('modeMeeting') || document.getElementById('modeWork') || document.getElementById('saveBtn'));
  if (!isLeadPage && !isClientLK && !hasControls) return;

  const monthLabel = document.getElementById('monthLabel');
  const prevBtn = document.getElementById('prevMonth');
  const nextBtn = document.getElementById('nextMonth');

  const modeMeetingBtn = document.getElementById('modeMeeting');
  const modeWorkBtn = document.getElementById('modeWork');
  const saveBtn = document.getElementById('saveBtn');

  const form = document.getElementById('leadForm');

  // If this is client LK, initial data can be provided on window.__LK__
  const initial = window.__LK__ || {};

  let viewDate = new Date();
  viewDate.setDate(1);

  let mode = 'work'; // default to work dates; 'meeting' | 'work'
  let meetingDate = initial.meeting_date || null; // 'YYYY-MM-DD'
  let workDates = initial.work_dates || {}; // { 'YYYY-MM-DD': { time: 'HH:MM-HH:MM' } }

  const pad = (n) => (n < 10 ? '0' + n : '' + n);
  const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

  function setMode(newMode) {
    mode = newMode;
    if (modeMeetingBtn) modeMeetingBtn.classList.toggle('active', mode === 'meeting');
    if (modeWorkBtn) modeWorkBtn.classList.toggle('active', mode === 'work');
  }

  if (modeMeetingBtn) modeMeetingBtn.addEventListener('click', () => setMode('meeting'));
  if (modeWorkBtn) modeWorkBtn.addEventListener('click', () => setMode('work'));

  const monthNames = ['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'];

  function render() {
    const year = viewDate.getFullYear();
    const month = viewDate.getMonth();
    if (monthLabel) monthLabel.textContent = `${monthNames[month]} ${year}`;

    const firstDay = new Date(year, month, 1);
    const startDow = (firstDay.getDay() + 6) % 7; // Mon=0
    const daysInMonth = new Date(year, month + 1, 0).getDate();

    calEl.innerHTML = '';

    // DOW header
    const dowRow = document.createElement('div');
    dowRow.className = 'cal-row cal-dow';
    ['Пн','Вт','Ср','Чт','Пт','Сб','Вс'].forEach((d) => {
      const c = document.createElement('div');
      c.className = 'cal-cell dow';
      c.textContent = d;
      c.classList.add('dow');
      dowRow.appendChild(c);
    });
    calEl.appendChild(dowRow);

    let day = 1;
    for (let r = 0; r < 6; r++) {
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

        const num = document.createElement('div');
        num.className = 'cal-num';
        num.textContent = String(day);
        cell.appendChild(num);

        // marks
        if (meetingDate === key) cell.classList.add('meeting');
        if (workDates[key]) cell.classList.add('work');
        if (workDates[key] && workDates[key].time) {
          const t = document.createElement('div');
          t.className = 'cal-time';
          t.textContent = workDates[key].time;
          cell.appendChild(t);
        }

        // left click
        cell.addEventListener('click', async (e) => {
          e.preventDefault();
          if (mode === 'meeting') {
            meetingDate = key;
          } else {
            if (!workDates[key]) workDates[key] = {};
            const existing = String(workDates[key].time || '');

            async function pickTime(existingRange){
              const m = (existingRange||'').match(/^(\d{2}):(\d{2})\s*-\s*(\d{2}):(\d{2})$/);
              const sh0 = m ? m[1] : '10';
              const sm0 = m ? m[2] : '00';
              const eh0 = m ? m[3] : '14';
              const em0 = m ? m[4] : '00';

              // Build hour options 06..23, minute options 00/15/30/45
              function hoursOpts(sel) {
                let s = '';
                for (let h = 6; h <= 23; h++) {
                  const v = String(h).padStart(2,'0');
                  s += `<option value="${v}"${v===sel?' selected':''}>${v}</option>`;
                }
                return s;
              }
              function minsOpts(sel) {
                return ['00','15','30','45'].map(v =>
                  `<option value="${v}"${v===sel?' selected':''}>${v}</option>`
                ).join('');
              }

              // Bottom sheet overlay — no dialog element, no native picker
              const overlay = document.createElement('div');
              overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;flex-direction:column;justify-content:flex-end;background:rgba(0,0,0,.45);';

              const sheet = document.createElement('div');
              sheet.style.cssText = 'background:#fff;border-radius:20px 20px 0 0;padding:20px 20px 32px;box-shadow:0 -4px 32px rgba(0,0,0,.18);max-height:80vh;overflow-y:auto;';
              sheet.innerHTML = `
                <div style="width:40px;height:4px;border-radius:4px;background:rgba(0,0,0,.15);margin:0 auto 16px;"></div>
                <div style="font-weight:900;font-size:18px;margin-bottom:18px;color:#016b82;">⏰ Время работы</div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px;">
                  <div>
                    <div style="font-size:13px;font-weight:700;color:#555;margin-bottom:6px;">Начало</div>
                    <div style="display:flex;gap:6px;align-items:center;">
                      <select id="__cal_sh" style="flex:1;padding:10px 8px;border:2px solid #e2e8f0;border-radius:10px;font-size:16px;font-weight:700;background:#f8fafc;-webkit-appearance:none;text-align:center;">${hoursOpts(sh0)}</select>
                      <span style="font-weight:900;font-size:18px;color:#016b82;">:</span>
                      <select id="__cal_sm" style="flex:1;padding:10px 8px;border:2px solid #e2e8f0;border-radius:10px;font-size:16px;font-weight:700;background:#f8fafc;-webkit-appearance:none;text-align:center;">${minsOpts(sm0)}</select>
                    </div>
                  </div>
                  <div>
                    <div style="font-size:13px;font-weight:700;color:#555;margin-bottom:6px;">Окончание</div>
                    <div style="display:flex;gap:6px;align-items:center;">
                      <select id="__cal_eh" style="flex:1;padding:10px 8px;border:2px solid #e2e8f0;border-radius:10px;font-size:16px;font-weight:700;background:#f8fafc;-webkit-appearance:none;text-align:center;">${hoursOpts(eh0)}</select>
                      <span style="font-weight:900;font-size:18px;color:#016b82;">:</span>
                      <select id="__cal_em" style="flex:1;padding:10px 8px;border:2px solid #e2e8f0;border-radius:10px;font-size:16px;font-weight:700;background:#f8fafc;-webkit-appearance:none;text-align:center;">${minsOpts(em0)}</select>
                    </div>
                  </div>
                </div>
                <div id="__cal_err" style="color:#e53e3e;font-size:13px;min-height:18px;margin-bottom:8px;"></div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
                  <button id="__cal_cancel" style="padding:13px;border:2px solid #e2e8f0;border-radius:12px;font-size:15px;font-weight:700;background:#f8fafc;cursor:pointer;">Отмена</button>
                  <button id="__cal_ok" style="padding:13px;border:none;border-radius:12px;font-size:15px;font-weight:700;background:#016b82;color:#fff;cursor:pointer;">Сохранить</button>
                </div>
              `;

              overlay.appendChild(sheet);
              document.body.appendChild(overlay);

              // Animate in
              sheet.style.transform = 'translateY(100%)';
              sheet.style.transition = 'transform .28s cubic-bezier(.32,.72,0,1)';
              requestAnimationFrame(() => { sheet.style.transform = 'translateY(0)'; });

              return await new Promise((resolve) => {
                function close(val) {
                  sheet.style.transform = 'translateY(100%)';
                  setTimeout(() => { try { overlay.remove(); } catch(e){} }, 300);
                  resolve(val);
                }

                overlay.addEventListener('click', (e) => { if (e.target === overlay) close(null); });
                sheet.querySelector('#__cal_cancel').addEventListener('click', () => close(null));
                sheet.querySelector('#__cal_ok').addEventListener('click', () => {
                  const sh = sheet.querySelector('#__cal_sh').value;
                  const sm = sheet.querySelector('#__cal_sm').value;
                  const eh = sheet.querySelector('#__cal_eh').value;
                  const em = sheet.querySelector('#__cal_em').value;
                  const startMins = parseInt(sh)*60 + parseInt(sm);
                  const endMins   = parseInt(eh)*60 + parseInt(em);
                  if (endMins <= startMins) {
                    sheet.querySelector('#__cal_err').textContent = 'Окончание должно быть позже начала';
                    return;
                  }
                  close(`${sh}:${sm}-${eh}:${em}`);
                });
              });
            }

            const picked = await pickTime(existing);
            if (picked !== null) workDates[key].time = picked;
          }
          render();
        });

        // right click -> remove
        cell.addEventListener('contextmenu', (e) => {
          e.preventDefault();
          if (meetingDate === key) meetingDate = null;
          if (workDates[key]) delete workDates[key];
          render();
        });

        row.appendChild(cell);
        day++;
      }
      calEl.appendChild(row);
    }
  }

  if (prevBtn) prevBtn.addEventListener('click', () => {
    viewDate = new Date(viewDate.getFullYear(), viewDate.getMonth() - 1, 1);
    render();
  });
  if (nextBtn) nextBtn.addEventListener('click', () => {
    viewDate = new Date(viewDate.getFullYear(), viewDate.getMonth() + 1, 1);
    render();
  });

  if (saveBtn) {
    saveBtn.addEventListener('click', async () => {
      // Landing page: create lead
      if (form) {
        const fd = new FormData(form);
        const payload = {
          parent_name: fd.get('parent_name'),
          telegram: fd.get('telegram'),
          child_name: fd.get('child_name'),
          child_age: fd.get('child_age'),
          notes: fd.get('notes'),
          meeting_date: meetingDate,
          work_dates: workDates,
        };
        const res = await fetch('/api/lead', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) {
          alert(data.error || 'Ошибка сохранения');
          return;
        }
        if (data.lk_url) {
          // Open LK inside Telegram Mini App if available, else browser
          if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.openLink) {
            window.Telegram.WebApp.openLink(data.lk_url);
          } else {
            window.location.href = data.lk_url;
          }
        } else {
          alert('Заявка принята! Ссылка на ЛК придёт в Telegram.');
        }
        return;
      }

      // LK page: update booking
      if (window.__LK__ && window.__LK__.token) {
        const res = await fetch(`/api/client/${window.__LK__.token}/update`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ meeting_date: meetingDate, work_dates: workDates }),
        });
        const data = await res.json();
        if (!res.ok) {
          alert(data.error || 'Ошибка');
          return;
        }
        alert('Изменения сохранены.');
      }
    });
  }

  render();
})();
