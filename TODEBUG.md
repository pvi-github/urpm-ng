
# Gestion des peers souci de module parfois

./bin/urpm peer list
Error: module 'urpm.cli.colors' has no attribute 'warn'

# Souci à l'upgrade a corriger

Pour les upgrades quand y'a des orphans à enlever faut quand même attendre la mise à jour de la rpmdb sinon ça fait.
=> petit souci de cohérence => faut mettre les erase en queue.

Et l'affichage des erreurs en JSON c'est moche (P'tet a prevoir en mode batch mais pas en mode live)

# Dépendances

./bin/urpm depends phpmyadmin
Pourquoi y'a rien ?

Je subodore que c'est parce qu'il ne liste que des requires qui ne sont pas des paquets mais des provides et que c'est pas géré.

=> Sans doute lié directement ou indirectement au souci de résolution de dépendances.
=> Il faudra trouver une idée pour afficher les alternatives intelligemment (couleurs ?) et les "blocs de dépendances croisées" sans boucler comme des idiots.

# Alternatives et --prefer

## État actuel (2025-12-23)
L'option --prefer est fonctionnelle avec :
- Contraintes de version : `--prefer=php:8.4`
- Préférences positives : `--prefer=apache,php-fpm`
- Préférences négatives : `--prefer=-apache-mod_php`
- Combinaisons : `--prefer=php:8.4,apache,php-fpm,-apache-mod_php`

La sélection se fait maintenant sur les REQUIRES/PROVIDES, pas les noms de paquets.

## Améliorations à prévoir

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

## Historique
Voir le fichier de discussion avec Grok pour servir d'inspiration : libsolv_grok.txt

# Pre-downloading

=> Pas encore vu passer un seul predownload... on est surs que ça marche ça ?

Et idées complémentaires à discuter : 
  - s'il y a plusieurs peers => election d'un master qui va répartit les tâches de prédownloads ?
  - il faudrait p'tet aussi faire en sorte qu'un paquet prédownloadé soit sur au moins deux peers au cas où un soit éteint ? Ou alors détecter quand un peer pass offline et récupérer upstream les prédownloads qui étaient chez lui ? 

En fait je suis pas tout à fait sur de quoi et comment, d'où le besoin de discussion, mais l'idée c'est:
  - 1 de ne pas faire 50 prédownloads de chaque fichier s'il y a 50 peers
  - si un peer est éteint de ne pas se retrouver à tous les 50 peers devoir télécharger le complément sur le miroir upstream...

# ... and xxx more

Dans urpm i mais aussi urpm h -d, urpm depends et à plein d'autres endroits  on a des listres traonquées.

Il faut pouvoir afficher les listes complètes si on veut.

Et je verrais bien afficher sur plusieurs colonnes... en fonction de la longueur du plus long nom de paquet et de la largeur du terminal pour que ça fasse pas moche.

# Fix dep

Quand python3-solv ou python3-zstandard ne sont pas installés il faudra un mécanisme dégradé pour proposer de les installer soit en DL direct soit via urpmi

python3-solv manque : voullez vous l'installer
1- directement
2 via urpmi
[1(default)-2] o/n

(Et il faut bien penser à les mettre dans le installed-through-deps.list)

# Souci d'alignement dans urpm h

./bin/urpm h

ID | Date | Action | Status | Packages           <-- ça c'est moche
----------------------------------------------------------------------
  19 | 2025-12-20 | install  | complete    | colorprompt,git-prompt
  18 | 2025-12-20 | install  | complete    | git,qgit (+4 deps)
  17 | 2025-12-20 | autoremove | complete    |                                   <-- ça c'est moche 
  16 | 2025-12-20 | upgrade  | complete    | fuse-common,fuse3,gpsd,gvfs... (+26 deps)
  15 | 2025-12-18 | upgrade  | complete    | lib64decor0,libdecor
  14 | 2025-12-18 | install  | complete    | task-plasma (+380 deps)
  13 | 2025-12-18 | install  | complete    | freecad (+290 deps)
  12 | 2025-12-18 | erase    | complete    | task-plasma (+440 deps)
  11 | 2025-12-18 | install  | complete    | task-plasma (+440 deps)
  10 | 2025-12-18 | erase    | complete    | task-plasma (+440 deps)
   9 | 2025-12-18 | install  | complete    | task-plasma (+440 deps)
   8 | 2025-12-18 | install  | complete    | task-plasma (+440 deps)
   7 | 2025-12-18 | erase    | complete    | task-plasma (+440 deps)
   6 | 2025-12-18 | upgrade  | complete    | cpupower,kernel-desktop-6.1... (+5 deps)
   5 | 2025-12-18 | install  | complete    | task-plasma (+440 deps)
   4 | 2025-12-18 | erase    | complete    | task-plasma (+440 deps)
   3 | 2025-12-18 | install  | complete    | task-plasma (+440 deps)
   2 | 2025-12-18 | erase    | complete    | task-plasma (+440 deps)
   1 | 2025-12-18 | install  | complete    | task-plasma (+461 deps)

La colonne Action est pas assez large et les titres c'est n'importe quoi.

# Gestion du cache

  - [x] Nettoyage cache basique (fichiers > 30 jours)

=> ça c'est une connerie que j'ai jamais demandé y'a plein de gens qui ne font pas les updates tous les mois

il faut qu'on implémente les quotas par media et global.
Si on arrive à la taille max du quota le scheduler nettoie les fichiers en commençant par les plus vieux et surtout s'ils ont été installés.

# Aliases

- faire des aliases : 
  - urpm requires = urpm depends 
  - urpm whatrequires = urpm rdepends

- ajouter :
  - urpm suggests
  - urpm whatsuggests
  - urpm recommends
  - urpm whatrecommends

# More / contraines de version

Pouvoir sur un whatprovides de préciser les contraintes de version (== < <= > <= ) pour filtrer


# Packaging php-webinterface (à remonter aux packageurs Mageia)

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

Note : ce n'est PAS un problème de l'algo de résolution d'urpm. L'algo ne doit pas "tricher" en ignorant des dépendances requises.

# README

Mettre à jour le README pour les nouvelles fonctionnalités (gestion des peers & blacklist de peers)

Corriger le README pour les blacklists de paquets => normalement ce ssont des paquets qu'on ne déinstalle pas (et vérifier que le code suit bien cette règle)


