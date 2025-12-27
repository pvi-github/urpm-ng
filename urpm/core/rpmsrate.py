"""
Parser for Mageia rpmsrate-raw file.

The rpmsrate-raw file defines package priorities and categories for installation.
It's used to determine which packages should be included in installation media
and for seed-based replication.

Format:
    SECTION_NAME
      [priority] [conditions...] package1 package2...

Priority: 1-5 (5=high, <4 never installed by default)
Conditions:
    CAT_xxx          - Category (desktop environment, etc.)
    LOCALES"xx"      - Language
    DRIVER"xxx"      - Hardware driver (regex)
    HW"xxx"          - Hardware description
    HW_CAT"xxx"      - Hardware category
    TYPE"xxx"        - Machine type (laptop, 64bit...)
    !condition       - Negation
    cond1 || cond2   - OR logic
"""

import re
from pathlib import Path
from typing import Dict, List, Set, Optional
from dataclasses import dataclass, field


# Default location on Mageia systems
DEFAULT_RPMSRATE_PATH = Path("/usr/share/meta-task/rpmsrate-raw")


@dataclass
class PackageEntry:
    """A package entry with its priority and conditions."""
    name: str
    priority: int = 5
    conditions: List[str] = field(default_factory=list)
    is_locale_pattern: bool = False  # If True, 'name' is a pattern prefix like 'libreoffice-langpack-'


@dataclass
class Section:
    """A section in rpmsrate-raw."""
    name: str
    entries: List[PackageEntry] = field(default_factory=list)


