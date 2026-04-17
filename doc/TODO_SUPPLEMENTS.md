# Supplements & Weak Dependencies — TODO

Audit du 2026-04-17. Suivi des améliorations identifiées dans la gestion
des Recommends, Suggests et Supplements.

## 1. [HAUTE] Orphan detector ignore les Supplements

### Problème

Un paquet installé via `Supplements:` n'a aucun arc entrant dans le graphe
de reverse-deps de l'orphan detector → détecté orphelin à tort et supprimé.

Exemple concret : `xdg-desktop-portal-kde` a `Supplements: (flatpak and
qtbase6-common)`. Sur un bureau Plasma avec flatpak, ce paquet est légitime
mais serait orpheliné si aucun autre paquet ne le requiert directement.

### Cause racine

`_build_reverse_deps` (orphans.py:1176-1203) ne parcourt que
`info['requires']` (qui inclut les recommends fusionnés). Les supplements
ne sont ni collectés ni injectés dans le graphe.

### Fix proposé

1. **`post_state`** : ajouter un champ `'supplements'` (même format que
   `requires` : liste de `(name, sense, evr)`).

2. **`_collect_from_header`** (rpmdb) : collecter `RPMTAG_SUPPLEMENTNAME`,
   `RPMTAG_SUPPLEMENTVERSION`, `RPMTAG_SUPPLEMENTFLAGS`.

3. **`_collect_from_synthesis`** : inclure le champ `supplements` du dict
   synthesis (déjà parsé dans synthesis.py:202).

4. **`_merge_pool_requires`** (orphans.py:1023-1098) : aussi merger
   `SOLVABLE_SUPPLEMENTS` depuis le pool (même pattern que recommends,
   lignes 1070-1084).

5. **`_build_reverse_deps`** : boucle supplémentaire sur
   `info['supplements']` avec direction inversée :

   ```python
   for name, info in state.items():
       for supp_name, supp_sense, supp_evr in info.get('supplements', []):
           for provider, prov_evr in cap_providers.get(supp_name, ()):
               if provider == name:
                   continue
               # X supplements cap_Y, Y provides cap_Y → Y keeps X alive
               rev[name].add(provider)
   ```

6. **`find_all_orphans`** (orphans.py:536-629) : collecter
   `SUPPLEMENTNAME` depuis rpmdb, ajouter au reverse-dep graph.

### Note sur les expressions booléennes

`Supplements: (A and B)` est stocké comme deux capabilities indépendantes
dans synthesis et libsolv. On les traite en OR (conservateur) : si l'une
des capabilities supplementées est satisfaite, le paquet est protégé.
Cela peut garder des paquets en trop mais ne supprimera jamais à tort.

### Fichiers impactés

- `urpm/core/resolution/orphans.py` — `_build_reverse_deps`,
  `_merge_pool_requires`, `_collect_from_header`, `_collect_from_synthesis`,
  `find_all_orphans`
- Pas de changement de schéma DB

---

## 2. [HAUTE] `add_local_rpms` ne charge pas supplements/enhances

### Problème

`pool.py:726-732` — lors du chargement de RPMs locaux dans le pool
(install de fichiers .rpm), seuls requires, provides, recommends et
suggests sont ajoutés aux solvables. Supplements et enhances sont ignorés.

### Fix

Ajouter `SOLVABLE_SUPPLEMENTS` et `SOLVABLE_ENHANCES` dans la boucle
de chargement `add_local_rpms`, même pattern que recommends/suggests.

### Fichiers impactés

- `urpm/core/resolution/pool.py` — `add_local_rpms`

---

## 3. [MOYENNE] `--with-suggests` ignoré en mode upgrade

### Problème

Le flag `--with-suggests` est dans le parser CLI (main.py:1194-1196) mais
n'est jamais lu dans `upgrade.py`. Les suggests sont silencieusement
ignorés en mode upgrade. `install.py` les traite via
`find_available_suggests()` après la résolution principale.

### Fix

Dans `upgrade.py`, lire `args.with_suggests` et appliquer le même
traitement itératif que dans `install.py:449-459`.

### Fichiers impactés

