// Mode intégré (onglet Custom Tabs de Jellyfin) : comportements propres à
// l'affichage dans une iframe. La classe « embedded » est posée dans le <head>
// de base.html ; hors iframe, ce script ne fait rien.
(() => {
  const root = document.documentElement;
  if (!root.classList.contains('embedded')) return;

  // Menu du compte (avatar) : se ferme au clic ailleurs ou avec Échap.
  const account = document.querySelector('.nav-account');
  if (account) {
    document.addEventListener('click', (event) => {
      if (account.open && !account.contains(event.target)) account.open = false;
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && account.open) {
        account.open = false;
        account.querySelector('summary').focus();
      }
    });
  }

  // Pastilles de navigation sur une ligne : la page active est ramenée en vue,
  // un fondu signale les pastilles cachées, la molette fait défiler à
  // l'horizontale.
  const links = document.querySelector('.navbar .nav-links');
  if (links) {
    const active = links.querySelector('a.active');
    if (active) {
      links.scrollLeft = active.offsetLeft - (links.clientWidth - active.offsetWidth) / 2;
    }
    const updateFade = () => {
      links.classList.toggle('fade-start', links.scrollLeft > 1);
      links.classList.toggle(
        'fade-end', links.scrollLeft + links.clientWidth < links.scrollWidth - 1,
      );
    };
    links.addEventListener('scroll', updateFade, { passive: true });
    window.addEventListener('resize', updateFade);
    links.addEventListener('wheel', (event) => {
      if (links.scrollWidth <= links.clientWidth || Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
        return;
      }
      links.scrollLeft += event.deltaY;
      event.preventDefault();
    }, { passive: false });
    updateFade();
  }

  // Reprise : l'onglet Jellyfin mémorise la page affichée et la rouvre à la
  // visite suivante (le plugin recrée l'onglet à chaque fois). Message envoyé
  // uniquement aux origines de Jellyfin Web déclarées dans les réglages.
  const origins = (root.dataset.embedOrigins || '').split(' ').filter(Boolean);
  const path = window.location.pathname + window.location.search;
  origins.forEach((origin) => {
    window.parent.postMessage({ type: 'tautufin:navigated', path }, origin);
  });
})();
