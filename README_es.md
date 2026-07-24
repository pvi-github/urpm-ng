# urpm-ng

Un gestor de paquetes moderno para Mageia Linux, escrito en Python.

urpm-ng es una reescritura completa del conjunto clásico urpmi, que ofrece mejor rendimiento, una resolución de dependencias más fina y funcionalidades modernas como el uso compartido P2P de paquetes.

## Requisitos previos

### Distribución

Por ahora necesitas Mageia 9 o Mageia 10.

### Puertos del cortafuegos (para el uso compartido P2P)

El paquete `urpm-ng-daemon` incluye `/etc/shorewall/rules.urpm-ng`
como fichero de include, y su `%post` lo engancha automáticamente a
`/etc/shorewall/rules`. En una máquina gestionada por Shorewall (el
valor por defecto en Mageia) los puertos siguientes quedan por tanto
abiertos justo después de la instalación, sin necesidad de tocar nada:

- **TCP 9876** (producción) o **TCP 9877** (modo dev) -- API HTTP de urpmd
- **UDP 9878** (producción) o **UDP 9879** (modo dev) -- Broadcasts de descubrimiento de pares

Si Shorewall no está en uso (`iptables` / `nftables` a pelo), abre
los puertos a mano — el fichero `/etc/shorewall/rules.urpm-ng` del
árbol de fuentes sirve como buena plantilla.

## Instalación

### Paquetes

urpm-ng está dividido en varios paquetes para más flexibilidad:

| Paquete | Descripción |
|---------|-------------|
| `urpm-ng-core` | Mínimo: CLI, resolvedor, base de datos |
| `urpm-ng-daemon` | Daemon en segundo plano + compartición P2P |
| `urpm-ng` | Meta: arrastra `-core` + `-daemon` (instalación estándar) |
| `urpm-ng-appstream` | Configuración de metadatos AppStream (metainfo del OS Mageia, config distro) |
| `urpm-ng-packagekit-backend` | Backend PackageKit (Discover, GNOME Software) + servicio D-Bus |
| `urpm-ng-desktop` | Meta: arrastra `-core` + `-daemon` + `-appstream` + `-packagekit-backend` |
| `urpm-ng-build` | Meta: arrastra `-core` (para `urpm image` / `urpm build` — los comandos viven en `-core`) |
| `urpm-ng-genmedia` | Generación de metadatos de medios del lado servidor (`urpm genmedia`, para mantenedores de mirror) |
| `urpm-ng-all` | Meta: arrastra todo lo anterior |

**Elige el paquete adecuado:**
- **Instalación mínima / contenedor**: `urpm-ng-core`
- **Uso CLI estándar**: `urpm-ng`
- **Escritorio con centros gráficos de software**: `urpm-ng-desktop`
- **Empaquetadores (usuarios de bm / mkimage)**: `urpm-ng-build`
- **Mantenedores de mirror que publican repositorios**: `urpm-ng-genmedia`

### Instalación / actualización rápida (`geturpm.sh`)

`geturpm.sh` es la vía recomendada para instalar urpm-ng en una Mageia
recién instalada, y también puede actualizar una instalación
existente. Detecta automáticamente la release Mageia y la
arquitectura, descarga la última urpm-ng desde el canal que elijas y
hace lo correcto tanto si urpm-ng ya está instalado como si no (las
máquinas nuevas arrancan con `urpmi`; las actualizaciones posteriores
pasan por urpm-ng mismo).

**Rápido — por pipe, sin inspección local**

```bash
curl -fsSL https://raw.githubusercontent.com/pvi-github/urpm-ng/main/geturpm.sh | bash
```

Los prompts (elección del canal, «Proceed?», contraseña root para
`su`) se leen desde `/dev/tty`, así que la versión por pipe es
totalmente interactiva — misma experiencia que ejecutar el script
desde un fichero.

**Verificado — descargar, leer, después ejecutar** (recomendado si
todavía no confías en la fuente):

```bash
curl -fsSLO https://raw.githubusercontent.com/pvi-github/urpm-ng/main/geturpm.sh
less geturpm.sh                  # inspeccionar antes de ejecutar
bash geturpm.sh                  # interactivo: pide canal y confirmación
```

**Selección de canal** (`--channel=CHAN`):

- `mgabiz` — descarga desde el repo del proyecto Mageia.biz (por
  defecto cuando no hay terminal disponible). Usa `urpm media
  discover` sobre el mirror mgabiz, por lo que las actualizaciones
  futuras pasan por el flujo estándar `urpm media update`.
- `github` — descarga los RPM directamente desde la página de
  releases de GitHub. Útil para probar una etiqueta concreta, o
  cuando la publicación en mgabiz va con retraso respecto a una
  release.

**Ejecuciones desatendidas** — añadir `-y` (saltar la confirmación
«Proceed?») y `--channel=CHAN` (saltar el prompt del canal) mediante
`bash -s --`:

```bash
curl -fsSL <url>/geturpm.sh | bash -s -- -y --channel=mgabiz
```

Nota: en la primera instalación, urpm-ng importa automáticamente su
configuración desde los ficheros `urpmi.cfg` y `urpmi/skip.list`
existentes.

## Primer arranque

urpm funciona tal cual. Las opciones avanzadas (blacklist, redlist, kernel-keep) están documentadas más abajo, en la sección **Configuración**.

Cuando se instala a nivel de sistema (en `/usr/bin/`), urpm usa:
- Base de datos: `/var/lib/urpm/packages.db`
- Puerto del daemon: 9876
- Fichero PID: `/run/urpmd.pid`

### Fuentes de medios

En una instalación hecha por la vía RPM (o vía `geturpm.sh`), los
medios Mageia estándar y los servidores desde los que descargarlos se
configuran automáticamente: `urpm-ng` importa el `urpmi.cfg`
existente en el primer arranque y `urpm server autoconfig` puebla el
pool de mirrors desde la API de mirrors Mageia. No hace falta nada
más para instalar paquetes.

En una máquina sin `urpmi.cfg` previo (chroot nuevo, build de imagen,
o un sistema que nunca tuvo urpmi), el mismo bootstrap se hace en una
única pasada manual:

```bash
urpm media list                       # ¿Nada aún? bootstrap:
urpm media import                     # Lee /etc/urpmi/urpmi.cfg por defecto; no-op si no existe
urpm server autoconfig                # Trae los mirrors desde la API de Mageia
urpm media update                     # Primera sincronización de metadatos
```

Para añadir un **repositorio comunitario** (MageiaLinux-Online,
mageia.biz, blogdrake, un mirror interno, ...), usa `urpm media
discover` — lee el `media.cfg` del repo y añade todos los medios que
anuncia de una sola vez:

```bash
urpm media discover https://www.mageia.biz/repo/Mageia/mgabiz/10/x86_64/media/
urpm media discover --dry-run https://download.mageialinux-online.org/...   # Previsualización
```

`urpm media add` queda reservado para un único medio custom no
compatible con discover — es decir, uno que sabes que no se publica
mediante un `media.cfg`. Mira la sección **Gestión de medios** más
abajo para la sintaxis.

---

# urpm - Interfaz de línea de comandos

## Opciones globales

Estas opciones se aplican a la mayoría de comandos y se colocan antes del subcomando:

```bash
-V, --version              # Mostrar la versión de urpm
-v, --verbose              # Salida verbosa
-q, --quiet                # Salida silenciosa
--nocolor                  # Desactivar la salida en colores
--root DIR                 # Usar DIR como raíz para la instalación RPM (chroot, config urpm desde el host)
--urpm-root DIR            # Usar DIR como raíz para la config urpm Y la instalación RPM
```

Los siguientes padres son heredados por los comandos transaccionales y de consulta (`install`, `upgrade`, `erase`, `download`, `depends`, …):

```bash
--arch ARCH                # Arquitectura objetivo (por defecto: sistema actual)
--debug COMPONENT          # Activar salida de debug: solver, tsrun, orphans, download, timing, all
--watched PACKAGES         # Nombres de paquetes separados por comas a vigilar durante la resolución
```

Nota: `--arch` (opción padre, fija la arquitectura objetivo de la operación) es distinto de `--allow-arch` (opción local en install/upgrade/download, permite arquitecturas adicionales junto a la del sistema — típicamente `i686` para wine/steam en x86_64).

