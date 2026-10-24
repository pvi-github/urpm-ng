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
    _collect_source_enabled_sections, _cross_arch_trees,
    _disable_source_media, _insert_target_media, _section_key,
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

#: Le catalogue 32 bits de la cible, tel que le miroir le sert.  Il est
#: déposé sous ``10/i586/`` et se déclare ``arch=i686`` : c'est l'alias
#: réel de Mageia 10, mesuré sur miroir, et c'est ce que l'ancien nom
#: d'arbre doit traverser pour retrouver ses médias.
MEDIA_CFG_32 = """[media_info]
version=10
arch=i686

[core/release]
name=Core Release
media_type=official:release
"""


@pytest.fixture
def world():
    """Une base neuve et les catalogues cibles sur disque."""
    root = pathlib.Path(tempfile.mkdtemp())
    cfg = root / "10/x86_64/media/media_info"
    cfg.mkdir(parents=True)
    (cfg / "media.cfg").write_text(MEDIA_CFG)
    for arch in ("i586", "i686"):
        cfg32 = root / f"10/{arch}/media/media_info"
        cfg32.mkdir(parents=True)
        (cfg32 / "media.cfg").write_text(MEDIA_CFG_32)

    db = PackageDatabase(str(root / "packages.db"))
    conn = db._get_connection()
    conn.execute(
        "INSERT INTO server (name, protocol, host, base_path, is_official,"
        " enabled, priority, added_timestamp)"
        " VALUES ('local','file','',?,1,1,50,0)", (str(root),))
    conn.commit()
    return db, conn


def _source_media(conn, name, short_name, section="core/release",
                  is_official=1, enabled=1, version="9", arch="x86_64"):
    conn.execute(
        "INSERT INTO media (name, short_name, is_official, mageia_version,"
        " architecture, relative_path, enabled, added_timestamp)"
        " VALUES (?,?,?,?,?,?,?,0)",
        (name, short_name, is_official, version, arch,
         f"{version}/{arch}/media/{section}", enabled))
    conn.commit()


def _stage1_with_32bit(db, sections=None):
    """La même séquence, en suivant aussi l'arbre étranger.

    L'ordre de la vraie fonction : les deux collectes lisent
    ``enabled``, donc elles passent avant la désactivation.
    """
    if sections is None:
        sections = _collect_source_enabled_sections(db, "9")
    cross = _cross_arch_trees(db, "9", "x86_64")
    _disable_source_media(db, "9")
    return _insert_target_media(
        db, ReleaseIdentity(identity="10", numeric="10"), "x86_64",
        cross_arches=tuple(cross),
        source_identity="9", source_enabled_sections=sections)


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

    def test_les_deux_noms_de_l_arbre_32_bits_sont_la_même_place(self):
        """Le défaut remonté du terrain, réduit à une ligne.

        La 9 publie ``i586``, la 10 publie ``i686`` : comparées
        littéralement, les deux ne se rencontrent jamais et les médias
        32 bits de l'utilisateur ressortent éteints de la mise à
        niveau.
        """
        assert (_section_key("9/i586/media/core/release", "i586")
                == _section_key("10/i686/media/core/release", "i686"))


class TestQuelsArbresÉtrangersSuivre:
    """Le catalogue x86_64 ne mentionne aucun média 32 bits.

    Mesuré : soixante et une sections, aucune en cross-arch.  Les
    médias 32 bits viennent d'un second catalogue, que Stage 1 n'allait
    pas chercher.
    """

    def test_un_média_32_bits_actif_donne_son_arbre(self, world):
        db, conn = world
        _source_media(conn, "Core Release", "core_release")
        _source_media(conn, "Core 32bit Release", "core_release_i586",
                      arch="i586")

        assert _cross_arch_trees(db, "9", "x86_64") == ["i586"]

    def test_l_architecture_de_la_machine_n_est_pas_étrangère(self, world):
        db, conn = world
        _source_media(conn, "Core Release", "core_release")

        assert _cross_arch_trees(db, "9", "x86_64") == []

    def test_ce_qui_était_éteint_n_est_pas_suivi(self, world):
        # On ne va chercher que ce que l'utilisateur avait activé.
        db, conn = world
        _source_media(conn, "Core 32bit Release", "core_release_i586",
                      arch="i586", enabled=0)

        assert _cross_arch_trees(db, "9", "x86_64") == []

    def test_noarch_n_est_pas_un_arbre(self, world):
        # ``noarch`` est un contenu, pas un arbre de miroir.
        db, conn = world
        _source_media(conn, "Noarch", "noarch_release", arch="noarch")

        assert _cross_arch_trees(db, "9", "x86_64") == []

    def test_une_machine_32_bits_ne_se_suit_pas_elle_même(self, world):
        # i586 et i686 sont la même place : sur une machine 32 bits,
        # l'arbre natif ne doit pas être redemandé comme étranger.
        db, conn = world
        _source_media(conn, "Core Release", "core_release", arch="i586")

        assert _cross_arch_trees(db, "9", "i686") == []


