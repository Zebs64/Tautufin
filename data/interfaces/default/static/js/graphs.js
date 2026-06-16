// Page graphiques : charge les endpoints /api/graphs/* dans des Chart.js.

const charts = {};

function params(extra = {}) {
  const p = new URLSearchParams({
    metric: document.getElementById('graph-metric').value,
    ...extra,
  });
  const period = document.getElementById('graph-days').value;
  if (period.startsWith('year:')) p.set('year', period.slice(5));
  else p.set('days', period);
  const userSel = document.getElementById('graph-user');
  if (userSel && userSel.value) p.set('user_id', userSel.value);
  return p;
}

function isDuration() {
  return document.getElementById('graph-metric').value === 'duration';
}

const fmtTick = v => isDuration() ? fmtDuration(v) : v;

// Valeur numérique d'un point quel que soit le type/orientation du graphe
// (barre verticale → parsed.y, horizontale → parsed.x, camembert → parsed).
function tipValue(ctx) {
  const v = typeof ctx.raw === 'number'
    ? ctx.raw
    : (ctx.parsed && typeof ctx.parsed === 'object'
        ? (ctx.parsed.y ?? ctx.parsed.x) : ctx.parsed);
  return fmtTick(v);
}

// Tronque les libellés d'axe trop longs (titres de films, noms…).
function truncTick(max = 26) {
  return function (value) {
    const l = this.getLabelForValue(value);
    return l && l.length > max ? l.slice(0, max - 1) + '…' : l;
  };
}

function renderChart(canvasId, type, data, options = {}) {
  const el = document.getElementById(canvasId);
  if (!el) return;
  if (charts[canvasId]) charts[canvasId].destroy();
  // On extrait `plugins` pour que le spread des autres options ne l'écrase pas
  // (sinon le tooltip de base — formatage durée — serait perdu).
  const {plugins = {}, ...rest} = options;
  charts[canvasId] = new Chart(el, {type, data, options: {
    responsive: true,
    maintainAspectRatio: false,
    ...rest,
    plugins: {
      tooltip: {callbacks: {label: ctx =>
        ` ${ctx.dataset.label || ctx.label} : ${tipValue(ctx)}`}},
      ...plugins,
    },
  }});
}

// Liste classée (façon blocs « populaires » de l'accueil) pour acteurs /
// réalisateurs : rang + nom + valeur formatée selon la mesure courante.
function rankedList(containerId, data, emptyMsg) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const cats = data.categories || [];
  const vals = (data.series[0] && data.series[0].data) || [];
  el.innerHTML = cats.length
    ? cats.map((c, i) =>
        `<li><span class="rank">${i + 1}</span>` +
        `<span class="name" title="${esc(c)}">${esc(c)}</span>` +
        `<span class="val">${fmtTick(vals[i])}</span></li>`).join('')
    : `<li class="muted">${emptyMsg || 'Aucune donnée'}</li>`;
}

function barFrom(data, canvasId, {horizontal = false, color = 0} = {}) {
  const valueAxis = horizontal ? 'x' : 'y';
  const catAxis = horizontal ? 'y' : 'x';
  renderChart(canvasId, 'bar', {
    labels: data.categories,
    datasets: data.series.map((s, i) => ({
      label: s.name,
      data: s.data,
      backgroundColor: CHART_COLORS[(color + i) % CHART_COLORS.length],
      borderRadius: 5, borderSkipped: false, maxBarThickness: 30,
    })),
  }, {
    indexAxis: horizontal ? 'y' : 'x',
    plugins: {legend: {display: data.series.length > 1}},
    scales: {
      [valueAxis]: {ticks: {callback: fmtTick}, beginAtZero: true,
                    grid: {color: 'rgba(255,255,255,0.06)'}, border: {display: false}},
      [catAxis]: {ticks: {callback: truncTick(horizontal ? 24 : 14), autoSkip: false},
                  grid: {display: false}},
    },
  });
}

// Hex → rgba (remplissages d'aire semi-transparents).
function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

// Aire empilée lissée (lectures dans le temps, ventilées par type de média).
function areaFrom(data, canvasId) {
  const series = data.series.filter(s => s.name !== 'Total');
  renderChart(canvasId, 'line', {
    labels: data.categories,
    datasets: series.map((s, i) => {
      const c = CHART_COLORS[i % CHART_COLORS.length];
      return {
        label: s.name, data: s.data, borderColor: c,
        backgroundColor: hexA(c, 0.28), fill: true, tension: 0.35,
        borderWidth: 2, pointRadius: 0, pointHoverRadius: 4,
      };
    }),
  }, {
    interaction: {intersect: false, mode: 'index'},
    plugins: {legend: {display: series.length > 1}},
    scales: {
      x: {stacked: true, grid: {display: false}},
      y: {stacked: true, beginAtZero: true, ticks: {callback: fmtTick},
          grid: {color: 'rgba(255,255,255,0.06)'}, border: {display: false}},
    },
  });
}

