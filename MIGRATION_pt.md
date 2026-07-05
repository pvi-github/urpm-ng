# Migrar de urpmi para urpm-ng

Uma referência de uma página para os utilizadores Mageia habituados
às ferramentas ``urpmi`` clássicas.  O ``urpm-ng`` substitui o
conjunto ``urpmi`` / ``urpme`` / ``urpmq`` / ``urpmf`` /
``urpmi.addmedia`` / ``urpmi.removemedia`` / ``urpmi.update`` por um
único binário ``urpm`` com subcomandos.

Cada subcomando tem um alias curto de uma letra — esta folha de
consulta usa as formas curtas porque é o que se escreve no dia-a-dia;
as formas longas (``install``, ``erase``, ``upgrade``, …) funcionam
de forma idêntica e leem-se melhor nos scripts.

Ler uma vez; guardar à mão quando ajudar outro utilizador a migrar.

Os parâmetros a fornecer estão indicados entre ``<parênteses
angulares>``.

## Operações sobre pacotes

| ``urpmi`` / ``urpme``                | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``urpmi <pkg>``                      | ``urpm i <pkg>``             |
| ``urpmi --auto <pkg>``               | ``urpm i -y <pkg>``          |
| ``urpmi --test <pkg>``               | ``urpm i --test <pkg>``      |
| ``urpme <pkg>``                      | ``urpm e <pkg>``             |
| ``urpmi --auto-update``              | ``urpm u``                   |
| ``urpmi --no-install <pkg>``         | ``urpm dl <pkg>``            |

Notas :
- ``--auto`` e ``-y`` são intercambiáveis em todo o ``urpm-ng``.
- ``urpm remove`` é aceite por conveniência para os utilizadores
  vindos de apt / dnf — o verbo canónico é ``e`` (``erase``).

## Gestão dos media

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
- ``m`` é o alias curto de ``media``.  ``m u`` = ``media update``,
  ``m a`` = ``media add``, ``m r`` = ``media remove``, ``m l`` =
  ``media list``, ``m disc`` = ``media discover``.  Escrever a
  forma completa ``urpm media update`` etc. funciona exatamente da
  mesma maneira.

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
- Aliases curtos : ``q`` = ``query`` (também ``search``, ``s``),
  ``sh`` = ``show``, ``d`` = ``depends``, ``rd`` = ``rdepends``
  (também ``whatrequires``, ``wr``), ``wp`` = ``whatprovides``,
  ``f`` = ``find``, ``l`` = ``list``.

## Construção / distribuição

| Mageia clássico                      | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``genhdlist2 <tree>``                | ``urpm genmedia <tree>``     |
| ``rpmbuild...`` ``bm -b <spec>``     | ``urpm build <spec>``        |
| ``mach``, ``mock``, ...              | ``urpm image make`` + ...    |
|                                      | ... ``urpm build --image``   |

## Diferenças de comportamento a conhecer

- **Um só binário, subcomandos.**  Todas as operações vivem sob
  ``urpm``.  O autocompletar Bash é instalado por defeito.
- **``urpm.cfg`` substitui ``urpmi.cfg``** em ``/etc/urpm/urpm.cfg``.
  Na primeira execução, ``urpm m import`` lê o antigo
  ``/etc/urpmi/urpmi.cfg`` e migra cada entrada, incluindo as
  baseadas em ``MIRRORLIST`` — nenhuma edição manual necessária.
- **Rollback nativo.**  ``urpm h`` (history) e ``urpm rollback``
  cobrem cada transação — não é preciso ferramenta de snapshots de
  terceiros.
- **Cache P2P LAN.**  Se ``urpmd`` correr em várias máquinas da
  mesma LAN, elas partilham automaticamente os pacotes descarregados.
  Nenhuma configuração necessária.
- **Suporte contentor / imagem de build.**  ``urpm image make``
  constrói uma imagem chroot / contentor Mageia mínima pronta para
  ``urpm build`` — sem mais engenhocas ``mach`` / ``mock``.
- **Códigos de saída estruturados** — ver ``urpm(1)`` ``EXIT CODES``.
  Os mais comuns correspondem aos do urpmi (0 = sucesso, diferente
  de zero = algo para verificar).

## Arranque rápido após a instalação (se não instalado como RPM)

```sh
# Importar os media que já tinha sob urpmi
sudo urpm m import

# Ligar espelhos aos media baseados em mirrorlist recém-importados
sudo urpm srv autoconfig

# Atualizar as listas de pacotes
sudo urpm m u

# Está pronto
urpm q firefox
sudo urpm i firefox
```

## Documentação completa

- ``urpm --help`` (também ``urpm <subcomando> --help``)
- ``man urpm``
- [README.md](README.md) — visão geral de instalação e funcionalidades
- [CHANGELOG.md](CHANGELOG.md) — histórico versão a versão
