"""Les médias cibles doivent hériter du choix de l'utilisateur.

Sur la machine d'un testeur, un distupgrade 9 vers 10 a créé soixante
médias cibles, en a activé zéro, synchronisé « 0/0 », et accusé le
résolveur :

    distupgrade solve returned ZERO actions — every candidate was held
    by libsolv.  ... this is a resolver anomaly.  Please report

Le résolveur n'y était pour rien, on lui avait donné un pool vide.
L'héritage d'activation reposait sur le ``short_name``, que seul notre
catalogue produit : ses médias venaient de la configuration d'urpmi et
portaient autre chose, donc aucune correspondance, donc tout
désactivé. Et un ensemble de correspondances vide était pris pour
« l'utilisateur n'avait rien d'actif ».

Le catalogue est servi en ``file://`` : pas de réseau, mais la vraie
fonction, la vraie primitive d'upsert et la vraie base.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from urpm.core.database import PackageDatabase
from urpm.core.distupgrade.stage1 import (
    _collect_source_enabled_sections, _disable_source_media,
    _insert_target_media, _section_key,
)
from urpm.core.distupgrade.version import ReleaseIdentity

MEDIA_CFG = """[media_info]
version=10
arch=x86_64

[core/release]
name=Core Release
media_type=official:release

[nonfree/release]
name=Nonfree Release
media_type=official:release
noauto=1
"""


@pytest.fixture
def world():
    """Une base neuve et un catalogue cible sur disque."""
    root = pathlib.Path(tempfile.mkdtemp())
    cfg = root / "10/x86_64/media/media_info"
    cfg.mkdir(parents=True)
    (cfg / "media.cfg").write_text(MEDIA_CFG)

    db = PackageDatabase(str(root / "packages.db"))
    conn = db._get_connection()
    conn.execute(
        "INSERT INTO server (name, protocol, host, base_path, is_official,"
        " enabled, priority, added_timestamp)"
        " VALUES ('local','file','',?,1,1,50,0)", (str(root),))
    conn.commit()
    return db, conn


def _source_media(conn, name, short_name, section="core/release",
                  is_official=1, enabled=1, version="9"):
    conn.execute(
        "INSERT INTO media (name, short_name, is_official, mageia_version,"
        " architecture, relative_path, enabled, added_timestamp)"
        " VALUES (?,?,?,?, 'x86_64', ?, ?, 0)",
        (name, short_name, is_official, version,
         f"{version}/x86_64/media/{section}", enabled))
    conn.commit()


def _stage1(db, sections=None):
    """La séquence réelle : collecter, désactiver, insérer.

    L'ordre compte : la collecte précède la désactivation, qui suffixe
    les noms source en ``[dg:9]`` et libère ainsi le nom canonique
    pour le média cible.  Sauter cette étape fait échouer l'insertion
    sur la contrainte d'unicité, ce qui n'est pas le sujet ici.
    """
    if sections is None:
        sections = _collect_source_enabled_sections(db, "9")
    _disable_source_media(db, "9")
    return _insert_target_media(
        db, ReleaseIdentity(identity="10", numeric="10"), "x86_64",
        source_identity="9", source_enabled_sections=sections)


def _enabled(conn):
    return {r["short_name"]: r["enabled"] for r in conn.execute(
        "SELECT short_name, enabled FROM media WHERE mageia_version='10'")}


class TestLaCléDHéritage:

    def test_la_version_ne_fait_pas_partie_de_la_clé(self):
        assert (_section_key("9/x86_64/media/core/release", "x86_64")
                == _section_key("10/x86_64/media/core/release", "x86_64"))

    def test_l_architecture_en_fait_partie(self):
        assert (_section_key("10/i686/media/core/release", "i686")
                != _section_key("10/x86_64/media/core/release", "x86_64"))

    def test_un_chemin_sans_segment_media_n_a_pas_de_clé(self):
        # Forme d'un dépôt tiers : traité ailleurs, pas par l'héritage.
        assert _section_key("rpms/x86_64", "x86_64") is None


class TestLHéritage:

    def test_le_choix_de_l_utilisateur_est_repris(self, world):
        """Témoin : des médias source issus de notre catalogue."""
        db, conn = world
        _source_media(conn, "Core Release", "core_release")
        _source_media(conn, "Nonfree Release", "nonfree_release",
                      section="nonfree/release")

        _stage1(db)

        # Nonfree est en noauto au catalogue, mais l'utilisateur
        # l'avait activé : son choix prime.
        assert _enabled(conn) == {"core_release": 1, "nonfree_release": 1}

    def test_une_base_venue_d_urpmi_hérite_quand_même(self, world):
        """Le cas du testeur, celui qui a tout déclenché."""
        db, conn = world
        _source_media(conn, "Core Release (HDnoire1)", "core_release_hdnoire1")

        _stage1(db)

        assert _enabled(conn)["core_release"] == 1

    def test_ce_qui_n_était_pas_actif_ne_le_devient_pas(self, world):
        db, conn = world
        _source_media(conn, "Core Release", "core_release")
        _source_media(conn, "Nonfree Release", "nonfree_release",
                      section="nonfree/release", enabled=0)

        _stage1(db)

        assert _enabled(conn) == {"core_release": 1, "nonfree_release": 0}

    def test_sans_correspondance_on_retombe_sur_la_règle_du_catalogue(
            self, world):
        """Un ensemble vide ne veut pas dire « rien n'était actif ».

        C'est l'autre moitié du défaut : pris au pied de la lettre, il
        désactivait les soixante médias cibles sans un mot.
        """
        db, conn = world

        _stage1(db, set())

        # noauto honoré, et rien d'autre : core oui, nonfree non.
        assert _enabled(conn) == {"core_release": 1, "nonfree_release": 0}


class TestCeQuiNePeutPasÊtreCréé:

    def test_un_média_ignoré_est_nommé_et_remonté(self, world):
        """La primitive donne le motif, Stage 1 le jetait.

        Un média source non officiel garde son nom d'affichage, que la
        contrainte d'unicité du schéma réserve alors au détriment du
        média cible homonyme.
        """
        db, conn = world
        _source_media(conn, "Core Release", "core_release", is_official=0)

        _created, _failed, skipped = _stage1(db, set())

        assert [name for name, _reason in skipped] == ["core_release"]
        assert "already taken" in skipped[0][1]


class TestOnNeRésoutPasSurUnPoolVide:
    """Le dernier maillon : ne pas accuser le résolveur.

    Quand aucun média cible n'a pu être synchronisé, la résolution n'a
    rien à résoudre et rend zéro action.  Le message qui suivait
    envoyait l'utilisateur signaler une anomalie de résolveur pour une
    cause située deux étapes plus haut.
    """

    def test_la_garde_précède_la_résolution(self):
        import ast

        src = (pathlib.Path(__file__).resolve().parents[2]
               / "urpm/cli/commands/distupgrade.py").read_text()
        tree = ast.parse(src)

        def line_of(fragment):
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and fragment in node.value):
                    return node.lineno
            raise AssertionError(f"{fragment!r} introuvable")

        synced = line_of("synced {n_ok}/{n_total} media")
        guard = line_of("No target-release medium could be synchronised")
        resolving = line_of("Resolving the target-release package plan")

        assert synced < guard < resolving, (
            "la garde doit se placer entre le compte-rendu de "
            "synchronisation et la résolution")