class RpmsrateParser:
    """Parse the rpmsrate-raw file from Mageia."""

    # Regex patterns
    SECTION_PATTERN = re.compile(r'^([A-Z][A-Z0-9_]*)$')
    PRIORITY_PATTERN = re.compile(r'^([1-5])\s+')
    CONDITION_PATTERNS = [
        re.compile(r'(CAT_[A-Z0-9_]+)'),
        re.compile(r'(LOCALES"[^"]+")'),
        re.compile(r'(DRIVER"[^"]+")'),
        re.compile(r'(HW"[^"]+")'),
        re.compile(r'(HW_CAT"[^"]+")'),
        re.compile(r'(TYPE"[^"]+")'),
        re.compile(r'(LIVE)(?:\s|$)'),
        re.compile(r'(SMP)(?:\s|$)'),
        re.compile(r'(USB)(?:\s|$)'),
        re.compile(r'(PCMCIA)(?:\s|$)'),
        re.compile(r'(DVB)(?:\s|$)'),
        re.compile(r'(RADIO)(?:\s|$)'),
    ]

    def __init__(self, path: Path = None):
        """Initialize parser.

        Args:
            path: Path to rpmsrate-raw file. Defaults to /usr/share/meta-task/rpmsrate-raw
        """
        self.path = path or DEFAULT_RPMSRATE_PATH
        self.sections: Dict[str, Section] = {}

    def parse(self) -> Dict[str, Section]:
        """Parse the rpmsrate-raw file.

        Returns:
            Dict mapping section name to Section object
        """
        if not self.path.exists():
            raise FileNotFoundError(f"rpmsrate-raw not found: {self.path}")

        content = self.path.read_text(encoding='utf-8', errors='replace')
        return self.parse_content(content)

    def parse_content(self, content: str) -> Dict[str, Section]:
        """Parse rpmsrate content from string.

        Args:
            content: File content as string

        Returns:
            Dict mapping section name to Section object
        """
        self.sections = {}
        current_section: Optional[Section] = None
        current_priority = 5

        for line in content.split('\n'):
            # Remove comments
            if '#' in line:
                line = line[:line.index('#')]

            # Skip empty lines
            stripped = line.strip()
            if not stripped:
                continue

            # Check for section header (not indented, all caps)
            if not line[0].isspace() and self.SECTION_PATTERN.match(stripped):
                section_name = stripped
                current_section = Section(name=section_name)
                self.sections[section_name] = current_section
                current_priority = 5  # Reset priority for new section
                continue

            # Must be in a section to parse entries
            if current_section is None:
                continue

            # Parse indented line (package entries)
            entries = self._parse_entry_line(stripped, current_priority)
            if entries:
                # Update current priority from first entry if specified
                if entries[0].priority != current_priority:
                    current_priority = entries[0].priority
                current_section.entries.extend(entries)

        return self.sections

    def _parse_entry_line(self, line: str, default_priority: int) -> List[PackageEntry]:
        """Parse a single entry line.

        Args:
            line: Line content (stripped)
            default_priority: Priority to use if not specified

        Returns:
            List of PackageEntry objects
        """
        entries = []
        priority = default_priority
        conditions = []

        # Check for priority at start
        priority_match = self.PRIORITY_PATTERN.match(line)
        if priority_match:
            priority = int(priority_match.group(1))
            line = line[priority_match.end():]

        # Extract conditions and packages
        remaining = line
        pos = 0

        while pos < len(remaining):
            # Skip whitespace
            while pos < len(remaining) and remaining[pos].isspace():
                pos += 1
            if pos >= len(remaining):
                break

            # Check for negation
            negated = False
            if remaining[pos] == '!':
                negated = True
                pos += 1

            # Check for OR operator
            if remaining[pos:pos+2] == '||':
                pos += 2
                continue

            # Try to match a condition
            matched_condition = False
            for pattern in self.CONDITION_PATTERNS:
                match = pattern.match(remaining[pos:])
                if match:
                    cond = match.group(1)
                    if negated:
                        cond = '!' + cond
                    conditions.append(cond)
                    pos += match.end()
                    matched_condition = True
                    break

            if matched_condition:
                continue

            # Check for quoted condition we might have missed
            if remaining[pos:pos+2] in ('HW', 'DR', 'LO', 'TY'):
                # Find the closing quote
                quote_start = remaining.find('"', pos)
                if quote_start != -1:
                    quote_end = remaining.find('"', quote_start + 1)
                    if quote_end != -1:
                        cond = remaining[pos:quote_end + 1]
                        if negated:
                            cond = '!' + cond
                        conditions.append(cond)
                        pos = quote_end + 1
                        continue

            # Must be a package name - find end of token
            token_start = pos
            while pos < len(remaining) and not remaining[pos].isspace():
                pos += 1

            if pos > token_start:
                pkg_name = remaining[token_start:pos]
                # Skip if it looks like a condition we failed to parse
                if any(pkg_name.startswith(p) for p in ['CAT_', 'DRIVER', 'HW', 'LOCALES', 'TYPE']):
                    continue
                # Skip regex patterns from DRIVER conditions (contain | or start with |)
                if '|' in pkg_name or pkg_name.startswith('|'):
                    continue
                # Skip subsection markers (all caps, no dash, no digits)
                # e.g., NOCOPY is a subsection, not a package
                if pkg_name.isupper() and '-' not in pkg_name and not any(c.isdigit() for c in pkg_name):
                    continue
                entries.append(PackageEntry(
                    name=pkg_name,
                    priority=priority,
                    conditions=list(conditions)  # Copy current conditions
                ))

        # Detect locale patterns: if 2+ packages differ only by locale suffix,
        # add a pattern entry for expansion
        # e.g., "libreoffice-langpack-ar libreoffice-langpack-ca" -> pattern "libreoffice-langpack-"
        if len(entries) >= 2:
            patterns_found = self._detect_locale_patterns(entries, priority, conditions)
            entries.extend(patterns_found)

        return entries

    def _detect_locale_patterns(self, entries: List[PackageEntry], priority: int,
                                 conditions: List[str]) -> List[PackageEntry]:
        """Detect locale patterns from a list of package entries.

        If two packages on the same line differ only by a locale suffix (2-3 letter code),
        generate a pattern entry for expansion.

        Examples:
            libreoffice-langpack-ar, libreoffice-langpack-ca -> libreoffice-langpack-
            hunspell-bg, hunspell-ca -> hunspell-
            firefox-de, firefox-fr -> firefox-
        """
        import re
        pattern_entries = []
        seen_prefixes = set()

        # Locale suffix pattern: ends with -XX or -XX_YY (2-3 letter codes)
        locale_suffix = re.compile(r'^(.+-)([a-z]{2,3}(?:_[A-Z]{2})?)$')

        prefixes = {}  # prefix -> list of suffixes
        for entry in entries:
            match = locale_suffix.match(entry.name)
            if match:
                prefix, suffix = match.groups()
                if prefix not in prefixes:
                    prefixes[prefix] = []
                prefixes[prefix].append(suffix)

        # If a prefix has 2+ different suffixes, it's a locale pattern
        for prefix, suffixes in prefixes.items():
            if len(suffixes) >= 2 and prefix not in seen_prefixes:
                seen_prefixes.add(prefix)
                pattern_entries.append(PackageEntry(
                    name=prefix,
                    priority=priority,
                    conditions=list(conditions),
                    is_locale_pattern=True
                ))

        return pattern_entries

    def get_packages(self,
                     sections: List[str],
                     active_categories: List[str] = None,
                     ignore_conditions: List[str] = None,
                     min_priority: int = 4) -> Set[str]:
        """Get packages from specified sections.

        Args:
            sections: List of section names to include (e.g., ["INSTALL", "CAT_PLASMA5"])
            active_categories: Categories that are "active" (e.g., ["CAT_PLASMA5", "CAT_GNOME"])
                              Used to evaluate CAT_xxx conditions.
            ignore_conditions: Condition prefixes to ignore (e.g., ["DRIVER", "HW", "HW_CAT"])
                              These conditions are treated as "maybe true".
            min_priority: Minimum priority to include (default 4)

        Returns:
            Set of package names (does NOT include locale patterns - use get_packages_and_patterns)
        """
        packages, _ = self.get_packages_and_patterns(
            sections, active_categories, ignore_conditions, min_priority
        )
        return packages

    def get_packages_and_patterns(self,
                                   sections: List[str],
                                   active_categories: List[str] = None,
                                   ignore_conditions: List[str] = None,
                                   min_priority: int = 4) -> tuple:
        """Get packages and locale patterns from specified sections.

        Args:
            sections: List of section names to include (e.g., ["INSTALL", "CAT_PLASMA5"])
            active_categories: Categories that are "active" (e.g., ["CAT_PLASMA5", "CAT_GNOME"])
                              Used to evaluate CAT_xxx conditions.
            ignore_conditions: Condition prefixes to ignore (e.g., ["DRIVER", "HW", "HW_CAT"])
                              These conditions are treated as "maybe true".
            min_priority: Minimum priority to include (default 4)

        Returns:
            Tuple of (packages: Set[str], locale_patterns: Set[str])
            locale_patterns are prefixes like 'libreoffice-langpack-' to be expanded
        """
        if not self.sections:
            self.parse()

        if active_categories is None:
            # Extract CAT_xxx from sections list
            active_categories = [s for s in sections if s.startswith('CAT_')]

        if ignore_conditions is None:
            ignore_conditions = []

        packages = set()
        locale_patterns = set()

        for section_name in sections:
            section = self.sections.get(section_name)
            if not section:
                continue

            for entry in section.entries:
                if entry.priority < min_priority:
                    continue

                # Evaluate conditions
                if self._evaluate_conditions(entry.conditions, active_categories, ignore_conditions):
                    if entry.is_locale_pattern:
                        locale_patterns.add(entry.name)
                    else:
                        packages.add(entry.name)

        return packages, locale_patterns

    def _evaluate_conditions(self,
                            conditions: List[str],
                            active_categories: List[str],
                            ignore_conditions: List[str]) -> bool:
        """Evaluate if conditions are satisfied.

        For replication, we're permissive: hardware conditions are ignored (treated as true),
        and category conditions are checked against active_categories.

        Args:
            conditions: List of conditions from the entry
            active_categories: Active CAT_xxx categories
            ignore_conditions: Condition prefixes to ignore

        Returns:
            True if entry should be included
        """
        if not conditions:
            return True

        for cond in conditions:
            negated = cond.startswith('!')
            if negated:
                cond = cond[1:]

            # Check if this condition should be ignored
            should_ignore = any(cond.startswith(prefix) for prefix in ignore_conditions)
            if should_ignore:
                # Treat ignored conditions as "maybe true" - include the package
                continue

            # Evaluate category conditions
            if cond.startswith('CAT_'):
                is_active = cond in active_categories
                if negated:
                    is_active = not is_active
                if not is_active:
                    return False

            # For TYPE conditions, be permissive for replication
            # TYPE"64bit" and TYPE"laptop" etc. - we want both
            elif cond.startswith('TYPE"'):
                continue  # Ignore TYPE conditions

            # LOCALES - skip for now (TODO: make configurable)
            elif cond.startswith('LOCALES"'):
                continue

            # LIVE condition - skip (we're not building live media)
            elif cond == 'LIVE':
                if not negated:
                    return False  # Only include !LIVE entries

        return True

    def list_sections(self) -> List[str]:
        """List all section names.

        Returns:
            List of section names
        """
        if not self.sections:
            self.parse()
        return list(self.sections.keys())

    def get_section_stats(self) -> Dict[str, int]:
        """Get package count per section.

        Returns:
            Dict mapping section name to entry count
        """
        if not self.sections:
            self.parse()
        return {name: len(section.entries) for name, section in self.sections.items()}
