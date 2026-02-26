# rpmdrake-ng - Spécification fonctionnelle

> Interface graphique moderne de gestion des paquets pour Mageia

---

## 1. Architecture de l'interface

### 1.1 Layout général

```
┌─────────────────────────────────────────────────────────────────┐
│ ┌───────────────────────┐  ┌──────────┬──────────┬─────┬───┐    │
│ │ 🔍 Rechercher...      │  │ Installer│ Supprimer│ Màj │ ⬆ │    │
│ └───────────────────────┘  └──────────┴──────────┴─────┴───┘    │
│  ┌──────────────────────────────────────────┐│┌──────────────┐  │
│  │                                          │││              │  │
│  │           ZONE PRINCIPALE                │▐│   FILTRES    │  │
│  │           (liste paquets)                │▐│              │  │
│  │                                          │▐│              │  │
│  │                                          │││              │  │
│  └──────────────────────────────────────────┘│└──────────────┘  │
│                                              ▲                  │
│                                           poignée               │
│ ┌─────────────────────────────────────────────────────────────┐ │
│ │ /                                                           │ │
│ └─────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

- **Barre de recherche** (haut gauche) : saisie directe = recherche (équivalent `/search`)
- **Boutons d'action** (haut droite) : [Installer] [Supprimer] [Màj] [⬆ Tout mettre à jour]
- **Zone principale** (gauche) : affichage des paquets
- **Colonne filtres** (droite) : réduit les mouvements de souris
- **Poignée** (séparateur vertical) : redimensionner/masquer la colonne filtres
- **Zone de commande** (bas) : masquable via `Ctrl+/`, pour commandes `/xxx`

### 1.2 État initial

- Filtre actif par défaut : **Mises à jour disponibles**
- Si aucune mise à jour : zone principale vide avec message centré "Système à jour"

---

## 2. Zone de commande

> Principe : **tout ce qui est faisable à la souris doit être faisable au clavier**

### 2.1 Syntaxe des commandes

#### Recherche et affichage

> Cohérence avec `urpm` CLI : mêmes verbes, mêmes comportements

| Commande | Équivalent CLI | Description |
|----------|----------------|-------------|
| `/search <terme>` | `urpm search` | Recherche dans nom, summary, description |
| `/show <pkg>` | `urpm show` | Afficher les détails d'un paquet |
| `/list` | `urpm list` | Lister les paquets affichés |

#### Filtres

| Commande | Description |
|----------|-------------|
| `/filter upgrades` | Toggle : mises à jour disponibles |
| `/filter installed` | Toggle : paquets installés |
| `/filter available` | Toggle : paquets non installés |
| `/filter conflicts` | Toggle : paquets en conflit |
| `/filter libs` | Toggle : afficher/masquer les bibliothèques |
| `/filter devel` | Toggle : afficher/masquer les paquets -devel |
| `/filter debug` | Toggle : afficher/masquer les paquets -debug |
| `/filter i586` | Toggle : afficher/masquer les paquets 32-bit |
| `/filter lang <code>` | Toggle : afficher/masquer une langue (ex: `de`, `ja`) |
| `/filter category <cat>` | Filtrer par catégorie |
| `/filter reset` | Réinitialiser tous les filtres |

**Syntaxe combinée** : préfixes `+` (activer) et `-` (désactiver)

```
/filter +upgrades-libs      # activer mises à jour, masquer libs
/filter +installed+devel    # activer installés et devel
/filter -debug-i586         # masquer debug et 32-bit
```

#### Sélection

> Les paquets peuvent être référencés par **nom** ou par **numéro de ligne**
> Syntaxe numéros : `1` ou `1,3,5` ou `1-5` ou `1,3,5-10`
> **Note** : les numéros sont convertis en ID paquet dès la saisie (stabilité si les filtres changent)

| Commande | Description |
|----------|-------------|
| `/select <ref>` | Cocher ligne(s) |
| `/select 1,3,5-8` | Cocher lignes 1, 3, 5 à 8 |
| `/select all` | Tout cocher |
| `/unselect <ref>` | Décocher ligne(s) |
| `/unselect all` | Tout décocher |

#### Actions sur les paquets

> **Sans argument** : applique à la sélection courante
> **Avec argument** : applique aux paquets spécifiés (ignore la sélection)
> Toute action ouvre directement la fenêtre de confirmation.

| Commande | Équivalent CLI | Description |
|----------|----------------|-------------|
| `/install` | `urpm install` | Installer la sélection |
| `/install <ref>` | `urpm install` | Installer les paquets spécifiés |
| `/erase` | `urpm erase` | Supprimer la sélection |
| `/erase <ref>` | `urpm erase` | Supprimer les paquets spécifiés |
| `/upgrade` | `urpm upgrade` | Mettre à jour la sélection |
| `/upgrade <ref>` | `urpm upgrade` | Mettre à jour les paquets spécifiés |
| `/upgrade all` | `urpm upgrade` | Mettre à jour tous les paquets |

> **Bouton dédié** : "Tout mettre à jour" équivalent à `/upgrade all`

#### Interface

| Commande | Description |
|----------|-------------|
| `/expand <ref>` | Déplier ligne(s) en mode détaillé |
| `/expand all` | Tout déplier |
| `/collapse <ref>` | Replier ligne(s) en mode concis |
| `/collapse all` | Tout replier |
| `/view concise` | Forcer mode concis global |
| `/view detailed` | Forcer mode détaillé global |
| `/toggle filters` | Afficher/masquer la colonne filtres |
| `/theme dark` | Passer en thème sombre |
| `/theme light` | Passer en thème clair |
| `/help` | Afficher l'aide des commandes |

### 2.2 Autocomplétion

- Autocomplétion des **commandes** (`/ins` → `/install`)
- Autocomplétion des **noms de paquets visibles** dans la liste actuelle (pas de requête BDD)
- Historique des commandes (flèches haut/bas)
- Tab pour compléter
- Échap pour annuler

### 2.3 Raccourcis clavier globaux

| Raccourci | Action |
|-----------|--------|
| `Ctrl+/` | Afficher/masquer la barre de commande |
| `Ctrl+F` | Focus sur la barre de recherche |
| `/` ou `Ctrl+K` | Focus sur la barre de commande (si visible) |
| `Échap` | Retour à la liste / annuler |
| `Entrée` | Exécuter commande / ouvrir détails |
| `Ctrl+A` | Sélectionner tout |

---

## 3. Colonne des filtres

### 3.1 Structure

```
┌──────────────────┐
│ ▼ Catégories     │  ← Arborescence repliable
│   ├─ Bureautique │
│   ├─ Multimédia  │
│   │  ├─ Audio    │
│   │  └─ Vidéo    │
│   ├─ Réseau      │
│   └─ ...         │
├──────────────────┤
│ État             │  ← Cases à cocher (combinables)
│ ☑ Mises à jour   │
│ ☐ Installés      │
│ ☐ Disponibles    │
│ ☐ Conflits       │
├──────────────────┤
│ Afficher aussi   │  ← Toggles (on/off)
│ ☐ Bibliothèques  │  (décoché = masqué)
│ ☐ Devel (-devel) │
│ ☐ Debug (-debug) │
│ ☐ 32-bit (i586)  │
├──────────────────┤
│ Langues     [⚙]  │
│ ☑ Français  (fr) │  ← Langues système cochées
│ ☑ English   (en) │
│ ☐ Deutsch   (de) │  ← Autres langues masquées
│ ☐ 日本語    (ja) │
│ ...              │
└──────────────────┘
```

### 3.2 Catégories

- Source : rpmsrate (groupes RPM)
- Affichage : **arborescence repliable** (plusieurs branches ouvertes simultanément)
- Sélection multiple possible
- Compteur de paquets par catégorie

```
┌──────────────────┐
│ ▼ Bureautique    │
│   ├─ Traitement  │
│   ├─ Tableur     │
│   └─ Présentation│
│ ▼ Multimédia     │
│   ├─ Audio       │
│   └─ Vidéo       │
│ ▶ Réseau         │
│ ▶ Développement  │
└──────────────────┘
```

### 3.3 Filtres par état

> Cases à cocher : combinables entre eux

| État | Description |
|------|-------------|
| Mises à jour | Paquets avec nouvelle version disponible (coché par défaut) |
| Installés | Paquets présents sur le système |
| Disponibles | Paquets non installés |
| Conflits | Paquets non installables (infobulle : "dépendances manquantes ou conflits") |

> **Exemple** : cocher "Installés" + "Mises à jour" = paquets installés ayant une mise à jour disponible

> **UX** : L'état "Conflits" affiche une infobulle explicative au survol.
> En vue détaillée, le détail du conflit est affiché (quel paquet manque, quel conflit).

### 3.4 Filtres d'affichage

> Toggles "Afficher aussi" : **décoché = masqué**, coché = visible
> Ces paquets sont masqués par défaut pour simplifier l'affichage

| Filtre | Critère | Coché par défaut |
|--------|---------|------------------|
| Bibliothèques | Groupe RPM "System/Libraries" | Non |
| Devel | `*-devel` | Non |
| Debug | `*-debug*` | Non |
| 32-bit | arch `i586` sur système `x86_64` | Non |

> **Note** : Le filtre "Bibliothèques" se base sur le groupe RPM, pas sur le nom.
> Ainsi `libreoffice` (groupe "Office") et `neovim` (groupe "Editors") restent visibles.

### 3.5 Filtres par langue

Les paquets localisés (`*-fr`, `*-de`, `*-ja`, etc.) sont filtrés selon les langues de l'utilisateur.

**Comportement :**
- Détection automatique des locales système (`LANG`, `LANGUAGE`, `/etc/locale.conf`)
- Les langues détectées sont cochées par défaut → paquets visibles
- Les autres langues sont décochées → paquets masqués
- L'utilisateur peut cocher/décocher manuellement

**Configuration (icône ⚙) :**
- Liste complète des langues disponibles
- Permet d'ajouter des langues non détectées automatiquement
- Sauvegardé dans les préférences utilisateur

**Exemple :** Sur un système `LANG=fr_FR.UTF-8`, les paquets `-fr` et `-en` (anglais toujours inclus) sont visibles, les `-de`, `-ja`, `-es`... sont masqués.

### 3.6 Poignée de redimensionnement

- Double-clic : masquer/afficher la colonne
- Drag : redimensionner
- État mémorisé entre sessions

---

## 4. Zone principale

### 4.1 Mode concis (défaut)

> Une ligne par paquet, numérotée pour sélection rapide

```
┌──────────────────────────────────────────────────────────────────────┐
│  1 │ ☐ │ 🦊 │ firefox       │ 125.0 → 126.0  │ Navigateur web        │
│  2 │ ☐ │ 📝 │ neovim        │ 0.9.5 → 0.10.0 │ Éditeur de texte      │
│  3 │ ☑ │ 📧 │ thunderbird   │ 115.9 → 115.10 │ Client de messagerie  │
└──────────────────────────────────────────────────────────────────────┘
```

Colonnes :
- Numéro de ligne (pour sélection rapide via commandes)
- Case à cocher (sélection pour action)
- Icône du paquet (AppStream, ou icône générique si absente)
- Nom du paquet
- Version (installée → disponible)
- Résumé (summary)

### 4.2 Mode détaillé

> Plusieurs lignes par paquet avec description et actions directes

```
┌────────────────────────────────────────────────────────────────┐
│  1 │ ☐ │ neovim                              │ 0.9.5 → 0.10.0  │
│    │   │ Vim-fork focused on extensibility and usability       │
│    │   │ [Installer] [Infos]                                   │
├────┼───┼───────────────────────────────────────────────────────┤
│  2 │ ☐ │ firefox                              │ 125.0 → 126.0  │
│    │   │ The Mozilla Firefox web browser                       │
│    │   │ [Installer] [Infos]                                   │
└────────────────────────────────────────────────────────────────┘
```

- Description complète visible
- Boutons d'action directe : [Installer] / [Supprimer] / [Infos]

**Expansion individuelle ou globale :**

| Action | Souris | Commande |
|--------|--------|----------|
| Déplier une ligne | Clic sur ▶ ou double-clic | `/expand 3` |
| Replier une ligne | Clic sur ▼ | `/collapse 3` |
| Déplier plusieurs | - | `/expand 1,3,5-8` |
| Tout déplier | - | `/expand all` |
| Tout replier | - | `/collapse all` |
| Mode global concis | - | `/view concise` |
| Mode global détaillé | - | `/view detailed` |

**Affichage mixte :**

```
┌────────────────────────────────────────────────────────────────┐
│  1 │ ☐ │▶│ neovim        │ 0.9.5 → 0.10.0 │ Éditeur de texte   │
│  2 │ ☐ │▼│ firefox                          │ 125.0 → 126.0    │
│    │   │ │ The Mozilla Firefox web browser                     │
│    │   │ │ [Installer] [Infos]                                 │
│  3 │ ☑ │▶│ thunderbird   │ 115.9 → 115.10 │ Client messagerie  │
└────────────────────────────────────────────────────────────────┘
```

- ▶ = ligne repliée (concise)
- ▼ = ligne dépliée (détaillée)

### 4.3 État vide

```
┌────────────────────────────────────────────────────────────────┐
│                                                                │
│                                                                │
│                      ✓ Système à jour                          │
│                                                                │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