function pieFrom(data, canvasId) {
  const vals = data.series[0] ? data.series[0].data : [];
  const total = vals.reduce((a, b) => a + (b || 0), 0) || 1;
  renderChart(canvasId, 'doughnut', {
    labels: data.categories,
    datasets: [{
      data: vals,
      backgroundColor: CHART_COLORS,
      borderColor: 'rgba(17,24,39,0.9)', borderWidth: 2,
    }],
  }, {
    cutout: '58%',
    plugins: {
      legend: {position: 'right', labels: {font: {size: 11}}},
      tooltip: {callbacks: {label: ctx =>
        ` ${ctx.label} : ${fmtTick(ctx.parsed)} (${Math.round(ctx.parsed / total * 100)}%)`}},
    },
  });
}

async function loadAll() {
  const isAdmin = document.querySelector('.charts-grid').dataset.isAdmin === '1';
  const group = document.getElementById('graph-group').value;

  fetchJSON('/api/graphs/plays_over_time?' + params({group}))
    .then(d => areaFrom(d, 'chart-over-time')).catch(() => {});

  if (isAdmin)
    fetchJSON('/api/graphs/by_user?' + params())
      .then(d => pieFrom(d, 'chart-by-user')).catch(() => {});

  fetchJSON('/api/graphs/top_items?' + params({kind: 'movie'}))
    .then(d => barFrom(d, 'chart-top-movies', {horizontal: true})).catch(() => {});
  fetchJSON('/api/graphs/top_items?' + params({kind: 'series'}))
    .then(d => barFrom(d, 'chart-top-series', {horizontal: true, color: 1})).catch(() => {});
  fetchJSON('/api/graphs/top_people?' + params({kind: 'actor'}))
    .then(d => rankedList('list-top-actors', d, 'Aucun acteur (synchro en cours ?)')).catch(() => {});
  fetchJSON('/api/graphs/top_people?' + params({kind: 'director'}))
    .then(d => rankedList('list-top-directors', d, 'Aucun réalisateur (synchro en cours ?)')).catch(() => {});
  fetchJSON('/api/graphs/by_day_of_week?' + params())
    .then(d => barFrom(d, 'chart-by-dow', {color: 5})).catch(() => {});
  fetchJSON('/api/graphs/by_hour_of_day?' + params())
    .then(d => barFrom(d, 'chart-by-hour', {color: 6})).catch(() => {});
  fetchJSON('/api/graphs/by_library?' + params())
    .then(d => barFrom(d, 'chart-by-library', {color: 2})).catch(() => {});
  fetchJSON('/api/graphs/by_genre?' + params())
    .then(d => barFrom(d, 'chart-by-genre', {color: 3})).catch(() => {});
  fetchJSON('/api/graphs/by_resolution?' + params())
    .then(d => pieFrom(d, 'chart-by-resolution')).catch(() => {});
  fetchJSON('/api/graphs/by_client?' + params())
    .then(d => barFrom(d, 'chart-by-client', {color: 4})).catch(() => {});
  fetchJSON('/api/graphs/by_play_method?' + params())
    .then(d => pieFrom(d, 'chart-by-method')).catch(() => {});
  fetchJSON('/api/graphs/transcode_cost?' + params())
    .then(showTranscodeCost).catch(() => {});
}

// Estimation conso/coût du transcodage, sous le camembert des méthodes.
function showTranscodeCost(d) {
  const el = document.getElementById('transcode-cost');
  if (!el) return;
  if (!d.seconds) {
    el.textContent = '⚡ Aucun transcodage vidéo sur la période — rien de gaspillé 🎉';
    return;
  }
  el.innerHTML =
    `⚡ ~${fmtDuration(d.seconds)} de transcodage vidéo` +
    `<br>Électricité consommée : <strong>${d.kwh} kWh</strong>` +
    ` · Coût estimé : <strong>${d.cost.toFixed(2)} €</strong>` +
    `<br><span class="micro">base : +${d.watts} W, ${d.eur_per_kwh} €/kWh</span>`;
}

// Persistance des filtres (mesure / période / groupement / utilisateur),
// comme les préférences de l'accueil.
const GRAPHS_PREF_KEY = 'graphs.filters';
const filterSelects = document.querySelectorAll('#graph-filters select');

function restoreFilters() {
  let saved;
  try { saved = JSON.parse(localStorage.getItem(GRAPHS_PREF_KEY) || '{}'); }
  catch { saved = {}; }
  filterSelects.forEach(el => {
    const v = saved[el.id];
    // On ne restaure que si l'option existe encore (utilisateur/année variables).
    if (v != null && [...el.options].some(o => o.value === v)) el.value = v;
  });
}

function saveFilters() {
  const data = {};
  filterSelects.forEach(el => { data[el.id] = el.value; });
  try { localStorage.setItem(GRAPHS_PREF_KEY, JSON.stringify(data)); } catch {}
}

filterSelects.forEach(el => el.addEventListener('change', () => {
  saveFilters();
  loadAll();
}));

restoreFilters();
loadAll();
