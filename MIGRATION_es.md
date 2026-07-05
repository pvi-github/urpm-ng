# Migrar de urpmi a urpm-ng

Una referencia de una página para los usuarios de Mageia habituados
al conjunto de herramientas ``urpmi`` clásico.  ``urpm-ng`` reemplaza
el conjunto ``urpmi`` / ``urpme`` / ``urpmq`` / ``urpmf`` /
``urpmi.addmedia`` / ``urpmi.removemedia`` / ``urpmi.update`` con un
único binario ``urpm`` y sus subcomandos.

Cada subcomando tiene un alias corto de una letra — esta chuleta usa
las formas cortas porque es lo que se escribe a diario; las formas
largas (``install``, ``erase``, ``upgrade``, …) funcionan igual y son
más legibles en los scripts.

Léelo una vez; tenlo a mano cuando ayudes a otro usuario a migrar.

Los parámetros a proporcionar se anotan entre ``<paréntesis
angulares>``.

## Operaciones sobre paquetes

| ``urpmi`` / ``urpme``                | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``urpmi <pkg>``                      | ``urpm i <pkg>``             |
| ``urpmi --auto <pkg>``               | ``urpm i -y <pkg>``          |
| ``urpmi --test <pkg>``               | ``urpm i --test <pkg>``      |
| ``urpme <pkg>``                      | ``urpm e <pkg>``             |
| ``urpmi --auto-update``              | ``urpm u``                   |
| ``urpmi --no-install <pkg>``         | ``urpm dl <pkg>``            |

Notas :
- ``--auto`` y ``-y`` son intercambiables en todo ``urpm-ng``.
- ``urpm remove`` se acepta como comodidad para los usuarios que
  vienen de apt / dnf — el verbo canónico es ``e`` (``erase``).

## Gestión de medios

| ``urpmi.*`` / ``urpmq``              | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``urpmi.update -a``                  | ``urpm m u``                 |
| ``urpmi.update <medianame>``         | ``urpm m u <medianame>``     |
| ``urpmi.addmedia <url>``             | ``urpm m a <url>``           |
| ``urpmi.addmedia --distrib <url>``   | ``urpm m disc <url>``        |
| ``urpmi.removemedia <medianame>``    | ``urpm m r <medianame>``     |
| ``urpmi.removemedia -a``             | ``urpm m r --all``           |
| ``urpmq --list-media``               | ``urpm m l``                 |

Notas :
- ``m`` es el alias corto de ``media``.  ``m u`` = ``media update``,
  ``m a`` = ``media add``, ``m r`` = ``media remove``, ``m l`` =
  ``media list``, ``m disc`` = ``media discover``.  Escribir la
  forma completa ``urpm media update`` etc. funciona exactamente
  igual.

## Consultas

| ``urpmq`` / ``urpmf``                | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``urpmq <pkg>``                      | ``urpm q <pkg>``             |
| ``urpmq -i <pkg>``                   | ``urpm sh <pkg>``            |
| ``urpmq -d <pkg>``                   | ``urpm d <pkg>``             |
| ``urpmq -R <pkg>``                   | ``urpm rd <pkg>``            |
| ``urpmf --provides <pkg>``           | ``urpm wp <pkg>``            |
| ``urpmf --whatrequires <pkg>``       | ``urpm wr <pkg>``            |
| ``urpmf --files <path>``             | ``urpm f <path>``            |
| ``urpmq --list-orphans``             | ``urpm l --orphans``         |

Notas :
- Alias cortos : ``q`` = ``query`` (también ``search``, ``s``),
  ``sh`` = ``show``, ``d`` = ``depends``, ``rd`` = ``rdepends``
  (también ``whatrequires``, ``wr``), ``wp`` = ``whatprovides``,
  ``f`` = ``find``, ``l`` = ``list``.

## Compilación / distribución

| Mageia clásico                       | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``genhdlist2 <tree>``                | ``urpm genmedia <tree>``     |
| ``rpmbuild...`` ``bm -b <spec>``     | ``urpm build <spec>``        |
| ``mach``, ``mock``, ...              | ``urpm image make`` + ...    |
|                                      | ... ``urpm build --image``   |

## Diferencias de comportamiento a tener en cuenta

- **Un solo binario, subcomandos.**  Todas las operaciones viven
  bajo ``urpm``.  La autocompletación de Bash se instala por defecto.
- **``urpm.cfg`` reemplaza a ``urpmi.cfg``** en
  ``/etc/urpm/urpm.cfg``.  En la primera ejecución, ``urpm m
  import`` lee el antiguo ``/etc/urpmi/urpmi.cfg`` y migra cada
  entrada, incluyendo las basadas en ``MIRRORLIST`` — ninguna
  edición manual es necesaria.
- **Rollback nativo.**  ``urpm h`` (history) y ``urpm rollback``
  cubren cada transacción — no se necesitan herramientas de snapshot
  de terceros.
- **Caché P2P LAN.**  Si ``urpmd`` corre en varias máquinas de la
  misma LAN, comparten automáticamente los paquetes descargados.
  No hace falta configuración.
- **Soporte de contenedor / imagen de compilación.**  ``urpm image
  make`` construye una imagen de chroot / contenedor Mageia mínima
  lista para ``urpm build`` — se acabaron los apaños ``mach`` /
  ``mock``.
- **Códigos de salida estructurados** — véase ``urpm(1)``
  ``EXIT CODES``.  Los más comunes coinciden con urpmi (0 = éxito,
  distinto de cero = algo que mirar).

## Inicio rápido tras la instalación (si no se instala como RPM)

```sh
# Importar los medios que ya tenías bajo urpmi
sudo urpm m import

# Adjuntar espejos a los medios basados en mirrorlist recién importados
sudo urpm srv autoconfig

# Refrescar las listas de paquetes
sudo urpm m u

# Estás listo
urpm q firefox
sudo urpm i firefox
```

## Documentación completa

- ``urpm --help`` (también ``urpm <subcomando> --help``)
- ``man urpm``
- [README.md](README.md) — presentación de instalación y funcionalidades
- [CHANGELOG.md](CHANGELOG.md) — historial versión por versión
