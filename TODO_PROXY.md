# TODO_PROXY - Proxying Multi-Version et Quotas

## État d'implémentation (Décembre 2024)

### ✅ Implémenté

**Réplication seed-based (DVD-like)**
- `urpm/core/rpmsrate.py` - Parser rpmsrate-raw avec détection patterns locales
- `collect_dependencies()` - Résolution dépendances avec Requires + Recommends + file deps
- `urpm proxy sync` - Téléchargement parallèle du seed set
- `urpm media seed-info` - Affiche le set calculé
- `urpm media set --replication=seed` - Active la politique seed
- Option `--latest-only` pour ne garder qu'une version par paquet
- Déduplication multi-versions (kernel-desktop-6.12.1, 6.12.5... → seulement le dernier)
- Expansion patterns locales (libreoffice-langpack-ar, -ca → tous les langpacks)
- DEFAULT_SEED_SECTIONS étendu pour contenu DVD complet

**Résultat actuel** : ~3.5 GB release + ~1.5 GB updates ≈ 5 GB (comparable au DVD 4.2 GB)

### ⏳ Limitations connues

- ~2200 paquets manquants vs DVD (principalement libs) car synthesis ne contient pas tous les file provides
- À revoir quand urpmf sera implémenté (utilisation hdlist pour résolution complète)

### 🔜 À faire

- [ ] Quotas et rétention (CacheManager)
- [ ] Proxy multi-version (mga10 sert mga9)
- [ ] Rate limiting configurable
- [ ] `/api/request-download` endpoint
- [ ] Renommer `urpm proxy` → `urpm mirror` (plus cohérent avec la fonction réelle)

---

## Demande initiale

J'aimerais attaquer la partie proxying :
- Un urpmd sur une mga10 peut servir une mga9 ou une mga11
- Le peer client (urpm) peut interroger les peers ayant des paquets compatibles avec sa propre version
- Le urpmd (ou urpm update) peut être configuré pour récupérer les synthèses d'autres versions de Mageia, télécharger tous les paquets de ces média au fil de l'eau, en garder un quota max, nettoyer automatiquement ce qui est trop ancien

Points clés :
- `urpm media add` doit permettre de déclarer de nouveaux médias, les peers doivent savoir quels médias utiliser pour eux-mêmes
- Vérification de volumétrie au moment de demander une réplication full
- Éviction : d'abord nettoyer les RPMs non-référencés dans les synthesis actuels, puis score d'obsolescence si besoin
- Rate limiting configurable et débrayable (mode install party = open bar)

---

## Objectif

Permettre à un urpmd de servir des paquets pour d'autres versions de Mageia (ex: mga10 sert mga9), avec gestion des quotas et politiques de réplication.

---

## 1. Modèle de données

### 1.1 Modifications table `media`

```sql
ALTER TABLE media ADD COLUMN proxy_enabled INTEGER DEFAULT 1;
-- 1 = ce média peut être servi aux peers
-- 0 = ce média n'est pas partagé

ALTER TABLE media ADD COLUMN replication_policy TEXT DEFAULT 'on_demand';
-- 'none'      = pas de réplication (métadonnées seulement)
-- 'on_demand' = réplique ce qui est téléchargé localement (comportement actuel)
-- 'full'      = miroir complet
-- 'since'     = depuis une date (voir replication_since)

ALTER TABLE media ADD COLUMN replication_since INTEGER;
-- Timestamp Unix pour policy='since'

ALTER TABLE media ADD COLUMN quota_mb INTEGER;
-- Quota en MB pour ce média (NULL = pas de limite)

ALTER TABLE media ADD COLUMN retention_days INTEGER DEFAULT 30;
-- Durée de rétention des paquets en jours
```

### 1.2 Nouvelle table `cache_files`

```sql
CREATE TABLE cache_files (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    media_id INTEGER,
    file_path TEXT NOT NULL,       -- Chemin relatif depuis medias/
    file_size INTEGER NOT NULL,
    added_time INTEGER NOT NULL,   -- Timestamp téléchargement
    last_accessed INTEGER,         -- Dernier accès (pour LRU)
    is_referenced INTEGER DEFAULT 1, -- Dans synthesis actuel ?
    FOREIGN KEY (media_id) REFERENCES media(id),
    UNIQUE(filename, media_id)
);
```

