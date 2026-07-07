# Contribuer à urpm-ng

urpm-ng est un petit (espérons qu'il grandira bien) projet bénévole. Une poignée de mainteneurs, un petit groupe de testeurs réguliers, et beaucoup à faire. Si tu utilises Mageia et que quelque chose ici t'attire, on serait content d'avoir un petit peu d'aide — même un « j'ai essayé, ça n'a pas marché en faisant ça » de cinq minutes vaut bien plus que tu ne l'imagines.

Ce document est là pour montrer *comment* tu peux aider (et rendre ça plus facile), quel que soit ton niveau d'engagement. Aucune nécessité ici que tu aies déjà patché un outil de distribution.

## Comment tu peux aider

Cinq voies, du plus léger au plus lourd. Choisis celle qui correspond au temps que tu as — aucune n'est de seconde zone.

### 1. Tester et nous dire ce qui se passe (bien et mal)

La chose la plus utile qu'un nouveau venu puisse faire c'est tester : installe urpm-ng sur ta machine (suis la section *Installation* du [`README.md`](README.md) pour les instructions RPM actuelles), utilise-le quelques jours pour ce que tu fais d'habitude avec ``urpmi``, et signale tout ce qui t'a surpris — un plantage, un message erroné, une traduction manquante, quelque chose de pénible, de répétitif, ou de pas naturel.

- Où signaler : **issues GitHub** sur <https://github.com/pvi-github/urpm-ng/issues>.
- Merci d'inclure, au minimum :
  - La version Mageia (``cat /etc/mageia-release``).
  - L'architecture (``uname -m``).
  - La version d'urpm-ng (``urpm --version`` — et ``rpm -q urpm-ng-core`` pour confirmer quel RPM est installé et s'il s'agit de celui du système).
  - La ligne de commande exacte qui a entrainé le souci, ce que tu as obtenu et ce que tu attendais.
- Pas besoin d'attacher des logs sauf demande de notre part.

### 2. Traduire — ou nettoyer les traductions existantes

Six langues sont déjà traduites (fr / de / es / it / nl / pt). La couverture est large mais pas complète : des chaînes passent en anglais, et certaines msgstr sonnent maladroites (voir mal traduites), et une oreille native pourra nous aider à traquer les faux amis ou les erreurs "idiomatiques" qu'on a forcément commises (et elles sont peut être nombreuses). Si l'une de ces langues est ta langue maternelle, une passe sur les traductions existantes pour affiner la formulation et adopter les tournures idiomatiques locales serait plsu que bienvenue.

- Les chaînes se trouvent dans les fichiers ``.po`` sous [`po/`](po/) ; ouvre-les avec l'éditeur de ton choix (poedit convient).
- Les entrées vides ou ``fuzzy`` sont des chaînes nouvelles ou possiblement approximatives — le plus facile pour commencer.
- Lance ``msgfmt --check-format po/<lang>.po -o /dev/null`` — si ça passe, le build passera aussi.
- Idem pour la doc : les canoniques ``README.md`` / ``MIGRATION.md`` / ``CHANGELOG.md`` ont des versions par langue (``README_fr.md`` etc.) qui auraient aussi besoin de relectures natives.

### 3. Améliorer la documentation

Pages de manuel, README, aide-mémoire de migration, changelog — tout ce qui est rédigé. 
Même la correction d'une coquille est utile. Les pages man se trouvent dans ``man/<lang>/man1/urpm.1`` ; valide les avec ``groff -man -Tutf8 -ww man/<lang>/man1/urpm.1``.

### 4. Corriger un bug ou ajouter une petite fonctionnalité

Le backlog se trouve à deux endroits :

- [`TODO.md`](TODO.md) à la racine du repo — la liste visible.
- Les divers fichiers ``doc/TODO_*.md`` — backlogs thématiques et notes par sujet. Certains sont prêts à coder, d'autres demandent d'abord réflexion et discussion. Demande avant d'investir un week-end complet (c'est plus prudent).

Lis la suite pour le workflow build / test / patch.

### 5. Travailler sur la machinerie (le plus costaud)

Refactoring, travail sur le résolveur, jobs en arrière-plan d'``urpmd``, build de spec-file, durcissement mkimage / conteneurs de build. C'est là qu'on travaille la feuille de route technique du projet.
Fais nous un coucou d'abord — se coordonner évite de se marcher sur les pieds, ou de se faire marcher dessus. Et on ne mord pas promis.

## Récupérer les sources et builder

Deux chemins de build. Le **simple** utilise ``bm`` (le wrapper ``build-mageia``) sur ta machine et ne demande au départ que ``urpmi``. Le **reproductible** utilise ``urpm build`` dans un conteneur et demande qu'urpm-ng soit déjà installé.