### 4.4 Actions sur les paquets

- Clic : sélectionner/désélectionner
- Double-clic : ouvrir détails
- Clic droit : menu contextuel (installer, supprimer, infos, dépendances)

---

## 5. Panneau de détails

> Panneau latéral ou modal au clic sur un paquet

- Nom complet et version
- Description longue
- Dépendances (requires, recommends, suggests)
- Fichiers fournis
- Changelog
- Boutons d'action

**Liens externes** :
- 🔗 **Projet upstream** : lien vers URL du champ RPM `URL` (si présent)
- 🐛 **Signaler un problème** : ouvre `https://bugs.mageia.org/enter_bug.cgi?product=Mageia&component=RPM+Packages&short_desc=[nom-paquet]`

---

## 6. Boutons d'action

> Intégrés dans la barre supérieure, à droite du champ de recherche (voir layout 1.1)

| Bouton | Action |
|--------|--------|
| [Installer] | Installer la sélection (ouvre confirmation) |
| [Supprimer] | Supprimer la sélection (ouvre confirmation) |
| [Màj] | Mettre à jour la sélection (ouvre confirmation) |
| [⬆] | Tout mettre à jour (`/upgrade all`) |

- Boutons actifs uniquement si la sélection est compatible avec l'action
- Infobulle au survol : "3 paquets sélectionnés (45 Mo)"

