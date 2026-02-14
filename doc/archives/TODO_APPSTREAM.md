# Projet d'intégration urpm-ng avec AppStream et GNOME Software / Discover

Ce document détaille le plan complet pour rendre urpm-ng compatible avec AppStream, GNOME Software et Discover, étape par étape.

---

## 1. Objectifs

- Permettre à GNOME Software et Discover de voir les applications disponibles dans les dépôts urpm-ng.
- Fournir un backend PackageKit pour urpm-ng afin de gérer les transactions RPM depuis l'UI.
- Intégrer les fichiers AppStream dans le cache local pour un affichage correct des applications.
- Automatiser la génération des fichiers AppStream côté dépôt pour une chaîne complète et maintenable.

---

## 2. Phase 1 : Backend PackageKit MVP

### Objectif
Permettre à GNOME Software / Discover d'installer et de supprimer des paquets via urpm-ng.

### Étapes

1. Implémenter un backend D-Bus PackageKit pour urpm-ng
    - Méthodes à fournir : `InstallPackages`, `RemovePackages`, `RefreshCache`, `GetUpdates`, `SearchPackages`.
    - Chaque méthode appelle urpmd pour exécuter la transaction réelle.
2. Gérer la sécurité avec polkit pour les élévations nécessaires.
3. Installer le backend dans `/usr/lib/packagekit-backend/`.
4. Configurer PackageKit pour utiliser ce backend comme défaut.
5. Tests :
    - Vérifier l'accès D-Bus (`gdbus introspect`).
    - Tester via `pkcon` ou GNOME Software pour installer et supprimer des paquets.

**Livrable Phase 1 :** Backend PackageKit fonctionnel, transactions urpm-ng opérationnelles.

---

## 3. Phase 2 : Gestion des fichiers AppStream locaux

### Objectif
Rendre les applications visibles dans GNOME Software / Discover.

### Étapes

1. Lors de `media update` ou équivalent, récupérer ou générer `appstream.xml.gz` pour chaque dépôt.
2. Copier ces fichiers dans le répertoire attendu par libappstream-glib, généralement :
   - `/var/cache/app-info/`
3. Rebuild de l'index AppStream local :
   ```bash
   appstreamcli refresh-cache
   ```
4. Tests :
   - Vérifier que `appstreamcli search <app>` renvoie les applications.
   - Vérifier que GNOME Software / Discover voient les applications dans l'UI.

**Livrable Phase 2 :** Applications visibles via l’UI, installation possible via le backend PackageKit.

---

## 4. Phase 3 : Production des fichiers AppStream côté dépôt

### Objectif
Automatiser la génération des fichiers AppStream pour tous les paquets des dépôts urpm-ng.

### Étapes

1. Modifier `genhdlist2` ou créer une alternative pour produire `appstream.xml.gz` pour chaque dépôt.
2. Vérifier la cohérence des IDs AppStream avec les paquets (`<provides>` correct).
3. Déployer les fichiers AppStream sur les dépôts.
4. Côté client : récupérer ces fichiers lors de `media update` et rebuild de l’index.

**Livrable Phase 3 :** Chaîne complète, propre et automatisée de dépôt → client → UI.

---

## 5. Roadmap globale et priorité

1. **Phase 1 : Backend PackageKit** → MVP fonctionnel rapidement.
2. **Phase 2 : AppStream local** → compléter l’affichage des applications.
3. **Phase 3 : Production côté dépôt** → automatiser pour tous les dépôts.

---

## 6. Schéma conceptuel du flux complet

```
[ Dépôt / urpm-ng avec appstream.xml.gz ]
               ↓ (media update --appstream)
   [ /var/cache/app-info/ + refresh-cache ]
               ↓
   [ libappstream-glib index local ]
               ↓
[ GNOME Software / Discover UI ]
               ↓ clic Installer
        [ PackageKit backend urpm-ng D-Bus ]
               ↓
           [ urpmd transactions RPM ]
               ↓
      Installation / suppression / mise à jour
```