### 1.3 Nouvelle table `proxy_config`

```sql
CREATE TABLE proxy_config (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- Clés:
-- 'enabled' = '1'/'0'           -- switch global du proxy
-- 'disabled_versions' = '8,9'   -- versions Mageia qu'on ne sert pas
-- 'global_quota_mb' = '10240'   -- quota global en MB
-- 'rate_limit_enabled' = '1'/'0'
-- 'rate_limit_requests_per_min' = '60'
```

---

## 2. Modifications CLI

### 2.1 `urpm media add` - options étendues

```bash
# Ajouter un média pour usage local (comportement actuel)
urpm media add https://mirror.example.com/10/x86_64/media/core/release/

# Ajouter un média proxy-only (mga9 sur machine mga10)
urpm media add --proxy-only https://mirror.example.com/9/x86_64/media/core/release/

# Définir la politique de réplication
urpm media add --replication=full ...
urpm media add --replication=since:2024-01-01 ...
urpm media add --replication=on_demand ...  # défaut

# Définir le quota
urpm media add --quota=5G ...
```

### 2.2 `urpm media set` - modifier les politiques

```bash
urpm media set <name> --proxy=yes|no           # Servir ce média aux peers
urpm media set <name> --replication=full|on_demand|since:DATE|none
urpm media set <name> --quota=SIZE
urpm media set <name> --retention=DAYS
```

### 2.3 `urpm proxy` - gestion du proxy

```bash
urpm proxy status                    # État du proxy, quotas utilisés
urpm proxy enable                    # Activer le mode proxy
urpm proxy disable                   # Désactiver
urpm proxy quota <SIZE>              # Quota global
urpm proxy disable-version 8,9      # Ne pas servir ces versions Mageia
urpm proxy enable-version 9          # Re-servir une version
urpm proxy sync [media]              # Forcer sync selon politique
urpm proxy clean                     # Appliquer quotas/rétention
```

---

## 3. Modifications Daemon

### 3.1 Annonce des peers enrichie

Modifier `/api/announce` et discovery pour inclure :
```json
{
  "host": "192.168.1.10",
  "port": 9876,
  "version": "0.1.0",
  "local_version": "10",        // Version Mageia locale
  "local_arch": "x86_64",       // Arch locale
  "proxy_enabled": true,
  "served_media": [             // Médias disponibles
    {"version": "10", "arch": "x86_64", "types": ["core_release", "core_updates"]},
    {"version": "9", "arch": "x86_64", "types": ["core_release"]}
  ]
}
```

### 3.2 `/api/have` enrichi

Ajouter filtre optionnel par version/arch :
```json
// Request
{
  "packages": ["foo-1.0-1.mga9.x86_64.rpm"],
  "filter": {"version": "9", "arch": "x86_64"}  // Optionnel
}
```

### 3.3 `/api/request-download` - nouveau endpoint

Permet à un peer de demander le téléchargement d'un paquet :
```json
// Request
{
  "packages": ["foo-1.0-1.mga9.x86_64.rpm"],
  "media": "core_release"
}

// Response
{
  "accepted": ["foo-1.0-1.mga9.x86_64.rpm"],
  "rejected": [],  // Quota dépassé, proxy désactivé, etc.
  "eta_seconds": 120
}
```

### 3.4 Scheduler - nouvelles tâches

```python
# Tâche : Réplication selon politique
def _run_replication_sync(self):
    for media in db.list_media():
        policy = media['replication_policy']
        if policy == 'full':
            self._sync_full_mirror(media)
        elif policy == 'since':
            self._sync_since(media, media['replication_since'])

# Tâche : Enforcement des quotas
def _run_quota_enforcement(self):
    cache_mgr = CacheManager(self.db, self.base_dir)
    cache_mgr.enforce_quotas()
```

---

## 4. CacheManager - nouvelle classe

