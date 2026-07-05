# Contribuir a urpm-ng

urpm-ng es un pequeño proyecto voluntario. Un puñado de mantenedores, un grupo muy pequeño de testeadores regulares, y mucho por hacer. Si usas Mageia y algo aquí te llama la atención, agradeceríamos tu ayuda — incluso un "lo probé, se rompió en el paso X" de cinco minutos vale más de lo que crees.

Este documento está para dejar claro *cómo* puedes ayudar, sea cual sea tu nivel de compromiso. Nada aquí supone que hayas parcheado antes una herramienta de distribución.

## Cómo puedes ayudar

Cinco vías, de la más ligera a la más pesada. Elige la que se ajuste al tiempo que tengas — ninguna es de segunda categoría.

### 1. Pruébalo y cuéntanos qué pasa

Lo más útil que un recién llegado puede hacer. Instala urpm-ng en tu máquina (sigue la sección *Installation* del [`README.md`](README.md) para las instrucciones RPM actuales), úsalo un par de días para lo que sueles hacer con ``urpmi``, e informa de todo lo que te haya sorprendido — un cuelgue, un mensaje erróneo, una traducción ausente, un flujo que resultara torpe.

- Dónde reportar: **issues de GitHub** en <https://github.com/pvi-github/urpm-ng/issues>.
- Por favor incluye, como mínimo:
  - La versión de Mageia (``cat /etc/mageia-release``).
  - La arquitectura (``uname -m``).
  - La versión de urpm-ng (``urpm --version`` — y ``rpm -q urpm-ng-core`` para confirmar qué RPM está instalado y si es el del sistema).
  - La línea de comandos exacta que se comportó mal, qué obtuviste y qué esperabas.
- Sin necesidad de adjuntar logs a menos que los pidamos.

### 2. Traduce — o pule las traducciones existentes

Seis idiomas ya traducidos (fr / de / es / it / nl / pt). La cobertura es amplia pero no completa: cadenas se cuelan sin traducir, algunas msgstr suenan forzadas, y un oído nativo detecta falsos amigos que una primera pasada no ve. Si alguno de esos es tu lengua materna, un repaso a las traducciones existentes para pulir la formulación y adoptar giros idiomáticos locales es muy bienvenido.

- Las cadenas viven en ficheros ``.po`` bajo [`po/`](po/); ábrelos en tu editor preferido (poedit sirve).
- Las entradas vacías o ``fuzzy`` son cadenas nuevas o posiblemente desfasadas — lo más fácil por donde empezar.
- Ejecuta ``msgfmt --check-format po/<lang>.po -o /dev/null`` — si pasa, la construcción también pasará.
- Igual con la doc: los canónicos ``README.md`` / ``MIGRATION.md`` / ``CHANGELOG.md`` tienen hermanos por idioma (``README_fr.md`` etc.); también se beneficiarían de una relectura nativa.

### 3. Mejora la documentación

Páginas de manual, README, chuleta de migración, changelog — cualquier cosa en prosa. Hasta una corrección de errata es útil. Las páginas man viven en ``man/<lang>/man1/urpm.1``; valida con ``groff -man -Tutf8 -ww man/<lang>/man1/urpm.1``.

### 4. Corrige un bug o añade una funcionalidad pequeña

El backlog vive en dos sitios:

- [`TODO.md`](TODO.md) en la raíz del repo — la lista visible.
- Los varios ficheros ``doc/TODO_*.md`` — backlogs temáticos y notas por asunto. Algunos están listos para codificar, otros necesitan primero una discusión. Pregunta antes de invertir un fin de semana entero.

Sigue leyendo para el flujo build / test / patch.

### 5. Únete a la fontanería

Refactorizaciones, trabajo en el resolvedor, trabajos de fondo de ``urpmd``, trabajo en spec-files, endurecimiento de mkimage / contenedores de build. Aquí vive la hoja de ruta técnica del proyecto. Saluda primero — coordinarse evita pisarse los pies, o que te pisen.

## Obtener las fuentes y construir

Dos caminos de build. El **simple** usa ``bm`` (el wrapper ``build-mageia``) en tu máquina y solo necesita ``urpmi``. El **reproducible** usa ``urpm build`` dentro de un contenedor y necesita que urpm-ng ya esté instalado.

### Dependencias de arranque (una sola vez)

En una Mageia recién instalada, ``urpmi`` está disponible pero ``sudo`` puede no estar configurado — la forma clásica ``su -c`` funciona en todas partes:

