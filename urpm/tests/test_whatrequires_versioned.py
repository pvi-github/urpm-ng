"""Le côté médias doit répondre comme le côté rpmdb.

La synthesis range une dépendance dans une seule chaîne, contrainte
comprise, ``typelib(GnomeDesktop)[== 4.0]``. L'égalité sur le nom nu ne
tombait donc jamais sur une entrée versionnée, soit 22,7 % des lignes
de ``requires`` sur une installation complète. Mesuré sur la base réelle
avant correction : ``typelib(GnomeDesktop)`` rendait zéro consommateur
là où six existaient.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from urpm.core.database import PackageDatabase


@pytest.fixture
def db():
    tmp = tempfile.mkdtemp()
    base = PackageDatabase(str(pathlib.Path(tmp) / "packages.db"))
    conn = base._get_connection()
    conn.execute(
        "INSERT INTO media (id, name, short_name, mageia_version,"
        " architecture, relative_path, enabled, added_timestamp)"
        " VALUES (1,'M','m','10','x86_64','10/x86_64/media/core',1,0)")
    yield base


def _package(db, name, requires=()):
    conn = db._get_connection()
    cur = conn.execute(
        "INSERT INTO packages (media_id, name, name_lower, version, release,"
        " arch, nevra, source, added_timestamp)"
        " VALUES (1,?,?, '1','1.mga10','x86_64',?, 'synthesis', 0)",
        (name, name.lower(), f"{name}-1-1.mga10.x86_64"))
    pkg_id = cur.lastrowid
    for cap in requires:
        conn.execute("INSERT INTO requires (pkg_id, capability) VALUES (?,?)",
                     (pkg_id, cap))
    conn.commit()
    return pkg_id


class TestLaContrainteEstHonorée:

    def test_le_consommateur_de_la_bonne_version_est_rendu(self, db):
        _package(db, "gnome-shell", ["typelib(GnomeDesktop)[== 4.0]"])
        _package(db, "lutris", ["typelib(GnomeDesktop)[== 3.0]"])

        found = {r["name"] for r in
                 db.whatrequires("typelib(GnomeDesktop)", "4.0")}

        assert found == {"gnome-shell"}

    def test_celui_de_l_autre_version_ne_l_est_pas(self, db):
        _package(db, "gnome-shell", ["typelib(GnomeDesktop)[== 4.0]"])

        assert db.whatrequires("typelib(GnomeDesktop)", "3.0") == []

    def test_un_require_sans_contrainte_accepte_n_importe_quelle_version(self, db):
        # Règle de rpm : une dépendance sans contrainte est satisfaite
        # par n'importe quel fournisseur du nom.
        _package(db, "gjs", ["typelib(GnomeDesktop)"])

        found = {r["name"] for r in
                 db.whatrequires("typelib(GnomeDesktop)", "4.0")}

        assert found == {"gjs"}

    def test_sans_version_connue_seul_le_nom_compte(self, db):
        # Le passage D-Bus n'a pas de paquet en main : il demande par
        # nom et reçoit tout, ce qui est le comportement d'avant.
        _package(db, "gnome-shell", ["typelib(GnomeDesktop)[== 4.0]"])
        _package(db, "lutris", ["typelib(GnomeDesktop)[== 3.0]"])

        found = {r["name"] for r in db.whatrequires("typelib(GnomeDesktop)")}

        assert found == {"gnome-shell", "lutris"}


class TestLesPiègesDuPréfixe:

    def test_un_nom_qui_contient_des_crochets_n_est_pas_confondu(self, db):
        # ``python3.13dist(coverage[toml])`` est un nom entier, pas un
        # nom suivi d'une contrainte.  Le balayage par préfixe le
        # ramène, c'est au parseur de trancher.
        _package(db, "tox", ["python3.13dist(coverage[toml])"])

        assert db.whatrequires("python3.13dist(coverage") == []
        found = {r["name"] for r in
                 db.whatrequires("python3.13dist(coverage[toml])")}
        assert found == {"tox"}

    def test_la_limite_s_applique_après_le_filtre(self, db):
        """Le piège de cette correction.

        Appliquée en SQL, la limite tronque avant que la version soit
        regardée : on garderait des lignes que le filtre allait jeter,
        et on perdrait celles qu'il allait garder.
        """
        # Les lignes qui ne satisfont pas trient AVANT celles qui
        # satisfont : sans ça, une limite appliquée en SQL rendrait la
        # bonne réponse par accident et le test ne prouverait rien.
        for i in range(5):
            _package(db, f"aaa{i}", ["typelib(X)[== 1.0]"])
        for i in range(3):
            _package(db, f"zzz{i}", ["typelib(X)[== 2.0]"])

        found = {r["name"] for r in
                 db.whatrequires("typelib(X)", "2.0", limit=3)}

        assert found == {"zzz0", "zzz1", "zzz2"}


class TestLesTroisVerbes:

    @pytest.mark.parametrize("table,method", [
        ("recommends", "whatrecommends"),
        ("suggests", "whatsuggests"),
    ])
    def test_les_sœurs_suivent_la_même_règle(self, db, table, method):
        pkg_id = _package(db, "consommateur")
        conn = db._get_connection()
        conn.execute(f"INSERT INTO {table} (pkg_id, capability) VALUES (?,?)",
                     (pkg_id, "typelib(X)[== 2.0]"))
        conn.commit()

        assert getattr(db, method)("typelib(X)", "2.0")
        assert getattr(db, method)("typelib(X)", "1.0") == []


class TestLesAppelantsTransmettentLaVersion:
    """La correction ne vaut que si l'appelant donne la version.

    Le socle a beau savoir filtrer, un site d'appel qui interroge par
    nom seul reçoit tout, et le défaut est intact de son point de vue.
    """

    def test_depends_passe_la_version_partout(self):
        import ast

        src = (pathlib.Path(__file__).resolve().parents[2]
               / "urpm/cli/commands/depends.py").read_text()
        verbs = {"whatrequires", "whatrecommends", "whatsuggests"}
        bare = [
            node.lineno for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) in verbs
            and len(node.args) < 2
        ]

        assert not bare, (
            f"depends.py interroge la base par nom seul aux lignes "
            f"{bare} ; il faut passer la version du provide")
