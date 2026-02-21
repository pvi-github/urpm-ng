"""Tests for rpmsrate parser"""

import pytest
from pathlib import Path
from urpm.core.rpmsrate import RpmsrateParser


# Sample rpmsrate content for testing
SAMPLE_RPMSRATE = """
# Test rpmsrate file

CAT_DESKTOP
  5 task-desktop
  5 CAT_PLASMA5 plasma-desktop
  5 CAT_GNOME gnome-shell
  4 xorg-server
  3 lowpriority-pkg

CAT_PLASMA5
  5 task-plasma5
  5 dolphin konsole
  5 !CAT_GNOME kwin
  4 DRIVER"nvidia" nvidia-driver

CAT_GNOME
  5 task-gnome
  5 nautilus gnome-terminal
  5 TYPE"laptop" gnome-power-manager

CAT_SYSTEM
  5 kernel-desktop-latest
  5 DRIVER"iwlwifi" iwlwifi-firmware
  5 HW"Intel" intel-microcode
  5 HW_CAT"network/wifi" wireless-tools

INSTALL
  NOCOPY
    5 kernel-desktop-latest kernel-server-latest
    5 dhcp-client chrony
    4 grub2
    5 task-x11
"""


class TestRpmsrateParser:
    """Tests for RpmsrateParser."""

    @pytest.fixture
    def parser(self):
        """Create a parser with sample content."""
        p = RpmsrateParser()
        p.parse_content(SAMPLE_RPMSRATE)
        return p

    def test_parse_sections(self, parser):
        """Test that sections are parsed correctly."""
        assert 'CAT_DESKTOP' in parser.sections
        assert 'CAT_PLASMA5' in parser.sections
        assert 'CAT_GNOME' in parser.sections
        assert 'CAT_SYSTEM' in parser.sections
        assert 'INSTALL' in parser.sections

    def test_section_entry_count(self, parser):
        """Test entry counts per section."""
        assert len(parser.sections['CAT_DESKTOP'].entries) == 5
        assert len(parser.sections['CAT_PLASMA5'].entries) == 5  # task-plasma5, dolphin, konsole, kwin, nvidia-driver
        assert len(parser.sections['INSTALL'].entries) == 6  # NOCOPY filtered, 6 packages remain

    def test_nocopy_filtered(self, parser):
        """Test that NOCOPY subsection marker is filtered."""
        install_pkgs = parser.get_packages(['INSTALL'])
        assert 'NOCOPY' not in install_pkgs

    def test_priority_filtering(self, parser):
        """Test priority-based filtering."""
        # Default min_priority=4
        pkgs = parser.get_packages(['CAT_DESKTOP'])
        assert 'task-desktop' in pkgs
        assert 'xorg-server' in pkgs
        assert 'lowpriority-pkg' not in pkgs  # priority 3

    def test_category_condition(self, parser):
        """Test CAT_xxx condition evaluation."""
        # With CAT_PLASMA5 active
        pkgs = parser.get_packages(
            ['CAT_DESKTOP'],
            active_categories=['CAT_PLASMA5']
        )
        assert 'plasma-desktop' in pkgs
        assert 'gnome-shell' not in pkgs  # CAT_GNOME not active

    def test_negated_condition(self, parser):
        """Test !CAT_xxx negation."""
        # kwin has !CAT_GNOME condition
        pkgs = parser.get_packages(
            ['CAT_PLASMA5'],
            active_categories=['CAT_PLASMA5']
        )
        assert 'kwin' in pkgs  # CAT_GNOME not active, so !CAT_GNOME is true

        # With CAT_GNOME active, kwin should be excluded
        pkgs = parser.get_packages(
            ['CAT_PLASMA5'],
            active_categories=['CAT_PLASMA5', 'CAT_GNOME']
        )
        assert 'kwin' not in pkgs  # !CAT_GNOME is false

    def test_ignore_hardware_conditions(self, parser):
        """Test ignoring DRIVER/HW/HW_CAT conditions."""
        pkgs = parser.get_packages(
            ['CAT_SYSTEM'],
            ignore_conditions=['DRIVER', 'HW', 'HW_CAT']
        )
        # These should be included when hardware conditions are ignored
        assert 'kernel-desktop-latest' in pkgs
        assert 'iwlwifi-firmware' in pkgs
        assert 'intel-microcode' in pkgs
        assert 'wireless-tools' in pkgs

    def test_hardware_conditions_not_ignored(self, parser):
        """Test that hardware conditions are evaluated when not ignored."""
        pkgs = parser.get_packages(
            ['CAT_SYSTEM'],
            ignore_conditions=[]  # Don't ignore anything
        )
        # Without ignoring, these should still be included
        # because hardware conditions default to "maybe true"
        assert 'kernel-desktop-latest' in pkgs

    def test_multiple_sections(self, parser):
        """Test combining multiple sections."""
        pkgs = parser.get_packages(
            ['INSTALL', 'CAT_PLASMA5'],
            active_categories=['CAT_PLASMA5'],
            ignore_conditions=['DRIVER']
        )
        assert 'kernel-desktop-latest' in pkgs  # from INSTALL
        assert 'task-plasma5' in pkgs  # from CAT_PLASMA5
        assert 'dolphin' in pkgs
        assert 'konsole' in pkgs

    def test_list_sections(self, parser):
        """Test listing all sections."""
        sections = parser.list_sections()
        assert 'CAT_DESKTOP' in sections
        assert 'INSTALL' in sections

    def test_get_section_stats(self, parser):
        """Test section statistics."""
        stats = parser.get_section_stats()
        assert stats['CAT_DESKTOP'] == 5
        assert stats['INSTALL'] == 6


class TestRpmsrateWithRealFile:
    """Tests with the real rpmsrate-raw file if available."""

    @pytest.fixture
    def real_parser(self):
        """Try to load real rpmsrate-raw file."""
        # Try local test file first
        test_path = Path(__file__).parent.parent.parent.parent / 'essais' / 'rpmsrate-raw'
        if test_path.exists():
            p = RpmsrateParser(test_path)
            p.parse()
            return p
        pytest.skip("Real rpmsrate-raw file not found")

    def test_real_file_has_install_section(self, real_parser):
        """Test that real file has INSTALL section."""
        assert 'INSTALL' in real_parser.sections
        assert len(real_parser.sections['INSTALL'].entries) > 50

    def test_real_file_install_packages(self, real_parser):
        """Test extracting packages from real INSTALL section."""
        pkgs = real_parser.get_packages(['INSTALL'])
        assert len(pkgs) > 50
        # Some expected packages
        assert 'dhcp-client' in pkgs or 'chrony' in pkgs

    def test_real_file_desktop_packages(self, real_parser):
        """Test extracting desktop packages."""
        pkgs = real_parser.get_packages(
            ['INSTALL', 'CAT_PLASMA5', 'CAT_GNOME', 'CAT_XFCE'],
            active_categories=['CAT_PLASMA5', 'CAT_GNOME', 'CAT_XFCE'],
            ignore_conditions=['DRIVER', 'HW', 'HW_CAT']
        )
        assert len(pkgs) > 80
