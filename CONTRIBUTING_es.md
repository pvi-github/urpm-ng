# Contribuir a urpm-ng

Gracias por tomarse el tiempo de leer este documento.  urpm-ng aspira
a ser el reemplazo en Python de la cadena de herramientas `urpmi`
histórica de Mageia, y toda mejora — fix, funcionalidad, doc,
traducción, prueba — es bienvenida.

Este proyecto es llevado por una comunidad de voluntarios.  El tono de
nuestras revisiones y discusiones es *entre pares* : señalamos el
código, no a las personas ; formulamos sugerencias, no órdenes ;
hacemos preguntas honestas en lugar de retóricas.

> Las traducciones de este documento están en
> [`CONTRIBUTING.md`](CONTRIBUTING.md) (inglés, versión de referencia),
> [`CONTRIBUTING_de.md`](CONTRIBUTING_de.md),
> [`CONTRIBUTING_fr.md`](CONTRIBUTING_fr.md),
> [`CONTRIBUTING_it.md`](CONTRIBUTING_it.md),
> [`CONTRIBUTING_nl.md`](CONTRIBUTING_nl.md), y
> [`CONTRIBUTING_pt.md`](CONTRIBUTING_pt.md).

## Objetivos del proyecto

- Código **legible** : nombres explícitos, funciones cortas,
  responsabilidades claras.
- **Documentado** : docstrings en toda la API pública, comentarios
  donde el *porqué* no es obvio.
- **Fácil de onboardear** : un nuevo colaborador debe poder
  comprender un módulo sin leer todo el proyecto antes.
- **Coherente** : convenciones uniformes en todo el código.
- **Eficaz** : la calidad y el rendimiento no son incompatibles —
  buscamos ambos.

## Puesta en marcha

Requisitos previos :

- Python 3.13 o más reciente
- `python3-solv`, `python3-zstandard`, `python3-rpm`, `python3-curl`,
  `python3-pyyaml`
- `rpmbuild` (para las fixtures de prueba y el pipeline de build)