## Opciones de visualización

La mayoría de comandos admiten estas opciones de salida:

```bash
--show-all            # Mostrar todos los elementos sin truncar
--flat                # Un elemento por línea (parseable por scripts)
--json                # Salida JSON (para uso programático)
```

Por defecto, las listas largas se muestran en varias columnas y se truncan a 10 líneas con "... y N más". Usa `--show-all` para verlo todo.

Ejemplos:
```bash
urpm list installed --flat          # Un paquete por línea
urpm search firefox --json          # Salida JSON
urpm i task-plasma --show-all       # Mostrar todas las dependencias
```

## Transacciones atómicas vs best-effort

Desde 0.7.9, `urpm upgrade` corre en modo **best-effort** por defecto: los paquetes cuyas dependencias no pueden satisfacerse se retiran de la transacción y se reportan al final con su motivo (dependencia ausente, mismatch de versión, cascada SRPM hermana, …). La transacción se aplica para todo lo demás. Pasa `--atomic` para cambiar a modo estricto (recomendado en servidores): cualquier paquete no resoluble aborta toda la transacción.

`urpm install`, en cambio, es **atómico por defecto**: si algún paquete solicitado no puede instalarse, toda la transacción se deshace. Pasa `--no-atomic` para optar por el modo best-effort en la ruta de install.

## Códigos de salida

| Código | Significado |
|--------|-------------|
| 0      | Transacción completada con éxito, ningún paquete omitido |
| 1      | Fallo duro: transacción abortada (modo atómico, red, permiso, …) |
| 2      | Transacción parcial: correcta pero al menos un paquete fue descartado (paquetes omitidos listados en stderr con su motivo) |

Comprobación scriptable para el caso parcial:

```bash
urpm upgrade --auto || [ $? -eq 2 ] && echo "ok o parcial"
```

## Gestión de paquetes

### Instalar paquetes

```bash
urpm install <paquete>        # Instalar un paquete
urpm i <paquete>              # Alias corto

# Opciones
--auto, -y                    # Modo no interactivo
--test                        # Simulación (dry run)
--without-recommends          # Omitir los paquetes recomendados
--with-suggests               # Instalar también los paquetes sugeridos
--force                       # Forzar a pesar de problemas de dependencias
--reinstall                   # Reinstalar paquetes ya instalados (reparación)
--nosignature                 # Omitir verificación GPG (no recomendado)
--noscripts                   # Omitir scripts pre/post install (builds chroot/contenedor)
--no-peers                    # Desactivar descarga P2P desde pares LAN
--only-peers                  # Descargar solo desde pares LAN, sin mirrors arriba
--no-atomic                   # Modo best-effort (por defecto en install: atómico)
--download-only               # Descargar a caché, no instalar
--nodeps                      # Omitir resolución de dependencias (con --download-only)
--all                         # Instalar todas las familias coincidentes (ej. php8.4 + php8.5)
--install-src                 # Instalar el RPM fuente (extrae spec/sources en ~/rpmbuild/)
--config-policy {keep,replace,ask}  # Política de conflicto en ficheros de config (por defecto: keep)
--prefer=<prefs>              # Guiar las elecciones de alternativas (ver más abajo)
--allow-arch <arch>           # Permitir arquitecturas adicionales (ej. i686 para wine/steam)
--sync                        # Esperar la finalización completa (triggers post-install)
```

#### Instalación guiada por preferencias

Cuando instalas paquetes con alternativas (ej. phpmyadmin, que puede usar distintas versiones de PHP y servidores web), usa `--prefer` para guiar las elecciones:

```bash
# Preferir PHP 8.4 con Apache y php-fpm, excluir mod_php
urpm i phpmyadmin --prefer=php:8.4,apache,php-fpm,-apache-mod_php

# Preferir nginx en lugar de apache
urpm i phpmyadmin --prefer=php:8.4,nginx,php-fpm
```

Sintaxis de las preferencias:
- `capability:version` — Restricción de versión (ej. `php:8.4`)
- `pattern` — Preferir paquetes que proporcionen esta capacidad (ej. `apache`, `php-fpm`)
- `-pattern` — Desfavorecer paquetes que coincidan (ej. `-apache-mod_php`)

Las preferencias trabajan sobre REQUIRES y PROVIDES de los paquetes, no sobre los nombres.

#### Filtrado por arquitectura

Por defecto, urpm solo considera los paquetes que coinciden con la arquitectura de tu sistema y `noarch`. Esto evita la instalación accidental de paquetes i686 en sistemas x86_64 cuando los medios 32-bit están activados.

Para instalar paquetes 32-bit (wine, steam, multilib):

```bash
urpm install wine --allow-arch i686
urpm install steam --allow-arch i686

# Varias arquitecturas
urpm install mipaquete --allow-arch i686 --allow-arch armv7hl
```

### Eliminar paquetes

```bash
urpm erase <paquete>          # Eliminar un paquete
urpm e <paquete>              # Alias corto

# Opciones
--auto, -y                    # Modo no interactivo
--test                        # Simulación (dry run)
--auto-orphans                # Eliminar también las dependencias huérfanas (implícito con -y salvo --keep-orphans)
--keep-orphans                # No eliminar las dependencias huérfanas
--erase-recommends            # Eliminar también los paquetes solo recomendados (no requeridos)
--keep-suggests               # Mantener los paquetes sugeridos por los paquetes restantes
--force                       # Forzar a pesar de problemas de dependencias
--debug {solver,tsrun,all}    # Activar salida de debug para el resolvedor/transacción
--sync                        # Esperar la finalización completa (triggers post-uninstall)
```

### Actualizar metadatos (al estilo apt)

```bash
urpm update                   # Actualizar los metadatos de todos los medios
urpm update "Core Release"    # Actualizar un medio concreto
```

Desde 0.7.x, `files.xml.lzma` se descarga junto con `synthesis.hdlist.cz` en cuanto el medio lo publica — no hace falta ningún flag para activarlo.

### Descargar paquetes (sin instalar)

```bash
urpm download <paquete>       # Descargar un paquete al caché
urpm dl <paquete>             # Alias corto
urpm download --only-peers pkg  # Descargar solo desde pares LAN

# Opciones
--release, -r <version>       # Release objetivo para descargas cross-release (ej. cauldron)
--buildrequires, --br [SPEC]  # Descargar dependencias de build (auto-detecta o desde .spec/.src.rpm)
--without-recommends          # Omitir los paquetes recomendados
--nodeps                      # Descargar solo los paquetes listados, sin dependencias
--no-peers / --only-peers     # Como install (política de pares)
--allow-arch <arch>           # Permitir arquitecturas adicionales
--arch <arch>                 # Heredado: arquitectura objetivo
--show-all                    # Imprimir la lista completa de paquetes resueltos
                              # (por defecto se trunca a 20 con "... y N más")
```

### Actualizar paquetes

```bash
urpm upgrade                  # Actualizar todos los paquetes
urpm u                        # Alias corto
urpm upgrade <paquete>        # Actualizar paquetes concretos

# Opciones
--auto, -y                    # Modo no interactivo
--test                        # Simulación (dry run)
--atomic                      # Modo estricto: aborta toda la transacción ante cualquier paquete no resoluble.
                              # Por defecto: best-effort (ver "Transacciones atómicas vs best-effort" más arriba).
--with-recommends             # Instalar los paquetes recomendados
--with-suggests               # Instalar también los paquetes sugeridos
--noerase-orphans             # Conservar las dependencias huérfanas (no eliminarlas)
--download-only               # Descargar al caché sin aplicar la actualización
--nosignature                 # Omitir verificación GPG (no recomendado)
--no-peers / --only-peers     # Desactivar / restringir a pares LAN
--force                       # Forzar la actualización a pesar de problemas de dependencias
--config-policy {keep,replace,ask}  # Política de conflicto de config (por defecto: keep)
--allow-arch <arch>           # Permitir arquitecturas adicionales (ej. i686)
--sync                        # Esperar la finalización completa (triggers post-install)
```

### Auto-retirada de huérfanos

