// Import d'historique (upload, analyse, import) et inférence depuis le statut « Lu ».
const pathInput = document.getElementById('import-path');
const btnAnalyze = document.getElementById('btn-analyze');
const btnImport = document.getElementById('btn-import');
const fileInput = document.getElementById('import-file');
const btnUpload = document.getElementById('btn-upload');
const uploadResult = document.getElementById('upload-result');

fileInput.addEventListener('change', () => {
  btnUpload.disabled = !fileInput.files.length;
  uploadResult.textContent = '';
});

btnUpload.addEventListener('click', async () => {
  const file = fileInput.files[0];
  if (!file) return;
  btnUpload.disabled = true;
  btnUpload.textContent = 'Envoi en cours…';
  uploadResult.textContent = '';
  try {
    const form = new FormData();
    form.append('file', file);
    const r = await fetch('/api/import/upload', {
      method: 'POST', credentials: 'same-origin', body: form,
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
    uploadResult.textContent =
      `✓ ${body.filename} (${(body.size / 1048576).toFixed(1)} Mo)`;
    uploadResult.className = 'ok';
    pathInput.value = body.path;
    btnAnalyze.click();   // enchaîne directement sur l'analyse
  } catch (e) {
    uploadResult.textContent = '✗ ' + e.message;
    uploadResult.className = 'err';
  } finally {
    btnUpload.disabled = false;
    btnUpload.textContent = '📤 Envoyer le fichier';
  }
});

btnAnalyze.addEventListener('click', async () => {
  btnAnalyze.disabled = true; btnAnalyze.textContent = 'Analyse…';
  try {
    const r = await postJSON('/api/import/analyze', {path: pathInput.value});
    const FORMATS = {
      tsv: 'Backup .tsv Playback Reporting',
      sqlite: 'Base SQLite Playback Reporting',
      streamystats: 'Backup Streamystats (.json)',
    };
    document.getElementById('an-format').textContent =
      (FORMATS[r.format] || r.format)
      + (r.server_name ? ` — serveur « ${r.server_name} »` : '');
    document.getElementById('an-malformed-row').classList.toggle('hidden', !r.malformed);
    document.getElementById('an-malformed').textContent = r.malformed;
    document.getElementById('an-entries').textContent = r.entries;
    document.getElementById('an-dates').textContent =
      r.date_min ? `${fmtDate(r.date_min)} → ${fmtDate(r.date_max)}` : '—';
    document.getElementById('an-users').innerHTML = r.users.map(u =>
      `<tr><td>${esc(u.username || '(inconnu)')}</td>` +
      `<td><code>${esc(u.user_id)}</code></td><td>${u.entries}</td></tr>`).join('');
    document.getElementById('analyze-result').classList.remove('hidden');
    btnImport.disabled = r.entries === 0;
  } catch (e) { alert('Analyse impossible : ' + e.message); }
  finally { btnAnalyze.disabled = false; btnAnalyze.textContent = 'Analyser'; }
});

btnImport.addEventListener('click', async () => {
  btnImport.disabled = true; btnImport.textContent = 'Import en cours…';
  try {
    const r = await postJSON('/api/import/run', {path: pathInput.value});
    for (const k of ['imported', 'duplicates', 'filtered', 'errors'])
      document.getElementById('imp-' + k).textContent = r[k];
    document.getElementById('import-result').classList.remove('hidden');
  } catch (e) { alert('Import impossible : ' + e.message); }
  finally { btnImport.disabled = false; btnImport.textContent = 'Importer'; }
});

// --- Inférence depuis le statut « Lu » de Jellyfin ---
const inferUser = document.getElementById('infer-user');
const btnInfer = document.getElementById('btn-infer');
const btnInferClean = document.getElementById('btn-infer-clean');
const inferCount = document.getElementById('inf-count');
const cleanResult = document.getElementById('infer-clean-result');

btnInfer.addEventListener('click', async () => {
  btnInfer.disabled = true; btnInferClean.disabled = true;
  btnInfer.textContent = 'Inférence en cours…';
  cleanResult.textContent = '';
  try {
    const r = await postJSON('/api/infer/run', {user_id: inferUser.value});
    for (const k of ['users', 'created', 'duplicates', 'filtered',
                     'no_date', 'deleted', 'errors'])
      document.getElementById('inf-' + k).textContent = r[k];
    document.getElementById('infer-result').classList.remove('hidden');
    inferCount.textContent = r.inferred_count;
  } catch (e) { alert('Inférence impossible : ' + e.message); }
  finally {
    btnInfer.disabled = false; btnInferClean.disabled = false;
    btnInfer.textContent = "Inférer l'historique";
  }
});

btnInferClean.addEventListener('click', async () => {
  const scope = inferUser.value
    ? "de cet utilisateur" : "de TOUS les utilisateurs";
  if (!confirm(`Supprimer les sessions inférées ${scope} ? `
    + "Les lectures réelles (live/import) ne sont pas touchées.")) return;
  btnInfer.disabled = true; btnInferClean.disabled = true;
  btnInferClean.textContent = 'Suppression…';
  try {
    const r = await postJSON('/api/infer/cleanup', {user_id: inferUser.value});
    inferCount.textContent = r.inferred_count;
    cleanResult.textContent = `✓ ${r.deleted} session(s) inférée(s) supprimée(s)`;
    cleanResult.className = 'ok';
    document.getElementById('infer-result').classList.add('hidden');
  } catch (e) {
    cleanResult.textContent = '✗ ' + e.message; cleanResult.className = 'err';
  } finally {
    btnInfer.disabled = false; btnInferClean.disabled = false;
    btnInferClean.textContent = '🗑️ Supprimer les sessions inférées';
  }
});
