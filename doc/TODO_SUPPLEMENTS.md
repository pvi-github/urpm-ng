# Supplements & Weak Dependencies — TODO

Audit du 2026-04-17. Suivi des améliorations identifiées dans la gestion
des Recommends, Suggests et Supplements.

## Fait

- **#1 [HAUTE] Orphan detector ignore les Supplements** — livré par
  `cf3264d feat(orphans): honour Supplements/Enhances`.
  `orphans.py` collecte `RPMTAG_SUPPLEMENTNAME` depuis la rpmdb et
  injecte un arc protecteur dans `reverse_deps`. Expressions booléennes
  `(A and B)` traitées en OR conservateur.
- **#2 [HAUTE] `add_local_rpms` ne charge pas supplements/enhances** —
  livré par `cf3264d`. `pool.py:763-768` propage désormais
  `supplements` et `enhances` sur les solvables locaux (même pattern
  que recommends/suggests).

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

## Contexte : bug report Mageia associé

Bug remonté au bugzilla Mageia (2026-04-17) : `xdg-desktop-portal-kde` a
`Supplements: (flatpak and qtbase6-common)` — trop large, tire 64 paquets
KDE/KF6 sur les bureaux LXQt. Fix proposé au packager : cibler
`(flatpak and plasma-workspace)`.