```bash
urpm autoremove               # Retirar las dependencias sin usar (por defecto: --orphans)
urpm ar                       # Alias corto

# Selectores
--orphans, -o                 # Paquetes huérfanos (por defecto)
--kernels, -k                 # Kernels viejos
--faildeps, -f                # Deps de transacciones interrumpidas
--buildrequires, -b           # Dependencias de build (--builddeps, --br)
--all, -a                     # Todo lo anterior

# Opciones
--auto, -y                    # Modo no interactivo
```

## Búsqueda y consulta

### Buscar paquetes

```bash
urpm search <patrón>          # Buscar por nombre/resumen
urpm s <patrón>               # Alias corto
urpm q <patrón>               # Alias query (compatibilidad urpmq)

# Opciones
--installed                   # Buscar solo entre los paquetes instalados
--unavailable                 # Listar paquetes instalados ausentes de todos los medios
```

#### Encontrar paquetes no disponibles

Listar los paquetes instalados que ya no están disponibles en ningún medio configurado (como `urpmq --unavailable`):

```bash
urpm q --unavailable          # Listar todos los paquetes no disponibles
urpm q --unavailable php      # Filtrar por patrón
```

### Mostrar información de un paquete

```bash
urpm show <paquete>              # Mostrar los detalles de un paquete
urpm info <paquete>              # Alias
urpm show --files <paquete>      # Añade la lista de ficheros del paquete
                                 # (rpm -ql si instalado, files.xml.lzma en caso contrario)
urpm show --changelog <paquete>  # Añade el registro de cambios del paquete
                                 # (rpm -q --changelog; solo paquetes instalados)
```

### Listar paquetes

```bash
urpm list installed           # Listar los paquetes instalados
urpm list available           # Listar los paquetes disponibles
urpm list updates             # Listar las actualizaciones disponibles
urpm list upgradable          # Alias de updates
```

### Dependencias

```bash
urpm depends <paquete>        # Mostrar lo que un paquete requiere
urpm rdepends <paquete>       # Mostrar lo que requiere un paquete (deps inversas)
urpm why <paquete>            # Explicar por qué un paquete está instalado

# Opciones de depends
--tree                        # Mostrar el árbol de dependencias
--prefer=<prefs>              # Filtrar por preferencias (misma sintaxis que install)
--legend                      # Mostrar la leyenda de símbolos tras el árbol

# Opciones de rdepends
--tree                        # Mostrar el árbol de dependencias inversas
--all                         # Mostrar todas las dependencias inversas recursivas (plano)
--depth=N                     # Profundidad máxima del árbol (por defecto: 3)
--hide-uninstalled            # Mostrar solo caminos que llevan a paquetes instalados
--legend                      # Mostrar la leyenda de símbolos tras el árbol
```

Ejemplo con preferencias:
```bash
# Mostrar las deps de phpmyadmin prefiriendo PHP 8.4
urpm depends phpmyadmin --prefer=php:8.4
```

Ejemplo con rdepends:
```bash
# Mostrar el árbol de deps inversas para rtkit, profundidad 10, solo caminos instalados
urpm rdepends --tree --hide-uninstalled --depth=10 rtkit
```

### Dependencias débiles

```bash
urpm recommends <paquete>     # Mostrar los paquetes recomendados por un paquete
urpm whatrecommends <paquete> # Mostrar los paquetes que recomiendan un paquete
urpm suggests <paquete>       # Mostrar los paquetes sugeridos por un paquete
urpm whatsuggests <paquete>   # Mostrar los paquetes que sugieren un paquete
```

### Consultas sobre ficheros

```bash
urpm provides <paquete>       # Listar los ficheros proporcionados por un paquete
urpm whatprovides <fichero>   # Encontrar qué paquete proporciona un fichero
urpm find <patrón>            # Buscar ficheros en los paquetes (instalados + disponibles)
urpm find -i <patrón>         # Buscar solo en los paquetes instalados
urpm find -a <patrón>         # Buscar solo en los paquetes disponibles
urpm find <patrón> --all-versions  # Incluir todas las EVR que entregan la coincidencia
urpm find <patrón> --limit 500     # Subir el límite por defecto de 100 resultados
```

`urpm find` busca por defecto tanto en los paquetes instalados como en los disponibles. `files.xml.lzma` se descarga automáticamente como parte de cada `urpm media update` (condicionado a que el medio lo anuncie en `MD5SUM`), así que no hace falta ningún opt-in — el toggle `--sync-files` se retiró en 0.7.x.

## Marcado de paquetes

```bash
urpm mark manual <paquete>    # Marcar como instalado manualmente
urpm mark auto <paquete>      # Marcar como auto-instalado (dependencia)
urpm mark show <paquete>      # Mostrar el motivo de instalación
```

## Bloqueos de paquetes (holds)

Bloquea paquetes para evitar actualizaciones y reemplazos por obsoletos:

```bash
urpm hold <paquete>           # Bloquear un paquete
urpm hold <paquete> -r "motivo"  # Bloquear con un motivo
urpm hold                     # Listar los paquetes bloqueados
urpm unhold <paquete>         # Retirar el bloqueo
```

Los paquetes bloqueados están protegidos contra:
- Actualizaciones de versión durante `urpm upgrade`
- Ser reemplazados por paquetes que los obsoletan

Ejemplo:
```bash
# dhcpcd obsoleta a dhcp-client, pero quieres conservar dhcp-client
urpm hold dhcp-client -r "Prefer dhcp-client over dhcpcd"

# Ahora urpm upgrade omitirá dhcp-client y avisará:
#   Paquetes bloqueados (1) omitidos:
#     dhcp-client (sería obsoletado por dhcpcd)

# Para permitir el reemplazo más adelante:
urpm unhold dhcp-client
```

## Historial y deshacer

```bash
urpm history                  # Mostrar el historial de transacciones (las 20 últimas)
urpm history -i               # Filtro: solo transacciones de install
urpm history -r               # Filtro: solo transacciones de remove
urpm history -d <id>          # Mostrar detalles de la transacción <id>
urpm history --delete <id>... # Eliminar transacciones del log

urpm undo [id]                # Deshacer una transacción (por defecto: la última). Registra
                              # una entrada limpia en el historial. Usa --auto/-y para
                              # saltarte el prompt.

urpm rollback <n>             # Rollback de las n últimas transacciones
urpm rollback to <id>         # Rollback hasta una transacción concreta
urpm rollback to <date>       # Rollback hasta una fecha (AAAA-MM-DD o DD/MM/AAAA)
```

## Transacciones en segundo plano

Cuando una transacción se desprende (ej. vía el daemon o PackageKit), sigue su progreso con:

```bash
urpm progress                 # Mostrar el progreso actual y salir
urpm progress --watch         # Vigilar de forma continua hasta el final
```

## Identidad de distribución (`distro-switch`)

Una máquina lleva una única identidad de release a la vez — o bien una
estable numérica (`10`, `11`, …) o bien `cauldron`. Esa identidad
determina qué medios considera el resolutor cuando compone una
transacción de instalación o actualización; los medios cuya
`mageia_version` no coincide quedan fuera del pool de candidatos aunque
sigan habilitados en la BD.

Cambiar de identidad es un acto deliberado (un dist-upgrade en filigrana),
por eso vive en su propio verbo y no en `urpm config`.

```bash
urpm distro-switch cauldron     # mueve la máquina a cauldron
urpm distro-switch 11           # la mueve al árbol numérico mga11
urpm distro-switch cauldron:12  # cauldron con una numérica objetivo explícita
```

Antes de aplicar el cambio, el comando:

- Verifica que al menos un medio habilitado ya lleve la identidad
  objetivo (de lo contrario acabarías con un pool de candidatos vacío).
  El diagnóstico apunta a `urpm media autoconfig -r <objetivo>` cuando
  la verificación falla.
- Avisa sobre los medios de la identidad antigua que siguen habilitados
  — desaparecerán del campo de visión del resolutor hasta que se
  realineen o se deshabiliten.
- Refresca `system-numeric` best-effort (la numérica efectiva usada para
  las etiquetas de release `.mgaN` y para sembrar `/etc/mageia-release`
  dentro de los contenedores de compilación): la anulación explícita
  gana primero, luego la propia identidad si es numérica, si no una
  sonda del `media.cfg` de un servidor habilitado.

Tras el cambio, ejecuta `urpm media update` para sincronizar los
metadatos de la nueva identidad.