---

## 7. Fenêtre de transaction

> Fenêtre modale affichée lors de l'exécution d'une transaction

### 7.0 Confirmation initiale

Avant de lancer pkexec, afficher un résumé :

```
┌─────────────────────────────────────────────────────────────────┐
│  Confirmer les changements                                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Installer (3) :              Supprimer (1) :                   │
│    • firefox 126.0              • old-browser                   │
│    • thunderbird 115.10                                         │
│    • neovim 0.10.0                                              │
│                                                                 │
│  Téléchargement : 45 Mo       Espace disque : +120 Mo           │
│                                                                 │
│                                        [Annuler] [Confirmer]    │
└─────────────────────────────────────────────────────────────────┘
```

Après [Confirmer] → demande mot de passe pkexec → fenêtre de progression

### 7.1 Structure (progression)

```
┌─────────────────────────────────────────────────────────────────┐
│  Transaction en cours                                      [X]  │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─ Questions ────────────────────────────────────────────────┐ │
│  │ Le paquet "foo" propose plusieurs alternatives :           │ │
│  │   ○ foo-gtk (recommandé)                                   │ │
│  │   ○ foo-qt                                                 │ │
│  │   ○ foo-cli                                                │ │
│  │                                          [Passer] [Choisir]│ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                 │
│  ┌─ Téléchargement (3/12) ────────────────────────────────────┐ │
│  │ firefox-126.0.x86_64.rpm        ████████████░░░░  75% 2.1MB│ │
│  │ thunderbird-115.10.x86_64.rpm   ██████░░░░░░░░░░  40% 1.5MB│ │
│  │ neovim-0.10.0.x86_64.rpm        ███░░░░░░░░░░░░░  20% 800KB│ │
│  │ libreoffice-core-7.6.x86_64.rpm ██░░░░░░░░░░░░░░  12% 450KB│ │
│  │                                                            │ │
│  │ Total : 45 Mo / 120 Mo                   Vitesse : 12 MB/s │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                 │
│  ┌─ Installation ─────────────────────────────────────────────┐ │
│  │ ✓ firefox-126.0.x86_64                                     │ │
│  │ ✓ thunderbird-115.10.x86_64                                │ │
│  │ ⟳ neovim-0.10.0.x86_64          Installation...            │ │
│  │ ○ libreoffice-core-7.6.x86_64   En attente                 │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                 │
│  ┌─ Journal ──────────────────────────────────────────────────┐ │
│  │ [10:23:45] Préparation de la transaction...                │ │
│  │ [10:23:46] Téléchargement de firefox-126.0.x86_64.rpm      │ │
│  │ [10:23:52] Vérification des signatures...                  │ │
│  │ [10:23:53] Installation de firefox-126.0.x86_64            │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                 │
│                                              [Annuler] [Fermer] │
└─────────────────────────────────────────────────────────────────┘
```

