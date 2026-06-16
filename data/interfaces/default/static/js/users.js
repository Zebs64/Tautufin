// Page Utilisateurs : recherche + tri, entièrement côté client (la liste est
// rendue en entier par le serveur).

const tbody = document.querySelector('#users-table tbody');
const search = document.getElementById('users-search');
const empty = document.getElementById('users-empty');
const rows = Array.from(tbody.querySelectorAll('tr'));

const state = {sort: 'plays', order: 'desc'};

// Valeur comparable d'une ligne pour la clé de tri donnée.
function sortValue(tr, key) {
  if (key === 'name') return tr.dataset.name;
  if (key === 'last') return tr.dataset.last;           // 'YYYY-MM-DD HH:MM' triable tel quel
  return parseFloat(tr.dataset[key]) || 0;              // plays, duration
}

function apply() {
  const q = search.value.trim().toLowerCase();
  const dir = state.order === 'asc' ? 1 : -1;

  const visible = rows.filter(tr => !q || tr.dataset.name.includes(q));
  visible.sort((a, b) => {
    const va = sortValue(a, state.sort), vb = sortValue(b, state.sort);
    if (va < vb) return -dir;
    if (va > vb) return dir;
    return 0;
  });

  rows.forEach(tr => tr.hidden = true);
  visible.forEach(tr => { tr.hidden = false; tbody.appendChild(tr); });
  empty.hidden = visible.length > 0;
}

document.querySelectorAll('#users-table th.sortable').forEach(th =>
  th.addEventListener('click', () => {
    if (state.sort === th.dataset.sort) {
      state.order = state.order === 'desc' ? 'asc' : 'desc';
    } else {
      state.sort = th.dataset.sort;
      // Texte croissant par défaut, chiffres/dates décroissant par défaut.
      state.order = th.dataset.sort === 'name' ? 'asc' : 'desc';
    }
    document.querySelectorAll('#users-table th.sortable')
      .forEach(t => t.classList.remove('sorted-asc', 'sorted-desc'));
    th.classList.add(state.order === 'asc' ? 'sorted-asc' : 'sorted-desc');
    apply();
  }));

search.addEventListener('input', apply);

apply();