Checkout funcional :

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng
urpmi python3-solv python3-zstandard
pytest urpm/tests/
```

Ver [`doc/HOWTO_TESTS.md`](doc/HOWTO_TESTS.md) para el cheat sheet de
pytest, y [`doc/TESTING.md`](doc/TESTING.md) para el estado de la
cobertura y las lagunas conocidas.

## Flujo de trabajo

1. Rama desde `main` (o desde la rama de release activa si existe —
   por ejemplo `0.7.x` estuvo activa hasta la 0.7.15, `0.8.x` es la
   que se prepara en el momento de la redacción).
2. Escribir el cambio, con pruebas si es posible.
3. Lanzar las pruebas pertinentes localmente.  Las pruebas manuales
   siguen siendo necesarias para todo lo visible al usuario — la suite
   es una red anti-regresión, no una garantía funcional.
4. Abrir una pull request describiendo lo que hace el cambio *y por
   qué*.

## Convenciones de commit

- **Idioma** : inglés.  Los mensajes mixtos confunden el historial.
- **Asunto** : ≤ 50 caracteres, prefijo conventional-commit
  (`fix(...)`, `feat(...)`, `docs(...)`, `chore(...)`, `test(...)`).
- **Cuerpo** : explicar el *porqué*, no solo el *qué*.  El diff ya
  muestra el qué.
- **Sin atribución IA** : el hook de commit rechaza las líneas
  `Co-Authored-By` que nombran asistentes IA.  El autor del commit es
  el humano que tecleó `git commit`.

## Reglas de higiene git

- Nunca hacer `git add .` ni `git add -A`.  Añadir los archivos uno
  por uno — evita commitear accidentalmente un `.env`, un borrador o
  un artefacto generado.
- Siempre hacer `git status` antes de commitear para ver la lista de
  archivos stageados.
- Nunca commitear sin confirmación explícita, del reviewer o de uno
  mismo después de un `git status`.
- Nunca saltar los hooks de commit.  Si un hook se queja, corregir la
  causa en lugar de `--no-verify`.

## Lo que NO se commitea

- **Borradores** en la raíz del repo o bajo `doc/`.  Los borradores
  vivos (`RELEASE_NOTE.md`, `doc/TODO_*.md` en curso, `SPEC_*.md`
  antes de validación) viven en tu checkout local y se promueven al
  worktree solo cuando validados.
- **Artefactos de prueba locales** : `essais/` está en gitignore —
  poner los scripts transitorios, los workdirs de smoke test, las
  salidas de `mktemp` ahí.  Nunca en la raíz del repo.
- **Todo lo nombrado `bin/rebuild.sh` o `bin/recup.sh`** — scripts
  personales que no tienen nada que hacer upstream.
- **Vocabulario DNF** : urpm-ng apunta al ecosistema Mageia.
  Comparar a DNF en una nota a pie de página es aceptable, pero
  evitar tomar prestados los nombres de comandos DNF
  (`skip-broken`, `distro-sync`, ...) para nuestras propias opciones.
- **Archivos generados** : catálogos `.mo`, `__pycache__/`, `*.pyc`,
  builds Sphinx — todos gitignorados, dejar así.

## Convenciones de documentación

- **Idiomas** : código, comentarios y mensajes de commit en inglés.
  La doc de usuario bajo `doc/` está en mezcla francés/inglés (statu
  quo) ; las cadenas user-facing traducidas pasan por `po/` y las
  páginas man viven en `man/<lang>/`.
- **Tono** : pedagógico y explotable.  Un informe debe ser legible
  por alguien que no siguió la conversación.  Evitar las tablas
  telegráficas con códigos crípticos (C1/N7) salvo que cada celda
  esté explicada.
- **La longitud no es una virtud**, pero la brevedad a costa de la
  claridad tampoco.  Cada párrafo debe ofrecer al lector algo que no
  podría deducir solo.

## Pruebas

- Las pruebas viven en `urpm/tests/`, **no** en un directorio
  `tests/` en la raíz.  Sintetizar los RPM con
  `urpm/tests/gen_test_rpms.py` y lanzar pytest desde
  `urpm/tests/` (la infra de pruebas requiere este directorio de
  trabajo para algunas pruebas de integración).
- Los directorios temporales por prueba se limpian ahora mediante
  una fixture autouse en `BaseUrpmiTest`, así que una prueba que
  falla antes de su cleanup explícito ya no fuga entradas de `/tmp`.
- Para cambios en páginas man, validar con
  `groff -man -Tutf8 -ww man/<lang>/man1/urpm.1` — los warnings
  UTF-8 preexistentes no son bloqueantes, los errores nuevos sí.

## Revisión de código

Nos revisamos entre pares.  Cuando recibes una revisión :

- El reviewer apunta al código, no a ti.
- « ¿Por qué hiciste X ? » es una pregunta real ; responde
  honestamente en lugar de defenderte.
- « ¿Podríamos hacer Y en su lugar ? » es una propuesta, no un
  veredicto.

Cuando das una revisión :

- La autodepreciación está bien, la prescripción no.  Sugiere, no
  dictes.
- Pregunta antes de presumir.  « Puede que se me escape algo,
  pero ... » es una buena apertura.
- Marca los blockers con claridad ; los nitpicks de estilo siguen
  siendo nitpicks de estilo.

## Releases

El trabajo de release ocurre en una rama de versión (`0.7.x`,
`0.8.x`, ...) y se ff-merge a `main` en el punto de release.  `main`
lleva el historial de releases ; la rama activa lleva el trabajo en
curso.

`urpm/__init__.py` y `pyproject.toml` se bumpean juntos al taguear
una release.  Nunca decidir unilateralmente un número de versión —
preguntar al propietario del proyecto.

## Dónde viven las cosas

```
urpm/                  # Fuente
  cli/                 # Interfaz de línea de comandos
  core/                # Resolver, base de datos, download, install...
  daemon/              # urpmd
  genmedia/            # Generación lado servidor (urpm genmedia)
  tests/               # TODAS las pruebas viven aquí, NO en /tests/
rpmdrake/              # Front-end gráfico
man/<lang>/man1/       # Páginas man traducidas
po/                    # Catálogos de traducción (.po por idioma)
doc/                   # Docs de diseño, planes, archivos TODO
rpmbuild/SPECS/        # Archivos .spec de empaquetado Mageia
data/                  # Unidades systemd, reglas polkit, etc.
```

Para el catálogo cumulativo de funcionalidades de urpm-ng, ver
[`FEATURES.md`](FEATURES.md).  Para el historial de releases, ver
[`CHANGELOG.md`](CHANGELOG.md).  Para los backlogs, ver
[`TODO.md`](TODO.md) y los archivos específicos bajo
[`doc/TODO_*.md`](doc/).
