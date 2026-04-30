(function () {
  function parseRange(existingRange) {
    const m = String(existingRange || '').match(/^(\d{2}):(\d{2})\s*[-–—]\s*(\d{2}):(\d{2})$/);
    return {
      sh: m ? m[1] : '10',
      sm: m ? m[2] : '00',
      eh: m ? m[3] : '14',
      em: m ? m[4] : '00',
    };
  }

  function hoursOpts(selected) {
    let html = '';
    for (let h = 6; h <= 23; h += 1) {
      const v = String(h).padStart(2, '0');
      html += `<option value="${v}"${v === selected ? ' selected' : ''}>${v}</option>`;
    }
    return html;
  }

  function minsOpts(selected) {
    return ['00', '15', '30', '45'].map((v) =>
      `<option value="${v}"${v === selected ? ' selected' : ''}>${v}</option>`
    ).join('');
  }

  function durationLabel(minutes) {
    const hrs = Math.floor(minutes / 60);
    const mins = minutes % 60;
    if (hrs > 0 && mins > 0) return `${hrs}ч ${mins}м`;
    if (hrs > 0) return `${hrs}ч`;
    return `${mins}м`;
  }

  async function pick(existingRange, options) {
    const opts = Object.assign({ title: 'Время работы', startLabel: 'Начало', endLabel: 'Конец' }, options || {});
    const initial = parseRange(existingRange);

    const overlay = document.createElement('div');
    overlay.className = 'time-range-sheet';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:10020;display:flex;flex-direction:column;justify-content:flex-end;background:rgba(15,23,42,.48);backdrop-filter:blur(2px);';

    const sheet = document.createElement('div');
    sheet.style.cssText = 'background:#fff;border-radius:24px 24px 0 0;padding:20px 20px 0;box-shadow:0 -4px 40px rgba(15,23,42,.22);max-height:85vh;overflow-y:auto;padding-bottom:calc(24px + env(safe-area-inset-bottom, 0px));';
    sheet.innerHTML = `
      <div style="width:40px;height:4px;border-radius:4px;background:rgba(15,23,42,.14);margin:0 auto 18px;"></div>
      <div style="font-weight:900;font-size:18px;margin-bottom:18px;color:#016b82;text-align:center;">${opts.title}</div>
      <div style="display:grid;grid-template-columns:1fr auto 1fr;gap:8px;align-items:end;margin-bottom:18px;">
        <div>
          <div style="font-size:12px;font-weight:800;color:#4e7280;margin-bottom:8px;text-align:center;text-transform:uppercase;letter-spacing:.05em;">${opts.startLabel}</div>
          <div style="display:flex;gap:4px;align-items:center;">
            <select id="__tp_sh" style="flex:1;padding:12px 4px;border:2px solid #e2e8f0;border-radius:12px;font-size:18px;font-weight:800;background:#f8fafc;text-align:center;cursor:pointer;">${hoursOpts(initial.sh)}</select>
            <span style="font-weight:900;font-size:20px;color:#016b82;">:</span>
            <select id="__tp_sm" style="flex:1;padding:12px 4px;border:2px solid #e2e8f0;border-radius:12px;font-size:18px;font-weight:800;background:#f8fafc;text-align:center;cursor:pointer;">${minsOpts(initial.sm)}</select>
          </div>
        </div>
        <div style="padding-bottom:12px;color:#94a3b8;font-size:22px;font-weight:300;text-align:center;">→</div>
        <div>
          <div style="font-size:12px;font-weight:800;color:#4e7280;margin-bottom:8px;text-align:center;text-transform:uppercase;letter-spacing:.05em;">${opts.endLabel}</div>
          <div style="display:flex;gap:4px;align-items:center;">
            <select id="__tp_eh" style="flex:1;padding:12px 4px;border:2px solid #e2e8f0;border-radius:12px;font-size:18px;font-weight:800;background:#f8fafc;text-align:center;cursor:pointer;">${hoursOpts(initial.eh)}</select>
            <span style="font-weight:900;font-size:20px;color:#016b82;">:</span>
            <select id="__tp_em" style="flex:1;padding:12px 4px;border:2px solid #e2e8f0;border-radius:12px;font-size:18px;font-weight:800;background:#f8fafc;text-align:center;cursor:pointer;">${minsOpts(initial.em)}</select>
          </div>
        </div>
      </div>
      <div id="__tp_preview" style="text-align:center;font-size:15px;font-weight:800;color:#016b82;margin-bottom:8px;min-height:24px;"></div>
      <div id="__tp_err" style="color:#dc2626;font-size:13px;min-height:18px;margin-bottom:12px;text-align:center;"></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px;">
        <button type="button" id="__tp_cancel" style="padding:14px;border:2px solid #e2e8f0;border-radius:14px;font-size:15px;font-weight:800;background:#f8fafc;cursor:pointer;">Отмена</button>
        <button type="button" id="__tp_ok" style="padding:14px;border:none;border-radius:14px;font-size:15px;font-weight:800;background:linear-gradient(135deg,#016b82,#0ab5d4);color:#fff;cursor:pointer;">Сохранить</button>
      </div>
    `;

    overlay.appendChild(sheet);
    document.body.appendChild(overlay);
    sheet.style.transform = 'translateY(100%)';
    sheet.style.transition = 'transform .28s cubic-bezier(.32,.72,0,1)';
    requestAnimationFrame(() => { sheet.style.transform = 'translateY(0)'; });

    function read() {
      return {
        sh: sheet.querySelector('#__tp_sh').value,
        sm: sheet.querySelector('#__tp_sm').value,
        eh: sheet.querySelector('#__tp_eh').value,
        em: sheet.querySelector('#__tp_em').value,
      };
    }

    function updatePreview() {
      const v = read();
      const startMins = parseInt(v.sh, 10) * 60 + parseInt(v.sm, 10);
      const endMins = parseInt(v.eh, 10) * 60 + parseInt(v.em, 10);
      const diff = endMins - startMins;
      if (diff > 0) {
        sheet.querySelector('#__tp_preview').textContent = `${v.sh}:${v.sm} - ${v.eh}:${v.em} · ${durationLabel(diff)}`;
        sheet.querySelector('#__tp_err').textContent = '';
      } else {
        sheet.querySelector('#__tp_preview').textContent = '';
      }
    }

    ['__tp_sh', '__tp_sm', '__tp_eh', '__tp_em'].forEach((id) => {
      sheet.querySelector('#' + id).addEventListener('change', updatePreview);
    });
    updatePreview();

    return await new Promise((resolve) => {
      function close(value) {
        sheet.style.transform = 'translateY(100%)';
        setTimeout(() => { try { overlay.remove(); } catch (e) {} }, 280);
        resolve(value);
      }

      overlay.addEventListener('click', (e) => { if (e.target === overlay) close(null); });
      sheet.querySelector('#__tp_cancel').addEventListener('click', () => close(null));
      sheet.querySelector('#__tp_ok').addEventListener('click', () => {
        const v = read();
        const startMins = parseInt(v.sh, 10) * 60 + parseInt(v.sm, 10);
        const endMins = parseInt(v.eh, 10) * 60 + parseInt(v.em, 10);
        if (endMins <= startMins) {
          sheet.querySelector('#__tp_err').textContent = 'Конец должен быть позже начала';
          return;
        }
        close(`${v.sh}:${v.sm}-${v.eh}:${v.em}`);
      });
    });
  }

  function setInputs(startInput, endInput, range) {
    const parts = String(range || '').split('-');
    if (parts.length !== 2) return;
    if (startInput) startInput.value = parts[0];
    if (endInput) endInput.value = parts[1];
    if (startInput) startInput.dispatchEvent(new Event('change', { bubbles: true }));
    if (endInput) endInput.dispatchEvent(new Event('change', { bubbles: true }));
  }

  async function pickForInputs(startInput, endInput, options) {
    if (!startInput || !endInput) return null;
    const current = startInput.value && endInput.value ? `${startInput.value}-${endInput.value}` : '';
    const range = await pick(current, options);
    if (range) setInputs(startInput, endInput, range);
    return range;
  }

  document.addEventListener('click', async (event) => {
    const btn = event.target.closest('[data-time-range-start][data-time-range-end]');
    if (!btn) return;
    const startInput = document.getElementById(btn.getAttribute('data-time-range-start'));
    const endInput = document.getElementById(btn.getAttribute('data-time-range-end'));
    const range = await pickForInputs(startInput, endInput, {
      title: btn.getAttribute('data-time-title') || 'Время работы',
    });
    if (range && btn.getAttribute('data-time-preview')) {
      const preview = document.getElementById(btn.getAttribute('data-time-preview'));
      if (preview) preview.textContent = range.replace('-', ' - ');
    }
  });

  window.NannyTimePicker = { pick, pickForInputs, setInputs };
})();