---

## 7. Conseils / pièges à éviter

- Ne pas laisser d’autres backends (dnf/urpmi) réécrire les fichiers AppStream locaux.
- Toujours vérifier et rebuilder le cache appstream si nécessaire après mise à jour de média.
- Vérifier la cohérence des IDs AppStream et des paquets fournis?
- Commencer par un backend minimal (install/remove) avant de gérer toutes les méthodes D-Bus.

---

## 8. Checklist MVP Backend PackageKit

- [ ] Implémenter `InstallPackages`
- [ ] Implémenter `RemovePackages`
- [ ] Implémenter `RefreshCache`
- [ ] Implémenter `SearchPackages`
- [ ] Polkit : règles et authentification
- [ ] Déployer backend dans `/usr/lib/packagekit-backend/`
- [ ] Configurer PackageKit pour utiliser ce backend par défaut
- [ ] Tester transactions via `pkcon`
- [ ] Tester transactions via Discover ou Gnome software

---

---

## 9. Complément : Backend PackageKit — endpoints d’état et de requête

Cette section complète le plan avec les points spécifiques nécessaires pour que GNOME Software et KDE Discover puissent **afficher correctement l’état des applications** (installé / disponible / mise à jour).

Beaucoup d’implémentations se concentrent uniquement sur les transactions (installer / supprimer), mais la partie la plus sollicitée par l’UI est en réalité la **lecture d’état**.

### 9.1 Deux familles d’API côté backend PackageKit

#### A. Transactions (actions utilisateur)

Ces méthodes déclenchent des opérations réelles sur le système :

- `InstallPackages`
- `RemovePackages`
- `UpdatePackages`
- `RefreshCache`

Elles doivent appeler directement **urpmd** pour exécuter les transactions RPM.

---

#### B. Query / State (lecture d’état)

Ces méthodes sont appelées en permanence par l’interface graphique pour savoir quoi afficher.

| Méthode | Rôle |
|--------|------|
| `Resolve` | Savoir si un paquet existe et s’il est installé |
| `GetPackages` | Lister des paquets selon des filtres |
| `GetDetails` | Taille, licence, version, dépôt |
| `GetUpdates` | Lister les mises à jour disponibles |
| `SearchNames` | Recherche par nom |
| `SearchDetails` | Recherche texte dans descriptions |

Sans ces méthodes, l’UI ne peut pas afficher correctement les boutons « Installer », « Supprimer » ou « Mettre à jour ».

---

### 9.2 Endpoint critique : `Resolve`

C’est la méthode la plus importante pour un MVP.

Entrée typique :

```
firefox
```

Sortie attendue (conceptuelle) :

```
firefox;1.2.3;x86_64;installed
```

Cette réponse permet à GNOME Software / Discover de décider :

- Bouton « Installer »
- Bouton « Supprimer »
- Bouton « Mettre à jour »

---

### 9.3 `GetUpdates`

Sans cette méthode, l’onglet « Mises à jour » des interfaces graphiques restera vide.

Elle doit :

- Lister les paquets installés
- Comparer versions locales vs dépôts
- Retourner uniquement ceux ayant une version plus récente disponible

---

### 9.4 Séparation des responsabilités

| Composant | Responsabilité |
|---------|----------------|
| AppStream | Catalogue visuel (icônes, descriptions, screenshots) |
| PackageKit | État du système + transactions |
| Backend urpm-ng | Vérité terrain (paquets installés / disponibles) |
| GNOME Software / Discover | Interface utilisateur |

**AppStream ne contient jamais l’état « installé ».**
C’est exclusivement le backend PackageKit qui fournit cette information.

---

### 9.5 MVP réaliste recommandé

Pour un premier backend fonctionnel :

- [ ] `Resolve`
- [ ] `InstallPackages`
- [ ] `RemovePackages`
- [ ] `GetUpdates`

Les autres méthodes peuvent être implémentées progressivement.

---

*Fin du document*

