"""Ce qui tourne après le commit doit déjà être en mémoire avant lui.

Une mise à jour d'urpm-ng par urpm-ng remplace ses propres fichiers
pendant la transaction. Un import différé placé après le commit lit
alors le code **neuf** sur le disque et l'appelle sur les objets
**anciens** restés en mémoire. C'est ce qui s'est produit en 0.9.13
vers 0.9.14 :

    PackageOperations.hooks_triggered_by() takes 2 positional
    arguments but 3 were given

La transaction était pourtant réussie et enregistrée. L'exception a
remonté jusqu'au gestionnaire générique, le code de retour a dit faux,
et la fin de la queue post-commit a sauté, dont le marquage des
dépendances.

Trois invariants sont épinglés ici, un par geste de la correction.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

#: Les deux fonctions qui exécutent une transaction puis en traitent
#: les suites.  ``stages_4_5`` n'y est pas : il ne commit rien, il
#: rapporte, et son appel est déjà protégé.
SITES = {
    "urpm/cli/commands/_install_pipeline.py": "run_install_transaction",
    "urpm/core/distupgrade/phase_a.py": "run_phase_a_upgrade",
}

#: Ce que la queue post-commit appelle, et qui doit donc être chargé
#: avant la transaction.
POST_COMMIT_NAMES = {"run_post_operation_hooks", "Operation"}


def _tree(relpath):
    return ast.parse((REPO / relpath).read_text(encoding="utf-8"))


def _module_level_imports(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"fonction {name} introuvable")


@pytest.mark.parametrize("relpath,func", sorted(SITES.items()))
def test_the_post_commit_names_are_imported_at_module_load(relpath, func):
    """Premier geste : les imports remontés avant la transaction."""
    tree = _tree(relpath)

    assert POST_COMMIT_NAMES <= _module_level_imports(tree), (
        f"{relpath} doit importer {sorted(POST_COMMIT_NAMES)} au "
        f"chargement du module, pas dans {func}")


@pytest.mark.parametrize("relpath,func", sorted(SITES.items()))
def test_nothing_post_commit_is_imported_inside_the_function(relpath, func):
    """Le même geste, vu par l'autre bout : plus d'import local."""
    body = _function(_tree(relpath), func)
    local = {alias.asname or alias.name.split(".")[0]
             for node in ast.walk(body)
             if isinstance(node, (ast.Import, ast.ImportFrom))
             for alias in node.names}

    assert not (POST_COMMIT_NAMES & local), (
        f"{relpath}:{func} réimporte {sorted(POST_COMMIT_NAMES & local)} "
        "en local ; ce code tourne après le commit et doit être chargé "
        "avant lui")


def test_the_dependency_marking_precedes_the_rules():
    """Deuxième geste : enregistrer d'abord, évaluer les règles ensuite.

    Le marquage fait partie de ce qu'on enregistre de la transaction.
    Il ne doit pas dépendre du bon déroulement des règles, qui, elles,
    peuvent faire appel à n'importe quoi.
    """
    body = _function(_tree("urpm/cli/commands/_install_pipeline.py"),
                     "run_install_transaction")
    calls = [n for n in ast.walk(body) if isinstance(n, ast.Call)]

    def line_of(name):
        for call in calls:
            func = call.func
            label = getattr(func, "attr", None) or getattr(func, "id", None)
            if label == name:
                return call.lineno
        raise AssertionError(f"appel à {name} introuvable")

    assert line_of("mark_dependencies") < line_of("run_post_operation_hooks")


@pytest.mark.parametrize("relpath,func", sorted(SITES.items()))
def test_the_rule_evaluation_cannot_fail_a_committed_operation(relpath, func):
    """Troisième geste : l'évaluation des règles est protégée.

    Le chemin D-Bus le dit dans ses propres mots, « never fail a done
    operation ».  Les deux chemins qui commitent doivent en faire
    autant : les paquets sont posés, l'historique est écrit, une règle
    qui se passe mal se rapporte, elle ne transforme pas un succès en
    échec.
    """
    body = _function(_tree(relpath), func)
    def wraps_the_call(node):
        return any(
            isinstance(c, ast.Call)
            and getattr(c.func, "id", None) == "run_post_operation_hooks"
            for c in ast.walk(node))

    def catches_everything(node):
        # ``except Exception`` ou plus large.  Un ``except`` étroit ne
        # protège de rien : le défaut d'origine était une TypeError.
        for handler in node.handlers:
            if handler.type is None:
                return True
            if getattr(handler.type, "id", None) in ("Exception",
                                                     "BaseException"):
                return True
        return False

    wrapping = [node for node in ast.walk(body)
                if isinstance(node, ast.Try) and wraps_the_call(node)]
    assert wrapping, (
        f"{relpath}:{func} appelle run_post_operation_hooks sans filet")

    # Le plus petit : la fonction entière est déjà dans un try qui
    # annule la transaction et relance.  C'est justement celui-là qui
    # a marqué « abandonnée » une transaction réussie, donc il ne
    # compte pas comme filet.
    innermost = min(wrapping, key=lambda n: n.end_lineno - n.lineno)

    assert catches_everything(innermost), (
        f"{relpath}:{func} : le filet autour de run_post_operation_hooks "
        "n'attrape pas Exception, donc il ne protège pas du défaut qui "
        "a motivé ce test (une TypeError)")
