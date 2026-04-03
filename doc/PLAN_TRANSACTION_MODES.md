# Reconception : modes synchrone et asynchrone des transactions RPM

## Contexte et problème

Le mécanisme actuel de `transaction_queue.py` fait un `os.fork()` et libère le
parent avant la fin du `ts.run()` ("optimistic early release"). Le parent rend
la main à l'utilisateur alors que l'installation des RPMs n'est pas terminée.

Conséquences :
- L'utilisateur croit que c'est fini alors que ça installe encore
- Le message "waiting for rpmdb update" masque la réalité (c'est le ts.run complet)
- Les fichiers ne sont pas présents immédiatement (README, binaires, configs)
- Les README sont extraits depuis les RPMs avant le fork (hack) puis ré-extraits
  depuis le filesystem après le ts.run (doublon)
- Si ts.run échoue après le early release, l'utilisateur n'est pas prévenu
  proprement (alerte stderr)
- Le child `print()` directement sur stdout après que le parent soit parti (YOLO)

## Design : deux modes

### Mode synchrone (défaut pour `urpm install`)

Le parent attend la fin complète du ts.run() avant de rendre la main.

**Flow :**
1. Fork (séparation de privilèges)
2. Child envoie des messages de progression via le pipe :
   - `progress` : `{package, current, total, bytes_done, bytes_total}`
   - `op_done` : `{count, rpmnew_files, readme_messages}`
3. Parent affiche une barre de progression temps réel (pleine largeur terminal)
4. À la réception de `op_done`, parent attend `queue_done`
5. Affichage des README :
   - **Sans `-y`** : ouvre les README dans `$PAGER` (less par défaut)
   - **Avec `-y`** : affiche un rappel une ligne et rend la main :
     `1 README à consulter. Lire : urpm readme`
6. Le prompt revient

**Changements :**
- Supprimer le pre-fork `collect_readme_from_rpms()` dans install.py (inutile,
  on attend la fin maintenant)
- Supprimer `parent_can_exit` pour ce mode
- Enrichir les messages `progress` avec bytes_done/bytes_total (pour le %)

### Mode asynchrone (défaut pour `urpm upgrade`, `urpm remove`)

Le parent rend la main **immédiatement** après le lancement de la transaction.
Aucune sortie console après le message de lancement. L'utilisateur retrouve
son prompt dans un terminal propre.

**Flow :**
1. Fork (séparation de privilèges)
2. Parent affiche un message informatif unique et rend la main :
   ```
   Transaction lancée en arrière-plan (15 paquets).
   Progression : urpm progress
   README après installation : urpm readme
   ```
   Le message `urpm readme` n'apparaît **que** si la transaction contient des
   paquets ayant des README (détecté pré-fork en scannant les RPMs).
3. Child fait le ts.run() en arrière-plan, **zéro sortie console** (pas de
   print, pas de stderr). Toute information est écrite dans le fichier de
   progression.
4. À la fin, notification desktop (si dbus disponible) :
   "15 paquets mis à jour."

**Principe fondamental :** en mode asynchrone, le terminal appartient à
l'utilisateur. Le child n'écrit **rien** sur stdout ni stderr. Les README ne
sont **jamais** affichés automatiquement — l'utilisateur les consulte quand il
veut via `urpm readme`.

**Suivi à la demande :**
- `urpm progress` : affiche l'état de la transaction en cours
  ```
  Transaction en cours : upgrade (15 paquets)
    [███████░░░░░░░░░░░░░░░] 7/15 lib64qt6-6.10.0-3.mga10 (52%)
    Démarrée il y a 45s
  ```
- Si rien en cours : "Aucune transaction en cours."

**README différé :**
- `urpm readme` : affiche les README de la dernière transaction (dans un pager)
- `urpm readme --last` : idem (explicite)
- `urpm readme --transaction <id>` : README d'une transaction spécifique
- `urpm readme --list` : liste les transactions qui avaient des README

### Override par l'utilisateur

En ligne de commande :
- `urpm install --async` : force le mode asynchrone
- `urpm upgrade --sync` : force le mode synchrone
- `urpm remove --sync` : force le mode synchrone

**Cas particulier : `-y` avec `--sync`** — le mode synchrone attend la fin de
la transaction, mais si `-y` est passé (non-interactif), les README ne sont
**pas** affichés dans un pager (less bloquerait un script). À la place, un
rappel une ligne est affiché : `1 README à consulter. Lire : urpm readme`

### Configuration persistante (`/etc/urpm/urpm.cfg`)

Les défauts peuvent être changés dans le fichier de configuration :

```ini
[transaction]
# Mode par défaut pour chaque opération.
# Valeurs : sync, async
install_mode = sync
upgrade_mode = async
remove_mode = async
```

Priorité (du plus faible au plus fort) :
1. Défauts codés en dur (install=sync, upgrade/remove=async)
2. `/etc/urpm/urpm.cfg` section `[transaction]`
3. `/etc/urpm/conf.d/*.cfg` (drop-in overrides)
4. `--sync` / `--async` en ligne de commande

Implémentation : ajouter `TransactionSettings` dans `settings.py` avec les
trois champs, parser la section `[transaction]`, et consulter `get_settings()`
au moment du fork.

## Pré-requis : création de `/etc/urpm/` à l'installation

**Bug existant :** le RPM spec (`urpm-ng.spec`) n'installe pas les fichiers de
configuration dans `/etc/urpm/`. Le répertoire n'est même pas créé.

**Correction nécessaire dans le spec :**

```rpm
%install
# ...existing installs...

# Install configuration files
install -dm755 %{buildroot}%{_sysconfdir}/urpm
install -dm755 %{buildroot}%{_sysconfdir}/urpm/conf.d
install -m644 data/etc/urpm/urpm.cfg %{buildroot}%{_sysconfdir}/urpm/urpm.cfg
install -m644 data/etc/urpm/conf.d/00-urpmi-compat.cfg \
    %{buildroot}%{_sysconfdir}/urpm/conf.d/00-urpmi-compat.cfg

%files core -f %{pyproject_files}
# ...existing files...
%dir %{_sysconfdir}/urpm
%dir %{_sysconfdir}/urpm/conf.d
%config(noreplace) %{_sysconfdir}/urpm/urpm.cfg
%config(noreplace) %{_sysconfdir}/urpm/conf.d/00-urpmi-compat.cfg
```

Les fichiers de config sont marqués `%config(noreplace)` : les modifications
de l'utilisateur survivent aux mises à jour du paquet.

## Callbacks RPM disponibles

Le `ts.run()` de RPM appelle un callback à chaque étape. Callbacks utiles :

| Callback | Moment | Données disponibles |
|----------|--------|---------------------|
| `RPMCALLBACK_INST_OPEN_FILE` | Début d'un paquet | nom, numéro |
| `RPMCALLBACK_INST_PROGRESS` | Progression dans un paquet | bytes traités, bytes total |
| `RPMCALLBACK_INST_CLOSE_FILE` | Paquet terminé | fd |
| `RPMCALLBACK_UNINST_START` | Début suppression | nom |
| `RPMCALLBACK_UNINST_PROGRESS` | Progression suppression | |
| `RPMCALLBACK_TRANS_START` | Début phase préparation | |
| `RPMCALLBACK_TRANS_PROGRESS` | Progression préparation | |
| `RPMCALLBACK_TRANS_STOP` | Fin préparation | |
| `RPMCALLBACK_SCRIPT_START` | Début scriptlet | |
| `RPMCALLBACK_SCRIPT_ERROR` | Erreur scriptlet | |

Actuellement seuls `INST_OPEN_FILE`, `INST_CLOSE_FILE` et `UNINST_START` sont
exploités. Il faut ajouter `INST_PROGRESS` pour le pourcentage par paquet.

## Stockage

### README en DB (table `transaction_readmes`)

```sql
CREATE TABLE transaction_readmes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL,
    package_name TEXT NOT NULL,
    readme_type TEXT NOT NULL,    -- 'install', 'upgrade', 'generic'
    content TEXT NOT NULL,
    FOREIGN KEY (transaction_id) REFERENCES transactions(id) ON DELETE CASCADE
);
```

Les README sont stockés par le child après ts.run(), indexés par transaction.
`urpm readme` les lit depuis la DB.

### Progression en cours : fichier `/run/urpm/transaction.json`

**PAS en base de données** — la progression est de la donnée éphémère à haute
fréquence d'écriture. La mettre en DB causerait des contentions de lock et
polluerait la base avec de la donnée jetable.

Le child écrit périodiquement son état dans un fichier :

```json
{
    "transaction_id": 42,
    "type": "upgrade",
    "total": 15,
    "current": 7,
    "current_package": "lib64qt6-6.10.0-3.mga10",
    "bytes_done": 45000000,
    "bytes_total": 120000000,
    "started_at": 1712345678,
    "pid": 12345,
    "has_readmes": true,
    "error": null
}
```

Écriture atomique (`.tmp` + `os.rename()`) pour éviter les lectures partielles.
Fichier supprimé à la fin de la transaction. `urpm progress` lit ce fichier.

## Effets de bord et cas limites

### 1. Double transaction simultanée

**Problème :** l'utilisateur lance `urpm install X` (sync) pendant qu'un
`urpm upgrade` (async) tourne en arrière-plan.

**Solution :** `InstallLock` existe déjà (flock). Le deuxième process attend
avec un message clair :
```
Transaction en cours (upgrade, PID 12345) — en attente...
  [███████░░░░░░░░░░░░░░░] 7/15 lib64qt6-6.10.0-3.mga10
```
Afficher la progression de la transaction en cours pendant l'attente (lire
`/run/urpm/transaction.json`).

