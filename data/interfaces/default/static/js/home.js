// Dashboard : activité en cours (refresh 10 s) + top stats par période.

const activityBox = document.getElementById('activity');

// Titre principal : pour une série, « Série · SxEy » ; le titre d'épisode
// passe en sous-titre. Poster/backdrop suivent la série (comme un film).
function mediaLabel(s) {
  if (s.series_name)
    return `${s.series_name} · S${s.season_number || 0}E${s.episode_number || 0}`;
  return s.item_name;
}

// Raisons de transcodage Jellyfin → libellés FR (popover ⚠).
const TRANSCODE_REASONS = {
  ContainerNotSupported: 'Conteneur non supporté par le client',
  VideoCodecNotSupported: 'Codec vidéo non supporté',
  AudioCodecNotSupported: 'Codec audio non supporté',
  SubtitleCodecNotSupported: 'Codec de sous-titres non supporté',
  AudioIsExternal: 'Piste audio externe',
  SecondaryAudioNotSupported: 'Audio secondaire non supporté',
  VideoProfileNotSupported: 'Profil vidéo non supporté',
  VideoLevelNotSupported: 'Niveau vidéo non supporté',
  VideoResolutionNotSupported: 'Résolution vidéo non supportée',
  VideoBitrateNotSupported: 'Débit vidéo trop élevé pour le client',
  AudioBitrateNotSupported: 'Débit audio trop élevé',
  AudioChannelsNotSupported: 'Nombre de canaux audio non supporté',
  AudioSampleRateNotSupported: "Fréquence d'échantillonnage audio non supportée",
  AudioProfileNotSupported: 'Profil audio non supporté',
  VideoFramerateNotSupported: "Fréquence d'images non supportée",
  RefFramesNotSupported: 'Trames de référence non supportées',
  AnamorphicVideoNotSupported: 'Vidéo anamorphique non supportée',
  InterlacedVideoNotSupported: 'Vidéo entrelacée non supportée',
  VideoBitDepthNotSupported: 'Profondeur de couleur non supportée',
  VideoRangeTypeNotSupported: 'Plage dynamique (HDR/SDR) non supportée',
  DirectPlayError: 'Erreur de lecture directe',
};
function reasonLabel(code) {
  return TRANSCODE_REASONS[code] || String(code).replace(/([A-Z])/g, ' $1').trim();
}

function methodLabel(m) {
  if (m === 'Transcode') return 'Transcodage';
  if (m === 'DirectStream') return 'Remux direct';
  return 'Lecture directe';
}

// Ligne « source → cible » d'un flux (container/vidéo/audio).
function streamLine(direct, src, tgt) {
  if (!src && !tgt) return '—';
  return (direct || !tgt)
    ? `Lecture directe (${esc(src || '?')})`
    : `Transcodage (${esc(src || '?')} → ${esc(tgt)})`;
}

// Heure de fin estimée d'après la progression.
function etaClock(s) {
  if (!s.runtime_seconds) return '';
  const remaining = s.runtime_seconds * (1 - (s.percent_complete || 0) / 100);
  const end = new Date(Date.now() + remaining * 1000);
  return end.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
}

function npRow(label, value, extra) {
  return `<div class="np-row"><dt>${label}</dt><dd>${value}${extra || ''}</dd></div>`;
}

