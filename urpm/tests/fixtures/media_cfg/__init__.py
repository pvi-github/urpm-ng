"""media.cfg fixtures used by the upsert_media_tree primitive tests.

This package holds canonical media.cfg test catalogues covering every
shape encountered in the wild.  They are loaded with
``load_media_cfg(name)`` defined in :mod:`urpm.tests.fixtures`.

Catalogues:
  official_mageia_9_x86_64.cfg
      Official Mageia 9 x86_64 mirror (core/nonfree/tainted ×
      release/updates plus core backports + backports_testing).
  custom_signed_mgabiz.cfg
      Single-media community repository (mgabiz-style).
  mlo_arch_empty.cfg
      Multi-media community repository with empty ``arch=`` in
      ``[media_info]`` — the real MLO 9 case.  Forces the catalogue
      to be the fallback for arch.
  multi_arch.cfg
      Cross-architecture catalogue (x86_64 + i586 + aarch64 cross
      references via ``../../<arch>/media/<...>`` section names).
  no_name_field.cfg
      Catalogue without ``name=`` on its sections — exercises the
      ``_make_display_name`` Title-Cased fallback.
  empty_catalog.cfg
      Only ``[media_info]`` section, no media — degenerate but
      valid.
  malformed.cfg
      Not a valid INI file — must raise on parse.
"""
