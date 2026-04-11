# TODO — xfails & skips triage

Inventaire exhaustif des tests marqués `xfail` / `skip` dans la suite, classés
par **chemin de code impacté** pour faciliter l'attaque par famille. Objectif :
reconvertir chaque entrée en test actif vert (ou la supprimer si obsolète).

Date de l'inventaire : **2026-04-10**
Périmètre : `urpm/tests/` uniquement.
Totaux : **16 xfails + 3 skips** = 19 entrées à traiter.

> Entrées hors backlog (légitimes, à ne pas toucher) :
> - `urpm/tests/test_orphans.py:217` — `pytest.importorskip('rpm')`
> - `urpm/tests/test_rpmsrate.py:167` — `pytest.skip` inline si le fichier
>   `rpmsrate-raw` n'est pas présent (infra, pas un bug).

---

## Synthèse par famille

| Famille | Chemin de code cible | xfails | skips | Effort | Priorité |
|---|---|---:|---:|---|---|
| A. `find_upgrade_orphans` — contraintes de version / renames | `urpm/core/resolution/orphans.py` + `urpm/cli/commands/upgrade.py` | 2 | 0 | M | Haute |
| B. `find_erase_orphans` / unrequested bookkeeping | `urpm/core/resolution/orphans.py` + `urpm/cli/commands/upgrade.py` (`mark_dependencies`) | 3 | 0 | M | Haute |
| C. `cmd_upgrade` — transaction silencieusement no-op | `urpm/cli/commands/upgrade.py` (chemin d'exécution rpm) | 1 | 0 | M | Haute |
| D. Résolveur libsolv — conflits & virtual-provides | `urpm/core/resolution/*.py` | 10 | 0 | L | Moyenne |
| F. Test incorrect (obsolète) | *n/a* — supprimer ou réécrire | 0 | 1 | S | Haute |
| G. Fonctionnalités non implémentées | `urpm/cli/commands/install.py` ; infra multi-arch | 0 | 2 | L | Basse |

Effort : S = <1 j · M = 1–3 j · L = 3 j+. `?` = à re-évaluer avant estimation.

---

## Détail par famille

### Famille A — `find_upgrade_orphans` : renames et contraintes de version

Symptôme commun : un paquet renommé (sans `Obsoletes`) ou avec `Requires`
versionnée n'est pas détecté comme orphelin après upgrade. Le plan upgrade
sort sans la suppression attendue.

| Fichier:ligne | Test | Raison décorateur |
|---|---|---|
| `test_install.py:1863` | `TestOrphans::test_auto_select_f` | Resolver: dep rename without Obsoletes not handled |
| `test_install.py:1902` | `TestOrphans::test_auto_select_t` | find_upgrade_orphans ignores version constraints |

**Notes investigation :**
- `test_f` : fix attendu plutôt dans `cmd_upgrade` (exclusion des orphelins
  transitifs de `rpm_paths`). Un commentaire dans `orphans.py` annonce
  l'exclusion mais elle n'a pas d'effet — à confirmer.
- `test_t` : confirmé Famille A après re-triage. Plan upgrade pour `t-1 →
  t-2` ne contient que `Update t-2 / Install tt2-2`, sans la suppression
  de `tt1` qui est devenu orphelin. La raison du décorateur est correcte ;
  l'ancien soupçon "DB lock" était un flake parallèle. Trace :
  `/tmp/redflag_t.log`.

### Famille B — `find_erase_orphans` / bookkeeping `unrequested`

Symptôme : un paquet **explicite** perd son statut au passage d'une opération
(upgrade ou install/upgrade/autoremove), puis se fait silencieusement
moissonner comme orphelin. Cible code : `mark_dependencies` /
`mark_as_explicit` / `_save_unrequested_packages` autour des flows
`install`, `upgrade`, `autoremove`.

| Fichier:ligne | Test | Raison décorateur |
|---|---|---|
| `test_install.py:1923` | `TestOrphans::test_urpme_gg_g` | Bookkeeping: explicit status lost during upgrade (gg demoted to dep) |
| `test_install.py:1929` | `TestOrphans::test_unorphan_v1` | Resolver: package lost after install/upgrade/autoremove |
| `test_install.py:1934` | `TestOrphans::test_unorphan_v2` | Resolver: package lost after install/upgrade/autoremove |

**Notes investigation :**
- `test_urpme_gg_g` : reclassé depuis Famille C. Scénario re-rejoué :
  `gg` installé explicitement, puis `g` explicitement, puis `urpm upgrade`.
  Le résumé d'upgrade affiche `gg-2` en "Dépendance" — `gg` a perdu son
  statut explicite pendant la mise à jour. Sur `urpme g`, `gg` est alors
  flaggé orphelin et supprimé ; il ne devrait pas l'être. Raison
  décorateur "upgrade not applied" est trompeuse, à corriger en
  `"Bookkeeping: explicit status lost during upgrade"`. Trace :
  `/tmp/redflag_gg.log`.
- `test_unorphan_v1` / `v2` : jumeaux. Probablement même cause racine que
  `gg_g`. À traiter en grappe.

### Famille C — `cmd_upgrade` : transaction silencieusement no-op

| Fichier:ligne | Test | Raison décorateur |
|---|---|---|
| `test_install.py:1889` | `TestOrphans::test_auto_select_o` | cmd_upgrade: rpm transaction silently applies nothing (plan correct, no-op execution) |

**Notes investigation :**
- `test_auto_select_o` : reclassé depuis Famille A après re-triage. Le
  plan upgrade est calculé **correctement** (Update `o-2`, Install
  `oo2-2`, Remove `oo1-1`), les paquets sont téléchargés, le message
  final affiche "2 packages upgraded"… mais la rpmdb reste sur `o-1-1,
  oo1-1-1`. **La transaction rpm est un no-op silencieux.** Aucun
  affichage d'install/remove n'apparaît après le téléchargement. Raison
  décorateur à corriger en `"cmd_upgrade: rpm transaction silently
  applies nothing"`. Trace : `/tmp/redflag_o.log`.
- Différence avec `test_t` : `test_t` calcule un plan **incomplet**
  (oubli de `tt1`) ; `test_o` calcule un plan **complet** mais ne
  l'exécute pas. Symptômes proches en surface, causes opposées.

### Famille D — Résolveur libsolv : conflits et virtual-provides

Bloc le plus volumineux du backlog. Tous partagent des raisons génériques
("conflict … fails", "virtual-provide … fails"). Probablement **un seul fix
libsolv** peut en déverrouiller plusieurs à la fois.

| Fichier:ligne | Test | Raison |
|---|---|---|
| `test_install.py:435`  | `TestInstall::test_failing_promotion` | Resolver bug: upgrade promotion fails |
| `test_install.py:811`  | `TestHandleConflictDeps::test_simple_c_then_d` | Resolver: conflict dependency resolution fails |
| `test_install.py:818`  | `TestHandleConflictDeps::test_simple_d_then_c` | Resolver: conflict dependency resolution fails |
| `test_install.py:825`  | `TestHandleConflictDeps::test_simple_e_then_f` | Resolver: virtual-provide conflict resolution fails |
| `test_install.py:832`  | `TestHandleConflictDeps::test_simple_f_then_e` | Resolver: virtual-provide conflict resolution fails |
| `test_install.py:839`  | `TestHandleConflictDeps::test_conflict_on_install` | Resolver: simultaneous conflicting install fails |
| `test_install.py:865`  | `TestHandleConflictDeps::test_conflict_on_upgrade` | Resolver: conflict resolution during upgrade fails |
| `test_install.py:960`  | `TestHandleConflictDeps2::test_conflict_upgrade_c_d` | Resolver: mismatch in conflict-upgrade resolution |
| `test_install.py:977`  | `TestHandleConflictDeps2::test_conflict_upgrade_a_b` | Resolver: a1 not replaced by a2, provider install fails |
| `test_install.py:2063` | `TestPrefer2::test_prefer_b1_over_b2` | Resolver: prefer logic installs both providers instead of one |

**Stratégie suggérée :**
1. Rejouer chaque test avec `--runxfail -x` et collecter la trace libsolv.
2. Grouper par cause racine (solver flag manquant ? mauvaise pondération ?
   mauvaise préférence de provider ?).
3. Traiter par grappe, pas un par un.

### Famille F — Test obsolète à supprimer

| Fichier:ligne | Test | Raison |
|---|---|---|
| `test_install.py:1907` | `TestOrphans::test_auto_select_r_with_rr2` | Test itself is incorrect — libsolv only installs one provider |

**Action :** supprimer ou réécrire pour refléter le comportement correct de
libsolv (installe un seul provider). **Gain facile**, à faire en premier.

### Famille G — Fonctionnalités non encore implémentées

| Fichier:ligne | Test | Raison |
|---|---|---|
| `test_install.py:1018` | `TestI586ToI686::test_i586_replaced_by_i686` | Infra: no multi-arch RPMs available for testing |
| `test_install.py:1120` | `TestMediaInfoDir::test_force_skip_unknown` | `--force` not yet implemented |

**Actions :**
- `i586→i686` : skip infra → à réactiver quand on aura un jeu de RPMs
  multi-arch dans `urpm/tests/data/`.
- `--force` : skip fonctionnel → à réactiver après implémentation de
  l'option `--force` dans `install`.

---

## Red flags — re-triage 2026-04-11

Trois entrées avaient une raison de décorateur en désaccord avec le symptôme
observé. Re-rejouées avec `pytest --runxfail -x -v`, traces dans `/tmp/`.

| Test | Famille initiale | Famille confirmée | Conclusion |
|---|---|---|---|
| `test_auto_select_o` | A | **C** | Plan upgrade correct mais transaction rpm no-op. Raison décorateur à corriger. |
| `test_auto_select_t` | A | **A** (inchangée) | Plan upgrade incomplet, `tt1` non flaggé. Le "DB lock" précédent était un flake parallèle. |
| `test_urpme_gg_g` | C | **B** | Statut explicite perdu pendant upgrade. Raison décorateur à corriger. |

**Statut :** raisons des décorateurs corrigées dans la même session. Le
backlog peut maintenant être ordonnancé sans risque de fixer au mauvais
endroit.

---

## Synthèse cross-fichiers

- **Module chaud** : `urpm/core/resolution/` accumule **15/16 xfails** (tout
  sauf la famille G skips). C'est l'endroit où investir.
- **Fichiers non concernés** : `test_cli.py`, `test_database.py`,
  `test_download.py`, `test_suggests.py`, `test_synthesis.py` — aucune
  entrée. `test_orphans.py` est propre depuis le rewrite de
  `find_upgrade_orphans` (commit `84a6780`).
- **Gains rapides** (à faire en premier) :
  1. Famille F (1 skip) — suppression / réécriture triviale.
  2. Famille A (3 xfails) — trio structuré, fix probablement localisé.
  3. Re-triage des 3 red flags — bloquant pour prioriser la suite.
- **Gros morceau** : famille D (10 xfails libsolv) — à attaquer en dernier,
  après avoir collecté les traces et identifié les causes racines communes.

---

*Ce document est un instantané de planification. Il doit être mis à jour à
chaque fois qu'un xfail est corrigé, reclassé ou supprimé.*
