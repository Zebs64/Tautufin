// Wrapped : apparition des sections au défilement et chiffres qui défilent.
// Les valeurs finales sont déjà dans le HTML : sans JS, ou si l'utilisateur
// demande moins d'animations, la page s'affiche telle quelle.
(() => {
  const root = document.querySelector('.wrapped');
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!root || reduced || !('IntersectionObserver' in window)) return;

  const DURATION_MS = 1400;
  const easeOut = t => 1 - Math.pow(1 - t, 3);
  const formats = {
    count: v => Math.round(v).toLocaleString('fr-FR'),
    countDuration: v => fmtDuration(v),
    countDecimal: v => v.toLocaleString('fr-FR', {minimumFractionDigits: 1, maximumFractionDigits: 1}),
  };

  function countUp(el) {
    const key = Object.keys(formats).find(k => el.dataset[k] !== undefined);
    const target = Number(el.dataset[key]);
    if (!key || !Number.isFinite(target) || target <= 0) return;
    const finalText = el.textContent;
    const start = performance.now();
    const step = now => {
      const t = Math.min(1, (now - start) / DURATION_MS);
      el.textContent = t < 1 ? formats[key](target * easeOut(t)) : finalText;
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // Les éléments ne sont masqués qu'à partir d'ici (classe posée par le JS).
  document.documentElement.classList.add('wrapped-animate');
  const observer = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add('is-visible');
      entry.target.querySelectorAll('[data-count], [data-count-duration], [data-count-decimal]')
        .forEach(countUp);
      observer.unobserve(entry.target);
    });
  }, {threshold: 0.18, rootMargin: '0px 0px -40px 0px'});
  root.querySelectorAll('.reveal').forEach(el => observer.observe(el));

  // Affiches : légère inclinaison 3D qui suit le pointeur.
  root.querySelectorAll('.wrapped-poster-wrap').forEach(wrap => {
    wrap.addEventListener('pointermove', event => {
      const box = wrap.getBoundingClientRect();
      const x = (event.clientX - box.left) / box.width - 0.5;
      const y = (event.clientY - box.top) / box.height - 0.5;
      wrap.style.setProperty('--tilt-x', `${(-y * 10).toFixed(2)}deg`);
      wrap.style.setProperty('--tilt-y', `${(x * 12).toFixed(2)}deg`);
    });
    wrap.addEventListener('pointerleave', () => {
      wrap.style.removeProperty('--tilt-x');
      wrap.style.removeProperty('--tilt-y');
    });
  });
})();