### 2. Erreur ts.run() en mode async

**Problème :** le parent est déjà parti, l'utilisateur ne voit pas l'erreur.

**Solutions (cumulatives) :**
- Écrire l'erreur dans `/run/urpm/transaction.json` (champ `error`)
- Écrire dans la DB (table transactions, colonne error)
- Notification desktop : "Erreur d'installation. `urpm progress` pour détails."
- Au prochain `urpm` : vérifier `check_background_error()` (existe déjà)
  et afficher un warning clair avec les détails

### 3. Reboot pendant transaction async

**Problème :** l'utilisateur éteint/reboote pendant que le child installe.

**Solutions :**
- Le process child utilise un signal handler (SIGTERM) pour écrire proprement
  l'état d'erreur dans `/run/urpm/transaction.json` avant de mourir
- Si le child est tué brutalement (SIGKILL, coupure courant) : la rpmdb peut
  être corrompue. C'est le même risque qu'avec DNF — `rpm --rebuilddb` en
  recovery
- Afficher un warning au prochain `urpm` si `/run/urpm/transaction.json` existe
  mais le PID est mort (transaction interrompue)
- Pas de dépendance à systemd pour inhiber le shutdown — on gère avec les
  signaux POSIX standard. Si un init system envoie SIGTERM avant SIGKILL,
  le handler a le temps de nettoyer