### Dépendances de bootstrap (une seule fois)

Sur une Mageia fraîche, ``urpmi`` est présent mais ``sudo`` peut ne pas être configuré — le classique ``su -c`` fonctionne partout :

```sh
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng

# L'outil de build (bm) plus chaque BuildRequires que le spec déclare.
# --buildrequires lit le spec directement, donc la liste reste synchro
# automatiquement. bm lui-même n'est pas dans les BuildRequires du spec
# (il invoque rpmbuild plutôt que d'être consommé par %build), d'où les
# deux commandes.
su -c "urpmi bm && urpmi --buildrequires rpmbuild/SPECS/urpm-ng.spec"
```

### Chemin simple — Avec ``bm`` sur l'hôte

```sh
make rpm-all
make install-all
```

``make install-all`` choisit la bonne commande d'install à ta
place :

- Sur une machine sans urpm-ng, il fait ``urpmi urpm-ng-core``
  d'abord, puis ``urpm i`` sur les méta-paquets pour que le
  résolveur récupère les sous-paquets voisins.
- Sur une install existante, il passe direct par ``urpm i
  --auto --reinstall`` sur les méta.

Un seul ``su -c'' donc un seul prompt de mot de passe.  Variantes
plus étroites quand tu ne veux pas toute la stack :

- ``make install-core`` — juste ``urpm-ng-core``
- ``make install``      — le méta ``urpm-ng`` (core + daemon),
  sans backend GUI ni rpmdrake-ng

### Chemin reproductible — build en conteneur

Cette méthode n'est utilisable seulement une fois urpm-ng installé sur l'hôte (poule-et-œuf à la toute première install). Elle garantit un build "sain" (et permet aussi de faire très facilement des builds pour d'autres versions de mageia ou d'autres architectures).

```sh
# A faire une seule fois : créer l'image de build (exemple mga10 sur x86_64)
# le tag est le nom par lequel on "appellera" l'image pour les builds.
# on peut fabriquer plusieurs images si on veut builder pour plusieurs releases 
#     de mageia et/ou plusieurs architectures avec un seul environnement de travail.
su -c "urpm image make --release 10 --tag mga10-64"

# Ensuite, à chaque build — on buiold 'un coup les deux specs (urpm-ng et rpmdrake-ng)
urpm build --image mga10-64 rpmbuild/SPECS/urpm-ng.spec \
                            rpmdrake/rpmbuild/SPECS/rpmdrake-ng.spec

# Installer les RPM tout juste construits — même helper que le chemin simple.
make install-all
```

### Lancer les tests

```sh
# Attention le pytest complet prend longtemps, entre une demi-heure et une heure.
pytest urpm/tests/
```

Voir [`doc/TESTING.md`](doc/TESTING.md) pour un aide-mémoire pytest et un topo sur les trous de couverture connus.

Pour itérer en mode dev sans rebuilder un RPM à chaque fois, les fichiers sources s'exécutent directement depuis le checkout — ``python -m urpm.cli.main <sous-commande>`` fonctionne à condition que le ``$PYTHONPATH`` contienne la racine du checkout.

## Première contribution — le circuit complet

