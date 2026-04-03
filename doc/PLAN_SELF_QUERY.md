# Plan : éliminer le self-query urpmd et nettoyer le réseau conteneur

## Problème

urpmd sur l'hôte s'interroge lui-même via HTTP (`GET /api/peers`, `POST /api/have`
sur 127.0.0.1) — trafic inutile. La cause racine est `peer_client.py` qui ajoute
`127.0.0.1` en dur comme premier peer, et `--network=host` sur les conteneurs de
build qui rend ambiguë la distinction self/other.

## Décision de design

Passer les conteneurs de build en **réseau isolé** (défaut podman) au lieu de
`--network=host`. Conséquences :

| Capacité                     | Avec `--network=host` | Réseau isolé |
|------------------------------|-----------------------|--------------|
| Tirer des paquets du hôte    | Oui (127.0.0.1)      | Oui (`host.containers.internal`) |
| Tirer des paquets des peers  | Oui (broadcast)       | Oui (NAT sortant, via liste du hôte) |
| Servir aux peers             | Oui                   | Non (pas de route entrante) |
| Discovery UDP broadcast      | Oui                   | Non (inutile pour un build) |
| Conflits de port             | Oui (problème)        | Non |
| Ambiguïté 127.0.0.1         | Oui (problème)        | Non |

Pour un conteneur de build éphémère, servir et broadcaster est inutile.

## Étapes

### 1. `build.py` : retirer `network='host'`

Fichier : `urpm/cli/commands/build.py`, lignes ~926-933.

Retirer `network='host'` du `container.run()`. Le conteneur utilisera le réseau
isolé par défaut (slirp4netns ou pasta selon podman).

### 2. `peer_client.py` : ne plus s'auto-ajouter comme peer sur l'hôte

Fichier : `urpm/core/peer_client.py`, `_query_local_urpmd()`.

Actuellement ligne 175 :
```python
peers = [Peer(host='127.0.0.1', port=port)]
```

Remplacer par : ne pas inclure 127.0.0.1 dans la liste des peers quand on EST
urpmd (contexte scheduler). Le CLI et rpmdrake continuent d'interroger 127.0.0.1
normalement.

Option : ajouter un paramètre `exclude_self=False` au constructeur de
`PeerClient`, que le scheduler passe à `True`.

### 3. `peer_client.py` : s'assurer que `_try_container_host()` est utilisé

Le fallback `host.containers.internal` / `host.docker.internal` existe déjà.
Vérifier qu'il est bien appelé quand `_query_local_urpmd()` échoue (ce qui
arrivera si urpmd n'écoute pas sur 127.0.0.1 vu du conteneur en réseau isolé —
en fait il écoute, mais c'est le urpmd du conteneur, pas celui de l'hôte).

Point d'attention : en réseau isolé, 127.0.0.1 dans le conteneur = le urpmd du
conteneur lui-même. Il faut que le conteneur distingue "mon propre urpmd" de
"celui de l'hôte". Solution : `_try_container_host()` doit être essayé EN PLUS
de (ou à la place de) `_query_local_urpmd()`.

### 4. Vérifier le flux complet conteneur de build

Scénario à valider :
1. `urpm build` lance un conteneur en réseau isolé
2. Le conteneur démarre urpmd sur 127.0.0.1:9876 (son propre namespace)
3. Le peer_client du conteneur contacte `host.containers.internal:9876` → urpmd hôte
4. L'hôte retourne sa liste de peers LAN
5. Le conteneur contacte les peers directement par IP (NAT sortant)
6. Téléchargement des paquets OK

### 5. `mkimage` : vérifier qu'il n'est pas impacté

`mkimage` travaille en chroot sans conteneur réseau — pas impacté a priori.

## Risques

- **`host.containers.internal` pas disponible partout** : dépend de la version
  de podman. Tester sur Mageia 10. Fallback possible sur l'IP gateway du
  conteneur (`ip route | grep default`).
- **Performance NAT** : négligeable pour du téléchargement de RPMs.
- **Conteneur sans urpmd** : si le conteneur ne lance pas urpmd, le CLI utilise
  directement `peer_client` → doit pouvoir atteindre le hôte. À tester.
