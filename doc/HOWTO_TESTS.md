# HOWTO — Lancer les tests urpm-ng

Les tests urpm-ng sont dans `urpm/tests/` et regroupés par thème
(`test_cli.py`, `test_database.py`, `test_resolver.py`, `test_synthesis.py`...).
Ce document liste les façons courantes de n'exécuter qu'un sous-ensemble de
tests sans relancer toute la suite.

## Tout lancer

```bash
python -m pytest urpm/tests/
```

## Par fichier

Le plus simple et le plus fréquent :

```bash
python -m pytest urpm/tests/test_synthesis.py
python -m pytest urpm/tests/test_synthesis.py urpm/tests/test_resolver.py
```

## Par classe ou fonction (syntaxe `::`)

Utile quand on cible un test précis ou une classe spécifique d'un fichier :

```bash
python -m pytest urpm/tests/test_cli.py::TestMediaCommands
python -m pytest urpm/tests/test_cli.py::TestMediaCommands::test_add_media
```

## Par mot-clé (`-k`)

Très pratique pour attraper un sous-groupe thématique sans se soucier de la
structure de fichiers/classes. Le motif matche les noms de tests (fichier,
classe, fonction) :

```bash
python -m pytest -k "ipfs"              # tous les tests dont le nom contient ipfs
python -m pytest -k "add_media or remove"
python -m pytest -k "not slow"          # exclusion
python -m pytest urpm/tests/test_cli.py -k "media"
```

## Par marker (`-m`)

Si des tests sont annotés avec `@pytest.mark.xxx` :

```bash
python -m pytest -m "not slow"
python -m pytest -m "integration"
```

Les markers doivent être déclarés dans `pytest.ini` ou `pyproject.toml` pour
éviter les warnings `PytestUnknownMarkWarning`.

## Options utiles à combiner

| Option | Effet |
|---|---|
| `-x` | Stop au premier échec |
| `--ff` | Rejoue en priorité les tests qui ont échoué la fois précédente |
| `-v` | Liste chaque test exécuté avec son résultat |
| `-q` | Sortie minimale |
| `-s` | Ne capture pas stdout (affiche les `print` des tests) |
| `--collect-only` | Liste ce qui serait lancé sans exécuter — pratique pour valider un filtre `-k` avant de se lancer |
| `--lf` | Relance uniquement les derniers tests qui ont échoué |
| `--tb=short` | Traceback condensé en cas d'échec |

## Combinaisons courantes

Itération rapide sur un fichier pendant le dev :

```bash
python -m pytest urpm/tests/test_synthesis.py -x -v
```

Débogage d'un échec ciblé :

```bash
python -m pytest urpm/tests/test_cli.py::TestMediaCommands::test_add_media -v -s --tb=long
```

Rejouer ce qui a cassé après une correction :

```bash
python -m pytest --lf -x
```

Valider un filtre `-k` avant de lancer :

```bash
python -m pytest urpm/tests/ -k "media and not slow" --collect-only
```

## Référence

- Documentation pytest officielle : https://docs.pytest.org/
- État actuel des tests du projet : voir `doc/TESTING.md` pour la couverture
  par module et les tests manquants.
