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

// Durées : l'API renvoie des secondes, les graphiques tracent des heures
// (graduations rondes « 2 h ») et les infobulles disent « 2 h 15 ».
const toPlot = values => isDuration() ? values.map(v => (v || 0) / 3600) : values;
const fmtTick = v => isDuration() ? fmtHoursTick(v) : Number(v).toLocaleString('fr-FR');
const fmtPoint = v => isDuration() ? fmtDurationLong(v * 3600) : playsLabel(v);
// Listes classées : valeurs brutes (secondes), libellé compact.
const fmtValue = v => isDuration() ? fmtDurationLong(v) : Number(v || 0).toLocaleString('fr-FR');

// Valeur numérique d'un point quel que soit le type/orientation du graphe
// (barre verticale → parsed.y, horizontale → parsed.x, camembert → parsed).
function tipValue(ctx) {
  const v = typeof ctx.raw === 'number'
    ? ctx.raw
    : (ctx.parsed && typeof ctx.parsed === 'object'
        ? (ctx.parsed.y ?? ctx.parsed.x) : ctx.parsed);
  return fmtPoint(v);
}

const MONTHS_LONG = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet',
                     'août', 'septembre', 'octobre', 'novembre', 'décembre'];

// Périodes de l'axe temporel (« 2026-11-11 », « 2026-S45 », « 2026-11 ») :
// court pour l'axe (« 11 nov. », « sem. 45 », « nov. 2026 »), long pour
// l'infobulle (« 11 nov. 2026 », « semaine 45 (2026) », « novembre 2026 »).
function fmtPeriod(label, long) {
  let m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(label || '');
  if (m) return fmtDay(new Date(+m[1], m[2] - 1, +m[3]), long);
  m = /^(\d{4})-S(\d{2})$/.exec(label || '');
  if (m) return long ? `semaine ${+m[2]} (${m[1]})` : `sem. ${+m[2]}`;
  m = /^(\d{4})-(\d{2})$/.exec(label || '');
  if (m) return long ? `${MONTHS_LONG[m[2] - 1]} ${m[1]}` : `${MONTHS_SHORT[m[2] - 1]} ${m[1]}`;
  return label;
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
  // Idem pour les callbacks d'infobulle, fusionnés avec ceux de base.
  const {plugins = {}, ...rest} = options;
  const {tooltip = {}, ...otherPlugins} = plugins;
  charts[canvasId] = new Chart(el, {type, data, options: {
    responsive: true,
    maintainAspectRatio: false,
    ...rest,
    plugins: {
      ...otherPlugins,
      tooltip: {
        ...tooltip,
        callbacks: {
          label: ctx => ` ${ctx.dataset.label || ctx.label} : ${tipValue(ctx)}`,
          ...(tooltip.callbacks || {}),
        },
      },
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
        `<span class="val">${fmtValue(vals[i])}</span></li>`).join('')
    : `<li class="muted">${emptyMsg || 'Aucune donnée'}</li>`;
}

function barFrom(data, canvasId, {horizontal = false, color = 0} = {}) {
  const valueAxis = horizontal ? 'x' : 'y';
  const catAxis = horizontal ? 'y' : 'x';
  renderChart(canvasId, 'bar', {
    labels: data.categories,
    datasets: data.series.map((s, i) => ({
      label: s.name,
      data: toPlot(s.data),
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

// Aire empilée, peu lissée (lectures dans le temps, par type de média).
function areaFrom(data, canvasId) {
  const series = data.series.filter(s => s.name !== 'Total');
  renderChart(canvasId, 'line', {
    labels: data.categories,
    datasets: series.map((s, i) => {
      const c = CHART_COLORS[i % CHART_COLORS.length];
      return {
        label: s.name, data: toPlot(s.data), borderColor: c,
        backgroundColor: hexA(c, 0.28), fill: true, cubicInterpolationMode: 'monotone',
        borderWidth: 2, pointRadius: 0, pointHoverRadius: 4,
      };
    }),
  }, {
    interaction: {intersect: false, mode: 'index'},
    plugins: {
      legend: {display: series.length > 1},
      tooltip: {
        filter: item => item.parsed.y > 0,  // pas de ligne « Musique : 0 min »
        callbacks: {title: items => (items.length ? fmtPeriod(items[0].label, true) : '')},
      },
    },
    scales: {
      x: {stacked: true, grid: {display: false},
          ticks: {maxRotation: 0, autoSkipPadding: 18,
                  callback(value) { return fmtPeriod(this.getLabelForValue(value), false); }}},
      y: {stacked: true, beginAtZero: true, ticks: {callback: fmtTick},
          grid: {color: 'rgba(255,255,255,0.06)'}, border: {display: false}},
    },
  });
}

function pieFrom(data, canvasId) {
  const vals = toPlot(data.series[0] ? data.series[0].data : []);
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
        ` ${ctx.label} : ${fmtPoint(ctx.parsed)} (${Math.round(ctx.parsed / total * 100)} %)`}},
    },
  });
}

function hasGraphData(data) {
  return Boolean((data.series || []).some(series =>
    (series.data || []).some(value => Number(value) > 0)));
}

function loadGraph(id, url, render, hasData = hasGraphData, emptyMessage) {
  ChartState.load(id, () => fetchJSON(url), hasData, render, emptyMessage);
}

function loadAll() {
  const isAdmin = document.querySelector('.charts-grid').dataset.isAdmin === '1';
  const group = document.getElementById('graph-group').value;

  loadGraph('chart-over-time', '/api/graphs/plays_over_time?' + params({group}),
    d => areaFrom(d, 'chart-over-time'));

  if (isAdmin)
    loadGraph('chart-by-user', '/api/graphs/by_user?' + params(),
      d => pieFrom(d, 'chart-by-user'));

  loadGraph('chart-top-movies', '/api/graphs/top_items?' + params({kind: 'movie'}),
    d => barFrom(d, 'chart-top-movies', {horizontal: true}));
  loadGraph('chart-top-series', '/api/graphs/top_items?' + params({kind: 'series'}),
    d => barFrom(d, 'chart-top-series', {horizontal: true, color: 1}));
  loadGraph('list-top-actors', '/api/graphs/top_people?' + params({kind: 'actor'}),
    d => rankedList('list-top-actors', d), hasGraphData, 'Aucun acteur');
  loadGraph('list-top-directors', '/api/graphs/top_people?' + params({kind: 'director'}),
    d => rankedList('list-top-directors', d), hasGraphData, 'Aucun réalisateur');
  loadGraph('chart-by-dow', '/api/graphs/by_day_of_week?' + params(),
    d => barFrom(d, 'chart-by-dow', {color: 5}));
  loadGraph('chart-by-hour', '/api/graphs/by_hour_of_day?' + params(),
    d => barFrom(d, 'chart-by-hour', {color: 6}));
  loadGraph('chart-by-library', '/api/graphs/by_library?' + params(),
    d => barFrom(d, 'chart-by-library', {color: 2}));
  loadGraph('chart-by-genre', '/api/graphs/by_genre?' + params(),
    d => barFrom(d, 'chart-by-genre', {color: 3}));
  loadGraph('chart-by-resolution', '/api/graphs/by_resolution?' + params(),
    d => pieFrom(d, 'chart-by-resolution'));
  loadGraph('chart-by-client', '/api/graphs/by_client?' + params(),
    d => barFrom(d, 'chart-by-client', {color: 4}));
  loadGraph('chart-by-method', '/api/graphs/by_play_method?' + params(),
    d => pieFrom(d, 'chart-by-method'));
  loadGraph('transcode-cost', '/api/graphs/transcode_cost?' + params(),
    showTranscodeCost, d => Number(d.seconds) > 0,
    'Aucun transcodage vidéo sur la période.');
}

// Estimation conso/coût du transcodage, sous le camembert des méthodes.
function showTranscodeCost(d) {
  const el = document.getElementById('transcode-cost');
  if (!el) return;
  el.innerHTML =
    `${icon('bolt')} ~${fmtDuration(d.seconds)} de transcodage vidéo` +
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
