// Réglages : test de connexion, synchro Jellyfin et santé, maintenance.
document.getElementById('test-jellyfin').addEventListener('click', async () => {
  const out = document.getElementById('test-result');
  out.textContent = '…';
  try {
    const r = await postJSON('/api/settings/test-jellyfin', {
      url: document.getElementById('jf-url').value,
      api_key: document.getElementById('jf-key').value,
    });
    showStatus(out, r.ok, r.ok ? `${r.server} (v${r.version})` : r.error);
  } catch (e) { showStatus(out, false, e.message); }
});
// Synchro Jellyfin : lancée en arrière-plan côté serveur, suivie par polling.
// Elle continue même si on quitte la page ; on la « reprend » à l'affichage.
let syncTimer = null;

function syncDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('fr-FR');
}

function renderSyncHealth(health, progress) {
  health = health || {};
  const status = health.status || 'never';
  const labels = {
    never: ['Non initialisée', 'badge-off'],
    running: ['En cours', 'badge-running'],
    success: ['À jour', 'badge-ok'],
    error: ['Erreur', 'badge-error'],
  };
  const badge = document.getElementById('sync-health-badge');
  const [label, badgeClass] = labels[status] || labels.never;
  badge.textContent = label;
  badge.className = `badge ${badgeClass}`;
  document.getElementById('sync-health-attempt').textContent = syncDate(health.last_attempt_at);
  document.getElementById('sync-health-success').textContent = health.last_success_at
    ? syncDate(health.last_success_at) : 'Jamais synchronisée';
  const mode = health.mode === 'incremental' ? 'Incrémental'
    : (health.mode === 'full' ? 'Full' : '—');
  const phase = status === 'running' ? (health.phase || progress.phase || 'démarrage') : null;
  document.getElementById('sync-health-mode').textContent = phase ? `${mode} · ${phase}` : mode;
  document.getElementById('sync-health-duration').textContent =
    health.duration_seconds == null ? '—' : `${health.duration_seconds.toLocaleString('fr-FR')} s`;
  document.getElementById('sync-health-users').textContent = health.users || 0;
  document.getElementById('sync-health-libraries').textContent = health.libraries || 0;
  document.getElementById('sync-health-inspected').textContent =
    health.items_inspected ?? health.items_received ?? 0;
  document.getElementById('sync-health-enriched').textContent = health.items_enriched || 0;
  document.getElementById('sync-health-changed').textContent = health.items_changed || 0;
  const error = document.getElementById('sync-health-error');
  error.hidden = status !== 'error';
  document.getElementById('sync-health-error-message').textContent = health.error || '';
}

function renderSyncHealthLoadError(error) {
  console.error('Chargement de la santé de synchronisation impossible', error);
  const badge = document.getElementById('sync-health-badge');
  badge.textContent = 'Erreur';
  badge.className = 'badge badge-error';
  const box = document.getElementById('sync-health-error');
  box.hidden = false;
  document.getElementById('sync-health-error-message').textContent =
    'Impossible de charger l’état de la synchronisation.';
}

function renderSync(s) {
  const box = document.getElementById('sync-progress');
  const label = document.getElementById('sync-progress-label');
  const fill = document.getElementById('sync-bar-fill');
  const out = document.getElementById('sync-result');
  renderSyncHealth(s.health, s);
  if (s.running) {
    box.hidden = false;
    out.textContent = ''; out.className = '';
    const pct = s.total ? Math.round(s.current / s.total * 100) : null;
    fill.classList.toggle('indeterminate', pct === null);
    fill.style.width = pct === null ? '100%' : pct + '%';
    let t = s.phase === 'users'
      ? 'Synchronisation des utilisateurs…'
      : `Bibliothèques ${s.current}/${s.total}` + (s.label ? ` — ${s.label}` : '');
    if (s.items) t += ` · ${s.items} médias`;
    label.textContent = t;
  } else {
    box.hidden = true;
    if (s.phase === 'done') {
      showStatus(out, true, `${s.users} utilisateurs, ${s.items} médias`);
    } else if (s.phase === 'error') {
      showStatus(out, false, s.error || 'erreur de synchronisation');
    }
  }
}

async function pollSync() {
  try {
    const s = await fetchJSON('/api/sync/status');
    renderSync(s);
    if (s.running && !syncTimer) syncTimer = setInterval(pollSync, 1000);
    if (!s.running && syncTimer) { clearInterval(syncTimer); syncTimer = null; }
  } catch (e) {
    if (syncTimer) clearInterval(syncTimer);
    syncTimer = null;
    renderSyncHealthLoadError(e);
  }
}

function watchSync() {
  if (syncTimer) return;
  syncTimer = setInterval(pollSync, 1000);
  pollSync();
}

document.getElementById('sync-now').addEventListener('click', async () => {
  const out = document.getElementById('sync-result');
  out.textContent = ''; out.className = '';
  try { await postJSON('/api/sync', {}); watchSync(); }
  catch (e) { showStatus(out, false, e.message); }
});

// Reprise : si une synchro tourne déjà (lancée avant, ou périodique), on
// affiche sa progression dès l'ouverture des Réglages.
pollSync();
document.getElementById('sync-health-retry').addEventListener('click', pollSync);
document.getElementById('verify-db').addEventListener('click', async () => {
  const out = document.getElementById('verify-result');
  out.textContent = 'Vérification…';
  out.className = 'maint-result';
  try {
    const r = await postJSON('/api/settings/verify-db', {});
    const mb = (r.size_bytes / 1048576).toFixed(1);
    const lines = [
      r.ok ? 'Intégrité OK' : r.integrity.join(' ; '),
      r.foreign_key_violations ? `${r.foreign_key_violations} violation(s) de clé étrangère` : null,
      `${r.counts.session_history} lectures · ${r.counts.items} médias · ` +
        `${r.counts.libraries} bibliothèques · ${r.counts.users} users Jellyfin · ` +
        `${r.counts.local_users} comptes locaux`,
      `Taille : ${mb} Mo`,
    ].filter(Boolean);
    out.innerHTML = icon(r.ok ? 'check_circle' : 'error') + ' ' + lines.map(esc).join('<br>');
    out.className = 'maint-result ' + (r.ok ? 'ok' : 'err');
  } catch (e) { showStatus(out, false, e.message, 'maint-result'); }
});
document.getElementById('clear-image-cache').addEventListener('click', async () => {
  const out = document.getElementById('clear-image-cache-result');
  out.textContent = 'Nettoyage…';
  out.className = 'maint-result';
  try {
    const r = await postJSON('/api/settings/clear-image-cache', {});
    const mb = (r.bytes / 1048576).toFixed(1);
    showStatus(out, true, `${r.files} fichier(s) supprimé(s), ${mb} Mo libérés`, 'maint-result');
  } catch (e) { showStatus(out, false, e.message, 'maint-result'); }
});
