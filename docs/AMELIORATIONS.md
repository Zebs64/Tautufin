# Propositions d'amélioration

Analyse du code de la v0.2.2 (`jellyfin_stats/`, `data/interfaces/default/`),
novembre 2026. Rien n'est implémenté ici. Chaque constat renvoie à l'endroit du
code où il a été vérifié ; les numéros de ligne sont ceux de la v0.2.2
formatée par ruff (ils ont pu bouger depuis).

Effort : **S** = moins d'une demi-journée, **M** = 1 à 3 jours, **L** = plus.

## En bref

Le code est soigné là où il a été travaillé récemment : la synchronisation est
transactionnelle et testée, l'isolation des données par utilisateur est faite
côté serveur, et le JS échappe ce qu'il injecte dans le DOM. Les faiblesses
sont ailleurs :

1. **La capture d'activité vit uniquement en mémoire.** Un redémarrage de
   l'appli coupe ou perd les lectures en cours. C'est le défaut de conception le
   plus coûteux pour un outil dont le seul travail est d'enregistrer des
   lectures. *Corrigé en v0.3.0.*
2. **Les sessions HTTP ne sont jamais révoquées** quand un compte local est
   supprimé ou que son mot de passe change, et le rôle est figé au login. *Corrigé
   en v0.2.3.*
3. **La machinerie du « curseur » de synchronisation protège une valeur qui ne
   sert presque à rien** (détail en dette technique).

## Tableau récapitulatif (par rapport valeur/effort)