**Note architecturale :** urpm-ng ne dépend d'aucun init system. Le daemon
`urpmd` fonctionne avec n'importe quel superviseur de service (systemd,
OpenRC, runit, s6...). Les fichiers `.service` fournis dans le paquet sont
une commodité, pas une dépendance.

### 4. Terminal fermé pendant transaction async

**Problème :** l'utilisateur ferme le terminal. Le child est détaché
(`os.setsid()`) donc il survit — pas de problème fonctionnel.

**Mais :** stdout/stderr du child pointent vers le pty fermé. Les écritures
échouent silencieusement. Pas de souci car le child n'écrit **rien** sur
stdout/stderr en mode async (principe fondamental). Les README et erreurs
passent par la DB et le fichier de progression.

### 5. urpm progress sans transaction en cours

```
Aucune transaction en cours.
Dernière transaction : #42, upgrade, 15 paquets — il y a 3h — OK
```

Si la dernière transaction a échoué :
```
Aucune transaction en cours.
Dernière transaction : #42, upgrade, 15 paquets — il y a 3h — ERREUR
  scriptlet failed: texlive-dist post-install
```

### 6. urpm readme sans README

```
Aucun README dans la dernière transaction (#42, upgrade, 15 paquets).
```

### 7. Espace disque insuffisant pendant ts.run()

Détection pré-transaction (existe déjà dans le code). Si ça arrive pendant
le ts.run() malgré tout :
- En mode sync : erreur affichée normalement via le pipe
- En mode async : écrite dans transaction.json + notification