- `urpm/cli/commands/upgrade.py`

---

## 4. [MOYENNE] Setting `install_recommends` mort

### Problème

`settings.py:57` définit `install_recommends: bool = True` mais ce setting
n'est jamais lu par le resolver. Le resolver prend `install_recommends` en
argument de ses méthodes, et c'est le CLI qui décide. Le setting existe
mais reste lettre morte.

### Fix

Faire du setting la valeur par défaut quand le CLI ne spécifie rien
explicitement. Le resolver devrait lire `ResolverSettings.install_recommends`
comme fallback si le paramètre n'est pas passé.

### Fichiers impactés

- `urpm/core/resolution/resolver.py` — constructeur ou `resolve_install`/
  `resolve_upgrade`
- `urpm/core/settings.py`

---

## 5. [BASSE] `_supplement_repo_requires` ne couvre que REQUIRES

### Problème

Le bug `add_mdk` de libsolv qui droppe ~7% des `@requires@` pourrait aussi
dropper des `@suggests@` / `@recommends@`. Le workaround
`_supplement_repo_requires` (pool.py:19-133) ne vérifie et ne corrige que
`SOLVABLE_REQUIRES`.

### Risque

Faible : les weak deps droppés ne cassent pas `rpm ts.check()`. L'impact
est une réduction silencieuse des recommends installés, pas un échec de
transaction.

### Fix éventuel

Étendre `_supplement_repo_requires` pour vérifier aussi
`SOLVABLE_RECOMMENDS` et `SOLVABLE_SUGGESTS` (même logique unset+re-add).

### Fichiers impactés

- `urpm/core/resolution/pool.py` — `_supplement_repo_requires`

---

## 6. [BASSE] `find_all_orphans` n'inclut pas SUGGESTNAME

### Problème

`find_all_orphans` (orphans.py:577-592) collecte `REQUIRENAME` et
`RECOMMENDNAME` mais pas `SUGGESTNAME`. Un paquet tenu uniquement par un
Suggests sera détecté orphelin. En revanche, `find_erase_orphans`
(orphans.py:1529-1534) collecte bien `SUGGESTNAME` (conditionnel
`keep_suggests`).

### Fix

Ajouter la collecte de `SUGGESTNAME` dans `find_all_orphans`, avec le même
garde `keep_suggests` que dans `find_erase_orphans` pour cohérence.

### Fichiers impactés

- `urpm/core/resolution/orphans.py` — `find_all_orphans`

---

## 7. [BASSE] Nettoyage `_supplement_repo_requires` post-fix prereq

### Problème

`_supplement_repo_requires` (pool.py:19-133) a été ajouté comme workaround
pour un supposé bug `add_mdk` droppant ~7% des requires. L'investigation
du 2026-04-17 a montré que le bug n'existe pas : `add_mdk` parse
correctement, mais `lookup_deparray(SOLVABLE_REQUIRES)` sans le paramètre
`SOLVABLE_PREREQMARKER` ne retourne que les deps *avant* le marker.

Le fix prereq (helper `lookup_all_requires`) rend la fonction quasi no-op
(elle ne trouvera quasiment plus de deps manquants). Elle reste en place
comme filet de sécurité.

### Nettoyage à faire

1. **Valider** sur plusieurs cycles de mises à jour que la fonction ne
   patche plus rien (surveiller le log `[pool-supplement]`).

2. **Simplifier** : convertir en diagnostic-only (log les écarts sans
   unset+re-add) si le compteur patché reste à 0 sur >3 mises à jour.

3. **Supprimer** à terme si le diagnostic confirme que `add_mdk` ne perd
   aucun dep réel.

### Fichiers impactés

- `urpm/core/resolution/pool.py` — `_supplement_repo_requires`

---

## Contexte : bug report Mageia associé

Bug remonté au bugzilla Mageia (2026-04-17) : `xdg-desktop-portal-kde` a
`Supplements: (flatpak and qtbase6-common)` — trop large, tire 64 paquets
KDE/KF6 sur les bureaux LXQt. Fix proposé au packager : cibler
`(flatpak and plasma-workspace)`.
