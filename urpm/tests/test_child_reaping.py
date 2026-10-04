"""Les enfants que le parent n'attend pas doivent finir par être ramassés.

La synchronisation intelligente rend la main au parent dès la fin de
l'extraction, pendant que l'enfant poursuit les triggers. Ne pas
l'attendre est volontaire, c'est ce qui rend deux minutes à
l'utilisateur. Mais personne ne le ramasse ensuite : sans effet pour la
CLI, dont le parent se termine et dont init adopte l'orphelin, et une
entrée de table de processus perdue par transaction dans un service qui
vit des jours.

Constaté sur la machine de développement : PID 2931114, état ``ZNs``,
père le service D-Bus lui-même, né pendant une réinstallation.
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import time

import pytest

from urpm.core import transaction_queue as tq

REPO = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_registry():
    """Le registre est au niveau du module : on le rend aux tests vide."""
    with tq._UNREAPED_LOCK:
        tq._UNREAPED_CHILDREN.clear()
    yield
    with tq._UNREAPED_LOCK:
        tq._UNREAPED_CHILDREN.clear()


def _spawn(exit_after: float = 0.0) -> int:
    """Un vrai enfant, pour ne pas prouver le ramassage sur une maquette."""
    pid = os.fork()
    if pid == 0:                                  # pragma: no cover - enfant
        if exit_after:
            time.sleep(exit_after)
        os._exit(0)
    return pid


class TestLeRamassage:

    def test_un_enfant_terminé_est_ramassé(self):
        pid = _spawn()
        with tq._UNREAPED_LOCK:
            tq._UNREAPED_CHILDREN.add(pid)
        # Laisser le temps à l'enfant de mourir avant de le ramasser.
        for _ in range(200):
            if os.path.exists(f"/proc/{pid}"):
                with open(f"/proc/{pid}/stat") as fh:
                    if " Z " in fh.read():
                        break
            time.sleep(0.01)

        assert tq.reap_finished_children() == 1
        assert pid not in tq._UNREAPED_CHILDREN

    def test_un_enfant_vivant_est_laissé_pour_la_fois_d_après(self):
        pid = _spawn(exit_after=30)
        with tq._UNREAPED_LOCK:
            tq._UNREAPED_CHILDREN.add(pid)
        try:
            assert tq.reap_finished_children() == 0
            assert pid in tq._UNREAPED_CHILDREN
        finally:
            os.kill(pid, 9)
            os.waitpid(pid, 0)

    def test_un_pid_qui_n_est_pas_le_nôtre_sort_du_registre(self):
        # Le père de ce processus n'est pas son enfant : waitpid rend
        # ChildProcessError, et on ne garde pas une entrée morte.
        with tq._UNREAPED_LOCK:
            tq._UNREAPED_CHILDREN.add(os.getppid())

        assert tq.reap_finished_children() == 1
        assert not tq._UNREAPED_CHILDREN

    def test_le_statut_des_sous_processus_voisins_n_est_pas_volé(self):
        """Le balayage n'attend jamais sur ``-1``.

        Le même processus lance rpm et podman par ``subprocess``.  Un
        ``waitpid(-1)`` ramasserait leur statut au passage et leur
        ``wait`` lèverait ou mentirait.
        """
        # ``/bin/false`` et pas ``/bin/true`` : quand on lui vole son
        # statut, CPython encaisse l'ECHILD et rend 0.  Seul un code de
        # retour non nul distingue le vol de l'attente normale.
        proc = subprocess.Popen(["/bin/false"])
        pid = _spawn()
        with tq._UNREAPED_LOCK:
            tq._UNREAPED_CHILDREN.add(pid)
        time.sleep(0.2)

        tq.reap_finished_children()

        assert proc.wait(timeout=5) == 1


class TestLeCâblage:
    """Les deux points d'appel, épinglés sur la source."""

    def test_le_fork_balaie_avant_de_se_dédoubler(self):
        src = (REPO / "urpm/core/transaction_queue.py").read_text()
        tree = ast.parse(src)
        fork_line = next(
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "fork")
        sweep_lines = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "reap_finished_children"]

        assert any(line < fork_line for line in sweep_lines), (
            "le balayage doit précéder le fork, sinon l'enfant précédent "
            "attend la transaction d'après la prochaine")

    def test_la_branche_sans_attente_inscrit_l_enfant(self):
        src = (REPO / "urpm/core/transaction_queue.py").read_text()

        assert "_UNREAPED_CHILDREN.add(child_pid)" in src, (
            "la branche smart sync doit inscrire l'enfant qu'elle "
            "renonce à attendre")

    @pytest.mark.parametrize("relpath", [
        "urpm/daemon/scheduler.py",
        "urpm/dbus/service.py",
    ])
    def test_les_processus_qui_durent_balaient_au_repos(self, relpath):
        tree = ast.parse((REPO / relpath).read_text())
        called = any(
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "reap_finished_children"
            for node in ast.walk(tree))

        assert called, (
            f"{relpath} vit des jours : il doit appeler "
            "reap_finished_children, pas seulement l'importer")