```sh
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng

# La herramienta de build (bm) más cada BuildRequires que declara el
# spec. --buildrequires lee el spec directamente, así la lista se
# mantiene sincronizada automáticamente. bm en sí no está en los
# BuildRequires del spec (invoca rpmbuild en lugar de ser consumido
# por %build), de ahí los dos comandos.
su -c "urpmi bm && urpmi --buildrequires rpmbuild/SPECS/urpm-ng.spec"
```

### Camino simple — ``bm`` en el host

```sh
make rpm-all
```

Después instala los RPM recién construidos.

**Primera vez — todavía no hay urpm-ng en el sistema** — pasa todos los RPM a ``urpmi`` de una tacada (el filtro versión-release evita coger un build más antiguo que siga en ``RPMS/``):

```sh
RPMS=$(find rpmbuild/RPMS rpmdrake/rpmbuild/RPMS \
            -name "*-$(cat VERSION)-$(cat RELEASE).*.rpm")
su -c "urpmi $RPMS"
```

**Iteraciones siguientes** — el resolvedor de urpm-ng escanea automáticamente el directorio hermano en busca de RPM locales (reporta "Found N sibling RPMs (available for dependencies)"), así que basta con apuntar a los dos meta-paquetes:

```sh
su -c "urpm i \
    rpmbuild/RPMS/noarch/urpm-ng-all-$(cat VERSION)-$(cat RELEASE).*.rpm \
    rpmdrake/rpmbuild/RPMS/noarch/rpmdrake-ng-$(cat VERSION)-$(cat RELEASE).*.rpm"
```

### Camino reproducible — build en contenedor

Solo utilizable una vez que urpm-ng está instalado en el host (huevo-y-gallina en la primerísima instalación).

```sh
# Una sola vez: crear la imagen de build (ejemplo mga10 sobre x86_64)
su -c "urpm image make --release 10 --tag mga10-64"

# Luego, en cada build — ambos specs (urpm-ng y rpmdrake-ng)
urpm build --image mga10-64 rpmbuild/SPECS/urpm-ng.spec \
                            rpmdrake/rpmbuild/SPECS/rpmdrake-ng.spec

# Instalación — urpm-ng ya está en el host (prerrequisito de este
# camino), así que ``urpm i`` sobre los dos meta basta: el resolvedor
# coge automáticamente los RPM hermanos del mismo directorio.
su -c "urpm i \
    rpmbuild/RPMS/noarch/urpm-ng-all-$(cat VERSION)-$(cat RELEASE).*.rpm \
    rpmdrake/rpmbuild/RPMS/noarch/rpmdrake-ng-$(cat VERSION)-$(cat RELEASE).*.rpm"
```

### Ejecutar los tests

```sh
pytest urpm/tests/
```

Ver [`doc/TESTING.md`](doc/TESTING.md) para una chuleta de pytest y las lagunas de cobertura conocidas.

Para iterar en modo dev sin reconstruir un RPM cada vez, los ficheros fuente se ejecutan directamente desde el checkout — ``python -m urpm.cli.main <subcomando>`` funciona con ``$PYTHONPATH`` incluyendo la raíz del checkout.

## Tu primera contribución — el circuito completo

1. **Rama.** Desde la rama de versión activa (actualmente ``0.8.x`` — revisa el fichero ``VERSION`` en la raíz del repo si dudas). ``main`` solo lleva la historia publicada; el trabajo nuevo nunca aterriza allí directamente, llega por fast-forward-merge desde la rama de versión en el momento del release.
2. **Cambia.** Escribe el fix o la funcionalidad. Si tocas el resolvedor, la cola de transacciones o ``urpmd``, añadir un test en ``urpm/tests/`` es casi obligatorio. Para trabajo de CLI o doc, un test manual en tu máquina basta.
3. **Prueba en local.** Ejecuta ``pytest urpm/tests/`` (suite completa para lo user-visible, fichero específico si no). Arregla cualquier regresión antes de continuar.
4. **Actualiza la superficie visible** si tu cambio es user-facing (un fix en un camino de código interno raramente lo necesita):
   - añade una entrada en [`CHANGELOG.md`](CHANGELOG.md) bajo el título de la siguiente versión;
   - actualiza los catálogos ``.po`` (cualquier cadena nueva user-facing en inglés es un nuevo msgid);
   - actualiza ``man/<lang>/man1/urpm.1`` si se añadió, renombró o quitó un flag;
   - actualiza el README / la chuleta MIGRATION si el cambio afecta los comandos del día a día.
