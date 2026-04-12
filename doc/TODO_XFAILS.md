# TODO — xfails & skips triage

Inventaire exhaustif des tests marqués `xfail` / `skip` dans la suite, classés
par **chemin de code impacté** pour faciliter l'attaque par famille. Objectif :
reconvertir chaque entrée en test actif vert (ou la supprimer si obsolète).

Date de l'inventaire : **2026-04-10** · dernière mise à jour : **2026-04-12**
Périmètre : `urpm/tests/` uniquement.
Totaux : **1 xfail + 2 skips** = 3 entrées restantes.

**Progression** :
- Famille A close (test_t débloqué le 2026-04-12, test_f reclassé en
  Famille C). Fix : comparaison EVR `_provider_satisfies` + dataclass
  `UpgradeOrphanPlan`.
- Famille C close (test_o + test_f débloqués le 2026-04-12). Fix :
  fusion des orphan erases dans la transaction principale (supprime
  l'état intermédiaire incohérent qui bloquait rpm).
- Famille B close (test_urpme_gg_g + test_unorphan_v1/v2 débloqués
  le 2026-04-12). Fix : deux bugs de bookkeeping corrigés — (1)
  `mark_dependencies` ne démote plus les UPGRADE/DOWNGRADE/REINSTALL,
  (2) `cmd_install` promeut les paquets explicites même quand le
  resolver les skippe (déjà installés).
- Famille D close à 9/10 (2026-04-12). Fix : (1) `_solve_with_auto_resolution`
  pour résolution itérative de conflits via libsolv Problem/Solution API,
  (2) chroot rpmdb loader remplacé par `_load_rpmdb` complet (Requires,
  Conflicts, Obsoletes), (3) `erase_names` transmis dans le flux install,
  (4) alternatives redondantes skippées quand un provider nommé couvre déjà
  la capability virtuelle (bug #46874). Le xfail restant
  (`test_failing_promotion`) est une différence de politique : le solver
  préfère substituer cross-arch plutôt que supprimer.

> Entrées hors backlog (légitimes, à ne pas toucher) :
> - `urpm/tests/test_orphans.py:217` — `pytest.importorskip('rpm')`
> - `urpm/tests/test_rpmsrate.py:167` — `pytest.skip` inline si le fichier
>   `rpmsrate-raw` n'est pas présent (infra, pas un bug).

---

## Synthèse par famille

| Famille | Chemin de code cible | xfails | skips | Effort | Priorité |
|---|---|---:|---:|---|---|
| A. `find_upgrade_orphans` — contraintes de version / renames | `urpm/core/resolution/orphans.py` + `urpm/cli/commands/upgrade.py` | ~~2~~ 0 | 0 | — | **Close** |
| B. `find_erase_orphans` / unrequested bookkeeping | `urpm/core/operations.py` + `urpm/cli/commands/install.py` + `upgrade.py` | ~~3~~ 0 | 0 | — | **Close** |
| C. `cmd_upgrade` — transaction silencieusement no-op | `urpm/cli/commands/upgrade.py` (chemin d'exécution rpm) | ~~2~~ 0 | 0 | — | **Close** |
| D. Résolveur libsolv — conflits & virtual-provides | `urpm/core/resolution/*.py` | ~~10~~ 1 | 0 | — | **9/10 Close** |
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

### Famille B — `find_erase_orphans` / bookkeeping `unrequested` ✅ CLOSE

Famille fermée le **2026-04-12**. Les trois tests sont débloqués.

| Fichier:ligne | Test | Statut |
|---|---|---|
| `test_install.py:1909` | `TestOrphans::test_urpme_gg_g` | ✅ Débloqué — décorateur xfail retiré |
| `test_install.py:1913` | `TestOrphans::test_unorphan_v1` | ✅ Débloqué — décorateur xfail retiré |
| `test_install.py:1916` | `TestOrphans::test_unorphan_v2` | ✅ Débloqué — décorateur xfail retiré |

**Cause racine :** deux bugs de bookkeeping dans le tracking
`installed-through-deps.list` :

1. **Bug 1 — demotion d'un paquet explicite lors d'un upgrade**
   (`operations.py`, `mark_dependencies`) — quand un paquet déjà
   installé explicitement est tiré comme dépendance transitive d'un
   autre install (ex: `install g` tire `gg` en UPGRADE), le resolver
   assigne `reason=DEPENDENCY` dans le contexte de la transaction
   courante. `mark_dependencies` appelait alors `mark_as_dependency`
   pour ce paquet, le déclassant en dep. **Fix :** ne marquer comme
   dep que les actions `TransactionType.INSTALL` (genuinely new).
   Les UPGRADE/DOWNGRADE/REINSTALL conservent leur statut existant.

2. **Bug 2 — promotion manquée pour un paquet déjà installé**
   (`install.py`, `cmd_install`) — quand l'utilisateur demande
   explicitement un paquet déjà installé à la bonne version, libsolv
   le skippe (`SOLVER_TRANSACTION_IGNORE`). Le early return "Nothing
   to do" court-circuitait `mark_dependencies`, donc `mark_as_explicit`
   n'était jamais appelé. **Fix :** appel `mark_as_explicit` pour les
   paquets demandés par l'utilisateur avant le early return.

3. **Nettoyage `upgrade.py`** — remplacement de l'appel direct
   `mark_as_dependency` (qui ignorait le champ `reason`) par
   `ops.mark_dependencies` pour respecter la sémantique EXPLICIT vs
   DEPENDENCY du resolver.

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

### Famille D — Résolveur libsolv : conflits et virtual-provides ✅ 9/10 CLOSE

9 tests débloqués le **2026-04-12** grâce à quatre corrections :
1. `_solve_with_auto_resolution()` dans `resolver.py` — résolution itérative
   de conflits via l'API Problem/Solution de libsolv (max 5 retries).
2. Chroot rpmdb loader dans `pool.py` — remplacé le loader incomplet
   (Provides seulement) par `_load_rpmdb()` complet (Requires, Conflicts,
   Obsoletes, weak deps).
3. Flux `erase_names` dans `install.py` → `operations.py` — les suppressions
   issues de conflits sont transmises à la transaction rpm.
4. Alternatives redondantes dans `alternatives.py` — skip quand un provider
   déjà en transaction ou nommément requis couvre la capability virtuelle
   (fix bug #46874).

**Tests débloqués** (→ `@pytest.mark.stable`) :
- `test_simple_c_then_d`, `test_simple_d_then_c` (conflit direct)
- `test_simple_e_then_f`, `test_simple_f_then_e` (conflit virtual-provide)
- `test_conflict_on_install` (install simultané de conflits)
- `test_conflict_on_upgrade` (conflit pendant upgrade)
- `test_conflict_upgrade_c_d`, `test_conflict_upgrade_a_b` (upgrade partiel)
- `test_prefer_b1_over_b2` (provider redondant, bug #46874)

**Xfail restant** (différence de politique, pas un bug) :

| Fichier:ligne | Test | Raison |
|---|---|---|
| `test_install.py:434` | `TestInstall::test_failing_promotion` | Solver prefers cross-arch substitute over removal (different policy from urpmi) |

Le solver libsolv trouve une solution techniquement valide (installer
f1.i686 quand f1.x86_64 est cassé) là où urpmi supprimait le paquet.
Nécessiterait un flag de politique configurable, pas un hack global.

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

- **Module chaud** : `urpm/core/resolution/` — nettoyé. Le seul xfail
  restant (`test_failing_promotion`) est une différence de politique, pas
  un bug de code.
- **Fichiers non concernés** : `test_cli.py`, `test_database.py`,
  `test_download.py`, `test_suggests.py`, `test_synthesis.py` — aucune
  entrée. `test_orphans.py` est propre depuis le rewrite de
  `find_upgrade_orphans` (commit `84a6780`).
- **Familles closes** : A, B, C, D (9/10), F — 17 xfails éliminés, 1 test
  obsolète supprimé. Reste 1 xfail + 2 skips.
- **Prochaine cible** : famille G (2 skips) — dépend d'infra multi-arch
  et de l'implémentation de `--force`.

---

*Ce document est un instantané de planification. Il doit être mis à jour à
chaque fois qu'un xfail est corrigé, reclassé ou supprimé.*
