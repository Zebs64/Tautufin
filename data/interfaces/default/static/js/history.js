// Historique : l'URL est la source canonique des filtres, du tri et de la pagination.
const filtersBox = document.getElementById('history-filters');
const isAdmin = filtersBox.dataset.isAdmin === '1';
const canView = filtersBox.dataset.canView === '1';
const tbody = document.querySelector('#history-table tbody');
const filterNames = ['user_id', 'media_type', 'library_id', 'date_from', 'date_to', 'search'];
const sortNames = new Set([...document.querySelectorAll('th.sortable')].map(th => th.dataset.sort));
const state = {sort: 'date', order: 'desc', page: 1, page_size: 25};
let requestGeneration = 0;

function mediaCell(r) {
  const title = r.series_name
    ? `${esc(r.series_name)} — S${r.season_number || 0}E${r.episode_number || 0} ${esc(r.item_name)}`
    : esc(r.item_name);
  const imageId = r.image_id || r.item_id;
  const thumb = imageId
    ? `<img class="thumb" loading="lazy" alt="" src="/image/item/${encodeURIComponent(imageId)}?w=120">`
    : '';
  const badge = r.source === 'infer'
    ? ' <span class="badge badge-off" title="Session reconstituée depuis le statut « Lu » de Jellyfin">inféré</span>'
    : '';
  const inner = r.item_id
    ? `<a href="/media/${encodeURIComponent(r.item_id)}">${title}</a>${badge}`
    : `${title}${badge}`;
  return `<span class="cell-media">${thumb}<span>${inner}</span></span>`;
}

function clientCell(r) {
  if (!r.client_name) return '<span class="muted">—</span>';
  return `<span class="cell-client">${clientLogo(r.client_name, r.device_name)} ${esc(r.client_name)}</span>`;
}

function columnsCount() {
  return 6 + (canView ? 1 : 0) + (isAdmin ? 1 : 0);
}