function activityCard(s) {
  const artId = s.series_id || s.item_id;        // série → fanart/poster série
  const pct = Math.max(0, Math.min(100, Math.round(s.percent_complete || 0)));
  const containerDirect = !s.transcoding || !s.container_tgt
    || s.container_tgt === s.container_src;
  const quality = s.quality_mbps ? `${s.quality_mbps} Mbps` : '—';
  const infoBtn = s.transcoding
    ? `<button type="button" class="np-info-btn" aria-label="Raisons du transcodage">!</button>
       <div class="np-pop" hidden>
         <div class="np-pop-head">⚠ Transcodage en cours</div>
         ${(s.transcode_reasons && s.transcode_reasons.length)
            ? `<ul>${s.transcode_reasons.map(r => `<li>${esc(reasonLabel(r))}</li>`).join('')}</ul>`
            : '<p class="muted">Raison non précisée par le serveur.</p>'}
       </div>`
    : '';
  const loc = s.ip_address
    ? '<div class="np-sep"></div>' + npRow('Localisation',
        `${s.is_lan ? 'LAN' : 'WAN'} · ${esc(s.ip_address)}`)
      + npRow('Bande passante', s.bandwidth_mbps ? `${s.bandwidth_mbps} Mbps` : '—')
    : '';
  const eta = etaClock(s);
  const avatar = s.jellyfin_user_id
    ? `<img class="np-avatar" loading="lazy" alt=""
            src="/image/user/${encodeURIComponent(s.jellyfin_user_id)}">`
    : '';
  return `
    <div class="card now-playing ${s.paused ? 'paused' : 'playing'}"
         style="background-image:url('/image/item/${encodeURIComponent(artId)}?type=Backdrop&w=900')">
      <div class="np-veil"></div>
      <div class="np-main">
        <img class="np-poster" loading="lazy" alt=""
             src="/image/item/${encodeURIComponent(artId)}?w=240">
        <dl class="np-grid">
          <span class="np-platform">${clientLogo(s.product || s.client_name, s.player || s.device_name)}</span>
          ${npRow('Produit', esc(s.product || s.client_name || '?'))}
          ${npRow('Lecteur', esc(s.player || s.device_name || '?'))}
          ${npRow('Qualité', `${quality} <span class="np-dim">${s.transcoding ? 'Transcodage' : 'Original'}</span>`, infoBtn)}
          <div class="np-sep"></div>
          ${npRow('Flux', methodLabel(s.stream_method)
              + (s.transcoding && s.transcode_progress ? ` (${Math.round(s.transcode_progress)}%)` : ''))}
          ${npRow('Conteneur', streamLine(containerDirect, s.container_src, s.transcoding ? s.container_tgt : null))}
          ${npRow('Vidéo', streamLine(s.video_direct, s.video_src, s.video_tgt))}
          ${npRow('Audio', streamLine(s.audio_direct, s.audio_src, s.audio_tgt))}
          ${npRow('Sous-titres', s.subtitle ? esc(s.subtitle) : 'Aucun')}
          ${loc}
        </dl>
      </div>
      <div class="np-progress"><div style="width:${pct}%"></div></div>
      <div class="np-footer">
        <span class="np-state">${s.paused ? '⏸' : '▶'}</span>
        <div class="np-titles">
          <b title="${esc(mediaLabel(s))}">${esc(mediaLabel(s))}</b>
          ${s.series_name ? `<span class="np-ep">S${s.season_number || 0} · E${s.episode_number || 0}</span>` : ''}
        </div>
        <div class="np-foot-time">${fmtDuration(s.watched_seconds)} / ${s.runtime_seconds ? fmtDuration(s.runtime_seconds) : '—'} · ${pct}%${eta ? ` · fin ~${eta}` : ''}</div>
        <div class="np-userbox">${esc(s.user_name)} ${avatar}</div>
      </div>
    </div>`;
}

function popoverOpen() {
  return !!activityBox.querySelector('.np-pop:not([hidden])');
}

async function refreshActivity() {
  // On ne réécrit pas la zone si un popover de transcodage est ouvert
  // (sinon le refresh 10 s le refermerait sous le doigt de l'utilisateur).
  if (popoverOpen()) return;
  try {
    const data = await fetchJSON('/api/activity');
    activityBox.innerHTML = data.sessions.length
      ? data.sessions.map(activityCard).join('')
      : '<p class="muted">Aucune lecture en cours.</p>';
  } catch (e) {
    activityBox.innerHTML = `<p class="err">Activité indisponible : ${esc(e.message)}</p>`;
  }
}

