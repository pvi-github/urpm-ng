# TODEBUG - Bugs et améliorations à traiter

## Corrigés ✅

### why et rdepends - CORRIGÉ
Les commandes `why` et `rdepends` fonctionnent maintenant correctement :
- Filtrage des provides virtuels
- Affichage avec couleurs selon type de dépendance (R/r/s)
- Option `--hide-uninstalled` pour ne montrer que les chemins vers paquets installés
Commits : cc1a6d0, a8dca7d, cd49a44

### Gestion des peers colors.warn - CORRIGÉ
Le module utilise maintenant `colors.warning()` partout.

### Aliases recommends/suggests - CORRIGÉ
Ajoutés : `urpm recommends`, `urpm whatrecommends`, `urpm suggests`, `urpm whatsuggests`
Commit : d2c5815

### Listes tronquées "... and xxx more" - CORRIGÉ
Options ajoutées : `--show-all`, `--flat`, `--json`
Affichage multi-colonnes automatique.
Commit : ad4a586

### Alternatives et --prefer - FONCTIONNEL
L'option `--prefer` fonctionne avec :
- Contraintes de version : `--prefer=php:8.4`
- Préférences positives : `--prefer=apache,php-fpm`
- Préférences négatives : `--prefer=-apache-mod_php`

La sélection se fait sur les REQUIRES/PROVIDES, pas les noms de paquets.
Commits : 544d3a9, 454586a, 3416669

### README - CORRIGÉ
Documentation mise à jour pour :
- Gestion des peers et blacklist
- Commandes server
- ip_mode IPv4/IPv6

### Souci à l'upgrade - CORRIGÉ
Les orphans sont maintenant gérés correctement avec la queue de transactions.

### Dépendances phpmyadmin - CORRIGÉ
`./bin/urpm depends phpmyadmin` affiche maintenant les alternatives correctement.

### Pre-downloading - FONCTIONNEL
Le pre-downloading fonctionne via urpmd scheduler.

### Souci d'alignement dans urpm h - CORRIGÉ
Les entêtes sont maintenant alignées avec les données.

### Performance démarrage install/upgrade - CORRIGÉ
Le chargement du pool libsolv était lent (~2-3s).
Optimisation : utilisation des méthodes natives libsolv au lieu de Python :
- `add_rpmdb()` : 0.1s au lieu de 0.5s
- `add_mdk()` : 0.25s au lieu de 1.35s
Temps total divisé par ~5 (de ~1.9s à ~0.35s pour la création du pool).
Commit : 1e5f4a7

### Aliases requires/whatrequires - CORRIGÉ
Ajoutés : `urpm requires` (alias de depends), `urpm whatrequires` (alias de rdepends)
+ Fix du parsing des capabilities .so pour l'affichage correct des dépendances.
Commit : 6a38daa

---

## En cours / À faire

### Performance de why
La commande `why` utilise `_get_rdeps()` qui itère sur tous les paquets installés pour chaque niveau du BFS.
Contrairement à `rdepends --hide-uninstalled` qui pré-construit le graphe en une passe, `why` fait des lookups répétés.

**TODO** : utiliser `_build_rdeps_graph()` pour `why` aussi.

### Fix dep (bootstrap)
Quand python3-solv ou python3-zstandard ne sont pas installés, proposer de les installer :
- soit en DL direct
- soit via urpmi

Et bien les mettre dans installed-through-deps.list.

### Gestion du cache - Quotas
Le nettoyage basique (fichiers > 30 jours) n'est pas forcément adapté.

**TODO** : implémenter les quotas par media et global.
Si on arrive à la taille max du quota, le scheduler nettoie les fichiers en commençant par les plus vieux et surtout s'ils ont été installés.

### Contraintes de version dans whatprovides
Pouvoir préciser les contraintes de version (== < <= > >=) pour filtrer.

---

## Notes externes (pas des bugs urpm)

### Packaging php-webinterface (à remonter aux packageurs Mageia)

Problème : `php-webinterface` n'est fourni que par des paquets spécifiques à un webserver :
- `php8.4-fpm-nginx` (requiert nginx)
- `php8.4-fpm-apache` (requiert apache)
- `apache-mod_php8.4` (requiert apache)
- `php8.4-cgi` (compatible avec tous mais pas fpm)

Conséquence : on ne peut pas avoir `lighttpd + php-fpm` car il n'existe pas de paquet `php8.4-fpm-lighttpd` ou `php8.4-fpm-generic`.

Solutions possibles côté packaging :
1. `php8.4-fpm` fournit directement `php-webinterface` (config FastCGI générique)
2. Créer un paquet `php8.4-fpm-fcgi` ou `php8.4-fpm-generic` qui fournit `php-webinterface` sans dépendre d'un webserver spécifique
3. Les paquets `-nginx` et `-apache` deviennent juste des configs spécifiques optionnelles

Note : ce n'est PAS un problème de l'algo de résolution d'urpm.

---

## Améliorations futures --prefer

### Ordre des choix
- Quand on choisit au 2ème choix (après la version de PHP) entre cli, cgi et fpm,
  y'a rien pour permettre de choisir "rien" qui irait avec apache-mod_php
  (qui n'a besoin d'aucun des 3).
- Idéalement il faudrait choisir la version de PHP PUIS la webinterface et
  en dernier le serveur web si le choix de la webinterface n'a pas réglé la question.

### Choix multiples
- Pour l'instant rien ne permet de faire des choix multiples
  (genre php8.4 ET 8.5, ou fpm ET cli)
- Or quand on fait du développement la cli est presque toujours nécessaire
  et parfois il faut tester sur plusieurs versions de PHP

### Debug
Les flags DEBUG_RESOLVER (resolver.py) et DEBUG_PREFERENCES (main.py)
permettent d'activer les traces de debug.

Voir le fichier de discussion avec Grok pour servir d'inspiration : libsolv_grok.txt
