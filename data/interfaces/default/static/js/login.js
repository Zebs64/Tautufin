// Connexion : bascule entre compte Jellyfin et compte local.
document.querySelectorAll('.tab').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('login-mode').value = btn.dataset.tab;
  document.getElementById('login-hint').textContent =
    btn.dataset.tab === 'jellyfin' ? 'Utilisez vos identifiants Jellyfin habituels.' : '';
}));

// Onglet personnalisé de Jellyfin : la page parente (Jellyfin Web, origine
// autorisée dans les réglages) transmet le jeton de sa session, que le serveur
// fait vérifier par Jellyfin. Évite de retaper son mot de passe.
(() => {
  const box = document.getElementById('embed-login');
  if (!box || window.parent === window) return;
  const origins = box.dataset.origins.split(' ').filter(Boolean);
  const authBox = box.closest('.auth-box');
  const status = document.getElementById('embed-login-status');
  const retry = document.getElementById('embed-login-retry');
  let pending = null;

  const fail = (message) => {
    if (pending) {
      window.removeEventListener('message', pending.onMessage);
      clearTimeout(pending.timer);
      pending = null;
    }
    authBox.classList.remove('embed-pending');
    status.textContent = message;
    retry.hidden = false;
  };

  const login = (token) => fetch('/login/jellyfin-session', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  }).then(async (res) => {
    if (res.ok) {
      window.location.replace('/');
      return;
    }
    const data = await res.json().catch(() => ({}));
    fail(data.detail || 'Connexion automatique impossible.');
  }).catch(() => fail('Connexion automatique impossible.'));

  const attempt = () => {
    if (pending) return;
    authBox.classList.add('embed-pending');
    retry.hidden = true;
    status.textContent = 'Connexion avec votre session Jellyfin…';
    const onMessage = (event) => {
      const data = event.data;
      if (event.source !== window.parent || !origins.includes(event.origin)) return;
      if (!data || data.type !== 'tautufin:jellyfin-session' || typeof data.token !== 'string') return;
      window.removeEventListener('message', onMessage);
      clearTimeout(pending.timer);
      login(data.token);
    };
    pending = {
      onMessage,
      timer: setTimeout(() => fail('Aucune session Jellyfin détectée.'), 4000),
    };
    window.addEventListener('message', onMessage);
    // La demande ne contient rien de secret ; elle n'est délivrée que si la
    // page parente a bien l'une des origines autorisées.
    origins.forEach((origin) => {
      window.parent.postMessage({ type: 'tautufin:jellyfin-session-request' }, origin);
    });
  };

  box.hidden = false;
  retry.addEventListener('click', attempt);
  // Pas de connexion automatique juste après une déconnexion volontaire, ni
  // après un échec du formulaire (l'utilisateur visait peut-être un autre compte).
  if (box.dataset.auto === 'false' || new URLSearchParams(window.location.search).has('manual')) {
    retry.hidden = false;
  } else {
    attempt();
  }
})();
