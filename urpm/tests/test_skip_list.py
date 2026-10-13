"""Le fichier d'urpmi, lu et écrit tel qu'il est.

Le format n'est pas inventé : il vient de ``urpm::sys::get_packages_list``
pour l'analyse, et de ``urpmi.files`` pour la forme des entrées, un nom
ou une expression entre barres obliques comparée au nom complet
``nom-version-release.arch``.
"""

from __future__ import annotations

import pathlib

import pytest

from urpm.core import skip_list as sl


@pytest.fixture
def liste(tmp_path):
    return tmp_path / "skip.list"


class TestLAnalyseSuitCelleDUrpmi:

    @pytest.mark.parametrize("ligne,attendu", [
        ("thunderbird", "thunderbird"),
        ("  thunderbird  ", "thunderbird"),
        ("thunderbird # on y tient", "thunderbird"),
        ("# tout commenté", ""),
        ("", ""),
        ("   ", ""),
        ("/^apache/", "/^apache/"),
    ])
    def test_une_ligne_donne_son_entrée(self, ligne, attendu):
        assert sl.parse_line(ligne) == attendu

    def test_le_fichier_par_défaut_de_mageia_ne_contient_aucune_entrée(
            self, liste):
        # Celui que livre urpmi : trois lignes, toutes commentées.
        liste.write_text(
            "# Here you can specify the packages that won't be upgraded\n"
            "# for example, to exclude all apache packages :\n"
            "# /^apache/\n")

        assert sl.read_entries(liste) == []

    def test_un_fichier_absent_ne_masque_rien(self, tmp_path):
        assert sl.read_entries(tmp_path / "pas-la") == []


class TestLeMasquage:

    def test_un_nom_exact(self):
        skips = sl.SkipList(["thunderbird"])

        assert skips.masks("thunderbird", "140.0-1.mga10", "x86_64")
        assert not skips.masks("thunderbird-l10n-fr", "140.0-1.mga10",
                               "x86_64")

    def test_une_expression_porte_sur_le_nom_complet(self):
        # Ce que dit urpmi : « matched against the full name of the
        # package, which has the form name-version-release.arch ».
        skips = sl.SkipList(["/^libreoffice/"])

        assert skips.masks("libreoffice-core", "26.2.4.2-1.mga10", "x86_64")
        assert not skips.masks("autocorr-fr", "26.2.4.2-1.mga10", "x86_64")

    def test_une_expression_peut_viser_une_version(self):
        skips = sl.SkipList([r"/^firefox-153\./"])

        assert skips.masks("firefox", "153.3.0-1.mga10", "x86_64")
        assert not skips.masks("firefox", "154.0-1.mga10", "x86_64")

    def test_l_époque_ne_fait_pas_partie_du_nom_complet(self):
        # ``fullname`` chez urpmi n'a pas d'époque ; on lui passe donc
        # l'EVR sans, et une expression qui en contiendrait une ne
        # correspondrait pas, comme chez eux.
        skips = sl.SkipList(["/1:25.12/"])

        assert not skips.masks("dolphin", "25.12.1-1.1.mga10", "x86_64")

    def test_une_expression_invalide_est_signalée_et_ignorée(self):
        skips = sl.SkipList(["/[/", "thunderbird"])

        assert skips.invalid and skips.invalid[0][0] == "/[/"
        assert skips.masks("thunderbird")          # le reste fonctionne

    def test_une_liste_vide_ne_masque_rien(self):
        assert not sl.SkipList([])
        assert not sl.SkipList([]).masks("thunderbird")


class TestLÉcriture:

    def test_ajouter_crée_le_fichier_avec_l_entête_d_urpmi(self, liste):
        assert sl.add_entry("thunderbird", path=liste)

        contenu = liste.read_text()
        assert contenu.startswith("# Here you can specify")
        assert sl.read_entries(liste) == ["thunderbird"]

    def test_ajouter_deux_fois_ne_duplique_pas(self, liste):
        sl.add_entry("thunderbird", path=liste)

        assert sl.add_entry("thunderbird", path=liste) is False
        assert sl.read_entries(liste) == ["thunderbird"]

    def test_les_commentaires_et_l_inconnu_restent_au_caractère_près(
            self, liste):
        liste.write_text("# ma politique à moi\n"
                         "/^apache/\n"
                         "\n"
                         "# et une note\n"
                         "firefox\n")

        sl.add_entry("thunderbird", path=liste)
        sl.remove_entry("firefox", path=liste)

        assert liste.read_text() == ("# ma politique à moi\n"
                                     "/^apache/\n"
                                     "\n"
                                     "# et une note\n"
                                     "thunderbird\n")

    def test_retirer_ce_qui_n_y_est_pas_le_dit(self, liste):
        liste.write_text("firefox\n")

        assert sl.remove_entry("thunderbird", path=liste) is False

    def test_le_mode_du_fichier_est_conservé(self, liste):
        liste.write_text("firefox\n")
        liste.chmod(0o640)

        sl.add_entry("thunderbird", path=liste)

        assert liste.stat().st_mode & 0o777 == 0o640

    def test_un_nom_couvert_par_une_expression_est_nommable(self, liste):
        liste.write_text("/^libre/\n")

        assert sl.covering_patterns("libreoffice-core", path=liste) == [
            "/^libre/"]
        assert sl.covering_patterns("firefox", path=liste) == []


class TestLaRacine:

    def test_le_chemin_suit_la_racine(self, tmp_path):
        assert sl.skip_list_path(root=str(tmp_path)) == (
            tmp_path / "etc/urpmi/skip.list")

    def test_sans_racine_c_est_celui_d_urpmi(self):
        assert sl.skip_list_path() == pathlib.Path("/etc/urpmi/skip.list")
        assert sl.skip_list_path(root="/") == pathlib.Path(
            "/etc/urpmi/skip.list")