| # | Proposition | Domaine | Valeur | Effort |
|---|---|---|---|---|
| 1 | ✅ v0.2.3 — Révoquer les sessions (suppression, mot de passe, rôle) | Sécurité | Haute | S |
| 2 | ✅ v0.2.3 — Authentifier `POST /webhook` | Sécurité | Haute | S |
| 3 | ✅ v0.2.3 — Migrations atomiques | Robustesse | Haute | S |
| 4 | ✅ v0.3.1 — Filtres de date exploitant les index | Perf | Moyenne | S |
| 5 | ✅ v0.3.2 — Cache d'images : négatif qui expire, taille bornée | Robustesse | Moyenne | S |
| 6 | ✅ v0.2.3 — Mots de passe de plus de 72 octets (bcrypt 5) | Robustesse | Moyenne | S |
| 7 | ✅ v0.3.1 — Derrière un reverse proxy : IP réelle, cookie `Secure` | Sécurité | Moyenne | S |
| 8 | ✅ v0.2.3 — Servir Chart.js localement | Robustesse | Moyenne | S |
| 9 | Limiter l'écriture `last_active` à chaque requête | Perf | Faible | S |
| 10 | ✅ v0.3.0 — Persister les sessions de lecture en cours | Robustesse | **Très haute** | M |
| 11 | ✅ v0.3.0 — Stocker `series_id` dans l'historique | Données/Perf | Haute | M |
| 12 | ✅ v0.3.1 — Dédoublonnage tolérant entre imports et capture live | Données | Haute | M |
| 13 | ✅ v0.3.0 — Retirer du catalogue les médias supprimés de Jellyfin | Données | Moyenne | M |
| 14 | ✅ v0.3.1 — Contrôle d'origine (CSRF) et logout en POST | Sécurité | Moyenne | S |
| 15 | ✗ écarté — Notifications sortantes (ntfy, Discord, webhook) | Fonctionnel | Haute | M |
| 16 | ✅ v0.3.2 — Export CSV de l'historique | Fonctionnel | Moyenne | S |
| 17 | ✗ écarté — API en lecture seule par jeton (widgets de tableau de bord) | Fonctionnel | Moyenne | M |
| 18 | ✅ — Simplifier le curseur de synchronisation | Dette | Moyenne | S |
| 19 | ✅ — Découper `main.py` en routeurs | Dette | Moyenne | M |
| 20 | Sortir le JS inline des templates | Dette | Faible | M |
| 21 | Divers (code mort, tests de chaînes, SVG, setup) | Dette | Faible | S |
| 22 | Horodatages locaux naïfs (heure d'été) | Données | Faible | L |

Faits : n° 1, 2, 3, 6 et 8 en v0.2.3 (n° 5 en partie), n° 10, 11 et 13 en
v0.3.0, n° 4, 7, 12 et 14 en v0.3.1, n° 5 et 16 en v0.3.2. Les n° 15
(notifications) et 17 (API) sont écartés : pas de besoin, on reste simple.
Restent la dette technique (n° 18 à 22).

---

## Robustesse de la synchronisation et de la capture

### 10. Persister les sessions de lecture en cours — valeur très haute, M

**Statut : fait en v0.3.0** (table `active_sessions`, heure murale,
`stopped_at` = dernière présence, finalisation avant retrait).

**Problème.** L'état des lectures suivies est un simple dictionnaire en mémoire
(`activity.py:186`, `self._sessions`). Conséquences :

- **arrêt propre** (`docker stop`, mise à jour d'image) : `Scheduler.stop`
  (`scheduler.py:477`) finalise toutes les sessions suivies. Une lecture qui
  continue est donc coupée en deux. La première moitié est perdue si elle est
  sous `minimum_duration` (300 s par défaut) ; la seconde repart avec un
  nouveau `started_at` ;
- **arrêt brutal** (OOM, `kill -9`, coupure de courant) : tout ce qui était en
  cours est perdu, sans trace ;
- `started_at` est l'heure du premier polling (`activity.py:285`), pas le début
  réel de la lecture.

Tautulli a le même modèle de polling, mais garde ses sessions actives dans une
table SQLite (`sessions`, `plexpy/__init__.py:614` du clone de référence), qui
survit donc à un redémarrage.

**Solution.** Une table `active_sessions` (clé = `session_key`) mise à jour à
chaque polling avec `watched_seconds`, `position_ticks`, `paused` et
`last_seen` (horloge murale, pas monotone). Au démarrage :
- les sessions encore présentes dans `/Sessions` reprennent leur cumul ;
- celles qui ont disparu sont finalisées avec leur cumul enregistré.

`Scheduler.stop` ne finalise plus rien, il se contente de laisser la table à
jour. (Estimer `started_at` à `now - PositionTicks` serait faux pour une
lecture reprise en cours de média : à ne pas faire.)

**Fichiers.** `activity.py`, `scheduler.py`, `database.py` (migration v8),
tests sur le cycle arrêt/reprise.

### 12. Dédoublonnage tolérant entre imports et capture live — valeur haute, M

**Statut : fait en v0.3.1, sous une forme restreinte.** Mesure sur la base de prod :
aucun doublon import/live, mais 318 paires d'imports à moins de 5 minutes dont
70 à 85 % de durées différentes, soit des segments d'une même lecture
(Playback Reporting écrit une ligne par reprise après pause). Une fenêtre
appliquée à toutes les sources aurait donc supprimé du temps vu. Seul le
recouvrement avec une lecture **live** (±2 min) est écarté ; les lignes d'une
même source importée ne sont jamais fusionnées. La solution ci-dessous
(fenêtre générale, factorisée avec l'inférence) est à éviter.

**Problème.** Les imports Playback Reporting et Streamystats dédoublonnent par
`INSERT OR IGNORE` sur l'index unique `(utilisateur, média, started_at)`
(`import_playback.py:233`, `import_streamystats.py:184`). C'est une égalité à la
seconde près. Or :

- une lecture capturée en live a pour `started_at` l'heure du premier polling,
  à 15 s près ;
- Playback Reporting enregistre son propre horodatage.

Importer une période déjà couverte par la capture live crée donc des doublons.
Il en va de même entre les deux sources d'import dès que les horloges divergent
d'une seconde. Le README promet l'idempotence : c'est vrai pour réimporter le
même fichier, pas d'une source à l'autre. L'inférence fait déjà mieux, avec une
tolérance de ±24 h (`infer_history.py:173`).

**Solution.** Avant l'insertion, chercher une lecture existante du même couple
(utilisateur, média) dans une fenêtre de quelques minutes (`julianday`, comme
l'inférence), et la compter comme « déjà présente » dans le rapport d'import.
Garder l'index unique comme filet de sécurité.

**Fichiers.** `import_playback.py`, `import_streamystats.py`, factorisation
possible avec `infer_history.py`.

### 3. Migrations atomiques — valeur haute, S

**Statut : fait en v0.2.3** (transaction explicite par migration ; la sauvegarde automatique avant migration reste à faire).

**Problème.** `database.init` (`database.py:205`) applique les instructions d'une
migration dans le contexte `db()`, qui suppose une transaction. Mais le module
`sqlite3` n'ouvre pas de transaction implicite avant un `CREATE` ou un `ALTER`.
Vérifié : dans `db()`, un `CREATE TABLE` suivi d'une instruction en échec reste
créé après le rollback. Une migration à plusieurs instructions qui échoue au
milieu laisse donc un schéma partiel, avec un numéro de version non incrémenté.
Au démarrage suivant, la migration repart du début et échoue sur
« table already exists ». L'appli ne démarre plus.

**Solution.** `conn.execute("BEGIN")` explicite avant chaque migration (SQLite
sait annuler les DDL), avec la mise à jour de `schema_version` dans la même
transaction. Prévoir aussi une copie de sauvegarde automatique du fichier avant
la première migration en attente. Dix lignes.

**Fichiers.** `database.py`, un test qui injecte une migration en échec.

### 13. Retirer du catalogue les médias supprimés — valeur moyenne, M

**Statut : fait en v0.3.0** (`items.removed_at`, marqué par bibliothèque
inventoriée avec succès ; environ 470 fantômes sur la base de prod du
2026-11-11).

**Problème.** `sync_libraries_and_items` (`scheduler.py:108`) insère et met à
jour, mais ne supprime jamais. Un média retiré de Jellyfin reste dans `items`
pour toujours, et apparaît dans les catalogues et les compteurs des pages
Bibliothèques. Les utilisateurs, eux, sont désactivés (`scheduler.py:102`).
L'asymétrie montre que c'est un oubli.

**Solution.** L'inventaire de chaque bibliothèque est déjà complet. Après une
synchro réussie, marquer `removed_at` sur les `items` de la bibliothèque absents
de l'inventaire (plutôt que `DELETE` : l'historique y fait référence). Les
catalogues filtrent `removed_at IS NULL`, et l'historique garde le nom.

**Fichiers.** `scheduler.py`, `libraries.py`, `database.py` (colonne).

### 5. Cache d'images : négatif qui expire, taille bornée — valeur moyenne, S

**Statut : fait en v0.3.2** (le négatif expire après 24 h depuis la v0.2.3 ; cache borné
à 500 Mo par la boucle horaire, les plus anciens d'abord, et bouton « Vider le
cache d'images » dans Maintenance).

**Problème.** `_cached_fetch` (`main.py:829`) écrit un fichier vide quand
Jellyfin n'a pas d'image (`main.py:839`), et ce cache négatif n'expire jamais.
Un film ajouté avant que Jellyfin ait récupéré son affiche garde le placeholder
définitivement, jusqu'à suppression manuelle de `config/cache/images`. Le cache
positif n'a pas non plus de limite de taille : une entrée par média et par
largeur demandée.

**Solution.** Considérer un fichier vide plus vieux que 24 h comme absent
(`stat().st_mtime`). Ajouter une purge simple (les plus anciens au-delà de N Mo)
dans la boucle de nettoyage horaire existante, et un bouton « vider le cache
d'images » dans Maintenance.

**Fichiers.** `main.py`, `scheduler.py` (`_cleanup_loop`).

### 6. Mots de passe de plus de 72 octets — valeur moyenne, S

**Statut : fait en v0.2.3** (refus explicite, vérification tronquée à 72 octets pour les anciens comptes).

**Problème.** `requirements.txt` n'avait pas de borne haute, donc l'image Docker
embarque bcrypt 5. Or bcrypt 5 lève `ValueError` au-delà de 72 octets, au lieu
de tronquer en silence. `hash_password` (`auth.py:78`) ne gère pas ce cas :
créer ou réinitialiser un compte local avec une longue phrase de passe renvoie
une erreur 500. Pire, un compte créé sous bcrypt 4 avec un tel mot de passe ne
peut plus se connecter : `check_password` attrape l'erreur et renvoie `False`.

**Solution.** Refuser explicitement au-delà de 72 octets avec un message clair.
À défaut, pré-hacher en SHA-256 puis base64 avant bcrypt, ce qui demande une
migration des empreintes existantes. Le refus explicite suffit.

**Fichiers.** `auth.py`, `main.py` (formulaires setup et réglages).

### 8. Servir Chart.js localement — valeur moyenne, S

**Statut : fait en v0.2.3.**

**Problème.** Chart.js est chargé depuis jsDelivr dans quatre templates
(`graphs.html:76`, `clients.html`, `user.html`, `media.html`), sans attribut
`integrity`. Une instance sur un réseau local sans accès Internet n'affiche
aucun graphique, chaque page vue fait une requête vers un tiers, et une
compromission du CDN exécuterait du code dans l'appli.

**Solution.** Copier `chart.umd.min.js` (version épinglée) dans
`static/vendor/` et pointer les quatre templates dessus. Garder la version dans
le nom du fichier pour le cache navigateur.

**Fichiers.** `data/interfaces/default/templates/{graphs,clients,user,media}.html`,
`data/interfaces/default/static/vendor/`.

---

## Performances SQLite

Volumétrie de référence : quelques dizaines de milliers de lectures et autant
de médias. Rien n'est dramatique à cette échelle, mais trois motifs empêchent
SQLite d'utiliser les index existants (vérifié avec `EXPLAIN QUERY PLAN`).

### 4. Filtres de date exploitant les index — valeur moyenne, S

**Statut : fait en v0.3.1.** Résultats identiques sur 70 cas, temps
divisés par 2 à 3 sur une copie de prod multipliée par 50 (~446 000 lectures) :
historique 64 → 31 ms, graphique annuel 118 → 48 ms, clients 728 → 292 ms,
Wrapped 301 → 102 ms.

**Problème.**
- `history.py:48` filtre `date(started_at) >= date(?)` : envelopper la colonne
  dans une fonction donne un `SCAN` complet de l'index au lieu d'un `SEARCH`.
- Le filtre par année `strftime('%Y', started_at) = ?` (`graphs.py:38`,
  `clients.py:11`, `wrapped.py:58`, `:95`, `:186`) a le même défaut. En vue
  globale (sans filtre utilisateur), toute la table est lue.

**Solution.** Des bornes directes : `started_at >= ? AND started_at < ?`, avec
`'AAAA-01-01'` et `'AAAA+1-01-01'`, ou `date(?, '+1 day')` pour la borne haute
de l'historique. Le format texte `AAAA-MM-JJ HH:MM:SS` se compare correctement.

**Fichiers.** `history.py`, `graphs.py`, `clients.py`, `wrapped.py`.

### 11. Stocker `series_id` dans l'historique — valeur haute, M

**Statut : fait en v0.3.0** (`items.series_id` rempli par la synchro, épisodes
réenrichis une fois, rattachement de l'historique après chaque synchro ; repli
par nom pour les lectures dont le média a disparu). Reste : les tops par série
(`graphs.top_media`, `popular_media`, profil, Wrapped) regroupent encore par
`series_name`, donc fusionneraient deux séries homonymes ; ils prennent
toutefois l'affiche et le lien par identifiant.

**Problème.** La vignette de chaque ligne d'historique cherche la série par son
**nom** (`history.py:78`), via une sous-requête corrélée qui parcourt toute la
table `items` (pas d'index sur `type, name`), et ce pour chaque ligne affichée.
Le proxy d'images fait de même (`main.py`, route `/image/item`). Au-delà du
coût, c'est faux dès que deux séries portent le même nom (remakes, versions
US/UK) : la mauvaise affiche s'affiche. Or `series_id` est déjà capturé
(`activity.py:265`), puis jeté à l'insertion (`activity.py:319`).

**Solution.** Une colonne `series_id` dans `session_history`, remplie par la
capture live et par les imports quand la source la fournit. Pour l'existant, un
rattrapage par le nom (une fois) puis des jointures par identifiant. À défaut,
un simple index `items(type, name)` règle la performance, pas la justesse.

**Fichiers.** `activity.py`, `database.py`, `history.py`, `main.py`, imports.

### 9. Limiter l'écriture `last_active` — valeur faible, S

**Problème.** `resolve_session` (`auth.py:221`) fait deux ou trois lectures et
**une écriture** (`auth.py:248`) à chaque requête authentifiée. La page
Graphiques déclenche une douzaine d'appels API en parallèle ; chaque requête
SQL ouvre sa propre connexion et réexécute `PRAGMA journal_mode=WAL`
(`database.py:231`). Ces écritures
concurrentes se disputent le verrou SQLite avec le poller et la synchro.

**Solution.** Ne mettre à jour `last_active` que s'il date de plus de 5
minutes. Passer `journal_mode=WAL` une seule fois dans `init` : ce réglage est
persistant dans le fichier.

**Fichiers.** `auth.py`, `database.py`.

---

## Sécurité

Modèle actuel : sessions côté serveur, cookie signé `HttpOnly` +
`SameSite=Lax` (`main.py:477`), isolation des données imposée côté serveur
(`scoped_user_id`), proxy d'images qui valide les identifiants. C'est une bonne
base. Les trous :

### 1. Révoquer les sessions — valeur haute, S

**Statut : fait en v0.2.3** (y compris le garde-fou sur le dernier admin local).

**Problème.**
- `delete_local_user` (`auth.py:113`) supprime le compte mais pas ses lignes
  `http_sessions`, et `resolve_session` ne vérifie pas que le compte local
  existe encore. **Un compte supprimé reste connecté jusqu'à 7 jours.**
- `set_local_password` (`auth.py:117`) ne coupe pas non plus les sessions. Après
  une compromission, réinitialiser le mot de passe ne déconnecte pas l'intrus.
- Le rôle est copié dans la session au login (`auth.py:178`) et jamais
  réévalué. Un admin Jellyfin rétrogradé (ou un compte local qu'on voudrait
  rétrograder) garde les droits admin jusqu'à expiration.

**Solution.** Supprimer les sessions dans les deux fonctions. Dans
`resolve_session`, relire le rôle courant : `local_users.role`, ou
`users.is_admin` pour une session Jellyfin, comme c'est déjà fait pour
`access_blocked` et `can_view_all`. Ajouter au passage un garde-fou contre la
suppression du dernier admin local.

**Fichiers.** `auth.py`, `main.py`, tests.

### 2. Authentifier `POST /webhook` — valeur haute, S

**Statut : fait en v0.2.3** (en-tête `X-Tautufin-Token` ou `?token=`).

**Problème.** `POST /webhook` (`main.py:1279`) n'a aucune authentification. Il
ne permet pas d'injecter de l'historique, mais n'importe qui pouvant joindre
l'appli et connaissant (ou devinant à partir des pages) un couple utilisateur
et média peut :
- clore prématurément une lecture en cours (`PlaybackStop`) ;
- la marquer en pause (`PlaybackProgress`), ce qui arrête le décompte du temps
  vu.

**Solution.** Un secret partagé (`[Monitoring] webhook_token`, généré comme
`secret_key`), exigé en en-tête ou en paramètre et comparé avec
`secrets.compare_digest`. À défaut de jeton configuré, l'endpoint répond 404.
Le plugin Jellyfin Webhook sait ajouter un en-tête.

**Fichiers.** `main.py`, `config.py`, README.

### 7. Derrière un reverse proxy — valeur moyenne, S

**Statut : fait en v0.3.1** (`[Web] trusted_proxies` transmis à uvicorn, cookie `Secure`
déduit du schéma de la requête). Défaut inchangé (`127.0.0.1`) : à renseigner
derrière Traefik.

**Problème.** Le README propose un déploiement derrière Traefik, mais :
- le rate limit du login est indexé sur `request.client.host` (`main.py:440`).
  `uvicorn.run` (`main.py:1346`) est lancé sans `proxy_headers` ni
  `forwarded_allow_ips`, donc derrière un proxy toutes les tentatives
  partagent l'IP du proxy. Cinq mauvais mots de passe par minute, de n'importe
  qui, bloquent la connexion de tout le monde ;
- le cookie de session n'a pas l'attribut `Secure` ;
- le limiteur est en mémoire, donc remis à zéro au redémarrage (acceptable).

**Solution.** Une option `[Web] trusted_proxies` passée à `forwarded_allow_ips`.
Le cookie `Secure` peut se déduire automatiquement de `request.url.scheme` une
fois les en-têtes du proxy pris en compte.

**Fichiers.** `main.py`, `config.py`, README.

### 14. Contrôle d'origine (CSRF) et logout en POST — valeur moyenne, S

**Statut : fait en v0.3.1** (middleware sur POST/PUT/PATCH/DELETE : `Origin`, à défaut
`Referer`, comparé à `Host` ou `X-Forwarded-Host` ; `null` refusé ; sans les
deux en-têtes, requête hors navigateur acceptée ; déconnexion en POST).

**Problème.** `SameSite=Lax` bloque l'envoi du cookie sur un POST venant d'un
**autre site**, mais pas d'un autre sous-domaine du même site. Or c'est le cas
typique en auto-hébergement : dix services sous `*.mondomaine`, dont certains
moins bien protégés. Aucune route POST ne vérifie l'origine, et toutes les
actions d'admin passent par des formulaires POST (réinitialisation de la base,
suppression de comptes, réglages). `GET /logout` (`main.py:482`) est
déclenchable par une simple image.

**Solution.** Un middleware qui refuse les méthodes non sûres dont l'en-tête
`Origin` (ou, à défaut, `Referer`) ne correspond pas à l'hôte servi. C'est plus
simple qu'un jeton CSRF et suffisant ici. Passer le logout en POST.

**Fichiers.** `main.py`, `base.html`.

### Points mineurs (dans le n° 21)

- **Setup ouvert** : tant que l'assistant n'est pas terminé, quiconque joint
  l'appli peut créer le compte admin (`main.py:371`). C'est le modèle habituel
  de l'auto-hébergé, mais il vaut une phrase dans le README, ou un jeton de
  setup affiché dans les logs au premier démarrage.
- **Logo SVG** : un SVG téléversé (`main.py:168`) est servi en
  `image/svg+xml` sur l'origine de l'appli. Ouvert directement, il exécute son
  script. Seul un admin peut téléverser, d'où un risque faible. Ajouter
  `Content-Security-Policy: script-src 'none'` sur les routes `/branding/*`.
- **Import par chemin** : `/api/import/analyze` lit n'importe quel chemin
  fourni par un admin. Acceptable (réservé aux admins, et c'est la
  fonctionnalité voulue), mais restreindre aux dossiers `uploads/` et au
  montage Jellyfin ne coûterait rien.

---

## Écarts avec Tautulli qui auraient du sens

Tautufin couvre déjà ce qui compte le plus : activité en cours, historique,
graphiques, profils, Wrapped (absent de Tautulli). Ce qui manque et vaut le
coup :

### 15. Notifications sortantes — valeur haute, M

**Statut : écarté pour l'instant** (pas de besoin exprimé, novembre 2026).

La fonctionnalité de Tautulli la plus utilisée après l'historique : être
prévenu d'un début de lecture, d'un transcodage, d'un nouvel utilisateur ou de
l'échec d'une synchronisation. Le point d'accroche existe déjà, avec les
transitions dans `ActivityMonitor.poll` et `_finalize`, et l'état de santé de
la synchro.

**Solution minimale.** Un seul « notifieur » générique, un POST JSON vers une
URL configurée, couvre ntfy, Gotify, Discord (via un format) et n'importe quel
outil d'automatisation. Pas de moteur de modèles à la Tautulli : un payload
fixe et documenté.

**Fichiers.** nouveau `notify.py`, `activity.py`, `scheduler.py`, Réglages.

### 16. Export CSV de l'historique — valeur moyenne, S

**Statut : fait en v0.3.2** (`/api/history.csv`, mêmes filtres et mêmes droits que
l'historique, IP réservées aux admins, cellules de type formule neutralisées).

Une route `/api/history.csv` qui réutilise `history.get_history` avec les mêmes
filtres, sans pagination. C'est la seule façon simple de sortir ses données
autrement qu'avec un backup SQLite.

**Fichiers.** `main.py`, `history.py`, `history.html` (bouton d'export).

### 17. API en lecture seule par jeton — valeur moyenne, M

**Statut : écarté** (pas de besoin, on reste simple ; novembre 2026).

Les tableaux de bord d'auto-hébergement (Homepage, Homarr…) ont des widgets
Tautulli qui interrogent son API. Un jeton d'API admin (`[Auth] api_token`) et
deux ou trois routes stables (`/api/v1/activity`, `/api/v1/stats`) suffiraient.
Les routes actuelles sont internes à l'UI et exigent le cookie.

**Fichiers.** `main.py` (ou un routeur dédié, cf. n° 19), `config.py`,
`auth.py`, README.

### Ce qui ne me semble pas valoir l'effort

- **Newsletter** : lourde (modèles, envoi d'e-mails, planification) pour un
  usage marginal ; Jellyfin a des plugins pour ça.
- **Activité en temps réel par WebSocket ou SSE** : le polling de l'accueil
  toutes les 10 s (`home.js:418`) est suffisant ; le vrai délai vient du
  polling de Jellyfin (15 s). Seule amélioration utile : suspendre le
  rafraîchissement quand l'onglet est masqué (`document.visibilityState`).
- **Arrêt d'un flux à distance** : faisable via l'API Jellyfin, mais c'est une
  action d'administration de Jellyfin, pas de statistiques.

---

## Dette technique

### 18. Simplifier le curseur de synchronisation — valeur moyenne, S

**Statut : fait.** Le mode se déduit de `last_success_at` ; curseur, contrôle
de validité et « Curseur préservé » supprimés. L'ancienne clé reste en base
(seule la réinitialisation l'efface) pour qu'un retour à v0.3.2 reste
incrémental.

**Constat franc.** La synchro stocke un curseur horodaté (`items_cursor_utc`),
le préserve en cas d'échec, le restaure par rollback, expose
« curseur préservé » dans l'UI, et une dizaine de tests vérifient ces garanties.
Or le curseur **n'est jamais envoyé à Jellyfin** : `iter_item_inventory`
(`jellyfin_api.py:159`) ne prend que l'identifiant de bibliothèque, et
l'inventaire est complet à chaque passe. Son seul effet (`scheduler.py:286`)
est de choisir entre deux modes :
- curseur absent → mode « full », qui réenrichit tout ;
- curseur présent → mode « incremental », qui n'enrichit que les ETag
  changés.

Autrement dit, un booléen « une synchro a-t-elle déjà réussi ? ». C'est
l'ETag qui fait tout le travail incrémental, et il le fait bien.

**Solution.** Remplacer le curseur par ce booléen (ou le déduire de
`last_success_at` dans la santé de synchro). On garde le libellé de mode, et on
supprime la notion de « curseur préservé » de l'UI et des tests. Moins de code,
moins de concepts à expliquer.

**Fichiers.** `scheduler.py`, `database.py`, `settings.html`,
`tests/test_sync_*.py`.

### 19. Découper `main.py` — valeur moyenne, M

**Statut : fait.** `main.py` passe de 1 527 à ~210 lignes (application, middlewares,
CLI) ; les routes sont réparties dans `jellyfin_stats/web/` (un module par
domaine, `register(app, context)`), avec un contexte partagé pour les services
et dépendances. Corps des routes déplacés tels quels : table des routes
identique (58 routes, même ordre, mêmes dépendances).

`main.py` fait 1356 lignes, dont l'essentiel dans une seule fonction
`create_app` (`main.py:212`) qui déclare toutes les routes en closures. On ne
peut ni tester une route sans construire toute l'appli, ni s'y retrouver
facilement. Découper en `APIRouter` par domaine (auth, pages, API graphiques,
réglages, import, images), avec les dépendances (`config`, `api`, `monitor`)
injectées via `app.state`, sans changer le comportement.

**Fichiers.** `main.py`, futur dossier `jellyfin_stats/routes/`, tests qui
appellent `create_app`.

### 20. JS front — valeur faible, M

- `settings.html` et `import.html` contiennent respectivement 146 et 122
  lignes de JS inline, qui ne bénéficient ni du cache, ni d'une éventuelle CSP,
  ni du helper `fetchJSON` d'`app.js` (`import.html` refait ses `fetch`).
- Chart.js chargé depuis un CDN : voir le n° 8.
- Aucune organisation en modules : tout est en fonctions globales partagées
  via `app.js`. Ça tient à cette taille (environ 1400 lignes), mais un passage
  aux modules ES natifs (`<script type="module">`, sans outil de build) éviterait
  les dépendances implicites d'ordre de chargement, que
  `test_ui_templates.py` vérifie à la main.

**Fichiers.** `settings.html`, `import.html`, `static/js/*.js`,
`tests/test_ui_templates.py`.
- Point positif à garder : l'échappement (`esc()`) est appliqué de façon
  cohérente sur les données venant de Jellyfin et des clients, y compris
  noms d'appareils et titres.

### 21. Divers — valeur faible, S

- `JellyfinAPI.get_user` (`jellyfin_api.py:113`) n'est appelé nulle part.
- `tests/test_ui_templates.py` vérifie des chaînes dans les templates (ordre
  de cartes, libellés). Ces tests cassent à chaque reformulation sans prouver
  que la page fonctionne. Les remplacer, à terme, par quelques tests de rendu
  via `TestClient` (comme `ClientsPageTests`), ou par un test Playwright.
- `fastapi.testclient` émet un avertissement de dépréciation (Starlette
  recommande `httpx2`) : à suivre lors d'une montée de version.
- Le rate limiter et l'état d'activité supposent un seul processus. C'est le
  cas aujourd'hui, mais il faut le noter pour ne jamais lancer uvicorn avec
  `--workers`.

**Fichiers.** `jellyfin_api.py`, `tests/test_ui_templates.py`, `main.py`
(routes `/branding/*`, setup, import), README.

### 22. Horodatages locaux naïfs — valeur faible, L

Tout est stocké en heure locale sans fuseau (`now_iso`, `datetime('now',
'localtime')`) et dépend de la variable `TZ` du conteneur. À chaque passage à
l'heure d'hiver, une heure existe deux fois : les lectures de cette heure-là
s'ordonnent mal et les durées calculées par différence sont fausses. Changer
`TZ` après coup décale tout l'historique. Passer en UTC au stockage, avec
conversion à l'affichage, est la bonne correction. C'est une migration de
données sensible pour un gain réel faible : à faire seulement si l'appli doit
servir plusieurs fuseaux.

**Fichiers.** `database.py` (`now_iso`, migration), `activity.py`, toutes les
requêtes utilisant `'localtime'` (`graphs.py`, `clients.py`), imports
(`import_playback.py` convertit vers `TZ`), JS d'affichage des dates.
