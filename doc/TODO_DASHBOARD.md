# Tableau de bord — index des TODO urpm-ng

Ce document est un **index synthétique** pointant vers les TODO détaillés
du projet.  Pour le contenu opérationnel (items, prios, efforts, statuts),
suivre les liens vers les documents spécialisés.

> **Convention** : ce fichier ne porte pas la dette technique en propre.
> Toute action concrète doit vivre dans un `TODO_*.md` thématique ou dans
> le `TODO.md` racine.  Ce dashboard sert uniquement à se repérer.

---

## Backlog général

- [`/TODO.md`](../TODO.md) — backlog vivant racine, organisé par phases
  (Phase 1 priorité haute, Phase 2 prochaines features, Phase 3 GUI,
  Phase 4 advisories).  Source canonique pour les items non-spécialisés.

## Chantiers en cours et features livrées

- [`TODO_GENMEDIA.md`](TODO_GENMEDIA.md) — intégration `urpm genmedia`
  (réécriture genhdlist3 + AppStream).  Statut des bugs `upanier.py`,
  filtrage des composants AppStream, plan d'implémentation phasé.
- [`PLAN_GENMEDIA.md`](PLAN_GENMEDIA.md) — plan initial mars 2026
  (référence historique, voir TODO_GENMEDIA.md pour le courant).

## Dette technique identifiée

- [`TODO_SUPPLEMENTS.md`](TODO_SUPPLEMENTS.md) — audit weak deps
  (Recommends/Suggests/Supplements/Enhances).  Items #3, #5, #6 ouverts.
- [`TODO_DEBUG_LIBSOLV.md`](TODO_DEBUG_LIBSOLV.md) — bugs résiduels
  libsolv (`add_mdk`, `@recommends@` offset, `@supplements@`/`@enhances@`
  non parsés côté Python).
- [`TODO_LEX_SORT_AUDIT.md`](TODO_LEX_SORT_AUDIT.md) — items résiduels
  après le fix libreoffice mga9 (3 sites à inspecter).
- [`TODO_XFAILS.md`](TODO_XFAILS.md) — statut des `xfail`/`skip` dans la
  suite de tests, familles A-F.

## Infra et performance

- [`TODO_SHRINK_FILES_DB.md`](TODO_SHRINK_FILES_DB.md) — plan pour
  ramener `files.xml.lzma` parsé de 3,8 Go à ~150 Mo via scan streaming.
- [`TODO_MANAGE_BUILDDEPS.md`](TODO_MANAGE_BUILDDEPS.md) — évolution
  multi-source du tracking des BuildRequires.

## Tests et CI

- [`TESTING.md`](TESTING.md) — état de la couverture pytest, sites
  manuels, plan d'infra.
- GitHub Actions CI — à faire (entrée dans `/TODO.md`).

## Documents archivés

- [`archives/DONE_MU_LOCK.md`](archives/DONE_MU_LOCK.md) — Media Update
  Lock (livré).

---

## Comment contribuer à ce dashboard

- **Ne pas dupliquer** d'items déjà tracés dans un `TODO_*.md` thématique.
- **Ajouter ici** uniquement les nouveaux thèmes (créer un `TODO_*.md`
  dédié si le volume le justifie) et leur référence en index.
- Quand une feature est livrée, déplacer son `TODO_*.md` vers
  `archives/DONE_*.md` et mettre à jour le lien ici.