5. **Commit.** Sujeto corto (~50 caracteres), prefijo convencional (``fix(zona):``, ``feat(zona):``, ``docs:``, ``chore:``, ``test:``, ``refactor:``). El cuerpo explica el *por qué* — el diff ya muestra el *qué*.

Antes de abrir una pull request, repasa esta checklist:

- [ ] ``make rpm-all`` (o el build en contenedor) se ejecuta con éxito.
- [ ] ``pytest urpm/tests/`` pasa sin regresión.
- [ ] Has **instalado tus RPM construidos en local** y probado desde esa copia instalada (sube la línea ``release`` en ``rpmbuild/SPECS/urpm-ng.spec`` localmente para que el número de RPM sea mayor que el del sistema y se instale limpiamente encima — solo conveniencia local, nunca commitear ese bump).
- [ ] Los comandos smoke evidentes siguen funcionando sobre el build instalado, sin que tu cambio rompa ninguno:
  - ``urpm i <unpaquete>`` — camino de instalación
  - ``urpm q <unpaquete>`` — consulta
  - ``urpm e <unpaquete>`` — erase
  - ``urpm f /ruta/al/fichero`` — find
  - ``urpm m u`` — media update
  - ``urpm u`` — upgrade del sistema
- [ ] Tu rama está **rebased** sobre la rama destino (sin commits de merge entre tu trabajo y la punta).
- [ ] Docs / páginas man / traducciones actualizadas como en el paso 4.

6. **Push** a tu fork o a tu rama.
7. **Abre una pull request** en GitHub. Describe la intención, la cobertura de tests y cualquier limitación conocida. Menciona la línea de release apuntada y confirma la checklist de arriba.
8. **Itera sobre la review.** Un revisor mirará tu diff y hará preguntas o sugerirá ajustes. Buscamos un intercambio entre pares — nada personal, todo sobre el código.

## Dónde encontrarnos

- **Issues & PRs**: <https://github.com/pvi-github/urpm-ng>
- **Contacto directo — Matrix**: [@maat_:matrix.org](https://matrix.to/#/@maat_:matrix.org)

## Dónde vive el código

```
urpm/                  # Fuentes Python
  cli/                 # Interfaz de línea de comandos (urpm, subcomandos)
  core/                # Resolvedor, download, install, BD, sync
  daemon/              # urpmd (servicio de fondo, P2P LAN)
  genmedia/            # Generación de metadatos del lado servidor
  tests/               # Todos los tests viven aquí (no en un tests/ raíz)
rpmdrake/              # Frontend GUI Qt6 (rpmdrake-ng)
pk-backend-urpm/       # Plugin C: backend PackageKit sobre urpm-ng
man/<lang>/man1/       # Páginas man traducidas
po/                    # Catálogos de traducción (.po)
doc/                   # Docs de diseño, planes, TODOs, specs
rpmbuild/SPECS/        # Empaquetado Mageia (.spec)
data/                  # Units systemd, reglas polkit, plantillas de config
```

Para un mapa más profundo, ver [`doc/ARCHITECTURE.md`](doc/ARCHITECTURE.md). Para el catálogo acumulado de funcionalidades, [`FEATURES.md`](FEATURES.md).

## Expectativas de estilo (corto)

- **Inglés** en el código, los comentarios y los mensajes de commit. Un histórico multilingüe desorienta.
- **Docstrings** en toda función o clase pública. Una línea sirve; explica el *por qué* solo cuando no es obvio a partir del nombre.
- **Tests** cuando sea práctico — la suite es una red anti-regresión, no una prueba formal. Los cambios user-visible deberían al menos venir con una nota de test manual.
- **Comentarios** allí donde el código esconde una sorpresa (workaround, race, invariante). Nunca un comentario que duplique el código.

## Ciclo de release

El trabajo pasa por una rama de versión (``0.8.x``, ``0.9.x``, …). Cuando una versión está lista, la rama se fast-forward-mergea a ``main``; ``main`` lleva por tanto el histórico publicado. Los tags se cortan desde ``main`` en ese momento y los RPM se publican en el canal binario del proyecto.

Los bumps de versión en ``VERSION`` / ``pyproject.toml`` / ``rpmbuild/SPECS/urpm-ng.spec`` son cosa del mantenedor — no commits un bump en tu contribución. Dicho esto, siéntete libre de subir **localmente** la línea ``release`` del spec para que tu RPM construido se instale limpiamente sobre el del sistema; solo no stageés esa línea.
