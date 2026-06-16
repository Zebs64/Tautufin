// Historique : tableau dynamique (tri, pagination, filtres) sur /api/history.

const filtersBox = document.getElementById('history-filters');
const isAdmin = filtersBox.dataset.isAdmin === '1';     // colonne IP
const canView = filtersBox.dataset.canView === '1';     // colonne Utilisateur (admin ou vision)
const tbody = document.querySelector('#history-table tbody');

const state = {sort: 'date', order: 'desc', page: 1, page_size: 25};

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

async function load() {
  const params = new URLSearchParams({
    sort: state.sort, order: state.order,
    page: state.page, page_size: state.page_size,
  });
  filtersBox.querySelectorAll('select, input').forEach(el => {
    if (el.value) params.set(el.name, el.value);
  });
  try {
    const data = await fetchJSON('/api/history?' + params);
    const cols = 6 + (canView ? 1 : 0) + (isAdmin ? 1 : 0);
    tbody.innerHTML = data.rows.length ? data.rows.map(r => `
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
      </tr>`).join('')
      : `<tr><td colspan="${cols}" class="muted">Aucune lecture trouvée.</td></tr>`;
    const pages = Math.max(1, Math.ceil(data.total / state.page_size));
    state.page = Math.min(state.page, pages);
    document.getElementById('page-info').textContent =
      `Page ${state.page} / ${pages} — ${data.total} lectures`;
    document.getElementById('prev-page').disabled = state.page <= 1;
    document.getElementById('next-page').disabled = state.page >= pages;
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" class="err">${esc(e.message)}</td></tr>`;
  }
}

document.querySelectorAll('th.sortable').forEach(th =>
  th.addEventListener('click', () => {
    if (state.sort === th.dataset.sort) {
      state.order = state.order === 'desc' ? 'asc' : 'desc';
    } else {
      state.sort = th.dataset.sort;
      state.order = 'desc';
    }
    document.querySelectorAll('th.sortable')
      .forEach(t => t.classList.remove('sorted-asc', 'sorted-desc'));
    th.classList.add(state.order === 'asc' ? 'sorted-asc' : 'sorted-desc');
    state.page = 1;
    load();
  }));

filtersBox.querySelectorAll('select, input').forEach(el =>
  el.addEventListener('change', () => { state.page = 1; load(); }));
filtersBox.querySelector('[name=search]')
  .addEventListener('input', debounce(() => { state.page = 1; load(); }, 350));

document.getElementById('prev-page').addEventListener('click', () => { state.page--; load(); });
document.getElementById('next-page').addEventListener('click', () => { state.page++; load(); });
document.getElementById('page-size').addEventListener('change', e => {
  state.page_size = parseInt(e.target.value, 10);
  state.page = 1;
  load();
});

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

load();