### 8. SIGINT (Ctrl+C) pendant transaction sync

Le parent reçoit SIGINT. **Comportement : annulation de la transaction.**

Le ts.run() de RPM est atomique par paquet mais pas par transaction. Un
SIGINT est transmis au child qui :
1. Positionne un flag `interrupted`
2. Au prochain callback `INST_CLOSE_FILE`, arrête de traiter les paquets
   suivants (le paquet en cours se termine proprement)
3. Envoie un message `interrupted` au parent via le pipe avec la liste des
   paquets installés et ceux qui restaient

Le parent affiche :
```
Interrompu. 7/15 paquets installés avant interruption.
Les paquets restants n'ont pas été installés.
```

**Note :** si le SIGINT arrive pendant un scriptlet ou l'écriture sur disque
d'un paquet, RPM finit le paquet en cours avant de rendre la main. On ne
peut pas interrompre au milieu d'un paquet sans corrompre la rpmdb.

**Mode async :** le parent est déjà parti. Ctrl+C n'a pas d'effet sur la
transaction. L'utilisateur peut `kill <pid>` mais c'est à ses risques
(mêmes précautions que pour reboot).

### 9. Scriptlets longs (post-install)

Certains paquets ont des scriptlets longues (ldconfig, mandb, fc-cache,
mkinitrd...). En mode sync ces scriptlets bloquent — l'utilisateur voit
`[15/15] texlive-dist` mais rien ne bouge pendant 30s.

**Solution :** exploiter `RPMCALLBACK_SCRIPT_START` pour afficher :
```
[██████████████████████████████████████████████] 15/15 texlive-dist — post-install scripts...
```

En mode async c'est transparent (le terminal est libre).

### 10. rpmdrake

rpmdrake utilise le helper root (`transaction_helper.py`) qui communique en
JSON via stdin/stdout. Le helper appelle `ops.resilient_install()`.

**Impact :** le helper est **toujours** en mode sync (il envoie la progression
à la GUI). Pas de changement de comportement pour rpmdrake, juste s'assurer
que `sync=True` est passé explicitement.

**Barre de progression rpmdrake :** le message "waiting for rpmdb update" doit
être remplacé par une vraie barre de progression dans la GUI. Le helper envoie
déjà les messages `progress` au parent (rpmdrake) via le pipe JSON. Il faut :
1. Enrichir les messages `progress` avec `bytes_done`/`bytes_total` et `script`
2. Côté rpmdrake : interpréter ces messages pour mettre à jour la
   `QProgressBar` et le label de statut en temps réel :
   - Paquet en cours : `"Installation : wireshark (3/15)"`
   - Scriptlet : `"Post-install : texlive-dist..."`
   - Pourcentage global : barre basée sur `current/total`
3. Supprimer le message "waiting for rpmdb update" qui ne correspond à rien

Les README sont envoyés dans le message `done` et affichés dans la dialog
de complétion (déjà implémenté).

### 11. urpmd (daemon)

urpmd ne fait **aucune installation** en arrière-plan. Les predownloads sont
de la mise en cache (téléchargement de RPMs), pas de l'installation. Les
auto-updates se limitent à la synchronisation des métadonnées (synthesis).

**Impact sur ce plan :** aucun. urpmd n'est pas concerné par les modes
sync/async de transaction car il ne lance jamais de `ts.run()`.

Les README ne sont pertinents que pour les transactions déclenchées par le CLI
ou rpmdrake.

### 12. Progression : données à transmettre

Le message `progress` actuel est minimal. Nouveau format :

```json
{
    "msg_type": "progress",
    "operation_id": "install",
    "phase": "install",
    "name": "wireshark-4.2.0-1.mga10.x86_64",
    "current": 3,
    "total": 15,
    "bytes_done": 45000,
    "bytes_total": 120000,
    "script": null
}
```

Phases : `prepare` (TRANS_START/PROGRESS), `install` (INST_*),
`erase` (UNINST_*), `script` (SCRIPT_START).

Le champ `script` indique le scriptlet en cours (pour le cas §9).

`bytes_done`/`bytes_total` vient de `INST_PROGRESS`. Note :
RPM passe `amount` et `total` au callback ; `total` est la taille du
cpio archive, pas du RPM. C'est suffisant pour un pourcentage.

