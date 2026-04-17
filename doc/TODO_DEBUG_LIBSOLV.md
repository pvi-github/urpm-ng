# TODO : Corrections libsolv `add_mdk` (repo_mdk.c)

## Contexte

libsolv charge les repos Mageia via `add_mdk()` qui parse le format synthesis.
Deux problèmes distincts ont été identifiés, avec des workarounds côté urpm-ng.

---

## 1. PREREQMARKER et ordering des requires

### Le problème

Le format synthesis marque les prereqs avec `[*]` :

```
@requires@glibc@/bin/sh[*]@lib64foo >= 2.0@rpm-helper[*]
```

`add_mdk` traite les deps séquentiellement. Quand il rencontre le premier
`[*]`, il pose un `SOLVABLE_PREREQMARKER` dans le deparray. Toutes les deps
suivantes — même sans `[*]` — atterrissent après le marqueur.

Résultat dans le deparray :

```
[ glibc | MARKER | /bin/sh, lib64foo >= 2.0, rpm-helper ]
```

`lookup_deparray(SOLVABLE_REQUIRES)` sans second argument ne retourne que la
section avant le marqueur. Environ 7% des paquets de Core Release ont leur
premier require marqué `[*]`, ce qui peut vider la section normale.

### Workaround urpm-ng (en place, commit 0071f41)

`lookup_all_requires()` dans `urpm/core/resolution/pool.py` fusionne les
deux sections :

```python
normal = solvable.lookup_deparray(solv.SOLVABLE_REQUIRES)
prereq = solvable.lookup_deparray(
    solv.SOLVABLE_REQUIRES, solv.SOLVABLE_PREREQMARKER,
)
return list(normal) + list(prereq)
```

15 call sites migrés dans le résolveur, l'orphan detector et la CLI.

### Fix libsolv envisagé

Deux options dans `ext/repo_mdk.c` :

- **Option A** : trier les deps dans `add_mdk` pour que les non-prereqs
  passent avant le MARKER, puis les prereqs après. Requiert un buffer
  intermédiaire.
- **Option B** : ne pas utiliser PREREQMARKER du tout dans `add_mdk` et
  mettre toutes les deps dans la section normale (comme `add_rpmdb`).
  Le prereq ordering n'est utile que pour `rpm --install`, pas pour la
  résolution de dépendances.

L'option B est plus simple et aligne le comportement sur `add_rpmdb`.

---

## 2. Requires complètement droppés par `add_mdk`

### Le problème

35 paquets sur Core Release ont des requires qui n'apparaissent ni dans la
section normale ni dans la section prereq après chargement par `add_mdk`.
Le parser C les droppe silencieusement. Cause exacte non identifiée —
probablement des capability strings que le parser ne gère pas.

### Workaround urpm-ng (en place, commit 3057a0d)

`_supplement_repo_requires()` dans `urpm/core/resolution/pool.py` re-parse
le synthesis avec le parser Python, compare les requires, et injecte les
manquants via `unset()` + `add_deparray()`.

**Coût** : 1.9s sur Core Release (80% du temps de pool creation, ~5x le
temps sans la rustine). Mesuré via `--debug timing`.

### TODO

1. Lister les 35 paquets concernés pour comprendre le pattern commun
2. Examiner `repo_mdk.c` lignes de parsing `@requires@` pour identifier
   quelles formes de capability strings sont droppées
3. Corriger le parser C
4. Une fois le fix libsolv déployé, retirer `_supplement_repo_requires()`

---

## 3. Bugs annexes dans `repo_mdk.c` (basse priorité)

### `@recommends@` offset incorrect

Ligne ~127 de `repo_mdk.c` : `buf + 10` devrait être `buf + 12`
(`@recommends@` fait 12 caractères). N'affecte pas Mageia actuellement
(pas de `@recommends@` dans les synthesis générés par genhdlist2).

### `@supplements@` et `@enhances@` non parsés

Ces tags ne sont pas reconnus par `add_mdk`. Même remarque : pas présents
dans les synthesis Mageia actuels.

---

## Fichiers concernés

- `ext/repo_mdk.c` — parser synthesis libsolv (dépôt `/home/superadmin/Sources/libsolv/`)
- `urpm/core/resolution/pool.py` — `lookup_all_requires()` et `_supplement_repo_requires()`
- Branche libsolv existante : `fix/mdk-requires-parser` (vide, aucun commit)

## Mesures de référence (avril 2026, Mageia 10)

```
Pool supplement Core Release: patched=35, 1.879s
Pool supplement Core Updates: patched=0, 0.000s
Pool supplement Nonfree Release: patched=0, 0.009s
Pool supplement Nonfree Updates: patched=0, 0.000s
Pool supplement Tainted Release: patched=0, 0.022s
Pool supplement Tainted Updates: patched=0, 0.000s
Pool creation total: 2.341s
```