// Ouverture/fermeture du popover des raisons de transcodage.
activityBox.addEventListener('click', e => {
  const btn = e.target.closest('.np-info-btn');
  if (!btn) return;
  e.stopPropagation();
  const pop = btn.parentElement.querySelector('.np-pop');
  const wasOpen = pop && !pop.hasAttribute('hidden');
  activityBox.querySelectorAll('.np-pop').forEach(p => p.setAttribute('hidden', ''));
  if (pop && !wasOpen) pop.removeAttribute('hidden');
});
document.addEventListener('click', () =>
  activityBox.querySelectorAll('.np-pop').forEach(p => p.setAttribute('hidden', '')));

// Définition des blocs (ordre d'affichage, calqué sur Tautulli). top_users
// n'est renvoyé qu'aux admins : on ne rend que les clés présentes.
const STAT_DEFS = [
  {kind: 'top_movies',       icon: '🎬', title: 'Films les plus regardés',     mode: 'metric'},
  {kind: 'popular_movies',   icon: '🍿', title: 'Films les plus populaires',   mode: 'users'},
  {kind: 'top_series',       icon: '📺', title: 'Séries les plus regardées',   mode: 'metric'},
  {kind: 'popular_series',   icon: '🔥', title: 'Séries les plus populaires',  mode: 'users'},
  {kind: 'recently_watched', icon: '🕑', title: 'Vu récemment',                mode: 'recent'},
  {kind: 'top_libraries',    icon: '🗂️', title: 'Bibliothèques les plus actives', mode: 'metric'},
  {kind: 'top_users',        icon: '👥', title: 'Utilisateurs les plus actifs', mode: 'metric'},
  {kind: 'top_clients',      icon: '📱', title: 'Clients les plus utilisés',   mode: 'metric'},
];

// Icône par type de bibliothèque Jellyfin (collection_type).
const LIBRARY_ICONS = {
  movies: '🎬', tvshows: '📺', music: '🎵', musicvideos: '🎤',
  books: '📚', homevideos: '🎥', photos: '🖼️', boxsets: '🎞️',
  livetv: '📡', playlists: '🎼',
};
function libraryIcon(type) { return LIBRARY_ICONS[(type || '').toLowerCase()] || '🗂️'; }

// Blocs basés sur un média (poster + backdrop hero). Les autres (users,
// clients) gardent le dégradé du thème avec un avatar / une icône.
const MEDIA_KINDS = new Set(
  ['top_movies', 'popular_movies', 'top_series', 'popular_series', 'recently_watched']);

function fmtRelative(ts) {
  if (!ts) return '';
  const d = new Date(String(ts).replace(' ', 'T'));
  if (isNaN(d)) return '';
  const sec = Math.round((Date.now() - d.getTime()) / 1000);
  if (sec < 90) return "à l'instant";
  // Paliers successifs : minutes → heures → jours → mois → années.
  const steps = [['min', 60], ['h', 24], ['j', 30], ['mois', 12], ['an', Infinity]];
  let value = sec / 60, unit = 'min';
  for (const [name, step] of steps) {
    unit = name;
    if (value < step) break;
    value /= step;
  }
  const n = Math.round(value);
  return `il y a ${n} ${unit}${unit === 'an' && n > 1 ? 's' : ''}`;
}

// Fond « hero » de la carte : backdrop du média #1 (retombe sur le poster
// côté serveur si pas de backdrop).
function statHero(kind, row0) {
  if (row0 && MEDIA_KINDS.has(kind) && row0.image_id)
    return `/image/item/${encodeURIComponent(row0.image_id)}?type=Backdrop&w=640`;
  return '';
}

// Élément #1 mis en avant : grand poster, avatar ou icône de client.
function statFeature(kind, row0) {
  if (!row0) return '';
  if (MEDIA_KINDS.has(kind) && row0.image_id)
    return `<img class="stat-feature poster" loading="lazy" alt=""
                 src="/image/item/${encodeURIComponent(row0.image_id)}?w=240">`;
  if (kind === 'top_users' && row0.user_id)
    return `<img class="stat-feature avatar avatar-lg" loading="lazy" alt=""
                 src="/image/user/${encodeURIComponent(row0.user_id)}">`;
  if (kind === 'top_clients')
    return `<span class="stat-feature stat-feature-icon">${clientLogo(row0.label, null)}</span>`;
  if (kind === 'top_libraries')
    return `<span class="stat-feature stat-feature-icon">${libraryIcon(row0.collection_type)}</span>`;
  return '';
}