## Gestión de medios

```bash
urpm media list               # Listar los medios configurados
urpm media add <url>          # Añadir un medio Mageia oficial (auto-parseado)
urpm media add --custom "Nombre" nombre_corto <url>  # Añadir un medio custom / de tercero
urpm media remove <nombre>... # Eliminar uno o varios medios
urpm media remove --all       # Eliminar TODOS los medios configurados (pide
                              # confirmación; añade -y/--auto para saltártela).
                              # Los servidores huérfanos (sin medios) se
                              # retiran en la misma pasada.
urpm media enable <nombre>    # Activar un medio
urpm media disable <nombre>   # Desactivar un medio
urpm media update [nombre]    # Actualizar los metadatos de los medios
urpm media import <fichero>   # Importar desde urpmi.cfg
urpm media link <nombre> +srv -srv  # Vincular/desvincular servidores a un medio
urpm media set <nombre> [opts]  # Modificar los parámetros de un medio (sharing, replication, quota…)
urpm media seed-info <nombre> # Mostrar la info del seed set (secciones, nº paquetes, tamaño estimado)
urpm media autoconfig -r 10   # Auto-añadir los medios Mageia oficiales para la release 10
urpm media discover <url>     # Descubrir medios desde el media.cfg de un repo
```

Flags útiles para `urpm media add`:

```bash
--import-key                  # Importar la clave GPG anunciada por el medio
--allow-unsigned              # Permitir paquetes sin firmar (solo medios custom)
--version <ver>               # Versión Mageia objetivo (solo medios custom: 9, 10, cauldron…)
--update                      # Marcar como medio de actualizaciones
--disabled                    # Añadir pero dejar desactivado
-y, --auto                    # No interactivo: aceptar el nombre/short_name auto-detectado
```

### Importar medios desde un urpmi.cfg heredado

Migra una máquina Mageia existente de `urpmi` a urpm-ng sin volver a añadir cada fuente a mano. Se importan tanto las entradas por URL como las `MIRRORLIST=` — estas últimas como medios pendientes a los que `urpm server autoconfig` acoplará servidores en la siguiente ejecución.

```bash
urpm media import /etc/urpmi/urpmi.cfg    # Ruta por defecto
urpm media import                          # Idem (por defecto /etc/urpmi/urpmi.cfg)

# Opciones
--replace                     # Sobrescribir los medios existentes que coincidan por short_name
-r, --release <version>       # Release Mageia objetivo (por defecto: valor de /etc/mageia-release)
--arch <arch>                 # Arquitectura objetivo (por defecto: `uname -m`)
-y, --auto                    # No interactivo: saltarse la confirmación
```

### Descubrir medios desde un repositorio

Descubre todos los medios disponibles desde cualquier repositorio compatible con Mageia (mirrors oficiales, repos comunitarios como MLO, mirrors corporativos):

```bash
urpm media discover https://repo.example.org/9/x86_64/media/       # Añadir todos los medios
urpm media discover --dry-run https://repo.example.org/9/x86_64/media/  # Solo previsualizar
urpm media discover --sources --debug https://...                   # Incluir SRPMS y debug

# Forzar activar / desactivar categorías (nonfree, tainted, 32bit, all)
urpm media discover --with nonfree,tainted https://...
urpm media discover --without nonfree https://...
urpm media discover --with all https://...
```

El comando descarga `media.cfg` desde el repositorio, descubre todos los medios y vincula los servidores existentes que hospedan el mismo contenido (verificado por checksum MD5 de `synthesis.hdlist.cz`).

### Vinculación servidor-medio

Vincula o desvincula servidores a fuentes de medio concretas:

```bash
urpm media link "Core Release" +mirror1 +mirror2   # Añadir servidores
urpm media link "Core Updates" -oldserver          # Retirar un servidor
urpm media link "Core Release" +all                # Añadir todos los servidores disponibles
urpm media link "Core Release" -all +preferred     # Resetear y añadir uno
```

Nota: al añadir servidores, urpm verifica que el contenido del medio coincide comparando los checksums MD5 de `synthesis.hdlist.cz` con los servidores de referencia existentes.

### Auto-configurar medios

Añade automáticamente los medios Mageia oficiales para una release:

```bash
urpm media autoconfig --release 10              # Añadir todos los medios oficiales para Mageia 10
urpm media autoconfig -r cauldron               # Añadir los medios para Cauldron
urpm media autoconfig -r 10 --no-nonfree        # Omitir los medios nonfree
urpm media autoconfig -r 10 --no-tainted        # Omitir los medios tainted
urpm media autoconfig -r 10 -n                  # Dry-run: mostrar lo que se añadiría
```

### Parámetros de medio

Configura la compartición y la replicación de los medios:

```bash
urpm media set "Core Release" --shared=yes           # Compartir con pares P2P
urpm media set "Core Release" --replication=seed     # Replicación completa (estilo DVD)
urpm media set "Core Release" --replication=on_demand  # Cachear lo que se descargue
urpm media set "Core Release" --quota=5G             # Limitar el tamaño del caché
urpm media set "Core Release" --retention=30         # Conservar los paquetes 30 días
urpm media set "Core Release" --priority=10          # Prioridad más alta
urpm media set "Core Release" --seeds=INSTALL,CAT_PLASMA5  # Secciones de seed
```

Ejemplos:
```bash
# Añadir un medio Mageia oficial (servidor y medio auto-detectados)
urpm media add https://ftp.belnet.be/mageia/distrib/9/x86_64/media/core/release/

# Añadir un medio de tercero custom
urpm media add --custom "RPM Fusion" rpmfusion https://download1.rpmfusion.org/free/fedora/40/x86_64/os/
```

## Gestión de servidores

Los servidores son fuentes mirror que pueden servir varios medios. urpm admite varios servidores por medio para balanceo de carga y failover.

```bash
urpm server list              # Listar los servidores configurados (con país)
urpm server add <nombre> <url>  # Añadir un servidor (comprueba la IP y escanea los medios)
urpm server remove <nombre> ... # Retirar uno o varios servidores
urpm server enable <nombre>   # Activar un servidor
urpm server disable <nombre>  # Desactivar un servidor
urpm server priority <nombre> <n>  # Fijar la prioridad del servidor (más alta = preferida)
urpm server test [nombre]     # Probar conectividad y detectar modo IP
urpm server ip-mode <nombre> <mode>  # Fijar el modo IP (auto/ipv4/ipv6/dual)
urpm server autoconfig        # Auto-añadir servidores desde la API de mirrors Mageia
urpm server stats [nombre]    # Mostrar las estadísticas de rendimiento de un servidor
urpm server status            # Mostrar los servidores en blacklist / con reputación baja
urpm server unblacklist <nombre>  # Levantar el blacklist de un servidor (tras revisión)
urpm server ack-blacklist <nombre>  # Reconocer un blacklist (silencia el banner sin levantarlo)
```

### Lista de servidores

Opciones para urpm server list:
```bash
--all                 # Mostrar todos los servidores, incluidos los desactivados
```

### Modo IP

Cada servidor tiene un modo IP para gestionar la conectividad IPv4/IPv6:
- `auto` — Dejar al sistema decidir (puede provocar un timeout de 30s si IPv6 falla)
- `ipv4` — Forzar solo IPv4
- `ipv6` — Forzar solo IPv6
- `dual` — Ambos funcionan, preferir IPv4 (recomendado para servidores dual-stack)

El modo IP se detecta automáticamente al añadir un servidor. Usa `server test` para re-detectar o `server ip-mode` para fijarlo a mano.

### Seguimiento de ancho de banda y failover automático

urpm sigue automáticamente el rendimiento de descarga de cada servidor. Tras cada descarga o sincronización de metadatos, la velocidad medida se registra con una EWMA (Exponentially Weighted Moving Average, α=0.3), lo que aporta inercia para que un único transporte lento no penalice injustamente a un buen servidor.

Los servidores se prueban en orden `priority DESC, bandwidth_kbps DESC`: si un servidor falla durante una descarga o una sync de metadatos, el siguiente mejor se prueba automáticamente sin intervención del usuario. Dentro de una misma sesión, también se conservan en memoria estimaciones de velocidad por servidor, así el orden se adapta en tiempo real sin esperar a la próxima ejecución.