```python
# urpm/core/cache.py

class CacheManager:
    """Gestion du cache avec quotas et rétention."""

    def __init__(self, db, base_dir):
        self.db = db
        self.base_dir = base_dir

    def get_media_usage(self, media_id) -> int:
        """Taille utilisée par un média en bytes."""

    def get_total_usage(self) -> int:
        """Taille totale du cache."""

    def enforce_quotas(self):
        """Applique les quotas, supprime les fichiers excédentaires."""
        # 1. Quota global
        # 2. Quotas par média
        # 3. Rétention (fichiers > N jours)
        # Priorité d'éviction : plus vieux, non-référencés, basse priorité

    def evict_for_space(self, needed_bytes) -> bool:
        """Libère de l'espace pour un nouveau téléchargement."""

    def register_file(self, filename, media_id, path, size):
        """Enregistre un fichier dans cache_files."""

    def update_access(self, filename):
        """Met à jour last_accessed pour LRU."""

    def mark_unreferenced(self, media_id, current_files):
        """Marque les fichiers qui ne sont plus dans synthesis."""
```

---

## 5. Ordre d'implémentation

### Phase 1 : Infrastructure (base)
1. Migration DB : nouvelles colonnes + tables
2. CacheManager : classe de base
3. CLI `urpm media set` pour les nouvelles options

### Phase 2 : Quotas et rétention
4. Enregistrement des fichiers dans cache_files lors du téléchargement
5. CacheManager.enforce_quotas()
6. Scheduler : tâche de cleanup avec quotas
7. CLI `urpm proxy quota/clean/status`

### Phase 3 : Proxy multi-version
8. Annonce peers enrichie (version/arch/served_media)
9. Flag proxy_enabled sur media + disabled_versions dans proxy_config
10. Filtrage dans PeerClient par version/arch
11. CLI `urpm media set --proxy`, `urpm proxy disable-version`

### Phase 4 : Réplication avancée
12. Policies de réplication (full, since, on_demand)
13. Scheduler : tâche de réplication
14. `/api/request-download` endpoint
15. CLI `urpm proxy sync`

---

## 6. Fichiers à modifier

### Core
- `urpm/core/database.py` - Migration, nouvelles tables, méthodes
- `urpm/core/cache.py` - **NOUVEAU** - CacheManager
- `urpm/core/download.py` - Enregistrer dans cache_files
- `urpm/core/peer_client.py` - Filtre version/arch

### Daemon
- `urpm/daemon/daemon.py` - Nouveaux endpoints
- `urpm/daemon/server.py` - Handlers request-download
- `urpm/daemon/discovery.py` - Annonce enrichie
- `urpm/daemon/scheduler.py` - Tâches réplication/quota

### CLI
- `urpm/cli/main.py` - Commandes proxy, options media

---

## 7. Décisions de conception

### 7.1 Réplication full - Vérification volumétrie

Avant d'activer `--replication=full`, calculer et afficher :
```
urpm media set core_release --replication=full

Analyzing media 'Core Release'...
  Packages in synthesis: 38,706
  Estimated total size: 42.3 GB
  Current cache usage: 1.2 GB
  Available disk space: 120 GB

Proceed with full replication? [y/N]
```

Le scheduler téléchargera progressivement en respectant les quotas.

### 7.2 Priorité d'éviction - 2 phases

**Phase 1 : Nettoyage des non-référencés**
- Supprimer d'abord les RPMs qui ne sont plus dans aucun synthesis actif
- Ces fichiers sont obsolètes (anciennes versions remplacées par updates)

**Phase 2 : Score d'obsolescence** (si besoin de plus d'espace)
```python
score = age_days / media_priority
# Plus bas score = supprimé en premier
# Updates (priority=100) gardés plus longtemps que backports (priority=30)
```

### 7.3 Rate limiting - Configurable

```sql
-- Dans proxy_config
'rate_limit_enabled': '1'        -- 0 = open bar (install party)
'rate_limit_requests_per_min': '60'
'rate_limit_bandwidth_mbps': '100'
```

CLI :
```bash
urpm proxy rate-limit off          # Mode install party
urpm proxy rate-limit on           # Mode normal
urpm proxy rate-limit 120/min      # Personnalisé
```

---

## 8. Résumé des modes d'utilisation

### Mode "Poste de travail" (défaut)
- Médias locaux uniquement
- Cache on_demand
- Quota modéré (ex: 10GB)
- Sert ses pairs sur le LAN

### Mode "Serveur proxy"
- Médias locaux + médias proxy-only pour autres versions
- Réplication full ou since sur certains médias
- Gros quota (ex: 100GB+)
- Rate limiting actif

### Mode "Install party relay"
- Rate limiting désactivé
- Réplication on_demand (tout ce qui passe est gardé)
- Open bar pour les peers