### 13. Mode async et confirmation

Le prompt de confirmation ("Procéder ? [o/N]") reste synchrone dans tous les
cas. L'utilisateur confirme AVANT le fork. Le mode async ne change que la
phase post-fork (ts.run en arrière-plan vs attente complète).

### 14. Race condition : urpm progress pendant écriture de transaction.json

Le child écrit, urpm progress lit. Risque de lecture partielle.

**Solution :** écriture atomique (écrire dans un .tmp puis `os.rename()`).

### 15. Enchaînement rapide

L'utilisateur fait `urpm upgrade && urpm install X`. Le upgrade est async,
l'install arrive tout de suite. Le lock bloque l'install en attendant la fin
du upgrade. Pendant l'attente on affiche la progression du upgrade (§1).

Si l'utilisateur fait `urpm upgrade` deux fois de suite rapidement : le
deuxième attend le premier, puis fait sa propre résolution (qui trouvera
probablement rien à faire).

### 16. Impact sur PackageKit / Discover / GNOME Software

PackageKit utilise le backend C (`libpk_backend_urpm.so`) qui communique avec
le service D-Bus `org.mageia.Urpm.v1`. Ce service appelle le helper root
(`transaction_helper.py`) en mode sync.

**Points de vérification :**
- Le backend PackageKit attend déjà la fin complète de la transaction avant
  de signaler `PK_STATUS_FINISHED` — pas de changement nécessaire
- Les signaux `Percentage` et `ItemProgress` de PackageKit doivent être
  alimentés par les nouveaux messages `progress` enrichis
- Le helper root doit continuer à fonctionner en mode sync, indépendamment
  de la config CLI `[transaction]` — le mode est forcé par l'appelant
- Vérifier que la suppression de `parent_can_exit` et du early release ne
  casse pas le flow `helper → D-Bus service → PackageKit → Discover`
