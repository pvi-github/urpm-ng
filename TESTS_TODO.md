# urpm-ng Tests TODO

## Tests manuels à faire

- [ ] Détection des orphelins lors de l'upgrade
  - Attendre qu'il y ait des mises à jour disponibles
  - Tester `urpm upgrade --test` pour voir si des orphelins sont détectés
  - Vérifier que le diff requires fonctionne correctement
  - Tester `--noerase-orphans` pour garder les orphelins

- [ ] Vérification des signatures GPG
  - Tester installation d'un paquet signé (doit fonctionner)
  - Tester installation avec clé manquante (doit échouer)
  - Tester `--nosignature` pour bypass
  - Tester `urpm key import <url>` avec URL HTTPS

- [ ] Import de clé GPG lors de media add
  - Tester avec clé déjà présente (doit afficher "already in keyring")
  - Tester avec clé absente du trousseau (doit afficher les infos et demander confirmation)
  - Tester `--auto` pour import automatique sans confirmation
  - Tester `--nokey` pour ignorer la vérification de clé
  - Tester avec un média sans pubkey (doit afficher "No pubkey found")

- [ ] Commande mark
  - Tester `urpm mark show <pkg>` - affiche manual/auto
  - Tester `urpm mark manual <pkg>` - protège de l'autoremove
  - Tester `urpm mark auto <pkg>` - rend autoremovable
  - Vérifier que le fichier installed-through-deps.list est mis à jour
  - Tester avec paquet non installé (doit afficher erreur)

- [ ] Alternatives (OR deps)
  - Tester install avec alternatives (ex: task-plasma qui tire task-sound)
  - Tester mode --auto prend le premier choix automatiquement
  - Tester re-résolution après choix utilisateur


## Tests automatisés (P1)

Infrastructure de tests avec pytest et paquets RPM de test.

### Paquets de test à créer

Créer un dépôt de paquets RPM factices couvrant tous les cas :

**Cas simples :**
  - Paquet sans dépendances
  - Paquet avec dépendances simples (A → B → C)
  - Paquet avec conflit
  - Paquet avec obsoletes

**Dépendances faibles :**
  - Paquet avec Recommends
  - Paquet avec Suggests
  - Paquet avec Supplements
  - Paquet avec Enhances

**Alternatives (OR deps) :**
  - Dépendance satisfaite par plusieurs paquets (A requires X, X provided by B ou C)
  - Chaîne d'alternatives (task-sound → task-pulseaudio | task-pipewire)
  - Alternatives avec préférence (paquet déjà installé)

**Cas tordus :**
  - Dépendances circulaires (A → B → C → A)
  - Provides virtuels (ksysguard provided by libksysguard)
  - Familles versionnées (php8.4, php8.5)
  - Conflits transitifs
  - Obsoletes avec version

### Infrastructure pytest

  - [ ] Créer `tests/` avec structure pytest standard
  - [ ] Script de génération des RPM de test (spec files + rpmbuild)
  - [ ] Fixture pytest pour BDD temporaire avec média de test
  - [ ] Fixture pour environnement RPM isolé (chroot ou container)
  - [ ] Tests unitaires : parsing, resolver, database
  - [ ] Tests d'intégration : install/erase/upgrade end-to-end
  - [ ] CI GitHub Actions pour lancer les tests automatiquement