`urpm server autoconfig` mide la latencia hacia todos los candidatos mirror y persiste los resultados, así el orden de servidores es pertinente desde la primera descarga.

### Blacklist y reputación

Un servidor que sirva un RPM corrupto o sin firmar es
**auto-bloqueado**: queda excluido de las descargas siguientes hasta
que lo revises y lo levantes. Los fallos de firma se tratan como
señales activas de manipulación — no hay auto-desbloqueo temporal.

En paralelo al blacklist, urpm mantiene una **puntuación de
reputación** deslizante a 24 h (baseline 100) que baja con los
cuerpos corruptos, los HTTP 4xx/5xx, los errores de red y los
transportes lentos. La puntuación reordena el pool sin excluir
servidores del todo.

```bash
urpm server status               # Listar servidores blacklistados / con reputación baja
urpm server unblacklist <nombre> # Levantar el blacklist tras revisión humana
urpm server ack-blacklist <nombre>  # Reconocer (silencia el banner sin levantarlo)
```

En el momento de `install` / `upgrade` / `media update`, un banner rojo persistente lista cada blacklist no reconocido con instrucciones de reactivación — el banner no desaparece solo, únicamente `unblacklist` o `ack-blacklist` lo silencian.

`urpm server list` marca en rojo las filas blacklistadas, así una ojeada al pool basta para saber quién está fuera.

### Filtrado geográfico

Los servidores descubiertos desde la API de mirrors Mageia llevan metadatos de país y continente. La sección `[server]` de la configuración (ver más abajo) permite restringir qué mirrors se aceptan:

```ini
# /etc/urpm/conf.d/10-server.cfg
[server]
country_blacklist = UA, RU        # Excluir países concretos
continent_whitelist = EU          # Solo mirrors europeos
```

El filtrado se aplica al añadir mirrors (`urpm init`, `urpm media autoconfig`, `urpm server autoconfig` y la expansión del pool en segundo plano). Los servidores ya presentes en la base se completan con su país en la primera ejecución; los que no pasan el filtro se desactivan automáticamente.

Pon `auto_add = false` para impedir cualquier adición automática de mirror.

Usa `urpm server stats [nombre]` para inspeccionar las métricas recogidas:

```
$ urpm server stats mirror1
mirror1  https://mirror.example.com/mageia/
  Status        : enabled
  Priority      : 50
  IP mode       : dual
  Bandwidth     : 12 400 KB/s
  Latency       : 18 ms
  Success rate  : 98% (245/250)
  Last check    : 3m ago
  Media         : Core Release, Core Updates, Nonfree Release
```

## Gestión de pares

Cuando urpmd corre en varias máquinas de la misma LAN, se descubren mutuamente y comparten los paquetes en caché (P2P).

```bash
urpm peer list                # Listar los pares descubiertos
urpm peer downloads [host]    # Mostrar los paquetes descargados desde pares (filtrar por host)
urpm peer blacklist <host>    # Bloquear un par (ej. si sirve paquetes malos)
urpm peer unblacklist <host>  # Desbloquear un par
urpm peer clean <host>        # Eliminar los RPMs descargados desde un par concreto
                              # (usar tras un blacklist; <host> es obligatorio)
```

### Modo solo-local

Usa `--only-peers` para descargar exclusivamente desde pares LAN sin fallback a mirrors arriba:

```bash
urpm i --only-peers firefox   # Instalar solo si está disponible en los pares
urpm u --only-peers           # Actualizar solo con paquetes de los pares
urpm download --only-peers pkg  # Descargar solo desde los pares
```

Útil para redes air-gapped o cuando quieres garantizar que todos los paquetes vengan de fuentes locales de confianza.

## Gestión del caché

```bash
urpm cache info               # Mostrar información del caché
urpm cache clean              # Retirar los RPMs huérfanos del caché
urpm cache rebuild            # Reconstruir la base de paquetes desde los ficheros synthesis
urpm cache rebuild-fts        # Reconstruir el índice FTS para la búsqueda rápida de ficheros
urpm cache stats              # Estadísticas detalladas
```

`urpm cache clean` admite `--dry-run/-n` (previsualización), `--auto/-y` (sin confirmación) y `--verbose/-v` (lista cada fichero huérfano).

## Mirror / Replicación

urpm-ng puede replicar localmente un subconjunto de paquetes (similar a un conjunto de instalación DVD) y exponerlos a los pares LAN. Útil para install parties, instalaciones offline y para montar un mirror interno.

Dos piezas:

- **Política por medio** — `urpm media set <nombre> --replication=…`
  controla cómo se replica cada medio (solo metadatos, cacheo
  bajo demanda o seed completo).
- **`urpm mirror` top-level** — estado global del lado daemon (cuotas,
  versiones servidas, límite de ancho de banda saliente) y disparadores
  explícitos de mantenimiento.

### Control de mirror top-level

```bash
urpm mirror status            # Mostrar el estado del mirror, cuotas y versiones servidas
urpm mirror enable            # Empezar a servir los paquetes en caché a los pares
urpm mirror disable           # Dejar de servir paquetes
urpm mirror quota [SIZE]      # Mostrar o fijar la cuota global de caché (ej. 10G, 500M)
urpm mirror enable-version 10,cauldron   # Retomar el servicio para estas versiones
urpm mirror disable-version 8,9          # Dejar de servir estas versiones
urpm mirror clean [-n]        # Aplicar cuotas y políticas de retención (--dry-run previsualiza)
urpm mirror sync [medio]      # Forzar la sync de replicación para los medios con política `seed`
urpm mirror sync --latest-only           # Sync más pequeña, estilo DVD
urpm mirror rate-limit [on|off|N/min]    # Configurar el límite de ancho de banda saliente
```

### Replicación basada en seed

La replicación usa el fichero `rpmsrate-raw` de Mageia para determinar qué paquetes mirrorear (misma lógica que el contenido DVD).

```bash
# Activar la replicación basada en seed sobre un medio
urpm media set "Core Release" --replication=seed
urpm media set "Core Updates" --replication=seed

# Ver el seed set calculado
urpm media seed-info "Core Release"
# Salida:
#   Secciones: INSTALL, CAT_PLASMA5, CAT_GNOME, …
#   Paquetes seed desde rpmsrate: 437
#   Patrones de locale: 3
#   Paquetes de locale expandidos: +237
#   Con dependencias: 2300 paquetes
#   Tamaño estimado: ~3.5 GB

# Forzar la sync (descarga los paquetes faltantes)
urpm mirror sync

# Sync solo de la última versión de cada paquete (más pequeño, estilo DVD)
urpm mirror sync --latest-only
```

### Cómo funciona

1. Parsea `/usr/share/meta-task/rpmsrate-raw` (del paquete meta-task)
2. Extrae los paquetes de las secciones: INSTALL, CAT_PLASMA5, CAT_GNOME, CAT_XFCE, etc.
3. Expande los patrones de locales (ej. `libreoffice-langpack-ar` → todas las langpacks)
4. Resuelve las dependencias (Requires + Recommends)
5. Descarga los paquetes faltantes en paralelo

Las secciones de seed por defecto cubren todos los entornos de escritorio y aplicaciones importantes, dando ~5 GB de paquetes (comparable a un DVD Mageia).

### Políticas de replicación

```bash
urpm media set <nombre> --replication=none       # Solo metadatos, sin paquetes
urpm media set <nombre> --replication=on_demand  # Cachear lo que se descarga (por defecto)
urpm media set <nombre> --replication=seed       # Contenido estilo DVD desde rpmsrate
```

## Configuración

### Blacklist (nunca instalar/actualizar)

```bash
urpm config blacklist list    # Mostrar los paquetes blacklistados
urpm config blacklist add <pkg>
urpm config blacklist remove <pkg>
```

### Redlist (avisar antes de auto-remove)

```bash
urpm config redlist list      # Mostrar los paquetes redlistados
urpm config redlist add <pkg>
urpm config redlist remove <pkg>
```

### Gestión del kernel

```bash
urpm config kernel-keep       # Mostrar cuántos kernels conservar
urpm config kernel-keep <n>   # Fijar el número de kernels a conservar
```

### Modo de versión (sistema vs cauldron)

