# Tautufin

Statistiques et historique de lecture pour **Jellyfin** — l'équivalent de
[Tautulli](https://github.com/Tautulli/Tautulli) (Plex), construit sur l'API
REST de Jellyfin.

- 📊 Graphiques riches (Chart.js) : lectures dans le temps, films vs séries vs
  musique, top utilisateurs, genres, résolutions, clients, Direct Play vs
  transcodage…
- 📜 Historique de lecture filtrable (utilisateur, type, bibliothèque,
  période), triable et paginé
- 👥 **Multi-utilisateurs** : chaque utilisateur Jellyfin consulte ses propres
  stats ; seul l'admin voit la vue globale
- ⏱️ Seuils de durée minimale de lecture (comportement Tautulli)
- 📦 Import de l'historique du plugin
  [Playback Reporting](https://github.com/jellyfin/jellyfin-plugin-playbackreporting)
  (idempotent)
- 🌙 Thème sombre inspiré du skin [ElegantFin](https://github.com/lscambo13/ElegantFin), responsive
- 🖼️ Posters et avatars récupérés depuis Jellyfin (proxy côté serveur avec
  cache — la clé API n'est jamais exposée au navigateur)
- 🎨 Logo et favicon personnalisables (Settings → Apparence)

## Installation

### Docker (recommandé)

```bash
mkdir -p config        # important : sinon Docker crée le dossier en root
docker compose up -d
```

L'app écoute sur `http://localhost:8181`. La configuration et la base SQLite
sont persistées dans `./config/` (le conteneur tourne en utilisateur non-root,
le dossier doit donc appartenir à votre utilisateur).

### Manuelle

Prérequis : Python ≥ 3.10.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py                      # ou : python -m jellyfin_stats.main
```

Options : `--config <chemin>` (défaut `./config.ini`), `--host`, `--port`,
`--debug`.

## Premier lancement

Au premier accès, un **wizard de setup** s'affiche :

1. **URL du serveur Jellyfin** + **clé API** (à générer dans Jellyfin :
   *Tableau de bord → Avancé → Clés API*). Optionnel à cette étape,
   configurable ensuite dans *Settings*.
2. **Compte admin local de secours** (obligatoire) : utilisable même si
   Jellyfin est injoignable.

Une fois le setup terminé, une première synchronisation (utilisateurs,
bibliothèques, médias) démarre en arrière-plan, puis se répète périodiquement
(`sync_interval`).

### Webhook temps réel (optionnel)

Le polling de `/Sessions` suffit à capturer l'activité. Pour une réaction
immédiate aux play/pause/stop, installez le plugin **Webhook** dans Jellyfin et
ajoutez une destination *Generic* vers `http://<jellyfin-stats>:8181/webhook`
(format JSON, événements *Playback*).

## Les deux modes d'authentification

### 1. Compte Jellyfin (recommandé)

L'utilisateur saisit ses identifiants Jellyfin ; l'app les vérifie via
`POST /Users/AuthenticateByName`. L'`UserId` Jellyfin sert d'identifiant de
référence, et les admins Jellyfin (`Policy.IsAdministrator`) sont
automatiquement admins de l'app. Aucun mot de passe n'est géré ni stocké par
l'app, et **le token Jellyfin n'est jamais persisté** : seuls l'UserId, le nom
et le rôle sont conservés dans la session serveur.

### 2. Compte local

Comptes créés par l'admin dans *Settings* (stockage bcrypt). Un compte local
peut être **lié** à un utilisateur Jellyfin (il consulte alors ses stats) ou
rester non lié (ex. : admin technique sans compte Jellyfin, compte de secours,
accès externe sans exposer les credentials Jellyfin).

Reset de mot de passe local (CLI uniquement) :

```bash
python main.py --reset-password <username>
```

> **Choix d'implémentation — page de login à deux onglets.** La spec laissait
> le choix entre deux onglets explicites et une auto-détection (tenter
> Jellyfin, fallback local). Les deux onglets ont été retenus : l'auto-
> détection enverrait les mots de passe des comptes locaux au serveur Jellyfin
> (fuite inutile), rendrait les messages d'échec ambigus et doublerait les
> tentatives comptées par le rate limiting. Chaque onglet disparaît si le mode
> correspondant est désactivé dans la config.

### Permissions (deux rôles)

| Capacité                              | Admin | Utilisateur |
|---------------------------------------|:-----:|:-----------:|
| Voir ses propres stats & historique   | ✓     | ✓           |
| Voir les stats & historique globaux   | ✓     | ✗           |
| Voir les stats de tous les users      | ✓     | ✗           |
| Accéder aux Settings                  | ✓     | ✗           |
| Gérer les comptes locaux              | ✓     | ✗           |
| Déclencher un import Playback Report  | ✓     | ✗           |
| Voir le dashboard d'activité en cours | ✓     | ✗ (sa session uniquement) |

Les routes admin répondent **HTTP 403** (pas de redirect silencieux) ; le
filtrage par utilisateur est imposé côté serveur depuis la session, jamais
depuis un paramètre client. Le login est limité à 5 tentatives/minute/IP.

## Configuration (`config.ini`)

Voir [`config.ini.example`](config.ini.example) (commenté). Sections :

- `[Jellyfin]` : `url`, `api_key`, `verify_ssl`
- `[Auth]` : `session_lifetime` (défaut 7 jours), `jellyfin_auth_enabled`,
  `local_auth_enabled`, `secret_key` (généré au premier lancement)
- `[Monitoring]` :
  - `minimum_duration` (défaut 300) : durée minimale en secondes avant qu'une
    session soit enregistrée ; `0` = désactivé
  - `minimum_percent` (défaut 0) : pourcentage minimal du média regardé ;
    **condition OR** avec `minimum_duration` (comme Tautulli) — la session est
    gardée si *au moins un* des seuils actifs est atteint
  - `poll_interval`, `sync_interval`
- `[UI]` : `allow_user_library_pages`
- `[Web]` / `[Database]` : écoute HTTP, chemin SQLite

Les sessions ignorées par les seuils sont loguées en `DEBUG` (lancer avec
`--debug`) avec la raison.

## Import depuis Playback Reporting

Page *Import* (admin) — deux formats acceptés, détectés automatiquement au
contenu du fichier :

- la **base SQLite** du plugin (`playback_reporting.db`, défaut :
  `<jellyfin_data_dir>/playback_reporting.db`) ;
- un **fichier de backup** généré par le plugin
  (`PlaybackReportingBackup-AAAAMMJJ-HHMMSS.tsv`) : 9 colonnes séparées par
  tabulations, sans en-tête ; les lignes mal formées sont ignorées et
  comptées, comme le fait le plugin lui-même.

1. choisir la source : **upload direct depuis le navigateur** (pratique
   depuis un autre poste, max 512 Mo — le fichier est déposé dans
   `<config>/uploads/`, seul le dernier est conservé) ou chemin d'un fichier
   déjà présent sur le serveur (en Docker, monter le dossier en volume — voir
   `docker-compose.yml`) ;
2. **Analyser** : nombre d'entrées, plage de dates, utilisateurs présents ;
3. **Importer** : migration vers la base interne, noms d'utilisateurs résolus
   via `/Users`, enrichissement (durée totale, genres, bibliothèque) depuis le
   cache de médias, filtres de durée minimale appliqués.

L'import est **idempotent** : clé de déduplication `DateCreated + UserId +
ItemId` (index SQL unique). Rapport final : importées / doublons ignorés /
filtrées par durée minimum / erreurs.

## Architecture & choix techniques

> **Pourquoi FastAPI plutôt que CherryPy ?** CherryPy chez Tautulli est un
> choix historique. FastAPI apporte : la validation/conversion automatique des
> paramètres d'API, un système de dépendances qui modélise naturellement
> `require_auth`/`require_admin`, l'async natif pour les tâches de fond
> (polling, sync) sans thread dédié, et un écosystème actif. Les handlers
> restent synchrones (SQLite l'est aussi) et exécutés en threadpool : pas de
> sur-ingénierie async.

```
jellyfin_stats/
├── main.py              # point d'entrée, CLI, routes FastAPI
├── config.py            # config.ini (défauts, secret auto-généré)
├── database.py          # SQLite + migrations versionnées (schema_version)
├── auth.py              # logins Jellyfin/local, sessions serveur, rate limit
├── jellyfin_api.py      # wrapper API Jellyfin
├── activity.py          # suivi des sessions + seuils de durée minimale
├── history.py           # historique (filtres, tri, pagination)
├── graphs.py            # agrégations pour Chart.js
├── users.py / libraries.py
├── import_playback.py   # import Playback Reporting (idempotent)
└── scheduler.py         # polling /Sessions, sync périodique, purge sessions
data/interfaces/default/ # templates Jinja2 + static (thème sombre)
```

La base est en SQLite (`schema_version` + migrations ordonnées appliquées au
démarrage). Les sessions HTTP vivent côté serveur ; le cookie ne contient
qu'un token signé (`itsdangerous`), `HttpOnly` + `SameSite=Lax`.

### Endpoints Jellyfin utilisés

Référence : <https://api.jellyfin.org> — authentification par clé API via le
header `X-Emby-Authorization`.

| Endpoint                       | Usage                                   |
|--------------------------------|-----------------------------------------|
| `POST /Users/AuthenticateByName` | Login des utilisateurs Jellyfin       |
| `GET /System/Info`             | Test de connexion                       |
| `GET /Users`                   | Synchronisation des utilisateurs        |
| `GET /Sessions`                | Activité en cours (polling)             |
| `GET /Library/VirtualFolders`  | Bibliothèques                           |
| `GET /Items`                   | Médias (paginé, genres/codecs/durées)   |
| `POST /webhook` (entrant)      | Événements play/pause/stop du plugin Webhook |
