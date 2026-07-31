// Vue Clients : un agrégat JSON alimente KPIs, tableau, diagnostic et cinq graphiques.
(() => {
  const charts = {};
  let requestCache = null;
  let rows = [];
  let sort = {key: 'plays', direction: 'desc'};
  let pageGeneration = 0;

  function query() {
    const params = new URLSearchParams();
    const period = document.getElementById('clients-days').value;
    if (period.startsWith('year:')) params.set('year', period.slice(5));
    else params.set('days', period);
    const user = document.getElementById('clients-user');
    if (user && user.value) params.set('user_id', user.value);
    return '/api/clients?' + params;
  }

  function requestData() {
    if (!requestCache) {
      requestCache = fetchJSON(query());
      requestCache.then(
        () => { requestCache = null; },
        () => { requestCache = null; },
      );
    }
    return requestCache;
  }

  function destroy(id) {
    if (charts[id]) charts[id].destroy();
  }

  function bar(id, data, stacked = false) {
    destroy(id);
    charts[id] = new Chart(document.getElementById(id), {
      type: 'bar',
      data: {
        labels: data.categories,
        datasets: data.series.map((series, index) => ({
          label: series.name,
          data: series.data,
          backgroundColor: CHART_COLORS[index % CHART_COLORS.length],
          borderRadius: 5,
          borderSkipped: false,
          maxBarThickness: 34,
        })),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {legend: {display: data.series.length > 1}},
        scales: {
          x: {stacked, grid: {display: false}},
          y: {stacked, beginAtZero: true, ticks: {precision: 0},
              grid: {color: 'rgba(255,255,255,0.06)'}, border: {display: false}},
        },
      },
    });
  }

  function doughnut(id, data) {
    destroy(id);
    const values = data.series[0].data;
    const total = values.reduce((sum, value) => sum + Number(value || 0), 0) || 1;
    charts[id] = new Chart(document.getElementById(id), {
      type: 'doughnut',
      data: {labels: data.categories, datasets: [{
        data: values,
        backgroundColor: CHART_COLORS,
        borderColor: 'rgba(17,24,39,0.9)',
        borderWidth: 2,
      }]},
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '58%',
        plugins: {
          legend: {position: 'right', labels: {font: {size: 11}}},
          tooltip: {callbacks: {label: context =>
            ` ${context.label} : ${context.parsed} (${Math.round(context.parsed / total * 100)} %)`}},
        },
      },
    });
  }

  function hasData(data) {
    return Boolean((data.series || []).some(series =>
      (series.data || []).some(value => Number(value) > 0)));
  }

  function setKpis(summary) {
    const values = {
      'kpi-active': summary.active_clients,
      'kpi-plays': summary.plays,
      'kpi-duration': fmtDuration(summary.duration_seconds),
      'kpi-direct': `${summary.direct_play_percent.toFixed(1)} %`,
      'kpi-transcode': `${summary.transcode_percent.toFixed(1)} %`,
      'kpi-transcode-time': fmtDuration(summary.transcode_seconds),
      'kpi-cost': `${summary.transcode_cost.toFixed(2)} €`,
    };
    Object.entries(values).forEach(([id, value]) => {
      document.getElementById(id).textContent = value;
    });
  }

  function compare(a, b) {
    const av = a[sort.key] ?? '';
    const bv = b[sort.key] ?? '';
    const result = typeof av === 'number'
      ? av - Number(bv || 0)
      : String(av).localeCompare(String(bv), 'fr', {sensitivity: 'base'});
    return sort.direction === 'asc' ? result : -result;
  }

  function renderTable() {
    const table = document.getElementById('clients-table');
    const state = document.getElementById('clients-table-state');
    if (!rows.length) {
      table.hidden = true;
      state.hidden = false;
      state.textContent = 'Aucun client identifié sur cette période.';
      return;
    }
    const sorted = [...rows].sort(compare);
    table.querySelector('tbody').innerHTML = sorted.map(row => `<tr>
      <td><span class="cell-client">${clientLogo(row.client)} ${esc(row.client)}</span></td>
      <td>${row.plays}</td>
      <td>${fmtDuration(row.duration_seconds)}</td>
      <td>${row.direct_play}</td>
      <td>${row.transcode}</td>
      <td>${esc((row.last_used || '—').slice(0, 16))}</td>
    </tr>`).join('');
    state.hidden = true;
    table.hidden = false;
    table.querySelectorAll('th.sortable').forEach(th => {
      th.classList.toggle('sorted-asc', th.dataset.sort === sort.key && sort.direction === 'asc');
      th.classList.toggle('sorted-desc', th.dataset.sort === sort.key && sort.direction === 'desc');
      th.setAttribute('aria-sort', th.dataset.sort === sort.key
        ? (sort.direction === 'asc' ? 'ascending' : 'descending') : 'none');
    });
  }

  function renderDiagnostic(items) {
    const state = document.getElementById('clients-diagnostic-state');
    const list = document.getElementById('clients-diagnostic-list');
    list.replaceChildren();
    if (!items.length) {
      state.hidden = false;
      state.textContent = 'Aucun constat fiable sur cette période.';
      return;
    }
    state.hidden = true;
    items.forEach(item => {
      const li = document.createElement('li');
      li.textContent = item.text;
      list.appendChild(li);
    });
  }

  function loadAll() {
    const generation = ++pageGeneration;
    requestCache = null;
    const promise = requestData();
    promise.then(data => {
      if (generation !== pageGeneration) return;
      setKpis(data.summary);
      rows = data.clients;
      renderTable();
      renderDiagnostic(data.diagnostics);
    }).catch(error => {
      if (generation !== pageGeneration) return;
      console.error('Chargement du diagnostic clients impossible', error);
      document.getElementById('clients-table').hidden = true;
      const tableState = document.getElementById('clients-table-state');
      tableState.hidden = false;
      tableState.textContent = 'Impossible de charger le classement.';
      const diagnosticState = document.getElementById('clients-diagnostic-state');
      diagnosticState.hidden = false;
      diagnosticState.textContent = 'Impossible de charger le diagnostic.';
    });

    const graph = (id, key, render, empty) => ChartState.load(
      id, requestData, data => hasData(data.charts[key]),
      data => render(id, data.charts[key]), empty,
    );
    graph('clients-usage', 'usage', bar, 'Aucun client identifié sur cette période.');
    graph('clients-methods', 'methods_by_client', (id, data) => bar(id, data, true),
      'Aucune méthode associée à un client identifié.');
    graph('clients-resolutions', 'resolutions', doughnut, 'Aucune résolution observée.');
    graph('clients-video-codecs', 'video_codecs', doughnut, 'Aucun codec vidéo observé.');
    graph('clients-audio-codecs', 'audio_codecs', doughnut, 'Aucun codec audio observé.');
  }

  document.querySelectorAll('#clients-filters select').forEach(select =>
    select.addEventListener('change', loadAll));
  document.querySelectorAll('#clients-table th.sortable').forEach(th => {
    const activate = () => {
      const key = th.dataset.sort;
      sort.direction = sort.key === key && sort.direction === 'desc' ? 'asc' : 'desc';
      sort.key = key;
      renderTable();
    };
    th.addEventListener('click', activate);
    th.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        activate();
      }
    });
  });

  loadAll();
})();
