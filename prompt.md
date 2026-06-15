# Projet : Jellyfin Stats — équivalent de Tautulli pour Jellyfin

## Contexte

Tautulli (https://github.com/Tautulli/Tautulli) est une application web Python de
monitoring et de statistiques pour Plex Media Server. L'objectif est de créer une
application équivalente pour **Jellyfin**, en s'inspirant de l'architecture et des
fonctionnalités de Tautulli, mais en l'adaptant à l'API Jellyfin.

Clone le repo Tautulli pour référence :

```bash
git clone https://github.com/Tautulli/Tautulli.git tautulli_ref
```

**Ne pas implémenter** : tout ce qui touche aux notifications (agents, newsletters,
webhooks sortants). Ce n'est pas le sujet.

---

## Objectif principal

Créer une application web Python centrée sur les **statistiques et l'historique de
lecture**, avec des graphiques riches, en se connectant à l'API REST de Jellyfin.
L'application est **multi-utilisateurs** : chaque utilisateur Jellyfin peut se
connecter et consulter ses propres stats ; seul l'admin a accès à la vue globale.

---

## Stack technique attendue

- **Backend** : Python 3.10+, framework web léger (CherryPy comme Tautulli, ou FastAPI — à justifier)
- **Base de données** : SQLite (comme Tautulli), pour stocker l'historique, les stats agrégées et les comptes locaux
- **Sessions** : gestion des sessions HTTP côté serveur (cookie signé), avec expiration configurable
- **Frontend** : HTML/CSS/JS, graphiques via **Chart.js**
- **API source** : Jellyfin REST API (authentification par API key, header `X-Emby-Authorization`)
- **Configuration** : fichier `.ini` ou `.env`, sans usine à gaz
- **Docker** : Dockerfile + docker-compose.yml fournis

---

## Authentification & gestion des utilisateurs

### Deux modes de connexion (coexistants)

#### Mode 1 — Authentification Jellyfin (défaut recommandé)

- L'utilisateur saisit ses identifiants Jellyfin (username + password)
- L'app appelle `POST /Users/AuthenticateByName` sur l'API Jellyfin
- Si Jellyfin répond avec un token valide → session ouverte
- L'`UserId` Jellyfin retourné devient l'identifiant de référence dans toute l'app
- Les admins Jellyfin (`Policy.IsAdministrator == true` dans la réponse) obtiennent automatiquement le rôle admin dans l'app
- Avantage : aucune gestion de mots de passe dans l'app, SSO de facto

#### Mode 2 — Comptes locaux (indépendants de Jellyfin)

- Comptes créés manuellement par l'admin dans les Settings de l'app
- Stockés dans la table `local_users` (username, bcrypt hash, role, jellyfin_user_id optionnel)
- Si `jellyfin_user_id` est renseigné → les stats de ce compte local sont liées à cet utilisateur Jellyfin (même historique)
- Si `jellyfin_user_id` est null → compte purement local, sans stats associées (utile pour un admin technique qui n'a pas de compte Jellyfin)
- Cas d'usage : utilisateur sans compte Jellyfin, accès depuis l'extérieur sans exposer les credentials Jellyfin, compte de secours admin

### Page de login

- Formulaire unique avec deux onglets : "Compte Jellyfin" / "Compte local"
- Ou détection automatique : tenter d'abord Jellyfin, fallback local si échec (à trancher à l'implémentation, documenter le choix)
- Pas de lien "mot de passe oublié" pour les comptes Jellyfin (géré côté Jellyfin)
- Reset de mot de passe local : via CLI uniquement

```bash
python main.py --reset-password <username>
```

### Modèle de permissions (deux rôles uniquement, pas de sur-ingénierie)

| Capacité                              | Admin | Utilisateur |
|---------------------------------------|:-----:|:-----------:|
| Voir ses propres stats & historique   | ✓     | ✓           |
| Voir les stats & historique globaux   | ✓     | ✗           |
| Voir les stats de tous les users      | ✓     | ✗           |
| Accéder aux Settings de l'app         | ✓     | ✗           |
| Gérer les comptes locaux              | ✓     | ✗           |
| Déclencher un import Playback Report  | ✓     | ✗           |
| Voir le dashboard d'activité en cours | ✓     | ✗           |

### Comportement des vues selon le rôle

**Vue admin** : accès à tout, tous les filtres "par utilisateur" sont disponibles.

**Vue utilisateur connecté** :

- Le dashboard ne montre que son activité en cours (sa session active si elle existe)
- L'historique est filtré sur son `UserId` uniquement, sans possibilité de changer le filtre utilisateur
- Les graphiques sont calculés sur ses données uniquement
- Sa page profil affiche ses propres stats agrégées
- Les pages "Bibliothèques" et "Médias" restent accessibles en lecture (configurable par l'admin)

### Sécurité

- Toutes les routes API et pages HTML vérifient la session avant de répondre
- Les routes admin retournent HTTP 403 si rôle insuffisant (pas de redirect silencieux)
- Le token Jellyfin n'est **jamais** stocké en base ni exposé au frontend ; seul l'`UserId` et le rôle sont persistés en session serveur
- Rate limiting sur la page de login : 5 tentatives / minute / IP
- En cas de suppression d'un utilisateur Jellyfin, ses données historiques sont conservées mais son accès est révoqué à la prochaine tentative de login

### Table SQL pour les comptes locaux

```sql
CREATE TABLE local_users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username         TEXT UNIQUE NOT NULL,
    password_hash    TEXT NOT NULL,          -- bcrypt
    role             TEXT NOT NULL DEFAULT 'user',  -- 'admin' | 'user'
    jellyfin_user_id TEXT,                   -- nullable, FK logique vers UserId Jellyfin
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_login       DATETIME
);
```

### Configuration liée (`config.ini`)

```ini
[Auth]
# Durée de vie des sessions en secondes (défaut : 7 jours)
session_lifetime = 604800

# Activer ou non l'authentification Jellyfin
jellyfin_auth_enabled = true

# Activer ou non les comptes locaux
local_auth_enabled = true

# Secret pour signer les cookies de session (généré automatiquement au 1er lancement)
secret_key =
```

---

## Import depuis le plugin Playback Reporting

Le plugin Jellyfin "Playback Reporting" (https://github.com/jellyfin/jellyfin-plugin-playbackreporting)
stocke ses données dans une base SQLite séparée.

**Chemin par défaut** : `<jellyfin_data_dir>/playback_reporting.db`

### Schéma de la table principale

```sql
CREATE TABLE PlaybackActivity (
    DateCreated    DATETIME,
    UserId         TEXT,
    ItemId         TEXT,
    ItemType       TEXT,      -- 'Movie', 'Episode', 'Audio', etc.
    ItemName       TEXT,
    PlaybackMethod TEXT,      -- 'DirectPlay', 'DirectStream', 'Transcode'
    ClientName     TEXT,
    DeviceName     TEXT,
    PlayDuration   INTEGER    -- durée effectivement lue, en secondes
);
```

### Fonctionnalité d'import à implémenter

Créer une page **"Import depuis Playback Reporting"** (admin only) :

1. Champ pour spécifier le chemin vers `playback_reporting.db`
2. Bouton "Analyser" : affiche le nombre d'entrées détectées, la plage de dates, un aperçu des utilisateurs présents
3. Bouton "Importer" : migre les données vers la base interne, en résolvant les noms d'utilisateurs via `/Users/{userId}`
4. **Déduplication** : clé de dédup = `DateCreated + UserId + ItemId`
5. Les imports successifs doivent être idempotents
6. Rapport post-import : N entrées importées, N doublons ignorés, N filtrées par durée minimum, N erreurs

> Le champ `PlayDuration` correspond au temps réellement regardé, pas à la durée totale du média.

---

## Durée minimale de lecture (comportement Tautulli)

### Configuration (`config.ini`)

```ini
[Monitoring]
# Durée minimale en secondes avant qu'une session soit enregistrée.
# 0 = désactivé. Valeur recommandée : 300 (5 minutes)
minimum_duration = 300

# Pourcentage minimal du média regardé pour être comptabilisé.
# 0 = désactivé. Condition OR avec minimum_duration (comme Tautulli).
minimum_percent = 0
```

### Comportement attendu

- À la capture d'un événement `playback.stop` : comparer `PlayDuration` avec les seuils — si aucun n'est atteint, ne pas enregistrer la session
- À l'import Playback Reporting : appliquer les mêmes filtres sur `PlayDuration`
- Loguer les sessions ignorées en DEBUG avec la raison
- Page Settings : exposer ces deux paramètres avec une explication claire

Référence Tautulli : `plexpy/activity_handler.py` méthode `process_wait_and_check()`, `plexpy/config.py` clé `MINIMUM_DURATION`.

---

## Fonctionnalités à implémenter (par priorité)

### 1. Connexion & synchronisation Jellyfin

- Configuration de l'URL du serveur + API key admin
- Récupération et stockage : utilisateurs, bibliothèques, médias (films, séries, épisodes, musique)
- Synchronisation périodique depuis `/Sessions`
- Endpoint POST `/webhook` pour les événements temps réel (play, pause, stop, resume)

### 2. Tableau de bord (Home)

- **Admin** : activité en cours de tous les utilisateurs + top stats globaux configurables (24h / 7j / 30j / 12 mois)
- **Utilisateur** : sa session active (si elle existe) + ses top stats personnels
- Top stats : films les plus regardés, séries les plus regardées, utilisateurs les plus actifs (admin), clients les plus utilisés

### 3. Historique de lecture

- **Admin** : liste globale, filtrable par utilisateur / type de média / bibliothèque / période
- **Utilisateur** : son historique uniquement, filtre utilisateur absent de l'interface
- Colonnes : date, utilisateur (admin only), média, durée, % vu, client, IP
- Tri dynamique sur toutes les colonnes, pagination

### 4. Graphiques & Analytics

S'inspirer de `plexpy/graphs.py`. Les graphiques admin sont globaux avec filtre optionnel par utilisateur ; les graphiques utilisateur sont calculés sur ses données uniquement.

**Graphiques temporels :**

- Nombre de lectures par jour / semaine / mois (barres)
- Durée totale de visionnage par période
- Comparaison films vs séries vs musique dans le temps

**Graphiques par utilisateur (admin) :**

- Répartition des lectures par utilisateur (camembert + barres)
- Évolution de l'activité d'un utilisateur dans le temps

**Graphiques par contenu :**

- Bibliothèques les plus consultées
- Genres les plus regardés
- Top 10 films / séries / épisodes

**Graphiques techniques :**

- Résolution de lecture (4K / 1080p / 720p…)
- Clients utilisés (web, Infuse, Jellyfin Android…)
- Direct Play vs Transcoding

### 5. Pages utilisateur

- **Admin** : liste de tous les utilisateurs avec stats globales (dernière activité, total lectures, temps total) + page détaillée par user
- **Utilisateur** : sa propre page profil uniquement

### 6. Pages média

- Page détaillée par film/série : nombre de lectures, par qui (admin), quand
- Pour les séries : progression par saison/épisode

### 7. Statistiques de bibliothèque

- Taille de chaque bibliothèque (nombre d'éléments)
- Répartition par genre, année de sortie, codec vidéo/audio
- Médias les plus / moins regardés
- Accessible aux deux rôles en lecture (configurable par l'admin)

---

## Architecture attendue du projet

```
jellyfin-stats/
├── jellyfin_stats/
│   ├── __init__.py
│   ├── main.py              # point d'entrée, serveur web
│   ├── config.py            # gestion configuration
│   ├── database.py          # SQLite, schéma, migrations versionnées
│   ├── auth.py              # login Jellyfin + local, sessions,
│   │                        # décorateurs @require_auth / @require_admin
│   ├── jellyfin_api.py      # wrapper API Jellyfin
│   ├── activity.py          # capture sessions + filtres durée min
│   ├── history.py           # historique de lecture
│   ├── graphs.py            # données graphiques (tenant compte du rôle)
│   ├── users.py             # stats utilisateurs
│   ├── libraries.py         # stats bibliothèques
│   ├── import_playback.py   # import depuis playback_reporting.db
│   └── scheduler.py         # tâches planifiées
├── data/
│   └── interfaces/
│       └── default/
│           ├── templates/
│           │   ├── base.html         # layout commun (navbar adaptée au rôle)
│           │   ├── login.html
│           │   ├── home.html         # blocs conditionnels admin / user
│           │   ├── history.html
│           │   ├── graphs.html
│           │   ├── user.html
│           │   ├── settings.html     # admin only
│           │   └── import.html       # admin only
│           └── static/
│               ├── css/
│               └── js/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── config.ini.example
```

---

## Points d'attention

- **Isolation des données** : chaque requête SQL retournant de l'historique ou des stats accepte un paramètre `user_id` optionnel — filtré côté serveur via la session, jamais via un paramètre client
- **Navbar adaptative** : les liens "Utilisateurs", "Settings", "Import", "Activité globale" n'apparaissent pas pour un utilisateur non-admin
- **Premier lancement** : si aucun compte admin n'existe, afficher un wizard de setup (URL Jellyfin + API key + création compte admin local de secours)
- **Schéma SQL** : s'inspirer de `plexpy/database.py` de Tautulli (tables `session_history`, `users`, `libraries`…)
- **API Jellyfin** : documenter les endpoints utilisés — référence : https://api.jellyfin.org — endpoints clés : `/Sessions`, `/Users`, `/Items`, `/Playback/Progress`, `/Playback/Stopped`
- **Pas de surengineering** : architecture simple et lisible, KISS
- **Responsive** : mobile-friendly
- **Thème sombre** par défaut, cohérent avec l'UI de Jellyfin

---

## Livrable attendu

Projet fonctionnel, dockerisé, avec :

1. README clair : installation, configuration, premier lancement, description des deux modes d'authentification
2. `config.ini.example` commenté (sections `[Auth]` et `[Monitoring]` incluses)
3. Migrations SQL versionnées
4. Fonctionnalités prioritaires complètes : 1 à 4 + authentification multi-utilisateurs + import Playback Reporting + durée minimum de lecture