function positiveInt(value, fallback) {
  const parsed = Number.parseInt(value, 10);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function setControl(name, value) {
  const control = filtersBox.querySelector(`[name="${name}"]`);
  if (!control) return;
  if (control.tagName === 'SELECT' && ![...control.options].some(option => option.value === value)) {
    control.value = '';
    return;
  }
  control.value = value;
}

function readURL() {
  const query = new URLSearchParams(window.location.search);
  filterNames.forEach(name => setControl(name, query.get(name) || ''));
  const sort = query.get('sort') || 'date';
  state.sort = sortNames.has(sort) ? sort : 'date';
  state.order = query.get('order') === 'asc' ? 'asc' : 'desc';
  state.page = positiveInt(query.get('page'), 1);
  const size = positiveInt(query.get('page_size'), 25);
  state.page_size = [25, 50, 100].includes(size) ? size : 25;
  document.getElementById('page-size').value = String(state.page_size);
  renderSort();
  renderChips();
}

function apiQuery() {
  const query = new URLSearchParams({
    sort: state.sort,
    order: state.order,
    page: String(state.page),
    page_size: String(state.page_size),
  });
  filterNames.forEach(name => {
    const control = filtersBox.querySelector(`[name="${name}"]`);
    if (control && control.value) query.set(name, control.value);
  });
  return query;
}

function browserQuery() {
  const query = new URLSearchParams();
  filterNames.forEach(name => {
    const control = filtersBox.querySelector(`[name="${name}"]`);
    if (control && control.value) query.set(name, control.value);
  });
  if (state.sort !== 'date') query.set('sort', state.sort);
  if (state.order !== 'desc') query.set('order', state.order);
  if (state.page !== 1) query.set('page', String(state.page));
  if (state.page_size !== 25) query.set('page_size', String(state.page_size));
  return query;
}

function updateURL(mode) {
  if (mode === 'none') return;
  const query = browserQuery().toString();
  const url = query ? `/history?${query}` : '/history';
  if (mode === 'replace') window.history.replaceState(null, '', url);
  else window.history.pushState(null, '', url);
}

function renderSort() {
  document.querySelectorAll('th.sortable').forEach(th => {
    th.classList.remove('sorted-asc', 'sorted-desc');
    if (th.dataset.sort === state.sort) {
      th.classList.add(state.order === 'asc' ? 'sorted-asc' : 'sorted-desc');
    }
  });
}

function filterLabel(name, control) {
  const labels = {
    user_id: 'Utilisateur', media_type: 'Type', library_id: 'Bibliothèque',
    date_from: 'Du', date_to: 'Au', search: 'Recherche',
  };
  const value = control.tagName === 'SELECT'
    ? control.options[control.selectedIndex].text : control.value;
  return `${labels[name]} : ${value}`;
}

function renderChips() {
  const chips = document.getElementById('history-chips');
  chips.innerHTML = '';
  filterNames.forEach(name => {
    const control = filtersBox.querySelector(`[name="${name}"]`);
    if (!control || !control.value) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'filter-chip';
    button.dataset.filter = name;
    button.textContent = `${filterLabel(name, control)} ×`;
    button.setAttribute('aria-label', `Retirer le filtre ${filterLabel(name, control)}`);
    chips.appendChild(button);
  });
}

function renderRows(rows) {
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="${columnsCount()}" class="table-state muted">Aucune lecture trouvée.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td>${esc((r.started_at || '').slice(0, 16))}</td>
      ${canView ? `<td><span class="cell-user"><img class="avatar" loading="lazy" alt=""
          src="/image/user/${encodeURIComponent(r.jellyfin_user_id)}"><a
          href="/users/${encodeURIComponent(r.jellyfin_user_id)}">${esc(r.user_name)}</a></span></td>` : ''}
      <td>${mediaCell(r)}</td>
      <td>${esc(r.item_type || '—')}</td>
      <td>${fmtDuration(r.play_duration)}</td>
      <td>${r.percent_complete != null ? Math.round(r.percent_complete) + '%' : '—'}</td>
      <td>${clientCell(r)}</td>
      ${isAdmin ? `<td>${esc(r.ip_address || '—')}</td>` : ''}
    </tr>`).join('');
}

async function load(urlMode = 'push') {
  updateURL(urlMode);
  renderChips();
  const generation = ++requestGeneration;
  tbody.innerHTML = `<tr><td colspan="${columnsCount()}" class="table-state muted">Chargement…</td></tr>`;
  document.getElementById('history-total').textContent = 'Chargement…';
  try {
    const data = await fetchJSON('/api/history?' + apiQuery());
    if (generation !== requestGeneration) return;
    const pages = Math.max(1, Math.ceil(data.total / state.page_size));
    if (state.page > pages) {
      state.page = pages;
      updateURL('replace');
      load('none');
      return;
    }
    renderRows(data.rows);
    document.getElementById('history-total').textContent =
      `${data.total} lecture${data.total === 1 ? '' : 's'}`;
    document.getElementById('page-info').textContent = `Page ${state.page} / ${pages}`;
    document.getElementById('prev-page').disabled = state.page <= 1;
    document.getElementById('next-page').disabled = state.page >= pages;
  } catch (error) {
    if (generation !== requestGeneration) return;
    console.error('Chargement de l’historique impossible', error);
    tbody.innerHTML = `<tr><td colspan="${columnsCount()}" class="table-state err">` +
      'Impossible de charger l’historique. <button type="button" class="btn btn-sm" id="history-retry">Réessayer</button></td></tr>';
    document.getElementById('history-total').textContent = 'Indisponible';
    document.getElementById('history-retry').addEventListener('click', () => load('none'));
  }
}

document.querySelectorAll('th.sortable').forEach(th =>
  th.addEventListener('click', () => {
    if (state.sort === th.dataset.sort) state.order = state.order === 'desc' ? 'asc' : 'desc';
    else { state.sort = th.dataset.sort; state.order = 'desc'; }
    state.page = 1;
    renderSort();
    load();
  }));

filtersBox.querySelectorAll('select, input:not([type="search"])').forEach(control =>
  control.addEventListener('change', () => { state.page = 1; load(); }));
filtersBox.querySelector('[name="search"]').addEventListener('input', debounce(() => {
  state.page = 1;
  load();
}, 350));

document.getElementById('prev-page').addEventListener('click', () => { state.page--; load(); });
document.getElementById('next-page').addEventListener('click', () => { state.page++; load(); });
document.getElementById('page-size').addEventListener('change', event => {
  state.page_size = Number.parseInt(event.target.value, 10);
  state.page = 1;
  load();
});
document.getElementById('history-reset').addEventListener('click', () => {
  filterNames.forEach(name => setControl(name, ''));
  Object.assign(state, {sort: 'date', order: 'desc', page: 1, page_size: 25});
  document.getElementById('page-size').value = '25';
  renderSort();
  load();
});
document.getElementById('history-chips').addEventListener('click', event => {
  const chip = event.target.closest('[data-filter]');
  if (!chip) return;
  setControl(chip.dataset.filter, '');
  state.page = 1;
  load();
});
window.addEventListener('popstate', () => {
  readURL();
  load('none');
});

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

readURL();
load('replace');
