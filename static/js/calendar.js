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
              // Prefer a native-like picker (input type=time)
              if (typeof HTMLDialogElement === 'undefined') {
                const t = prompt('Укажи время (например 10:00-14:00):', existingRange || '');
                return t === null ? null : String(t).trim();
              }

              const m = existingRange.match(/^(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})$/);
              const start0 = m ? m[1] : '';
              const end0 = m ? m[2] : '';

              const dlg = document.createElement('dialog');
              dlg.style.border = 'none';
              dlg.style.borderRadius = '16px';
              dlg.style.padding = '16px';
              dlg.style.maxWidth = '420px';
              dlg.innerHTML = `
                <form method="dialog" style="display:grid; gap:10px;">
                  <div style="font-weight:900; font-size:16px;">Время работы</div>
                  <label style="margin:0; font-weight:900;">Начало</label>
                  <input type="time" name="start" value="${start0}" required style="padding:10px 12px; border:1px solid rgba(0,0,0,.12); border-radius:12px;" />
                  <label style="margin:0; font-weight:900;">Окончание</label>
                  <input type="time" name="end" value="${end0}" required style="padding:10px 12px; border:1px solid rgba(0,0,0,.12); border-radius:12px;" />
                  <div style="display:flex; gap:10px; justify-content:flex-end; margin-top:6px;">
                    <button value="cancel" class="btn" style="background:rgba(0,0,0,.06)">Отмена</button>
                    <button value="ok" class="btn primary" style="margin-top:0; width:auto;">Сохранить</button>
                  </div>
                </form>
              `;
              document.body.appendChild(dlg);

              return await new Promise((resolve) => {
                dlg.addEventListener('close', () => {
                  try {
                    if (dlg.returnValue !== 'ok') return resolve(null);
                    const fd = new FormData(dlg.querySelector('form'));
                    const start = String(fd.get('start')||'').trim();
                    const end = String(fd.get('end')||'').trim();
                    resolve(start && end ? `${start}-${end}` : null);
                  } finally {
                    try { dlg.remove(); } catch (e) {}
                  }
                }, { once:true });
                dlg.showModal();
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
        alert('Сохранено! Ссылка на ЛК показана ниже (для теста).');
        if (data.lk_url) {
          const lkLink = document.getElementById('lkLink');
          if (lkLink) {
            lkLink.style.display = 'block';
            lkLink.href = data.lk_url;
          }
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
