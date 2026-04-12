# TODO — xfails & skips triage

Inventaire exhaustif des tests marqués `xfail` / `skip` dans la suite, classés
par **chemin de code impacté** pour faciliter l'attaque par famille. Objectif :
reconvertir chaque entrée en test actif vert (ou la supprimer si obsolète).

Date de l'inventaire : **2026-04-10** · dernière mise à jour : **2026-04-12**
Périmètre : `urpm/tests/` uniquement.
Totaux : **13 xfails + 2 skips** = 15 entrées à traiter.

**Progression** :
- Famille A close (test_t débloqué le 2026-04-12, test_f reclassé en
  Famille C). Fix : comparaison EVR `_provider_satisfies` + dataclass
  `UpgradeOrphanPlan`.
- Famille C close (test_o + test_f débloqués le 2026-04-12). Fix :
  fusion des orphan erases dans la transaction principale (supprime
  l'état intermédiaire incohérent qui bloquait rpm).

> Entrées hors backlog (légitimes, à ne pas toucher) :
> - `urpm/tests/test_orphans.py:217` — `pytest.importorskip('rpm')`
> - `urpm/tests/test_rpmsrate.py:167` — `pytest.skip` inline si le fichier
>   `rpmsrate-raw` n'est pas présent (infra, pas un bug).

---

## Synthèse par famille

| Famille | Chemin de code cible | xfails | skips | Effort | Priorité |
|---|---|---:|---:|---|---|
| A. `find_upgrade_orphans` — contraintes de version / renames | `urpm/core/resolution/orphans.py` + `urpm/cli/commands/upgrade.py` | ~~2~~ 0 | 0 | — | **Close** |
| B. `find_erase_orphans` / unrequested bookkeeping | `urpm/core/resolution/orphans.py` + `urpm/cli/commands/upgrade.py` (`mark_dependencies`) | 3 | 0 | M | Haute |
| C. `cmd_upgrade` — transaction silencieusement no-op | `urpm/cli/commands/upgrade.py` (chemin d'exécution rpm) | ~~2~~ 0 | 0 | — | **Close** |
| D. Résolveur libsolv — conflits & virtual-provides | `urpm/core/resolution/*.py` | 10 | 0 | L | Moyenne |
| F. Test incorrect (obsolète) | *n/a* — supprimé | 0 | 0 | — | **Close** |
| G. Fonctionnalités non implémentées | `urpm/cli/commands/install.py` ; infra multi-arch | 0 | 2 | L | Basse |

Effort : S = <1 j · M = 1–3 j · L = 3 j+. `?` = à re-évaluer avant estimation.

---

## Détail par famille

### Famille A — `find_upgrade_orphans` : renames et contraintes de version ✅ CLOSE

Famille fermée le **2026-04-12**. Les deux tests étaient mal regroupés :

| Fichier:ligne | Test | Statut |
|---|---|---|
| `test_install.py:1902` | `TestOrphans::test_auto_select_t` | ✅ Débloqué — décorateur xfail retiré |
| `test_install.py:1863` | `TestOrphans::test_auto_select_f` | ↪ Reclassé Famille C |

**Fix effectif :**

1. **Dataclass `UpgradeOrphanPlan`** (`orphans.py`) — `find_upgrade_orphans`
   retourne maintenant `{removes, cancelled_new_versions}` au lieu d'une
   `list[PackageAction]` à plat. Cela sépare proprement les deux concerns :
   « paquets à effacer du rpmdb » vs « nouvelles versions à ne pas
   installer ». L'ancien retour plat conflait les deux et causait des
   transactions rpm silencieusement no-op quand libsolv recevait à la fois
   un INSTALL et un REMOVE sur le même nom de paquet.

2. **Graphe reverse-dep versionné** (`orphans.py`, `_build_reverse_deps` +
   `_provider_satisfies`) — une edge `req → provider` n'est désormais
   ajoutée que si l'EVR du provider satisfait vraiment la contrainte du
   require. Permet à `tt1 (Provides tt = 1)` d'être reconnu orphelin face
   à `tt2 (Provides tt = 2)` + `Requires tt >= 2`.

3. **Sémantique release-granularity dans `_provider_satisfies`** (bug
   découvert pendant la validation) — rpm traite `Requires: foo = 1`
   comme « n'importe quelle release ». Le fix blanke le release du
   provider lorsque la require n'en a pas, réplique fidèle de
   `rpmdsCompare`. Sans ce traitement, l'edge `nn → n` du cycle
   version-pinné symétrique était rejetée (labelCompare renvoie `1` sur
   `('0','1','1') vs ('0','1','')`) et les tests `_g/_m/_n` régressaient.

4. **Projection caller** (`upgrade.py`) — les actions
   `INSTALL/UPGRADE/DOWNGRADE/REINSTALL` dont le nom est dans
   `cancelled_new_versions` sont droppées de `result.actions` **avant**
   la catégorisation, et les `.rpm` correspondants sont filtrés de
   `rpm_paths` avant appel à `resilient_install`.

**Tests de régression** ajoutés dans `test_orphans.py` :
- `test_version_constraint_filters_stale_provider` — pin du comportement
  virtual-rename avec contrainte de version.
- `test_cancelled_new_install_not_emitted_as_remove` — pin de la
  séparation `removes` vs `cancelled_new_versions`.

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

### Famille C — `cmd_upgrade` : transaction silencieusement no-op ✅ CLOSE

Famille fermée le **2026-04-12**. Les deux tests sont débloqués.

| Fichier:ligne | Test | Statut |
|---|---|---|
| `test_install.py:1863` | `TestOrphans::test_auto_select_f` | ✅ Débloqué — décorateur xfail retiré |
| `test_install.py:1889` | `TestOrphans::test_auto_select_o` | ✅ Débloqué — décorateur xfail retiré |

**Cause racine :** les orphan erases étaient exécutés dans une
transaction rpm **séparée** (background `add_erase`), alors que les
orphelins ont souvent des `Requires` sur des paquets en cours d'upgrade
dans la transaction principale. rpm voyait un état intermédiaire
incohérent (ex: `oo1 Requires o=1` mais `o-1` → `o-2`) et bloquait.
Le code classait ce blocage comme "scriptlet deps (ignored)" via un
heuristique de nom de fichier (`rsplit('-', 2)[0]`), puis le filtre
`"is needed by (installed)"` sur `ts.run()` avalait le problème et
retournait `True, total, []` — succès annoncé, rpmdb inchangée.

**Fix :** fusion de `orphan_names` dans `erase_names` au site d'appel
de `resilient_install` dans `upgrade.py`. Tout passe dans une seule
`rpm.TransactionSet` : installs, upgrades, erases (obsoleted) et
orphan erases. rpm voit le tableau complet et applique tout d'un coup.
Pas d'impact sur le temps de transaction (une seule passe au lieu de
deux).

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

### Famille F — Test obsolète à supprimer ✅ CLOSE

Test `test_auto_select_r_with_rr2` supprimé le **2026-04-12**. Le test
partait du principe que libsolv installe les deux providers d'une même
capability, ce qui est incorrect. Le scénario mono-provider est déjà
couvert par `test_auto_select_r`.

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