Cuando están configurados a la vez medios del sistema y cauldron, `version-mode` elige cuál gana para las actualizaciones:

```bash
urpm config version-mode              # Mostrar el modo actual
urpm config version-mode system       # Quedarse en la versión del sistema instalada
urpm config version-mode cauldron     # Rodar con cauldron
urpm config version-mode auto         # Retirar la preferencia explícita
```

### Hooks de auto-actualización para los centros de software

Controla si GNOME Software, KDE Discover o la vía de actualización offline de PackageKit pueden instalar actualizaciones por su cuenta:

```bash
urpm config gnome-auto-upgrades [yes|no]      # GNOME Software
urpm config discover-auto-upgrades [yes|no]   # KDE Discover
urpm config packagekit-auto-upgrades [yes|no] # Updates offline de PackageKit
```

Sin argumento, cada subcomando imprime el ajuste actual. Estos hooks conmutan los ajustes dconf/PolicyKit del lado del escritorio; la política del sistema se aplica por separado desde el paquete `urpm-ng-desktop`.

### Inspeccionar o editar la configuración

```bash
urpm config show              # Mostrar la config efectiva desde todos los *.cfg
urpm config edit              # Abrir urpm.cfg en $EDITOR
urpm config edit 00-urpmi-compat   # Abrir un drop-in concreto
```

### Selección de servidor

La sección `[server]` en `/etc/urpm/conf.d/10-server.cfg` controla la selección automática de mirror:

| Clave | Por defecto | Descripción |
|-------|-------------|-------------|
| `auto_add` | `true` | Permitir la adición automática de mirrors |
| `country_blacklist` | *(vacío)* | Códigos ISO 3166 separados por coma a excluir (ej. `UA, RU`) |
| `country_whitelist` | *(vacío)* | Aceptar solo estos países (gana sobre blacklist) |
| `continent_blacklist` | *(vacío)* | Códigos de continente a excluir (`EU`, `NA`, `SA`, `AS`, `AF`, `OC`) |
| `continent_whitelist` | *(vacío)* | Aceptar solo estos continentes (gana sobre blacklist) |

Un mirror debe pasar **ambos** filtros de continente y país. La whitelist gana sobre la blacklist en cada nivel. Usa `urpm config show` para ver los ajustes efectivos.

## Claves GPG

```bash
urpm key list                 # Listar las claves GPG instaladas
urpm key import <fichero|url> # Importar una clave GPG
urpm key remove <keyid>       # Retirar una clave GPG
```

## Dependencias de build

Instala las dependencias de build para construir RPMs:

```bash
urpm install --buildrequires foo.spec    # Desde un fichero spec
urpm install --buildrequires foo.src.rpm # Desde un RPM fuente
urpm i -b                                # Auto-detecta en el árbol de build RPM
urpm i --br                              # Alias corto

# Opciones
--sync                        # Esperar a que terminen todos los scriptlets
```

Las dependencias de build instaladas quedan tracked en `/var/lib/rpm/installed-through-builddeps.list` y excluidas de la retirada normal de huérfanos. Para limpiarlas:

```bash
urpm autoremove --buildrequires          # Retirar todas las build deps tracked
urpm ar -b                               # Forma corta
```

## Sistema de build en contenedor

urpm proporciona un sistema de build completo en contenedor para paquetes RPM vía Docker o Podman.

### Gestión de imágenes

```bash
# Listar las imágenes de build disponibles
urpm image list

# Actualizar una imagen existente (re-sync medios + paquetes)
urpm image update mageia:10-build

# Eliminar una o varias imágenes
urpm image delete mageia:10-build mageia:10-ci
```

### Crear una imagen de build

```bash
urpm image make --release 10 --tag mageia:10-build
urpm image make --release 10 --tag mageia:10-ci --profile ci

# Imagen de build para un .spec o .src.rpm (auto-instala BuildRequires)
urpm image make --release 10 --tag mga:10-foo --buildrequires SPECS/foo.spec

# Opciones
-r, --release <version>       # Versión Mageia (ej. 10, cauldron)
-t, --tag <tag>               # Tag de la imagen (ej. mageia:10-build)
--profile <name>              # Perfil de paquetes (por defecto: build)
--arch <arch>                 # Arquitectura objetivo (por defecto: host)
-p, --packages <list>         # Paquetes adicionales (separados por coma)
--buildrequires <spec|srpm>   # Instalar los BuildRequires desde un .spec o .src.rpm
--addmedia <NAME> <URL>       # Añadir un medio extra dentro de la imagen (repetible) --
                              # ej. un mirror de tercero o interno
--import-key <URL>            # Importar una clave GPG pública dentro de la imagen (repetible) --
                              # se combina con --addmedia para medios de terceros firmados
--runtime docker|podman       # Runtime de contenedor (por defecto: auto-detección)
--keep-chroot                 # Conservar el chroot temporal tras la creación de la imagen
-w, --workdir <path>          # Directorio de trabajo para el chroot (por defecto: ~/.cache/urpm/mkimage).
                              # También se usa como TMPDIR para la fase de commit de podman
                              # para que los blobs de imagen no desborden un /tmp estrecho.
--exclude PKG                 # Elimina PKG de la imagen final mediante
                              # `urpm erase --force --keep-orphans --sync` (repetible).
                              # Uso canónico: `--exclude python3-zstandard` para que
                              # mach de firefox no tropiece con su propia restricción de versión.
--urpm-ng-source auto|local|media|github
                              # De dónde proviene urpm-ng-core (por defecto: cascada auto)
--urpm-ng-core <path>         # Instalar urpm-ng-core desde este RPM concreto
--allow-disttag-mismatch      # Acepta un RPM local cuyo disttag queda fuera de la
                              # ventana del objetivo (por defecto: solo .mgaN. para
                              # numérico; .mgaN. y .mga{N-1}. para cauldron/N — el
                              # empaquetador que recompila en su stable ya está cubierto sin este flag).
```

**Identidad de release en `--release`.** El argumento acepta tres formas:

- `--release 10` — fija la identidad de la máquina en una release estable numérica.
- `--release cauldron` — fija en el árbol de desarrollo en movimiento. La
  numérica efectiva (usada para las etiquetas de release `.mgaN` y para
  el macro `%mgaversion` dentro de los contenedores de compilación) se
  sondea best-effort desde el `media.cfg` del espejo en el momento del
  init. Sin conexión o si el sondeo falla, queda sin definir y los
  consumidores recurren a `/etc/mageia-release`.
- `--release cauldron:11` — cauldron con una numérica objetivo explícita.
  Vence al sondeo, funciona sin conexión y anula al espejo cuando el
  `media.cfg` del servidor va con retraso durante una ventana de flip.

> **Compatibilidad hacia atrás:** `urpm mkimage` se conserva como alias de `urpm image make`.

### Perfiles

Los perfiles definen qué paquetes se instalan en la imagen:

| Perfil | Descripción |
|--------|-------------|
| `build` | Entorno de build RPM (por defecto): rpm-build, gcc, make, etc. |
| `ci` | CI/testing: python3-pytest, git, python3-solv, etc. |
| `minimal` | Sistema mínimo usable con urpm |

Los perfiles se cargan desde:
- `/usr/share/urpm/profiles/*.yaml` (sistema, desde el paquete)
- `/etc/urpm/profiles/*.yaml` (añadidos locales)

### Construir paquetes

Por defecto, `urpm build` auto-actualiza los medios y paquetes dentro del contenedor antes de compilar, para que los builds se ejecuten siempre contra el último estado del repositorio. Usa `--no-update` para saltarte este paso trabajando offline o para acelerar builds repetidos.

