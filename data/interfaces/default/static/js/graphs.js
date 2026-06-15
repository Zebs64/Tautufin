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

function renderChart(canvasId, type, data, options = {}) {
  const el = document.getElementById(canvasId);
  if (!el) return;
  if (charts[canvasId]) charts[canvasId].destroy();
  charts[canvasId] = new Chart(el, {type, data, options: {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {tooltip: {callbacks: {label: ctx =>
      `${ctx.dataset.label || ctx.label} : ${fmtTick(ctx.parsed.y ?? ctx.parsed)}`}},
      ...options.plugins},
    ...options,
  }});
}

function barFrom(data, canvasId, {horizontal = false, color = 0} = {}) {
  renderChart(canvasId, 'bar', {
    labels: data.categories,
    datasets: data.series.map((s, i) => ({
      label: s.name,
      data: s.data,
      backgroundColor: CHART_COLORS[(color + i) % CHART_COLORS.length],
    })),
  }, {
    indexAxis: horizontal ? 'y' : 'x',
    plugins: {legend: {display: data.series.length > 1}},
    scales: {[horizontal ? 'x' : 'y']: {ticks: {callback: fmtTick}, beginAtZero: true}},
  });
}

function pieFrom(data, canvasId) {
  renderChart(canvasId, 'doughnut', {
    labels: data.categories,
    datasets: [{
      data: data.series[0] ? data.series[0].data : [],
      backgroundColor: CHART_COLORS,
      borderColor: '#1c1c1e',
    }],
  }, {plugins: {legend: {position: 'right'}}});
}

async function loadAll() {
  const isAdmin = document.querySelector('.charts-grid').dataset.isAdmin === '1';
  const group = document.getElementById('graph-group').value;

  fetchJSON('/api/graphs/plays_over_time?' + params({group})).then(data => {
    renderChart('chart-over-time', 'bar', {
      labels: data.categories,
      datasets: data.series.filter(s => s.name !== 'Total').map((s, i) => ({
        label: s.name, data: s.data,
        backgroundColor: CHART_COLORS[i % CHART_COLORS.length],
      })),
    }, {
      plugins: {legend: {display: true}},
      scales: {
        x: {stacked: true},
        y: {stacked: true, ticks: {callback: fmtTick}, beginAtZero: true},
      },
    });
  }).catch(() => {});

  if (isAdmin)
    fetchJSON('/api/graphs/by_user?' + params())
      .then(d => pieFrom(d, 'chart-by-user')).catch(() => {});

  fetchJSON('/api/graphs/top_items?' + params({kind: 'movie'}))
    .then(d => barFrom(d, 'chart-top-movies', {horizontal: true})).catch(() => {});
  fetchJSON('/api/graphs/top_items?' + params({kind: 'series'}))
    .then(d => barFrom(d, 'chart-top-series', {horizontal: true, color: 1})).catch(() => {});
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
}

document.querySelectorAll('#graph-filters select').forEach(el =>
  el.addEventListener('change', loadAll));

loadAll();
