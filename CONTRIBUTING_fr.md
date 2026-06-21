# Contribuer à urpm-ng

Merci de prendre le temps de lire ce document.  urpm-ng vise à devenir
le remplaçant Python de la chaîne d'outils `urpmi` historique de
Mageia, et toute amélioration — fix, fonctionnalité, doc, traduction,
test — est bienvenue.

Ce projet est porté par une communauté de bénévoles.  Le ton de nos
revues et de nos discussions est *de pair à pair* : on pointe le code,
pas les personnes ; on formule des suggestions, pas des ordres ; on
pose des questions honnêtes plutôt que rhétoriques.

> Les traductions de ce document sont disponibles dans
> [`CONTRIBUTING.md`](CONTRIBUTING.md) (anglais, version de référence),
> [`CONTRIBUTING_de.md`](CONTRIBUTING_de.md),
> [`CONTRIBUTING_es.md`](CONTRIBUTING_es.md),
> [`CONTRIBUTING_it.md`](CONTRIBUTING_it.md),
> [`CONTRIBUTING_nl.md`](CONTRIBUTING_nl.md), et
> [`CONTRIBUTING_pt.md`](CONTRIBUTING_pt.md).

## Objectifs du projet

- Code **lisible** : noms explicites, fonctions courtes,
  responsabilités claires.
- **Documenté** : docstrings sur tout le public API, commentaires là où
  le *pourquoi* n'est pas évident.
- **Facile à onboarder** : un nouveau contributeur doit pouvoir
  comprendre un module sans lire l'ensemble du projet d'abord.
- **Cohérent** : conventions uniformes dans tout le code.
- **Performant** : qualité et performance ne sont pas incompatibles —
  on vise les deux.

## Mise en place

Prérequis :

- Python 3.13 ou plus récent
- `python3-solv`, `python3-zstandard`, `python3-rpm`, `python3-curl`,
  `python3-pyyaml`
- `rpmbuild` (pour les fixtures de test et le pipeline build)

