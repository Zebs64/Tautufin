// États explicites pour les visualisations chargées en JSON.
const ChartState = (() => {
  const generations = {};

  function target(id) {
    return document.getElementById(id);
  }

  function boxFor(el) {
    const id = `${el.id}-state`;
    let box = document.getElementById(id);
    if (!box) {
      box = document.createElement('div');
      box.id = id;
      box.className = 'chart-state';
      box.setAttribute('role', 'status');
      el.insertAdjacentElement('beforebegin', box);
    }
    return box;
  }

  function show(id, state, message, retry) {
    const el = target(id);
    if (!el) return;
    const box = boxFor(el);
    el.hidden = state !== 'ready';
    box.hidden = state === 'ready';
    box.className = `chart-state chart-state-${state}`;
    box.setAttribute('aria-busy', state === 'loading' ? 'true' : 'false');
    box.textContent = message;
    if (state === 'error' && retry) {
      box.appendChild(document.createTextNode(' '));
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-sm';
      button.textContent = 'Réessayer';
      button.addEventListener('click', retry);
      box.appendChild(button);
    }
  }

  async function load(id, request, hasData, render, emptyMessage = 'Aucune donnée') {
    const generation = (generations[id] || 0) + 1;
    generations[id] = generation;
    const retry = () => load(id, request, hasData, render, emptyMessage);
    show(id, 'loading', 'Chargement…');
    try {
      const data = await request();
      if (generations[id] !== generation) return;
      if (!hasData(data)) {
        show(id, 'empty', emptyMessage);
        return;
      }
      show(id, 'ready', '');
      render(data);
    } catch (error) {
      if (generations[id] !== generation) return;
      console.error(`Chargement de ${id} impossible`, error);
      show(id, 'error', 'Impossible de charger ces données.', retry);
    }
  }

  return {load, show};
})();