// En-tête : unité affichée à droite du titre (varie selon le type de bloc).
function statUnits(def, rows, metric) {
  if (def.mode === 'recent') return rows[0] ? esc(rows[0].user_name || '') : '';
  if (def.mode === 'users') return 'spectateurs';
  return metric === 'duration' ? 'durée' : 'lectures';
}

// Valeur affichée à droite de chaque ligne.
function statValue(def, row, fmt) {
  if (def.mode === 'recent') return esc(fmtRelative(row.last_watch));
  if (def.mode === 'users') return row.value;
  return fmt(row.value);
}

function statCard(def, rows, fmt, metric) {
  const row0 = rows[0];
  const hero = statHero(def.kind, row0);
  const list = rows.length
    ? rows.map((r, i) => `
        <li${i === 0 ? ' class="selected"' : ''} data-idx="${i}"
            data-image="${esc(r.image_id || '')}" data-user="${esc(r.user_id || '')}"
            data-username="${esc(r.user_name || '')}"
            data-type="${esc(r.collection_type || '')}" data-label="${esc(r.label)}">
          <span class="stat-rank">${i + 1}</span>
          <span class="stat-label" title="${esc(r.label)}">${esc(r.label)}</span>
          <span class="stat-value">${statValue(def, r, fmt)}</span>
        </li>`).join('')
    : '<li class="muted">Aucune donnée sur la période</li>';
  return `
    <div class="card stat-card${hero ? ' has-hero' : ''}" data-kind="${def.kind}"
         ${hero ? `style="background-image:url('${hero}')"` : ''}>
      <div class="stat-veil"></div>
      <div class="stat-inner">
        <div class="stat-head">
          <h3><span class="stat-ico">${def.icon}</span>${esc(def.title)}</h3>
          <span class="stat-units">${statUnits(def, rows, metric)}</span>
        </div>
        <div class="stat-content">
          <div class="stat-feature-wrap">${statFeature(def.kind, row0)}</div>
          <ol class="stat-list">${list}</ol>
        </div>
      </div>
    </div>`;
}

// Clic sur une ligne : surbrillance + bascule du backdrop et du poster de la
// carte sur le média sélectionné (même logique que Tautulli).
function selectStatRow(li) {
  const card = li.closest('.stat-card');
  if (!card || li.classList.contains('muted')) return;
  card.querySelectorAll('.stat-list li').forEach(x => x.classList.remove('selected'));
  li.classList.add('selected');
  const kind = card.dataset.kind;
  const row = {image_id: li.dataset.image, user_id: li.dataset.user,
               label: li.dataset.label, collection_type: li.dataset.type};
  const hero = statHero(kind, row);
  if (hero) { card.classList.add('has-hero'); card.style.backgroundImage = `url('${hero}')`; }
  const wrap = card.querySelector('.stat-feature-wrap');
  if (wrap) wrap.innerHTML = statFeature(kind, row);
  // « Vu récemment » : l'en-tête affiche le spectateur du média sélectionné,
  // pas seulement celui de la lecture la plus récente.
  const def = STAT_DEFS.find(d => d.kind === kind);
  if (def && def.mode === 'recent') {
    const units = card.querySelector('.stat-units');
    if (units) units.textContent = li.dataset.username || '';
  }
}

// Préférences d'affichage persistées entre les sessions (métrique + période).
const STATS_PREF_KEY = 'home.stats';
const statsState = {period: '7d', metric: 'duration'};
try { Object.assign(statsState, JSON.parse(localStorage.getItem(STATS_PREF_KEY) || '{}')); }
catch (e) { /* prefs illisibles : on garde les défauts */ }
function saveStatsPrefs() {
  try { localStorage.setItem(STATS_PREF_KEY, JSON.stringify(statsState)); }
  catch (e) { /* quota / mode privé : sans gravité */ }
}

