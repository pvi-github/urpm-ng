# Media Update Lock — TODO

## Problème
Quand urpmd (ou un autre process) fait un `media update`, un 2e `urpm m u`
lancé en parallèle provoque "database is locked" (SQLite busy_timeout=5s
dépassé pendant un gros import de ~38000 paquets).

## Solution retenue
Un simple lock fichier avec PID, même pattern que `InstallLock` dans
`background_install.py`.

### Comportement
1. `urpm m u` tente d'acquérir le lock (non-bloquant)
2. Lock acquis → on fait le sync normalement
3. Lock déjà pris → on lit le PID dans le fichier
   - PID vivant → affiche "Mise à jour déjà en cours (PID xxx)" et sort
   - PID mort (orphelin) → on retente d'acquérir le lock (on devient leader ou follower)

### Fichier lock
`/run/urpm/sync.lock` (contient le PID du process qui sync)

## A faire

- [x] **1. Reporter le fix RLock `update_server_stats`**
`urpm/core/db/server.py` — `update_server_stats()` fait des écritures SQLite
sans `self._lock`. Ajouter `with self._lock:` autour du `conn.execute` + `conn.commit`.
**Déjà fait dans urpm-ng-papoteur** (`server.py:205`), à reporter ici dans urpm-ng.
Diff : les 5 lignes `conn.execute(...)` / `conn.commit()` doivent être wrappées
dans `with self._lock:`.

- [x] **2. Créer `SyncLock`**
Soit dans `background_install.py` (généraliser `InstallLock` avec un path
configurable), soit dans un fichier dédié. Le lock doit :
- Utiliser `fcntl.flock()` (non-bloquant)
- Écrire le PID du holder
- Détecter les locks orphelins (PID mort)
- Supporter `with` (context manager)

- [x] **3. Intégrer dans `cmd_media_update`**
Dans `urpm/cli/commands/media.py` :
- Acquérir le lock avant de lancer le sync
- Si lock pris et PID vivant → message + return 0
- Si lock orphelin → tenter de prendre le lead
- Libérer le lock à la fin (finally)

- [x] **4. Intégrer dans urpmd**
Le daemon doit aussi prendre le lock quand il fait ses syncs périodiques.
Vérifier `urpm/daemon/scheduler.py` — la fonction `_refresh_media()`.

- [x] **5. Augmenter `busy_timeout`**
Passer `BUSY_TIMEOUT_MS` de 5000 à 30000 dans `database.py` comme filet
de sécurité supplémentaire (le lock devrait empêcher la contention, mais
au cas où).
