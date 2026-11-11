// Fiche bibliothèque : recherche dans le catalogue.
const catalogSearch = document.getElementById('catalog-search');
const catalogCards = Array.from(document.querySelectorAll('#catalog .poster-card'));
const catalogCount = document.getElementById('catalog-count');
const catalogEmpty = document.getElementById('catalog-empty');
function applyCatalog() {
  const q = catalogSearch.value.trim().toLowerCase();
  let n = 0;
  catalogCards.forEach(c => {
    const show = !q || c.dataset.name.includes(q);
    c.hidden = !show;
    if (show) n++;
  });
  catalogCount.textContent = `${n} titre${n > 1 ? 's' : ''}`;
  catalogEmpty.hidden = n > 0 || !catalogCards.length;
}
catalogSearch.addEventListener('input', applyCatalog);
catalogSearch.addEventListener('search', applyCatalog);
applyCatalog();