Checkout fonctionnel :

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng
urpmi python3-solv python3-zstandard
pytest urpm/tests/
```

Voir [`doc/HOWTO_TESTS.md`](doc/HOWTO_TESTS.md) pour le cheat sheet
pytest, et [`doc/TESTING.md`](doc/TESTING.md) pour l'état de la
couverture et les manques connus.

## Workflow

1. Branche depuis `main` (ou depuis la branche de release active si
   elle existe — par exemple `0.7.x` était active jusqu'à la 0.7.15,
   `0.8.x` est celle préparée au moment de l'écriture).
2. Écrire le changement, avec tests si possible.
3. Lancer les tests pertinents localement.  Les tests manuels restent
   nécessaires pour tout ce qui est visible utilisateur — la suite est
   un filet anti-régression, pas une garantie fonctionnelle.
4. Ouvrir une pull request décrivant ce que fait le changement *et
   pourquoi*.

## Conventions de commit

- **Langue** : anglais.  Les messages mixtes embrouillent l'historique.
- **Sujet** : ≤ 50 caractères, préfixe conventional-commit
  (`fix(...)`, `feat(...)`, `docs(...)`, `chore(...)`, `test(...)`).
- **Corps** : expliquer le *pourquoi*, pas seulement le *quoi*.  Le
  diff montre déjà le quoi.
- **Pas d'attribution IA** : le hook de commit rejette les lignes
  `Co-Authored-By` qui nomment des assistants IA.  L'auteur du commit
  est l'humain qui a tapé `git commit`.

## Règles d'hygiène git

- Ne JAMAIS faire `git add .` ni `git add -A`.  Ajouter les fichiers
  un par un — ça évite de committer accidentellement un `.env`, un
  brouillon, ou un artefact généré.
- Toujours faire `git status` avant de committer pour voir la liste
  des fichiers stagés.
- Ne jamais committer sans confirmation explicite, du reviewer ou de
  toi-même après un `git status`.
- Ne jamais sauter les hooks de commit.  Si un hook se plaint, corrige
  la cause plutôt que `--no-verify`.

## Ce qu'on ne commit PAS

- **Brouillons** à la racine du repo ou sous `doc/`.  Les brouillons
  vivants (`RELEASE_NOTE.md`, `doc/TODO_*.md` en cours, `SPEC_*.md`
  avant validation) vivent dans ton checkout local et sont promus
  dans le worktree uniquement quand validés.
- **Artefacts de test locaux** : `essais/` est dans le gitignore —
  mettre les scripts transitoires, les workdirs de smoke test, les
  sorties de `mktemp` là.  Jamais à la racine du repo.
- **Tout ce qui est nommé `bin/rebuild.sh` ou `bin/recup.sh`** —
  scripts personnels qui n'ont rien à faire upstream.
- **Vocabulaire DNF** : urpm-ng cible l'écosystème Mageia.  Comparer à
  DNF dans une note de bas de page est OK, mais éviter d'emprunter
  les noms de commandes DNF (`skip-broken`, `distro-sync`, ...) pour
  nos propres options.
- **Fichiers générés** : `.mo` catalogues, `__pycache__/`, `*.pyc`,
  builds Sphinx — tous gitignorés, à laisser ainsi.

## Conventions de documentation

- **Langues** : code, commentaires et messages de commit en anglais.
  La doc utilisateur sous `doc/` est en mix français/anglais (statu
  quo) ; les chaînes user-facing traduites passent par `po/` et les
  man pages vivent dans `man/<lang>/`.
- **Ton** : pédagogique et exploitable.  Un rapport doit être lisible
  par quelqu'un qui n'a pas suivi la conversation.  Éviter les
  tableaux télégraphiques avec codes cryptiques (C1/N7) sauf si
  chaque cellule est expliquée.
- **La longueur n'est pas une vertu**, mais la brièveté au prix de la
  clarté non plus.  Chaque paragraphe doit livrer au lecteur quelque
  chose qu'il ne pourrait déduire seul.

## Tests

- Les tests vivent dans `urpm/tests/`, **pas** dans un répertoire
  `tests/` à la racine.  Synthétiser les RPMs avec
  `urpm/tests/gen_test_rpms.py` et lancer pytest depuis
  `urpm/tests/` (l'infra de test exige ce répertoire de travail pour
  certains tests d'intégration).
- Les répertoires temporaires par test sont désormais nettoyés par
  une fixture autouse sur `BaseUrpmiTest`, donc un test qui plante
  avant son cleanup explicite ne fuit plus d'entrées `/tmp`.
- Pour les changements de man pages, valider avec
  `groff -man -Tutf8 -ww man/<lang>/man1/urpm.1` — les warnings
  UTF-8 préexistants ne sont pas bloquants, les nouvelles erreurs
  le sont.

## Revue de code

On se review en pairs.  Quand tu reçois une revue :

- Le reviewer pointe le code, pas toi.
- « Pourquoi as-tu fait X ? » est une vraie question ; réponds
  honnêtement plutôt que de te défendre.
- « On pourrait faire Y à la place ? » est une proposition, pas un
  verdict.

Quand tu donnes une revue :

- L'auto-dépréciation est OK, la prescription non.  Suggère, ne dicte
  pas.
- Demande avant de présumer.  « Je rate peut-être quelque chose,
  mais ... » est une bonne ouverture.
- Marque clairement les blockers ; les nitpicks de style restent des
  nitpicks de style.

## Releases

Le travail de release se fait sur une branche de version (`0.7.x`,
`0.8.x`, ...) et est ff-mergé vers `main` au moment de la release.
`main` porte l'historique des releases ; la branche active porte le
travail en cours.

`urpm/__init__.py` et `pyproject.toml` sont bumpés ensemble quand une
release est taggée.  Ne jamais décider unilatéralement d'un numéro
de version — demande au propriétaire du projet.

## Où vivent les choses

```
urpm/                  # Source
  cli/                 # Interface ligne de commande
  core/                # Résolveur, base de données, download, install...
  daemon/              # urpmd
  genmedia/            # Génération côté serveur (urpm genmedia)
  tests/               # TOUS les tests vivent ici, PAS dans /tests/
rpmdrake/              # Front-end graphique
man/<lang>/man1/       # Pages man traduites
po/                    # Catalogues de traduction (.po par langue)
doc/                   # Docs de design, plans, fichiers TODO
rpmbuild/SPECS/        # Fichiers .spec de packaging Mageia
data/                  # Unités systemd, règles polkit, etc.
```

Pour le catalogue cumulatif des fonctionnalités d'urpm-ng, voir
[`FEATURES.md`](FEATURES.md).  Pour l'historique des releases, voir
[`CHANGELOG.md`](CHANGELOG.md).  Pour les backlogs, voir
[`TODO.md`](TODO.md) et les fichiers spécialisés sous
[`doc/TODO_*.md`](doc/).