```bash
# Build desde un RPM fuente (salida a ./build-output/)
urpm build -i mageia:10-build foo-1.0-1.mga10.src.rpm

# Build desde un fichero spec (salida a workspace/RPMS/ y SRPMS/)
urpm build -i mageia:10-build SPECS/foo.spec

# Build sin auto-actualizar medios/paquetes antes
urpm build -i mga10-build --no-update SPECS/foo.spec

# Build con dependencias locales (ej. libfoo compilado antes)
urpm build -i mageia:10-build SPECS/bar.spec -w 'RPMS/x86_64/libfoo*.rpm'

# Varias dependencias locales
urpm build -i mageia:10-build SPECS/app.spec \
    -w 'RPMS/x86_64/libfoo*.rpm' -w 'RPMS/x86_64/libbar*.rpm'

# Varios builds en paralelo
urpm build -i mageia:10-build *.src.rpm --parallel 4

# Constructor de tercero: taggea la salida como foo-1.0-1.mlo.mga10.x86_64.rpm
urpm build -i mageia:10-build --subrel mlo SPECS/foo.spec

# Sobrescribir packager/vendor/dist sin tocar el spec
urpm build -i mageia:10-build --rpmmacros ./my-macros SPECS/foo.spec

# Opciones
-i, --image <tag>             # Imagen Docker/Podman a usar
-o, --output <dir>            # Directorio de salida para builds SRPM (por defecto: ./build-output)
-w, --with-rpms <pattern>     # Pre-instalar RPMs locales antes del build (glob, repetible)
--no-update                   # Saltarse la auto-actualización de medios y paquetes antes del build
--runtime docker|podman       # Runtime de contenedor (por defecto: auto-detección)
-j, --parallel <N>            # Compilaciones aisladas multi-contenedor (por defecto: 1, encadenadas en contenedor compartido)
--stop-on-fail                # Detener la cadena al primer spec fallido (por defecto: continuar)
--rollback-between-builds     # Revertir los BuildRequires de cada spec entre builds (alias: --rbb)
--keep-container              # Conservar el contenedor tras el build (para debug)
--subrel <tag>                # Inyecta %subrel TAG para que los RPMs de salida sean NAME-VERSION-RELEASE.TAG.DIST.ARCH.rpm
--rpmmacros <file>            # Inyecta FILE como /root/.rpmmacros en el contenedor de build (combinable con --subrel)
--build-cpus N                # Limita el paralelismo de compilación a N hilos
                              # (rpmbuild %_smp_mflags = -jN + podman --cpus).
                              # Por defecto: max(1, nproc - 2), para que el host conserve
                              # dos núcleos libres para el trabajo interactivo.
--build-memory SIZE           # Tope de RAM del contenedor (p.ej. 8G, 12000M, 16GB).
                              # Se transmite a podman --memory. Por defecto: max(2G, MemTotal - 2G).
--full-throttle               # Atajo: sin tope de CPU, sin tope de memoria. Anula --build-cpus
                              # y --build-memory.
--strict-memory               # Ata --memory-swap a --build-memory (podman mata el proceso al
                              # alcanzar el tope de RAM). Por defecto: swap ilimitado, alineado
                              # con mock/systemd-nspawn. Usar en CI donde un swap silencioso
                              # se confundiría con un timeout.
--with FEATURE                # Pasa `--with FEATURE` a rpmbuild (%bcond del spec). Repetible.
--without FEATURE             # Pasa `--without FEATURE` a rpmbuild (%bcond del spec). Repetible.
```

#### Topes de recursos y paridad con mock

El trío `--build-cpus` / `--build-memory` / `--strict-memory` es la palanca
principal para compilar specs pesados (firefox, thunderbird, chromium) en
máquinas que no tienen 32+ GB de RAM libre. Los valores por defecto dejan
al host dos CPUs y dos GB de RAM para que siga siendo utilizable y, sobre
todo, **el swap queda ilimitado por defecto** — el contenedor puede volcar
páginas frías al swap del host como lo hace el envoltorio systemd-nspawn
de mock. Sin eso, el rustc de firefox se topa con un `SIGKILL` mucho antes
del tope de RAM real en hosts < 16 GB. `--strict-memory` vuelve a atar
`--memory-swap` para CI, donde un swap silencioso se confundiría con un hang.

#### Paso de bcond a rpmbuild

`--with FEATURE` y `--without FEATURE` se transmiten literalmente a
rpmbuild para que los specs que declaran `%bcond_with` / `%bcond_without`
puedan alternarse sin invocar rpmbuild manualmente. Ejemplo: un spec de
firefox que declara `%bcond_without unified_build` (unified translation
units activados por defecto) puede compilarse sin ellos para una prueba
limitada en memoria mediante
`urpm build --without unified_build ./SPECS/firefox.spec`.

### Layout del workspace

Para los builds a partir de spec, urpm soporta el layout de workspace RPM estándar:

```
workspace/
├── SPECS/
│   └── foo.spec
└── SOURCES/
    ├── foo-1.0.tar.gz
    └── patches/
```

Los resultados se colocan en:
```
workspace/
├── RPMS/
│   └── x86_64/
│       └── foo-1.0-1.mga10.x86_64.rpm
└── SRPMS/
    └── foo-1.0-1.mga10.src.rpm
```

### Ejemplo de workflow

```bash
# 1. Crear la imagen de build (una sola vez)
urpm image make --release 10 --tag mga:10-build

# 2. Compilar un paquete
urpm build --image mga:10-build ./mypackage.src.rpm

# 3. Más adelante, actualizar la imagen para recoger nuevos paquetes del repo
urpm image update mga:10-build

# 4. Revisar los resultados
ls ./build-output/
```

### Bootstrap manual (avanzado)

Bajo el capó, `urpm image make` llama a `urpm init` dentro de un chroot fresco para poblar el catálogo de medios. `urpm init` está expuesto directamente para los llamadores que necesiten bootstrapear un rootfs fuera de la vía contenerizada — scripts de instalador, builds de disco VM, o raíces de test preparadas. Los mirrors se toman desde la API de mirrors Mageia y se filtran por la sección `[server]` de `/etc/urpm/conf.d/10-server.cfg`.

```bash
# Bootstrapear un rootfs chroot para Mageia 10
urpm --urpm-root /tmp/rootfs init --release 10 --arch x86_64

# Usar una lista de mirrors custom
urpm init --mirrorlist 'https://mirrors.mageia.org/api/mageia.10.x86_64.list'

# Opciones
--release, -r <version>     # Versión Mageia objetivo (10, cauldron, …)
--mirrorlist <url>          # Sobrescribir la URL de la lista de mirrors auto-generada
--arch <arch>               # Arquitectura objetivo (por defecto: host)
--auto, -y                  # Modo no interactivo
--no-sync                   # Configurar los medios pero saltarse la sync inicial
```

Tras trabajar dentro de un chroot `--urpm-root`, desmonta `/dev` y `/proc` montados por `urpm init`:

```bash
urpm --urpm-root /tmp/rootfs cleanup
```

## Herramientas para mantenedores de repositorio

Los dos comandos siguientes están pensados para quien **publica** un
repositorio compatible con Mageia, no para quien lo consume. Se
documentan juntos para que quede obvio cuál entrega metadatos al
cliente y cuál los produce.

- **`urpm appstream`** (lado cliente) — refresca el catálogo AppStream
  en la máquina actual para que los centros de software vean
  descripciones actualizadas. Vive en `urpm-ng-appstream`.
- **`urpm genmedia`** (lado servidor) — produce el conjunto completo
  de metadatos de medios que un mirror sirve a sus clientes. Vive
  en `urpm-ng-genmedia`, subpaquete separado para que la instalación
  cliente base se quede ligera.

### Metadatos AppStream (`urpm appstream`)

urpm puede producir y refrescar los catálogos AppStream consumidos por KDE Discover y GNOME Software:

```bash
urpm appstream generate              # Generar el catálogo desde la base de paquetes
urpm appstream generate -m core/release    # Limitar a un medio concreto
urpm appstream generate --no-compress       # XML plano en lugar de gzip
urpm appstream status                # Mostrar el estado del catálogo por medio
urpm appstream merge                 # Fusionar los ficheros por medio en el catálogo unificado
urpm appstream merge --refresh       # Refrescar también el caché AppStream del sistema
urpm appstream init-distro           # Crear el fichero metainfo del OS (necesario para Discover/GS)
urpm appstream init-distro --force   # Sobrescribir un metainfo existente
```

### Generación de medios (`urpm genmedia`)

`urpm genmedia` es la contraparte del lado servidor de `urpm appstream`: donde `appstream` consume catálogos para poblar bases de cliente, `genmedia` **produce** el conjunto completo de metadatos de medios que un mirror Mageia sirve a sus clientes. Es una reescritura Python del histórico `genhdlist3`, integrada en urpm-ng y empaquetada aparte como `urpm-ng-genmedia` para que la huella de dependencias se quede fuera de la instalación cliente base.

