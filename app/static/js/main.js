/* ============================================================
   GDT Invoice Crawler — Main JavaScript
   app/static/js/main.js
   ============================================================ */

// ── Global SocketIO connection ──────────────────────────────
const socket = io({ transports: ['websocket', 'polling'] });

socket.on('connect',    () => setStatusDot(true,  'Connected'));
socket.on('disconnect', () => setStatusDot(false, 'Disconnected'));
socket.on('crawler_status', (data) => {
  setStatusDot(data.running, data.running ? 'Crawler running…' : 'Crawler idle');
});

function setStatusDot(running, label) {
  const dot = document.getElementById('status-dot');
  const txt = document.getElementById('status-text');
  if (!dot || !txt) return;
  dot.classList.toggle('idle', !running);
  txt.textContent = label;
}

// ── Number / currency formatters ────────────────────────────
function fmtVND(n) {
  return new Intl.NumberFormat('vi-VN').format(Math.round(n)) + ' ₫';
}

function fmtNum(n) {
  return new Intl.NumberFormat('vi-VN').format(n);
}

// ── Chart.js global defaults (light theme) ──────────────────
document.addEventListener('DOMContentLoaded', () => {
  if (typeof Chart === 'undefined') return;

  const GRID_COLOR  = '#f3f4f6';
  const TICK_COLOR  = '#6b7280';
  const FONT_FAMILY = "'IBM Plex Sans', sans-serif";

  Chart.defaults.color        = TICK_COLOR;
  Chart.defaults.borderColor  = GRID_COLOR;
  Chart.defaults.font.family  = FONT_FAMILY;
  Chart.defaults.font.size    = 11;

  Chart.defaults.plugins.tooltip = {
    ...Chart.defaults.plugins.tooltip,
    backgroundColor: '#ffffff',
    titleColor:      '#111928',
    bodyColor:       '#4b5563',
    borderColor:     '#e5e7eb',
    borderWidth:     1,
    padding:         10,
    cornerRadius:    8,
    titleFont:       { family: FONT_FAMILY, weight: '700', size: 12 },
    bodyFont:        { family: FONT_FAMILY, size: 11 },
    boxShadow:       '0 4px 12px rgba(0,0,0,0.1)',
  };

  Chart.defaults.plugins.legend.labels = {
    ...Chart.defaults.plugins.legend.labels,
    color:         TICK_COLOR,
    usePointStyle: true,
    pointStyle:    'circle',
    padding:       20,
    font:          { family: FONT_FAMILY, size: 11 },
  };

  Chart.defaults.scale.grid  = { color: GRID_COLOR, drawBorder: false };
  Chart.defaults.scale.ticks = { color: TICK_COLOR };
});

// ── Flash-message auto-dismiss ──────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.alert.alert-dismissible').forEach((el) => {
    setTimeout(() => {
      try { bootstrap.Alert.getOrCreateInstance(el).close(); } catch(e) {}
    }, 5000);
  });
});

// ── Inline alert helper ─────────────────────────────────────
function showAlert(el, message, type = 'success', duration = 3000) {
  if (!el) return;
  const styles = {
    success: ['var(--success-bg)',  'var(--success)', 'var(--success-border)'],
    danger:  ['var(--danger-bg)',   'var(--danger)',  'var(--danger-border)'],
    warning: ['var(--warning-bg)',  'var(--warning)', 'var(--warning-border)'],
    info:    ['var(--info-bg)',     'var(--info)',    'var(--info-border)'],
  };
  const [bg, color, border] = styles[type] || styles.info;
  el.style.cssText = `display:block; background:${bg}; border:1px solid ${border}; color:${color}; padding:10px 14px; border-radius:10px; font-size:13px; font-weight:500;`;
  el.innerHTML = message;
  setTimeout(() => { el.style.display = 'none'; }, duration);
}