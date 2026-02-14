discover voit les mises à jour et arrive à les déclencher...

Toutefois :

0) Souci dans les logs de python3 -m urpm.dbus.service --debug :

INFO:urpm.core.peer_client:Got 1 peers from local urpmd
DEBUG:urpm.core.download:P2P: peer discovery took 0.00s, found 1 peers
DEBUG:urpm.core.download:P2P: availability query took 0.00s
DEBUG:urpm.core.download:Trying server distrib-coffee (ip_mode=auto): http://distrib-coffee.ipsl.jussieu.fr/pub/linux/Mageia/distrib/10/x86_64/media/core/release/virtualbox-guest-additions-7.2.6-1.mga10.x86_64.rpm
INFO:urpm.core.download:Downloaded virtualbox-guest-additions-7.2.6-1.mga10.x86_64.rpm from distrib-coffee
DEBUG:urpm.core.download:Registered cache file: virtualbox-guest-additions-7.2.6-1.mga10.x86_64.rpm (697671 bytes)
DEBUG:urpm.core.download:Downloads completed in 1.34s
[_execute_install] PROBLEMS: [('package virtualbox-guest-additions-7.2.6-1.mga10.x86_64 is already installed', (2, None, 3437))]

============================================================
!ﾠ  ALERTE URPM: Échec d'installation détecté!
============================================================
  x ('package virtualbox-guest-additions-7.2.6-1.mga10.x86_64 is already installed', (2, None, 3437))

Les paquets concernés n'ont PAS été installés.
Relancez l'installation après vérification.
============================================================

=> vu que le mise à jour se fait il y a un souci de cohérence / logs

1) il manque au système appstream :

/usr/share/metainfo/mageia.metainfo.xml

<?xml version="1.0" encoding="UTF-8"?>
<component type="operating-system">
  <id>org.mageia.mageia</id>
  <name>Mageia</name>
  <summary>Mageia Linux Distribution</summary>
</component>

2) il manque une fonction backend :

PackageKitBackend: Error fetching updates: PackageKit::Transaction::ErrorInternalError "GetUpdateDetail not supported by backend"

(vu dans logs de discover lors d'un update)

3) malgré le /usr/share/metainfo/mageia.metainfo.xm discover ne voit rien et appstreamcli refresh-cache --force ne donne rien

Il manque quoi ?

4) 


---

## Status (2026-02-13)

- **Point 0** : RÉSOLU - filtrage des erreurs "already installed" bénignes lors des upgrades
- **Point 1** : RÉSOLU - `data/mageia.metainfo.xml` créé
- **Point 2** : RÉSOLU - `pk_backend_get_update_detail` implémenté
- **Point 3** : Dépend du point 1, devrait être OK maintenant
