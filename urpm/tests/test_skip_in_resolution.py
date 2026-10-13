"""Le masque joue à la résolution, et seulement là.

Trois règles, reprises d'urpmi et vérifiées dans sa source :

* un paquet masqué n'est pas déclaré, donc ni proposé en mise à jour
  ni utilisé pour satisfaire la dépendance d'un autre ; c'est ce que
  fait ``_find_candidate_packages`` dans ``URPM/Resolve.pm``, qui
  saute ``flag_skip`` pour toute dépendance ;
* une demande explicite l'emporte, ``select.pm`` remet le drapeau à
  zéro pour un paquet nommé par l'utilisateur ;
* interroger n'est pas résoudre, ``urpmf`` a son ``no_skiplist``.
"""

from __future__ import annotations

import pathlib

import pytest

from urpm.core.resolver import Resolver, _extract_name
from urpm.core.skip_list import SkipList


class TestLExemptionDesDemandesExplicites:

    @pytest.mark.parametrize("saisi,nom", [
        ("firefox", "firefox"),
        ("firefox-153.3.0-1.mga10.x86_64", "firefox"),
        ("task-kde", "task-kde"),
    ])
    def test_le_nom_est_extrait_de_ce_qui_est_saisi(self, saisi, nom):
        assert _extract_name(saisi) == nom


class TestLeMasqueEstPoséParDéfaut:

    def test_un_résolveur_neuf_lit_le_fichier(self, tmp_path, monkeypatch):
        (tmp_path / "etc/urpmi").mkdir(parents=True)
        (tmp_path / "etc/urpmi/skip.list").write_text("thunderbird\n")

        resolver = Resolver.__new__(Resolver)   # sans toucher la base
        from urpm.core import skip_list
        resolver.skip_mask = skip_list.SkipList.from_file(str(tmp_path))

        assert resolver.skip_mask.masks("thunderbird")
        assert not resolver.skip_mask.masks("firefox")

    def test_sans_fichier_rien_n_est_masqué(self, tmp_path):
        from urpm.core import skip_list

        assert not skip_list.SkipList.from_file(str(tmp_path))


class TestLeChargementDesMédias:
    """Le masque est appliqué là où les paquets entrent dans le pool."""

    def test_le_chargeur_consulte_le_masque_et_l_exemption(self):
        import ast

        src = (pathlib.Path(__file__).resolve().parents[2]
               / "urpm/core/resolution/pool.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_load_repo_packages")
        body = ast.dump(fn)

        assert "skip_mask" in body, (
            "le chargeur des médias doit consulter le masque")
        assert "skip_exempt" in body, (
            "une demande explicite doit échapper au masque")

    def test_la_rpmdb_n_est_jamais_masquée(self):
        """Masquer un paquet installé le ferait disparaître du système.

        Le masque porte sur les candidats des médias.  Le chargeur de
        la base rpm ne doit pas le consulter, sinon libsolv croirait
        le paquet absent et proposerait de le réinstaller.
        """
        import ast

        src = (pathlib.Path(__file__).resolve().parents[2]
               / "urpm/core/resolution/pool.py").read_text()
        for name in ("_load_rpmdb", "_load_rpmdb_ts"):
            fn = next(n for n in ast.walk(ast.parse(src))
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            assert "skip_mask" not in ast.dump(fn), (
                f"{name} ne doit pas masquer : ce sont les paquets "
                "installés, pas des candidats")


class TestInterrogerNEstPasRésoudre:

    def test_rdepends_désarme_le_masque(self):
        import ast

        src = (pathlib.Path(__file__).resolve().parents[2]
               / "urpm/cli/commands/depends.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "cmd_rdepends")
        disarmed = any(
            isinstance(node, ast.Assign)
            and any(getattr(t, "attr", None) == "skip_mask"
                    for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and node.value.value is None
            for node in ast.walk(fn))

        assert disarmed, (
            "cmd_rdepends interroge, il ne résout pas : le masque doit "
            "y être désarmé")