const statsBox = document.getElementById('home-stats');
statsBox.addEventListener('click', e => {
  const li = e.target.closest('.stat-list li');
  if (li) selectStatRow(li);
});

// Podium animé des 3 plus gros viewers (suit le filtre actif). Disponible
// uniquement quand un classement d'utilisateurs est renvoyé (vue admin).
const podiumBox = document.getElementById('podium');

function podiumPerson(user, place) {
  const avatar = user.user_id
    ? `/image/user/${encodeURIComponent(user.user_id)}`
    : '/static/img/avatar-placeholder.svg';
  return `
    <div class="podium-col place-${place + 1}" style="--i:${place}">
      <div class="podium-person">
        <span class="arm arm-l"></span>
        <span class="arm arm-r"></span>
        <span class="head"><img loading="lazy" alt="" src="${avatar}"></span>
        <span class="body"></span>
      </div>
      <div class="podium-block">
        <span class="podium-medal">${['🥇', '🥈', '🥉'][place]}</span>
        <span class="podium-num">${place + 1}</span>
      </div>
      <div class="podium-meta">
        <div class="podium-name" title="${esc(user.label)}">${esc(user.label)}</div>
        <div class="podium-val"></div>
      </div>
    </div>`;
}

function renderPodium(users, fmt) {
  if (!podiumBox) return;
  const top = (users || []).slice(0, 3);
  if (!top.length) { podiumBox.innerHTML = ''; return; }
  const order = top.length === 1 ? [0]
    : top.length === 2 ? [1, 0] : [1, 0, 2];  // 2e à gauche, 1er au centre
  podiumBox.innerHTML = `
    <div class="podium-title">🏆 Podium des viewers</div>
    <div class="podium-stage">
      ${order.map(p => podiumPerson(top[p], p)).join('')}
    </div>`;
  // Valeurs formatées (durée vs nombre) selon le filtre actif.
  order.forEach(p => {
    const el = podiumBox.querySelector(`.place-${p + 1} .podium-val`);
    if (el) el.textContent = fmt(top[p].value);
  });
}

async function refreshStats() {
  try {
    const stats = await fetchJSON(
      `/api/home_stats?period=${statsState.period}&metric=${statsState.metric}`);
    const fmt = v => statsState.metric === 'duration' ? fmtDuration(v) : v;
    statsBox.innerHTML = STAT_DEFS
      .filter(def => stats[def.kind])
      .map(def => statCard(def, stats[def.kind] || [], fmt, statsState.metric))
      .join('');
    renderPodium(stats.top_users, fmt);
  } catch (e) { /* stats non bloquantes */ }
}

// Reflète l'état restauré sur les boutons ; retombe sur le bouton actif par
// défaut (HTML) si la valeur sauvegardée ne correspond à rien.
function syncToggle(selector, key) {
  const btns = [...document.querySelectorAll(`${selector} .btn`)];
  const match = btns.find(b => b.dataset[key] === statsState[key])
    || btns.find(b => b.classList.contains('active')) || btns[0];
  btns.forEach(b => b.classList.toggle('active', b === match));
  if (match) statsState[key] = match.dataset[key];
}

function bindToggle(selector, key) {
  document.querySelectorAll(`${selector} .btn`).forEach(btn =>
    btn.addEventListener('click', () => {
      document.querySelectorAll(`${selector} .btn`).forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      statsState[key] = btn.dataset[key];
      saveStatsPrefs();
      refreshStats();
    }));
}

syncToggle('#period-select', 'period');
syncToggle('#metric-select', 'metric');
bindToggle('#period-select', 'period');
bindToggle('#metric-select', 'metric');

refreshActivity();
refreshStats();
setInterval(refreshActivity, 10000);