class TestL_ArbreÉtrangerEstSuivi:
    """Bout en bout, avec le renommage réel de l'arbre 32 bits."""

    def test_les_médias_32_bits_cibles_sont_créés_et_actifs(self, world):
        db, conn = world
        _source_media(conn, "Core Release", "core_release")
        _source_media(conn, "Core 32bit Release", "core_release_i586",
                      arch="i586")

        _created, _failed, skipped, cross = _stage1_with_32bit(db)

        assert skipped == []
        assert cross == []
        # Le média 32 bits cible existe, sous le nom d'arbre de la
        # cible, et il hérite du choix de l'utilisateur.  La paire
        # (short_name, architecture) est ce qui identifie un média :
        # un même nom court vit sur deux architectures.
        rows = {(r["short_name"], r["architecture"]): r for r in conn.execute(
            "SELECT short_name, architecture, relative_path, enabled, name"
            " FROM media WHERE mageia_version='10'")}
        medium = rows[("core_release", "i686")]
        assert medium["relative_path"] == "10/i686/media/core/release"
        assert medium["enabled"] == 1
        # Et son nom d'affichage porte l'architecture, sans quoi deux
        # « Core Release » se présenteraient à l'identique.
        assert "i686" in medium["name"]

    def test_le_serveur_reste_celui_de_l_arbre_natif(self, world):
        """L'alias ne doit pas créer un second serveur dégénéré.

        Entrer par ``10/i586/`` alors que le catalogue se déclare
        ``i686`` empêcherait le découpage ``<version>/<arch>/media``,
        et le média atterrirait sur un serveur au chemin de base vide.
        """
        db, conn = world
        _source_media(conn, "Core Release", "core_release")
        _source_media(conn, "Core 32bit Release", "core_release_i586",
                      arch="i586")

        _stage1_with_32bit(db)

        servers = [dict(r) for r in conn.execute(
            "SELECT id, base_path FROM server")]
        assert len(servers) == 1
        assert servers[0]["base_path"]

    def test_un_seul_miroir_en_retard_ne_fait_pas_un_arbre_perdu(self,
                                                                 world):
        """Il suffit d'un serveur qui porte l'arbre, comme pour le natif.

        Signaler chaque miroir qui ne l'a pas donnerait six lignes pour
        un arbre pourtant retrouvé, et l'opérateur apprendrait à ne
        plus lire ces lignes.
        """
        db, conn = world
        _source_media(conn, "Core Release", "core_release")
        _source_media(conn, "Core 32bit Release", "core_release_i586",
                      arch="i586")
        # Un second miroir qui ne sert que l'arbre natif.
        bare = pathlib.Path(tempfile.mkdtemp())
        cfg = bare / "10/x86_64/media/media_info"
        cfg.mkdir(parents=True)
        (cfg / "media.cfg").write_text(MEDIA_CFG)
        conn.execute(
            "INSERT INTO server (name, protocol, host, base_path,"
            " is_official, enabled, priority, added_timestamp)"
            " VALUES ('autre','file','',?,1,1,50,0)", (str(bare),))
        conn.commit()

        created, failed, _skipped, cross = _stage1_with_32bit(db)

        assert len(created) == 2
        assert failed == []
        assert cross == []

    def test_un_arbre_absent_est_nommé_et_non_fatal(self, world):
        """Un miroir sans l'arbre étranger ne doit pas arrêter l'étape.

        Sans le catalogue natif il n'y a rien vers quoi monter, et
        l'étape s'arrête ; sans l'arbre étranger l'utilisateur perd des
        médias, ce qui se dit.
        """
        db, conn = world
        _source_media(conn, "Core Release", "core_release")
        _source_media(conn, "Core ARM Release", "core_release_armv7hl",
                      arch="armv7hl")

        created, failed, skipped, cross = _stage1_with_32bit(db)

        assert created  # l'arbre natif, lui, est passé
        assert failed == []
        assert skipped == []
        assert [what for what, _reason in cross] == ["armv7hl tree"]


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

        _created, _failed, skipped, _cross = _stage1(db, set())

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
