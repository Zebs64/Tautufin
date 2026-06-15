// Helpers partagés.

async function fetchJSON(url) {
  const r = await fetch(url, {credentials: 'same-origin'});
  if (r.status === 401) { window.location = '/login'; throw new Error('Session expirée'); }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${r.status}`);
  }
  return r.json();
}

async function postJSON(url, payload) {
  const r = await fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  if (r.status === 401) { window.location = '/login'; throw new Error('Session expirée'); }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${r.status}`);
  }
  return r.json();
}

function esc(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : String(s);
  return div.innerHTML;
}

function fmtDuration(seconds) {
  seconds = Math.round(seconds || 0);
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m`;
}

// Icône par nom de client (même mapping que le filtre Jinja côté serveur).
const CLIENT_ICON_MAP = [
  [['android tv', 'androidtv', 'apple tv', 'tvos', 'roku', 'tizen',
    'webos', 'samsung', 'lg tv', 'shield'], '📺'],
  [['web', 'browser', 'chrome', 'firefox', 'edge', 'safari', 'opera'], '🌐'],
  [['iphone', 'ipad', 'ios', 'swiftfin'], '📱'],
  [['android', 'findroid'], '🤖'],
  [['kodi', 'infuse', 'emby', 'plex'], '🎦'],
  [['mpv', 'vlc', 'mediaplayer'], '🎞️'],
  [['dlna', 'cast', 'chromecast'], '📡'],
];

function clientIcon(name) {
  const n = (name || '').toLowerCase();
  for (const [keywords, icon] of CLIENT_ICON_MAP)
    if (keywords.some(k => n.includes(k))) return icon;
  return '💻';
}

// --- Logos officiels des clients (SVG dans /static/img/clients/) --------------
// Le navigateur est détecté depuis le nom de l'appareil (device), la plateforme
// depuis le nom du produit (client). Repli : logo Jellyfin.
function browserSlug(s) {
  s = (s || '').toLowerCase();
  if (s.includes('edg')) return 'edge';
  if (s.includes('firefox') || s.includes('fxios')) return 'firefox';
  if (s.includes('opera') || s.includes('opr')) return 'opera';
  if (s.includes('brave')) return 'brave';
  if (s.includes('safari') && !s.includes('chrome') && !s.includes('crios')) return 'safari';
  if (s.includes('chrome') || s.includes('chromium') || s.includes('crios')) return 'chrome';
  return null;
}

function clientSlug(product, device) {
  const p = (product || '').toLowerCase();
  const d = (device || '').toLowerCase();
  if (p.includes('web') || p.includes('browser'))
    return browserSlug(device) || browserSlug(product) || 'jellyfin';
  if (p.includes('wholphin')) return 'wholphin';
  if (/ios|ipad|iphone|tvos|mac|apple|swiftfin|infuse/.test(p)) return 'apple';
  if (p.includes('kodi')) return 'kodi';
  if (p.includes('plex')) return 'plex';
  if (p.includes('chromecast') || p.includes('cast') || p.includes('google tv')) return 'googletv';
  if (p.includes('android')) return 'android';
  if (p.includes('samsung') || p.includes('tizen')) return 'samsung';
  if (p.includes('windows')) return 'windows';
  if (p.includes('media player') || p.includes('mpv') || p.includes('jellyfin')) return 'jellyfin';
  if (d.includes('shield') || d.includes('nvidia')) return 'nvidia';
  if (d.includes('samsung')) return 'samsung';
  return browserSlug(device) || 'jellyfin';
}

function clientLogo(product, device, cls) {
  const slug = clientSlug(product, device);
  return `<img class="client-logo${cls ? ' ' + cls : ''}" loading="lazy" alt=""` +
         ` title="${esc(product || '')}" src="/static/img/clients/${slug}.svg">`;
}

// Palette commune des graphiques (déclinée du thème Jellyfin).
const CHART_COLORS = ['#00a4dc', '#aa5cc3', '#4caf50', '#ff9800', '#f44336',
                      '#3f51b5', '#009688', '#e91e63', '#cddc39', '#795548'];

if (window.Chart) {
  Chart.defaults.color = '#c5c9d2';              // texte/légendes plus lisibles
  Chart.defaults.borderColor = 'rgba(255,255,255,0.08)';
  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.plugins.legend.labels.pointStyle = 'circle';
  Chart.defaults.plugins.legend.labels.boxWidth = 8;
  Chart.defaults.plugins.legend.labels.padding = 12;
  Chart.defaults.plugins.tooltip.boxPadding = 6;
}

// Radar des genres (profil de goûts) — polygone accent rempli.
function radarChart(canvasId, labels, data) {
  const el = document.getElementById(canvasId);
  if (!el) return;
  if (!labels || labels.length < 3) {
    el.replaceWith(Object.assign(document.createElement('p'),
      {className: 'muted', textContent: 'Pas assez de genres pour un radar.'}));
    return;
  }
  return new Chart(el, {
    type: 'radar',
    data: {
      labels,
      datasets: [{
        label: 'Genres', data,
        backgroundColor: 'rgba(119, 91, 244, 0.32)',
        borderColor: '#9b86ff', borderWidth: 2,
        pointBackgroundColor: '#9b86ff', pointBorderColor: '#fff',
        pointBorderWidth: 1, pointRadius: 3, pointHoverRadius: 5,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {legend: {display: false}},
      scales: {r: {
        beginAtZero: true,
        grid: {color: 'rgba(255, 255, 255, 0.10)'},
        angleLines: {color: 'rgba(255, 255, 255, 0.10)'},
        pointLabels: {color: '#c9c9d4', font: {size: 12}},
        ticks: {display: false, showLabelBackdrop: false},
      }},
    },
  });
}

// Timeline en aire dégradée (accent violet) — partagée par les pages détail.
// Remplace le canvas par un message si aucune donnée.
function areaTimeline(canvasId, labels, data) {
  const el = document.getElementById(canvasId);
  if (!el) return;
  if (!labels || !labels.length) {
    el.replaceWith(Object.assign(document.createElement('p'),
      {className: 'muted', textContent: 'Pas encore de lecture enregistrée.'}));
    return;
  }
  const ctx = el.getContext('2d');
  const grad = ctx.createLinearGradient(0, 0, 0, el.offsetHeight || 260);
  grad.addColorStop(0, 'rgba(119, 91, 244, 0.45)');
  grad.addColorStop(1, 'rgba(119, 91, 244, 0.02)');
  // Point unique : un marqueur isolé ne se voit pas → on force son affichage.
  const single = data.filter(v => v > 0).length <= 1;
  return new Chart(el, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Lectures', data,
        borderColor: '#9b86ff', borderWidth: 2,
        backgroundColor: grad, fill: 'origin',
        tension: 0.35, pointRadius: single ? 4 : 0, pointHoverRadius: 5,
        pointBackgroundColor: '#9b86ff', pointBorderColor: '#fff', pointBorderWidth: 1,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: {intersect: false, mode: 'index'},
      plugins: {legend: {display: false},
                tooltip: {callbacks: {label: c => `${c.parsed.y} lecture${c.parsed.y > 1 ? 's' : ''}`}}},
      scales: {
        x: {grid: {display: false}, ticks: {maxRotation: 0, autoSkipPadding: 16}},
        y: {beginAtZero: true, ticks: {precision: 0},
            grid: {color: 'rgba(255,255,255,0.06)'}, border: {display: false}},
      },
    },
  });
}