Desde un directorio de ficheros RPM:

```bash
urpm genmedia /path/to/rpms          # Por defecto: generación completa
urpm genmedia /path/to/rpms --incremental   # Saltar los RPMs cuyo SHA-256 no ha cambiado
urpm genmedia /path/to/rpms --no-hdlist     # Saltar la salida hdlist.cz
urpm genmedia /path/to/rpms --xml-info      # Forzar la regeneración de los ficheros XML info
urpm genmedia /path/to/rpms --appstream-info  # Generar el catálogo AppStream
urpm genmedia /path/to/rpms --no-md5sum     # Saltar MD5SUM (más rápido para pruebas)
urpm genmedia /path/to/rpms --allow-empty-media  # Tolerar un directorio de entrada vacío
```

El comando produce el layout canónico que espera cualquier cliente urpm-ng o urpmi:

```
media_info/
  hdlist.cz                # Headers de paquetes binarios comprimidos
  synthesis.hdlist.cz      # Síntesis ligera de dependencias
  files.xml.lzma           # Listas de ficheros por paquete
  info.xml.lzma            # URL, sourcerpm, licencia, descripción
  changelog.xml.lzma       # Changelogs por paquete
  appstream.xml.gz         # Cuando se activa --appstream-info
  MD5SUM                   # Checksums de todo lo anterior
```

La pasada AppStream extrae los ficheros `*.metainfo.xml` embebidos y entregados por las aplicaciones upstream (KDE, GNOME, etc.) y genera un componente mínimo desde los campos de header RPM para paquetes que lo necesitan pero no lo traen. Los paquetes cuyo contenido es completamente no orientado al usuario (headers devel, símbolos de debug, archivos estáticos, libs de runtime puras) quedan **filtrados** en vez de emitidos con una categoría fallback ``System`` — llenarían Discover y GNOME Software sin llegar a ser instalables jamás desde una app store.

El directorio `media_info/` se bloquea mientras una generación está en curso, para que los clientes que leen en concurrencia vean siempre un snapshot coherente.

## Mensajes README de paquetes

`urpm readme` muestra los mensajes README de paquetes presentados al usuario durante una transacción (Mageia los guarda como `README.urpmi` / `README.upgrade`):

```bash
urpm readme                          # README de la transacción más reciente
urpm readme --transaction <id>       # README de una transacción concreta
urpm readme --list                   # Listar las transacciones con mensajes README
```

## Limpieza de huérfanos

```bash
urpm cleandeps                # Alias de `urpm autoremove --faildeps`:
                              # retira las dependencias huérfanas dejadas
                              # por transacciones interrumpidas.
```

---

# urpmd - Daemon en segundo plano

urpmd es un servicio en segundo plano que proporciona:
- API HTTP para las operaciones de paquetes
- Tareas planificadas en segundo plano
- Descubrimiento P2P de pares para el uso compartido LAN de paquetes



## Endpoints de la API

### Endpoints GET

| Endpoint | Descripción |
|----------|-------------|
| `/` | Info del servicio |
| `/api/ping` | Health check |
| `/api/status` | Estado del daemon |
| `/api/media` | Listar los medios configurados |
| `/api/available` | Listar los paquetes disponibles |
| `/api/updates` | Listar las actualizaciones disponibles |
| `/api/peers` | Listar los pares LAN descubiertos |

### Endpoints POST

| Endpoint | Descripción |
|----------|-------------|
| `/api/refresh` | Refrescar los metadatos de los medios |
| `/api/available` | Consultar los paquetes disponibles |
| `/api/announce` | Anunciar paquetes a los pares |
| `/api/have` | Consultar si un par tiene paquetes concretos |

## Tareas planificadas

El daemon realiza automáticamente:
- Sincronización de metadatos de medios
- Limpieza de caché
- Chequeo de disponibilidad de actualizaciones
- Descubrimiento de pares (broadcast UDP)

## Uso compartido P2P de paquetes

Cuando varias máquinas de la misma LAN ejecutan urpmd, se descubren automáticamente y pueden compartir paquetes RPM en caché, reduciendo el consumo de ancho de banda.

---

# Integración GUI (Discover / GNOME Software)

urpm-ng proporciona un backend PackageKit que permite a los centros gráficos de software gestionar los paquetes.

## Instalación

```bash
urpm install urpm-ng-desktop
```

O instalar el backend directamente:
```bash
urpm install urpm-ng-packagekit-backend
```

Esto instala:
- `libpk_backend_urpm.so` — Backend PackageKit
- Servicio D-Bus `org.mageia.Urpm.v1` — Operaciones privilegiadas
- Políticas PolicyKit — Prompts de autorización
- Configuración AppStream — Metadatos de catálogo de software

## Aplicaciones soportadas

- **KDE Discover** — Soporte completo (búsqueda, install, remove, actualizaciones)
- **GNOME Software** — Soporte completo (búsqueda, install, remove, actualizaciones)

## Cómo funciona

```
┌─────────────────┐
│  Discover /     │
│  GNOME Software │
└────────┬────────┘
         │
┌────────▼────────┐
│   PackageKit    │
│ (libpk_backend_ │
│    urpm.so)     │
└────────┬────────┘
         │
┌────────▼────────┐
│  Servicio D-Bus │
│  + PolicyKit    │
│ (org.mageia.    │
│   Urpm.v1)      │
└────────┬────────┘
         │
┌────────▼────────┐
│  urpm-ng core   │
│  (Python)       │
└─────────────────┘
```

### rpmdrake-ng (Qt6)

Una GUI Qt6 dedicada a la gestión de paquetes está en desarrollo. Ver `rpmdrake/README.md` para más detalles.

## Solución de problemas

```bash
# Comprobar si el servicio D-Bus está en marcha
systemctl status urpm-dbus.service

# Comprobar el backend PackageKit
pkcon backend-details

# Reiniciar los servicios tras una actualización
systemctl restart packagekit.service
systemctl restart urpm-dbus.service

# Comprobar la interfaz D-Bus
gdbus introspect --system --dest org.mageia.Urpm.v1 \
  --object-path /org/mageia/Urpm/v1
```

---

# Desarrollo y contribución

## Requisitos previos

### Puertos del cortafuegos

Ver la sección Requisitos previos para los puertos de red que abrir para el uso compartido P2P.

### Preparar el entorno

Clona el repositorio:

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng

```


### Configuración del modo dev

Crea un fichero `.urpm.local` en la raíz del proyecto para personalizar el modo dev:

```bash
cd /where/is/urpm-ng

# Modo dev (puerto 9877, datos de usuario en ~/var/lib/urpm-dev/)
# Cambiar a modo dev
touch .urpm.local
```

Nota, puedes cambiar dónde ponen sus datos urpm y urpmd editando el fichero .urpm.local:
```ini
# Directorio base custom (opcional)
base_dir=/path/lib/urpm-dev
```

En modo dev, por defecto, los datos se almacenan en `/var/lib/urpm-dev/` y el daemon usa el puerto 9877.

**Ten en cuenta que en modo dev urpmd solo interactuará con otros urpmd en modo dev.**

## Lanzar el daemon

```bash
# Lanzar el daemon (como root, sin modo segundo plano)

cd /where/is/urpm-ng

./bin/urpmd --dev

```

## Lanzar urpm

```bash
# Lanzar urpm (como root en una consola dedicada)

cd /where/is/urpm-ng

./bin/urpm --help

```

## Codear, probar, contribuir…

Se agradecen todo tipo de contribuciones: código, pruebas, traducciones, feedback… ninguna contribución es demasiado pequeña.

Ver `CLAUDE.md` para las guidelines de desarrollo y `doc/ARCHITECTURE.md` para la arquitectura técnica.

---

# Problemas conocidos / TODO

- **Rendimiento de `urpm find`** — La búsqueda en files.xml es más lenta que urpmf (2.5s vs 0.6s). Necesita optimización.

---

# Licencia

GPL-3.0 — Ver el fichero LICENSE para los detalles.

# Autores

- Maât (Pascal Vilarem)
- Papoteur (Mageia Contributor)
- Claude (Asistente IA)