- Les README ne sont pas pertinents pour PackageKit (Discover/GNOME Software
  n'affichent pas de README post-install)

**Test :** après implémentation, vérifier qu'une installation depuis Discover
fonctionne de bout en bout avec la barre de progression.

## UI : principes de design

L'interface doit rester **naturelle et jolie** pour l'utilisateur. Principes :

### Barre de progression pleine largeur

La barre utilise la quasi-totalité de la largeur du terminal, comme apt.
Détection de la largeur via `os.get_terminal_size()` (fallback 80 colonnes).

Format sur un terminal 80 colonnes :
```
[████████████████████████████████████░░░░░░░░░░] 7/15 lib64qt6-gui (52%)
```

Format sur un terminal 120 colonnes :
```
[████████████████████████████████████████████████████████████████░░░░░░░░░░░░░░░░░░░░] 7/15 lib64qt6-gui-6.10.0 (52%)
```

La barre prend tout l'espace disponible. Le nom du paquet est tronqué si
nécessaire (jamais la barre). Un seul `\r` pour mettre à jour la ligne.

Pendant les scriptlets longs :
```
[██████████████████████████████████████████████] 15/15 texlive-dist — post-install...
```

### Mode sync (install)

```
Résolution des dépendances...
3 paquets à installer (12.4 Mo)

  wireshark-4.2.0-1.mga10.x86_64      Core Release     8.2 Mo
  wireshark-tools-4.2.0-1.mga10.x86_64 Core Release    3.1 Mo
  libwireshark17-4.2.0-1.mga10.x86_64  Core Release    1.1 Mo

Procéder à l'installation ? [o/N] o

[████████████████████████████████████░░░░░░░░░░] 1/3 wireshark (37%)
```

Après installation (interactif, sans `-y`) :
```
[██████████████████████████████████████████████] 3/3 installés

─── README : wireshark ─────────────────────────────
After installation, you must add your user to the
'wireshark' group to capture packets:
  sudo usermod -aG wireshark $USER
────────────────────────────────────────────────────
```

Si le README est long, il s'ouvre dans `$PAGER` (less par défaut).
Si plusieurs README, ils sont concaténés avec des séparateurs clairs.

Après installation (non-interactif, avec `-y`) :
```
[██████████████████████████████████████████████] 3/3 installés
1 README à consulter. Lire : urpm readme
```

Le `-y` indique un contexte non-interactif (script, automatisation). Ouvrir
`less` bloquerait le déroulement — on se contente du rappel.

### Mode async (upgrade/remove)

```
Résolution des dépendances...
15 paquets à mettre à jour (142.7 Mo)

  lib64qt6-core-6.10.0-3.mga10.x86_64  Core Updates   45.2 Mo
  lib64qt6-gui-6.10.0-3.mga10.x86_64   Core Updates   38.1 Mo
  ... (13 autres)

Procéder à la mise à jour ? [o/N] o

Transaction lancée en arrière-plan (15 paquets).
Progression : urpm progress
README après installation : urpm readme
$
```

Le message `urpm readme` n'apparaît que si des paquets de la transaction
contiennent des README. Détection pré-fork en scannant les headers RPM.

Le prompt revient immédiatement. Rien d'autre n'apparaît dans le terminal.
Notification desktop à la fin (si dbus disponible).

## Documentation à mettre à jour

Chaque changement dans ce plan nécessite une mise à jour synchrone de :

1. **Docstrings inline** : fonctions modifiées dans `transaction_queue.py`,
   `install.py`, `settings.py`, callbacks RPM
2. **Man pages** (EN + FR) :
   - `man/en/man1/urpm.1` : documenter `--sync`/`--async`, `urpm progress`,
     `urpm readme`, section `[transaction]` dans la config
   - `man/fr/man1/urpm.1` : traduction complète
3. **README.md** : section configuration (ajouter `[transaction]`), nouvelles
   commandes `urpm progress` et `urpm readme`
4. **QUICKSTART.md** : mention du mode async pour upgrade
5. **Traductions** (`po/fr.po`, `po/de.po`, etc.) : nouvelles chaînes i18n

## Nettoyages à faire (suppressions de code)

1. `parent_can_exit` message type et sa gestion (parent + child)
2. `collect_readme_from_rpms()` et son appel pré-fork dans install.py
3. Le `print(stdout)` YOLO du child après early release (lignes 1142-1151)
4. Le `release_parent_after` flag et sa logique dans les callbacks
5. Le message d'erreur post-release sur stderr (lignes 1114-1131)
6. Le "waiting for rpmdb update" dans les callbacks et dans rpmdrake

## Ordre d'implémentation suggéré

1. **Pré-requis : fix `/etc/urpm/`** : ajouter l'installation des fichiers de
   config dans le spec RPM + ajouter la section `[transaction]` dans
   `urpm.cfg` et `settings.py`.

2. **Enrichir les callbacks RPM** : ajouter INST_PROGRESS, SCRIPT_START au
   pipe protocol. Tester que les données arrivent correctement.

3. **Mode sync complet** : supprimer early release, parent attend op_done +
   queue_done. Barre de progression pleine largeur. README en pager à la fin
   (sauf si `-y`). Tester sur `urpm install`.

4. **Stockage README en DB** : table transaction_readmes, stockage par le
   child après ts.run().

5. **`urpm readme`** : commande CLI pour lire les README depuis la DB.

6. **Mode async propre** : parent rend la main immédiatement, message
   informatif (avec mention `urpm readme` si README détectés). Child muet
   (zéro console). Progression dans `/run/urpm/transaction.json` (écriture
   atomique).

7. **`urpm progress`** : lit transaction.json et affiche l'état.

8. **Notification desktop** : à la fin du ts.run() async, notifier via D-Bus
   (optionnel, pas de dépendance dure).

9. **Défauts et config** : install=sync, upgrade/remove=async. Lecture de
   `[transaction]` dans settings.py. Overrides --sync/--async.

10. **rpmdrake** : remplacer "waiting for rpmdb update" par une vraie barre de
    progression. Alimenter QProgressBar + label depuis les messages `progress`
    enrichis.

11. **PackageKit** : vérifier que le flow D-Bus → helper → PackageKit →
    Discover fonctionne après les changements. Alimenter les signaux
    `Percentage`/`ItemProgress`.

12. **Nettoyages** : supprimer le code mort (pré-fork README, YOLO print, etc.)

13. **Documentation** : docstrings, man pages (EN+FR), README, traductions.