### 7.2 Zone Questions

Affichée uniquement quand une décision utilisateur est nécessaire :

| Type de question | Exemple |
|------------------|---------|
| Alternatives (OR deps) | Choix entre foo-gtk, foo-qt, foo-cli |
| Conflits | "Le paquet X entre en conflit avec Y. Supprimer Y ?" |
| Fichiers config | "foo.conf a été modifié. Garder / Remplacer / Diff ?" |
| Signature invalide | "Signature non vérifiée. Continuer ?" |

**Boutons** :
- [Passer] = utiliser le choix par défaut (ou ignorer si possible)
- [Choisir] = valider le choix sélectionné
- ≠ du [Annuler] en bas qui annule **toute** la transaction

### 7.3 Zone Téléchargement

- **Barres de progression parallèles** (jusqu'à N simultanées, configurable)
- Nom du fichier + pourcentage + taille téléchargée
- Total global + vitesse instantanée
- Masquée une fois tous les téléchargements terminés

### 7.4 Zone Installation

| Icône | État |
|-------|------|
| ○ | En attente |
| ⟳ | En cours |
| ✓ | Terminé |
| ✗ | Erreur |

- Liste scrollable si beaucoup de paquets
- Affiche l'action en cours (Installation... / Suppression... / Mise à jour...)

### 7.5 Zone Journal

- Log détaillé des opérations (horodaté)
- Repliable/dépliable
- Exportable (bouton "Sauvegarder le log")

### 7.6 Boutons

| Bouton | État | Action |
|--------|------|--------|
| [Annuler] | Transaction en cours | Annuler proprement (rollback si possible) |
| [Fermer] | Transaction terminée | Fermer la fenêtre |
| [Réessayer] | Après erreur | Relancer les paquets en échec |

---

## 8. Vues secondaires

> Ces vues remplacent complètement le layout principal (pas juste un filtre)

### 8.1 Historique des transactions

Accessible via menu ou `/history`

```
┌─────────────────────────────────────────────────────────────────┐
│  Historique des transactions                           [Retour] │
├─────────────────────────────────────────────────────────────────┤
│  ID  │ Date       │ Action          │ Paquets │ Utilisateur     │
│ ─────┼────────────┼─────────────────┼─────────┼──────────────── │
│  42  │ 2026-02-25 │ Mise à jour     │ 12      │ root            │
│  41  │ 2026-02-20 │ Installation    │ 3       │ pascal          │
│  40  │ 2026-02-18 │ Suppression     │ 1       │ pascal          │
│  ... │            │                 │         │                 │
├─────────────────────────────────────────────────────────────────┤
│  Détails transaction #42                                        │
│  ────────────────────────────────────────────────────────────── │
│  ✓ firefox 125.0 → 126.0                                        │
│  ✓ thunderbird 115.9 → 115.10                                   │
│  ✓ kernel-desktop 6.6.10 → 6.6.12                               │
│  ...                                                            │
└─────────────────────────────────────────────────────────────────┘
```

- Clic sur une ligne : affiche les détails
- Possibilité de réinstaller/annuler une transaction (si supporté)

### 8.2 Gestion des médias

Accessible via menu ou `/media`

```
┌─────────────────────────────────────────────────────────────────┐
│  Gestion des médias                                    [Retour] │
├─────────────────────────────────────────────────────────────────┤
│  ☑ │ Core Release      │ https://mirror.../release/     │ [⚙]   │
│  ☑ │ Core Updates      │ https://mirror.../updates/     │ [⚙]   │
│  ☐ │ Nonfree Release   │ https://mirror.../nonfree/     │ [⚙]   │
│  ☑ │ Tainted Updates   │ https://mirror.../tainted/     │ [⚙]   │
├─────────────────────────────────────────────────────────────────┤
│  [Ajouter média] [Autoconfig] [Rafraîchir]                      │
└─────────────────────────────────────────────────────────────────┘
```

- ☑/☐ : activer/désactiver média
- [⚙] : éditer (URL, priorité, options)
- [Autoconfig] : `urpm media autoconfig`
- [Rafraîchir] : déclenche un refresh manuel (fenêtre de progression)

> **Note** : urpmd effectue des refreshes automatiques en arrière-plan.
> Le bouton [Rafraîchir] permet de forcer un refresh immédiat si besoin.

### 8.3 Gestion des serveurs/peers

Accessible via menu ou `/servers`

> TODO: Spécifier le layout

---

## 9. Paramètres

### 9.1 Assistant premier lancement

> Fenêtre affichée à la première utilisation uniquement

```
┌─────────────────────────────────────────────────────────────────┐
│  Bienvenue dans rpmdrake-ng                                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Langues des paquets à afficher :                               │
│  ☑ Français (fr)      ← détecté depuis le système               │
│  ☑ English (en)       ← toujours inclus                         │
│  ☐ Deutsch (de)                                                 │
│  ☐ Español (es)                                                 │
│  ☐ Autres...                                                    │
│                                                                 │
│  ☐ Lancer le tour guidé de l'interface                          │
│  ☐ Activer les explications audio                               │
│                                                                 │
│                                        [Annuler] [Démarrer]     │
└─────────────────────────────────────────────────────────────────┘
```

- Langues pré-cochées selon `LANG`, `LANGUAGE`, `/etc/locale.conf`

### 9.2 Tour guidé (onboarding)

> Visite interactive de l'interface au premier lancement (optionnel)

**Principe** : mise en lumière successive des zones avec bulles explicatives

```
┌──────────────────────────────────────────────────────────────────┐
│ ┌─────────────────────────────────────┐  ┌─────────────────────┐ │
│ │ 🔍 Rechercher...                    │  │ Étape 1/6           │ │
│ └─────────────────────────────────────┘  │                     │ │
│  ╔══════════════════════════════════╗    │ La barre de         │ │
│  ║  ░░░░░ zone assombrie ░░░░░░░░░  ║────│ recherche permet    │ │
│  ║  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  ║    │ de trouver des      │ │
│  ║  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  ║    │ paquets par nom.    │ │
│  ╚══════════════════════════════════╝    │                     │ │
│                                          │ 🔊 [Écouter]        │ │
│                                          │                     │ │
│                                          │[Précédent] [Suivant]│ │
│                                          └─────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

**Étapes du tour** :
1. Barre de recherche
2. Liste des paquets (sélection, expand)
3. Colonne des filtres
4. Barre d'actions (Installer, Supprimer, Màj, Tout mettre à jour)
5. Barre de commande (`Ctrl+/` pour masquer)
6. Raccourcis utiles

**Audio** (optionnel) :
- Synthèse vocale via Qt TextToSpeech ou espeak
- Bouton [🔊 Écouter] sur chaque bulle
- Peut être désactivé à tout moment

**Navigation** :
- [Précédent] / [Suivant] ou flèches clavier
- [Passer] pour quitter le tour
- Relançable via menu Aide > Tour guidé

### 9.3 Préférences utilisateur

- Langues parlées (pour filtre)
- Filtres par défaut au démarrage
- Largeur colonne filtres
- Thème (clair/sombre/système)
- Délai debounce recherche (défaut 300ms)

### 9.4 Stockage

- Fichier : `~/.config/rpmdrake-ng/settings.toml`
- Format : TOML

---

## 10. Performances et optimisations

### 10.1 Chargement initial

- **Vue par défaut : Mises à jour uniquement** (évite de charger 30 000 paquets)
- Virtualisation de la liste : ne rendre que les lignes visibles (recyclage)
- Chargement lazy des métadonnées détaillées (description, changelog) à la demande

### 10.2 Filtrage - Architecture détaillée

#### Debounce (anti-rebond)

**Problème** : Si l'utilisateur tape "firefox", on ne veut pas lancer 7 requêtes (f, fi, fir, fire, firef, firefo, firefox).

**Solution** : Attendre que l'utilisateur arrête de taper avant de lancer la requête.

```
Frappe: f─i─r─e─f─o─x─────────────────────────►
                      │                       │
                      │◄─── délai timeout ───►│
                      │                       │
                      └── Timer reset à       └── Timer expire
                          chaque frappe           → lancer requête
```

> **Paramètre** : `search_debounce_ms` dans settings.toml (défaut : 300ms)
> Ajustable selon la réactivité souhaitée et les performances de la machine.

```python
class SearchDebouncer:
    def __init__(self, delay_ms=None):
        # Lire depuis config, défaut 300ms
        self.delay = delay_ms or settings.get('search_debounce_ms', 300)
        self.timer = None

    def on_text_changed(self, text):
        # Annuler le timer précédent
        if self.timer:
            self.timer.cancel()

        # Nouveau timer
        self.timer = Timer(self.delay / 1000,
                          lambda: self.execute_search(text))
        self.timer.start()

    def execute_search(self, text):
        # Lancer la vraie recherche
        self.controller.filter_packages(text)
```

#### Thread séparé (non-blocage UI)

**Problème** : Filtrer 30 000 paquets prend ~100-500ms. Pendant ce temps, l'UI serait gelée.

**Solution** : Exécuter le filtrage dans un thread worker, l'UI reste réactive.

```
┌─────────────────┐      ┌─────────────────┐
│   Main Thread   │      │  Worker Thread  │
│   (UI/GTK/Qt)   │      │   (Filtrage)    │
├─────────────────┤      ├─────────────────┤
│                 │      │                 │
│  User tape      │      │                 │
│  "firefox"      │      │                 │
│       │         │      │                 │
│       ▼         │      │                 │
│  Debounce OK    │─────►│  Lancer query   │
│                 │      │  SELECT * FROM  │
│  Spinner ON     │      │  packages WHERE │
│  (UI réactive)  │      │  name MATCH ... │
│       ▲         │      │       │         │
│       │         │◄─────│  Résultats      │
│  Afficher       │      │                 │
│  résultats      │      │                 │
│  Spinner OFF    │      │                 │
└─────────────────┘      └─────────────────┘
```

**Annulation** : Si l'utilisateur tape autre chose pendant que le worker tourne, on annule le worker en cours et on en relance un nouveau.

```python
class FilterWorker:
    def __init__(self):
        self.current_task = None

    def filter_async(self, criteria, callback):
        # Annuler la tâche précédente
        if self.current_task:
            self.current_task.cancel()

        # Lancer nouvelle tâche
        self.current_task = ThreadPoolExecutor().submit(
            self._do_filter, criteria
        )
        self.current_task.add_done_callback(
            lambda fut: GLib.idle_add(callback, fut.result())
            # ou Qt: QMetaObject.invokeMethod()
        )
```

#### Cache intelligent (filtrage incrémental)

**Problème** : L'utilisateur cherche "neo" (500 résultats), puis "neovim" (15 résultats). Inutile de re-parcourir les 30 000 paquets.

**Solution** : Filtrer dans le cache si le nouveau critère est plus restrictif.

```
Recherche: "neo"
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  BDD (30 000 paquets)                                   │
│  SELECT * FROM packages WHERE name MATCH 'neo*'         │
└─────────────────────────────────────────────────────────┘
    │
    ▼ 500 résultats → mis en cache

Recherche: "neovim" (commence par "neo")
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  Cache (500 paquets seulement)                          │
│  filter(cache, lambda p: 'neovim' in p.name)            │
└─────────────────────────────────────────────────────────┘
    │
    ▼ 15 résultats (filtrage local, ultra rapide)
```

**Logique d'invalidation du cache** :

```python
class FilterCache:
    def __init__(self):
        self.cached_query = None
        self.cached_results = None

    def filter(self, query, db):
        # Peut-on réutiliser le cache ?
        if (self.cached_query and
            query.startswith(self.cached_query) and
            self.is_more_restrictive(query)):
            # Filtrer dans le cache
            return self._filter_local(query)
        else:
            # Requête BDD complète
            self.cached_query = query
            self.cached_results = db.search(query)
            return self.cached_results

    def invalidate(self):
        """Appelé quand les filtres d'état/catégorie changent"""
        self.cached_query = None
        self.cached_results = None
```

**Invalidation automatique** quand :
- Changement de filtre d'état (Installés → Disponibles)
- Changement de catégorie
- Toggle "Afficher libs/devel/debug"
- Refresh des médias

### 10.3 Affichage

- Détails chargés à la demande (expand ou panneau latéral)
- Refresh des barres de progression : max 10/seconde

---

## 11. Technologies

**Framework initial : Qt6 + Python (PyQt6 ou PySide6)**

| Avantages | Inconvénients |
|-----------|---------------|
| Natif KDE, s'intègre bien sur GTK aussi | Dépendances Qt |
| API stable et mature | Taille des dépendances |
| QML pour UI moderne | |
| Bonne doc, large communauté | |

> **Stratégie** : développer le frontend Qt6 d'abord, puis transposer vers GTK4 si demande.
> L'interface abstraite (ViewInterface) permet cette transposition.

---

## 12. Architecture technique

### 12.1 Architecture MVC découplée

> rpmdrake-ng est une **vraie application GUI**, pas un wrapper CLI
> Architecture permettant **plusieurs frontends** (GTK, Qt) sur un backend commun

```
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                         FRONTEND (Vue)                          │    │
│  │  ┌─────────────────────┐         ┌─────────────────────┐        │    │
│  │  │  rpmdrake-ng-gtk    │         │  rpmdrake-ng-qt     │        │    │
│  │  │  ─────────────────  │         │  ─────────────────  │        │    │
│  │  │  GTK4 + libadwaita  │         │  Qt6 + QML          │        │    │
│  │  │  - Widgets GTK      │         │  - Widgets Qt       │        │    │
│  │  │  - Thème Adwaita    │         │  - Thème Breeze     │        │    │
│  │  │  - Style GNOME      │         │  - Style KDE        │        │    │
│  │  └──────────┬──────────┘         └──────────┬──────────┘        │    │
│  │             │                               │                   │    │
│  │             └───────────────┬───────────────┘                   │    │
│  │                             │                                   │    │
│  │                             ▼                                   │    │
│  │  ┌─────────────────────────────────────────────────────────┐    │    │
│  │  │              Interface abstraite (ViewInterface)        │    │    │
│  │  │  - on_package_list_update(packages)                     │    │    │
│  │  │  - on_progress(pkg, percent, speed)                     │    │    │
│  │  │  - on_question(type, message, choices) → response       │    │    │
│  │  │  - on_transaction_complete(summary)                     │    │    │
│  │  └─────────────────────────────────────────────────────────┘    │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                    │                                    │
│                                    ▼                                    │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    CONTRÔLEUR (rpmdrake.controller)             │    │
│  │  - Gestion état (sélection, filtres, mode vue)                  │    │
│  │  - Logique métier (actions, validation)                         │    │
│  │  - Dispatch événements GUI → Backend                            │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                    │                                    │
│                                    ▼                                    │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                    BACKEND user (lecture seule)                 │    │
│  │  - urpm.core.db         (requêtes, recherche FTS5)              │    │
│  │  - urpm.core.resolver   (résolution dépendances)                │    │
│  │  - urpm.core.media      (liste médias)                          │    │
│  │  - urpm.core.appstream  (icônes, descriptions)                  │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                    │                                    │
│                                    ▼                                    │
│                    ┌───────────────────────────────┐                    │
│                    │ pkexec (root)                 │                    │
│                    │ - urpm.core.downloader        │                    │
│                    │ - urpm.core.transaction_queue │                    │
│                    └───────────────────────────────┘                    │
└─────────────────────────────────────────────────────────────────────────┘
```

### 12.2 Structure des paquets RPM

| Paquet | Contenu |
|--------|---------|
| `rpmdrake-ng-common` | Contrôleur, interfaces, ressources partagées |
| `rpmdrake-ng-gtk` | Frontend GTK4/libadwaita (dépend de -common) |
| `rpmdrake-ng-qt` | Frontend Qt6/QML (dépend de -common) |

Installation :
- GNOME/LXDE/XFCE → `rpmdrake-ng-gtk`
- KDE/LXQt → `rpmdrake-ng-qt`
- Choix via alternatives ou méta-paquet `rpmdrake-ng`

### 12.3 Utilisation des modules urpm

**Côté GUI (user, lecture seule) :**

| Module | Usage |
|--------|-------|
| `urpm.core.db` | Requêtes, recherche FTS5 |
| `urpm.core.resolver` | Résolution dépendances, calcul transactions |
| `urpm.core.media` | Liste des médias/dépôts |
| `urpm.core.appstream` | Icônes, descriptions enrichies, screenshots |

**Côté Helper (root, via pkexec) :**

| Module | Usage |
|--------|-------|
| `urpm.core.downloader` | Téléchargement parallèle (→ /var/lib/urpm/) |
| `urpm.core.transaction_queue` | Exécution transactions RPM, IPC, rollback |

### 12.4 Registre d'actions

> Synchronisation commandes / clics / raccourcis

Toutes les actions sont définies dans un registre central :

```python
ACTION_REGISTRY = {
    "install": Action(
        command="/install",
        shortcut="Ctrl+I",
        icon="package-install",
        handler=controller.mark_install,
        enabled_when=lambda: selection.has_available(),
    ),
    "erase": Action(
        command="/erase",
        shortcut="Ctrl+E",
        icon="package-remove",
        handler=controller.mark_erase,
        enabled_when=lambda: selection.has_installed(),
    ),
    # ...
}
```

- Les boutons, menus et commandes utilisent tous le même handler
- Tests automatisés : vérifier que action via commande == action via clic

### 12.5 Élévation de privilèges et IPC

> Pas de D-Bus : souvent non fonctionnel sur LXDE, XFCE

#### Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│  rpmdrake-ng (user)                                                    │
│                                                                        │
│  ┌─────────────┐     ┌──────────────────┐                              │
│  │     GUI     │◄────│   Contrôleur     │                              │
│  └─────────────┘     └────────┬─────────┘                              │
│                               │                                        │
│        Lecture BDD ───────────┼─────────► urpm.core.db (user, read-only)
│        Résolution  ───────────┼─────────► urpm.core.resolver (user)    │
│                               │                                        │
│                               │ Download + Transaction                 │
│                               ▼ (cache dans /var/lib/urpm/ → root)     │
│  ┌────────────────────────────────────────────────────────────────┐    │
│  │  pkexec urpm-transaction-helper                                │    │
│  │  ════════════════════════════════════════════════════════════  │    │
│  │                         (root)                                 │    │
│  │  stdin ◄──── JSON commands ────── GUI                          │    │
│  │  stdout ────► JSON progress ─────► GUI                         │    │
│  │                                                                │    │
│  │  - urpm.core.downloader (téléchargement → /var/lib/urpm/)      │    │
│  │  - urpm.core.transaction_queue (installation RPM)              │    │
│  └────────────────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────────────┘
```

> **Note** : Le téléchargement écrit dans `/var/lib/urpm/` (cache système) donc
> doit aussi passer par le helper root. Seules les opérations **lecture seule**
> (recherche, filtres, résolution) tournent en user.

#### Protocole IPC (JSON sur stdin/stdout)

Basé sur `urpm.core.transaction_queue.QueueProgressMessage` existant.

**GUI → Helper (stdin) :**

```json
{"cmd": "execute", "operations": [
  {"type": "install", "packages": ["firefox", "thunderbird"]},
  {"type": "erase", "packages": ["old-pkg"]}
], "options": {"verify_signatures": true, "config_policy": "ask"}}
```

```json
{"cmd": "answer", "question_id": "alt_1", "choice": "foo-gtk"}
```

```json
{"cmd": "cancel"}
```

**Helper → GUI (stdout) :**

```json
{"type": "download_start", "total_packages": 5, "total_size": 125000000}
{"type": "download_progress", "name": "firefox-126.0.x86_64.rpm", "current": 45000000, "total": 98000000, "speed": 12500000}
{"type": "download_complete", "name": "firefox-126.0.x86_64.rpm"}
{"type": "install_start", "total": 5}
{"type": "install_progress", "name": "firefox", "current": 1, "total": 5, "step": "installing"}
{"type": "question", "question_id": "alt_1", "qtype": "alternative", "message": "Choisir...", "choices": ["foo-gtk", "foo-qt"]}
{"type": "install_complete", "name": "firefox", "success": true}
{"type": "transaction_done", "success": true, "rpmnew_files": ["/etc/foo.conf.rpmnew"]}
```

#### Helper CLI

```bash
# Lancé par la GUI via pkexec
pkexec /usr/libexec/rpmdrake-ng/transaction-helper

# Le helper lit stdin, écrit stdout, fait les transactions RPM
# Utilise les mêmes classes que urpm CLI (TransactionQueue, InstallLock, etc.)
```

#### Fichier PolicyKit

`/usr/share/polkit-1/actions/org.mageia.rpmdrake-ng.policy`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<policyconfig>
  <action id="org.mageia.rpmdrake-ng.transaction">
    <description>Execute RPM transactions</description>
    <message>Authentication is required to install/remove packages</message>
    <defaults>
      <allow_any>auth_admin</allow_any>
      <allow_inactive>auth_admin</allow_inactive>
      <allow_active>auth_admin_keep</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/libexec/rpmdrake-ng/transaction-helper</annotate>
  </action>
</policyconfig>
```

#### Gestion des erreurs et rollback

- Utilise `urpm.core.transaction_queue` qui gère déjà :
  - Lock fichier (`/var/lib/rpm/.urpm-install.lock`)
  - Détection transactions interrompues
  - Nettoyage automatique
- Si annulation pendant téléchargement : arrêt immédiat
- Si annulation pendant installation : terminer le paquet en cours, arrêter ensuite
- Fichiers `.rpmnew` remontés à la GUI pour traitement config_policy

#### Gestion des erreurs réseau

- **Retry silencieux** : 3 tentatives automatiques avant d'afficher une erreur
- **Miroirs alternatifs** : basculement automatique si un miroir échoue
- **Pas de popup intempestif** : les erreurs transitoires sont gérées silencieusement
- **Affichage erreur** : uniquement si tous les essais échouent, avec bouton [Réessayer]

#### Fermeture pendant transaction

Si l'utilisateur ferme la fenêtre pendant une transaction :
- Dialogue de confirmation : "Transaction en cours. Vraiment quitter ?"
- Si oui : annulation propre (arrêt téléchargement, ou fin du paquet en cours si installation)

### 12.6 Callbacks et signaux

Communication GUI ↔ Backend via callbacks/signaux :

| Signal | Données |
|--------|---------|
| `on_download_progress` | paquet, pourcentage, vitesse, taille |
| `on_download_complete` | paquet |
| `on_install_progress` | paquet, étape (preparing, installing, configuring) |
| `on_install_complete` | paquet, succès/erreur |
| `on_question` | type, message, choix[] → réponse |
| `on_transaction_complete` | résumé, erreurs[], rpmnew_files[] |

### 12.7 Notifications

- Notifications système via libnotify (optionnel)
- Intégration future avec mgaonline-ng (applet systray)

---

## 13. Tests

### 13.1 Tests unitaires

- Contrôleur : mock de la vue, vérifier que les actions produisent le bon état
- Parseur de commandes : `/install 1,3,5-8` → liste de paquets correcte
- Filtres : combinaisons de filtres, cache

### 13.2 Tests d'intégration

- Communication avec urpm.core (résolution, téléchargement)
- Protocole IPC avec transaction-helper
- Transactions réelles sur chroot de test

### 13.3 Tests UI

- Vérifier que clic et commande produisent le même résultat
- Tests de scroll avec 1000+ paquets
- Tests de performance filtrage

---

## 14. Accessibilité

- Navigation complète au clavier (Tab, flèches, Entrée)
- Labels ARIA pour lecteurs d'écran
- Contraste suffisant (respect WCAG AA)
- Taille de police ajustable (respect paramètres système)

---

## 15. Internationalisation

- Interface traduite via gettext (.po/.mo)
- Langues prioritaires : français, anglais
- Commandes `/xxx` en anglais (non traduites)
- Labels UI traduits selon locale système

---

## 16. Questions ouvertes

Aucune question en suspens.
