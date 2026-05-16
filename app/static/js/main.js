/* ============================================================
   GDT Invoice Crawler — Main JavaScript
   app/static/js/main.js
   ============================================================ */

// ── Global SocketIO connection ──────────────────────────────
const socket = io({ transports: ['websocket', 'polling'] });

socket.on('connect', () => {
  setStatusDot(true, 'Connected');
});

socket.on('disconnect', () => {
  setStatusDot(false, 'Disconnected');
});

socket.on('crawler_status', (data) => {
  if (data.running) {
    setStatusDot(true, 'Crawler running…');
  } else {
    setStatusDot(false, 'Crawler idle');
  }
});

/**
 * Update the sidebar crawler status indicator.
 * @param {boolean} running
 * @param {string}  label
 */
function setStatusDot(running, label) {
  const dot = document.getElementById('status-dot');
  const txt = document.getElementById('status-text');
  if (!dot || !txt) return;

  if (running) {
    dot.classList.remove('idle');
  } else {
    dot.classList.add('idle');
  }
  txt.textContent = label;
}

// ── Number formatters ───────────────────────────────────────

/**
 * Format a number as Vietnamese Dong currency string.
 * @param {number} n
 * @returns {string}  e.g. "1.234.567 ₫"
 */
function fmtVND(n) {
  return new Intl.NumberFormat('vi-VN').format(Math.round(n)) + ' ₫';
}

/**
 * Format a number with thousands separator.
 * @param {number} n
 * @returns {string}  e.g. "1.234.567"
 */
function fmtNum(n) {
  return new Intl.NumberFormat('vi-VN').format(n);
}

// ── Chart.js global defaults ────────────────────────────────
// Applied once after the DOM is ready so Chart.js is already loaded.
document.addEventListener('DOMContentLoaded', () => {
  if (typeof Chart === 'undefined') return;

  Chart.defaults.color = '#8b949e';
  Chart.defaults.borderColor = '#30363d';
  Chart.defaults.font.family = "'IBM Plex Sans', sans-serif";
  Chart.defaults.font.size = 11;
});

// ── Flash-message auto-dismiss ──────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // Bootstrap alerts with .alert-dismissible auto-close after 5 s
  document.querySelectorAll('.alert.alert-dismissible').forEach((el) => {
    setTimeout(() => {
      const bsAlert = bootstrap.Alert.getOrCreateInstance(el);
      if (bsAlert) bsAlert.close();
    }, 5000);
  });
});

// ── Utility: show a transient toast-style alert ─────────────
/**
 * Display a temporary inline alert inside a given container element.
 * @param {HTMLElement} el        - The container to inject the alert into.
 * @param {string}      message   - HTML-safe message text.
 * @param {'success'|'danger'|'warning'|'info'} type
 * @param {number}      duration  - Auto-dismiss delay in ms (default 3000).
 */
function showAlert(el, message, type = 'success', duration = 3000) {
  if (!el) return;
  el.style.display = 'block';

  const colorMap = {
    success: { bg: 'rgba(63,185,80,0.1)',   border: '#3fb950', color: '#3fb950' },
    danger:  { bg: 'rgba(248,81,73,0.1)',   border: '#f85149', color: '#f85149' },
    warning: { bg: 'rgba(210,153,34,0.1)',  border: '#d29922', color: '#d29922' },
    info:    { bg: 'rgba(121,192,255,0.1)', border: '#79c0ff', color: '#79c0ff' },
  };
  const c = colorMap[type] || colorMap.info;

  el.style.cssText = `
    display: block;
    background: ${c.bg};
    border: 1px solid ${c.border};
    color: ${c.color};
    padding: 10px 14px;
    border-radius: 8px;
    font-size: 13px;
  `;
  el.innerHTML = message;

  setTimeout(() => { el.style.display = 'none'; }, duration);
}