1. **Crée ta branche.** Depuis la branche version active (actuellement ``0.8.x`` — vérifie le fichier ``VERSION`` à la racine du repo en cas de doute). ``main`` porte uniquement l'historique publié ; aucun nouveau travail n'y atterrit directement, il y arrive par fast-forward-merge depuis la branche version au moment de la release.
2. **Modifie.** Écris le fix ou la fonctionnalité. Si tu touches au résolveur, à la file de transactions ou à ``urpmd``, ajouter un test dans ``urpm/tests/`` est quasi obligatoire. Pour de la doc, un test manuel sur ta machine peut suffire.
3. **Teste en local.** Lance ``pytest urpm/tests/`` (suite complète pour tout ce qui est user-visible, fichier ciblé sinon). Corrige toute régression avant de continuer.
4. **Mets à jour la partie visible** si ton changement est user-facing (un fix sur un chemin de code interne n'en a rarement besoin) :
   - mets à jour les catalogues ``.po`` (toute nouvelle chaîne anglaise user-facing est un nouveau msgid) ;
   - mets à jour ``man/<lang>/man1/urpm.1`` si un flag a été ajouté, renommé ou retiré ;
   - mets à jour le README / l'aide-mémoire MIGRATION si le changement affecte les commandes du quotidien.
5. **Commit.** Sujet court (~50 caractères), préfixe conventional (``fix(zone):``, ``feat(zone):``, ``docs:``, ``chore:``, ``test:``, ``refactor:``). Le corps explique le *pourquoi* — le diff montre déjà le *quoi*.

Avant d'ouvrir une pull request, passe cette checklist :

- [ ] ``make rpm-all`` (ou le build conteneur) réussit.
- [ ] ``pytest urpm/tests/`` passe sans régression.
- [ ] Tu as **installé tes RPM buildés en local** et testé depuis cette copie installée (augmente la ligne ``release`` dans ``rpmbuild/SPECS/urpm-ng.spec`` en local pour que le numéro de RPM soit supérieur à celui du système et s'installe proprement par-dessus — commodité locale uniquement, ne jamais committer ce bump).
- [ ] Les commandes évidentes marchent encore sur le build installé, sans que ton changement n'en casse aucune :
  - ``urpm i <unpaquet>`` — chemin d'installation
  - ``urpm q <unpaquet>`` — requête
  - ``urpm e <unpaquet>`` — erase
  - ``urpm f /chemin/vers/fichier`` — find
  - ``urpm m u`` — media update
  - ``urpm u`` — upgrade système
- [ ] Ta branche est **rebasée** sur la branche cible (pas de commit de merge entre ton travail et la pointe).
- [ ] Doc / pages man / traductions mises à jour comme au point 4.

6. **Push** sur ton fork ou ta branche.
7. **Ouvre une pull request** sur GitHub. Décris l'intention, la couverture de tests, et toute limitation connue. Confirme bien la checklist ci-dessus.
8. **Itère sur la review.** Un reviewer va regarder ton diff et poser des questions, identifier des problèmes ou demander des ajustements. On essaye de faire des retours bienveillants, mais il peut arriver qu'on se rate. Sache qu'il n'y a aucune mauvaise intention et que nos retours se font avec l'intérêt du projet et de Mageia comme boussole.

## Où nous joindre

- **Issues & PR** : <https://github.com/pvi-github/urpm-ng>
- **Contact direct — Matrix** : [@maat_:matrix.org](https://matrix.to/#/@maat_:matrix.org)

## Comment est organisé le code

```
urpm/                  # Source Python
  cli/                 # Interface en ligne de commande (urpm, sous-commandes)
  core/                # Résolveur, download, install, base, sync
  daemon/              # urpmd (service en arrière-plan, P2P LAN)
  genmedia/            # Génération des métadonnées côté serveur
  tests/               # Tous les tests vivent ici (pas dans un tests/ racine)
rpmdrake/              # Front-end GUI Qt6 (rpmdrake-ng)
pk-backend-urpm/       # Plugin C : backend PackageKit sur urpm-ng
man/<lang>/man1/       # Pages man traduites
po/                    # Catalogues de traduction (.po)
doc/                   # Docs de design, plans, TODO, specs
rpmbuild/SPECS/        # Empaquetage Mageia (.spec)
data/                  # Units systemd, règles polkit, modèles de config
```

Pour une cartographie plus profonde, voir [`doc/ARCHITECTURE.md`](doc/ARCHITECTURE.md). Pour le catalogue cumulé des fonctionnalités, [`README.fr.md`](README.fr.md) ou [`FEATURES.md`](FEATURES.md).

## Règles de style (court)

- **Anglais** dans le code, les commentaires, et les messages de commit. Un historique multilingue est déroutant. Donc une seule langue et pour tous se comprendre on a choisi l'anglais?
- **Docstrings** sur toute fonction ou classe publique. Une ligne suffit ; explique le *pourquoi* uniquement quand ce n'est pas évident depuis le nom.
- **Tests** quand c'est possible — la suite est un filet anti-régression, pas une preuve formelle. Les changements visibles des utilisateurs devraient au minimum embarquer une note de test manuel.
- **Commentaires** là où le code cache une subtilité (contournement même s'il faut éviter, race, invariant). On s'interdit aussi les commentaire qui dupliquent le code.

## Cycle de release

Le travail se fait sur une branche version (``0.8.x``, ``0.9.x``, …). Quand une version est prête, la branche est fast-forward-mergée dans ``main`` ; ``main`` porte donc l'historique publié. Les tags sont posés depuis ``main`` ou depuis la branche de release à un moment où les deux s'alignent. A ce moment-là et les RPM sont publiés avec la release.

Les bumps de version dans ``VERSION`` / ``pyproject.toml`` / ``rpmbuild/SPECS/urpm-ng.spec`` sont l'affaire du mainteneur — ne commite pas un bump de VERSION ou de RELEASE dans ta contribution. Ceci dit, sens-toi libre d'augmenter **localement** la ligne ``release`` du spec pour que ton RPM buildé s'installe par-dessus celui du système ; simplement, ne stage pas cette ligne.
