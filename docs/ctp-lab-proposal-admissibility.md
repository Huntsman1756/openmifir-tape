# Admisibilidad de la propuesta «MiFIR Consolidated Tape Lab»

Revisión del 17 de octubre de 2026. Contrasta la propuesta de laboratorio de
cinta consolidada con la autoridad vigente. Este documento **no amplía el
alcance de ningún gate, no crea autoridad nueva y no implementa nada**: sólo
clasifica qué partes de la propuesta son admisibles hoy, cuáles requieren una
especificación congelada y cuáles chocan con las reglas permanentes de
[`AGENTS.md`](../AGENTS.md).

## Veredicto

La dirección de la propuesta es correcta: **mejorar `openmifir-tape`, no empezar
otro proyecto**. Pero sus dos piezas centrales se adelantan a la autoridad y no
pueden iniciarse como trabajo de agente:

- El **adaptador ISO 20022 contra la cinta** pertenece a **G3** (*benchmark
  sources / future official EU CTP*), que no tiene especificación congelada.
- La **pantalla interactiva** pertenece a **G4** (*query/API/product surface*),
  sin especificación congelada, sin decisión de producto y con permisos de
  redistribución sin resolver.

El «esfuerzo inicial de 5–7 días» no cubre ni el gate ni los permisos. Lo que sí
es admisible hoy es lo que ya está habilitado: continuar **G2**, y avanzar la
idea del adaptador mediante una **nota de alcance para G3** (documentación), no
mediante código.

## Clasificación por componente

| Componente de la propuesta | Gate que lo gobierna | Estado del gate | ¿Admisible ahora? |
| --- | --- | --- | --- |
| Captura BME APA + Bloomberg APAE, originales inmutables, cancelaciones/correcciones, reconstrucción temporal | G0-A / G0-C / G0-D / G1 | Congelados y verificados | Sí — ya existe |
| «Mejorar `openmifir-tape`» sin capacidad nueva | G2 | Congelado, en implementación | Sí — es la línea actual |
| Adaptador estructural contra los esquemas ISO 20022 del CTP | **G3** (`docs/gates/G1.md`, roadmap) | Sin congelar ni redactar | No — requiere autoridad |
| Trade Replay / Quality Monitor (pantalla interactiva) | **G4** (`docs/gates/G1.md`, roadmap) | Sin congelar; producto y permisos pendientes | No — requiere autoridad |

G2 §1 excluye explícitamente de su alcance `UI/API/server`, `new sources` y
`orchestration`. Por tanto el adaptador y la pantalla **no** pueden colarse como
trabajo de G2.

## Corrección de hechos citados

La motivación de 2026 es real, pero la propuesta mezcla dos cintas distintas.
El perfil del proyecto es `es-corporate-bonds`, así que esto importa:

| Afirmación de la propuesta | Verificación | Fuente |
| --- | --- | --- |
| ESMA ha autorizado a EuroCTP | Cierto, **para acciones y ETF** (autorización 27/07/2026; en servicio desde el 14/09/2026). No es la cinta de bonos. | ESMA, nota de prensa `ESMA71-545613100-2975` |
| ISO 20022 para las cintas registrado y publicado el 12/06/2026 | Cierto. Submission 266, «ESMA — Consolidated Tape Provider Data Reporting», estado *Registered and published*, 12 Jun 2026. Los mensajes son relevantes **a la vez** para la cinta de bonos y la de acciones/ETF. | ISO 20022, *Status of ISO 20022 submissions*; ESMA, página *Consolidated Tape Providers* |
| La cinta oficial de bonos ya está viva | **No.** Ediphy (**fairCT**) fue seleccionada el 03/07/2026 para la cinta de bonos; el proceso de autorización sigue en curso. | ESMA, página *Consolidated Tape Providers* |
| `openmifir-tape` captura BME APA y Bloomberg APAE, conserva originales inmutables, soporta cancelaciones/correcciones y reconstruye observaciones en el tiempo | Cierto (G0-A, G0-C, G0-D, G1). | `README.md`, `docs/gates/G1.md` |

Consecuencia para el laboratorio: el benchmark natural del perfil de bonos **no
es EuroCTP** (acciones/ETF), sino la cinta de bonos de **fairCT** cuando se
autorice. Lo que sí es utilizable ya, sin contratar datos nuevos, es el conjunto
de mensajes ISO 20022 aplicables a bonos, registrado el 12/06/2026. Esto coincide
con `AGENTS.md`: cuando la cinta oficial de bonos entre en funcionamiento, se
convierte en fuente y benchmark.

## Reglas duras afectadas

- **Regla 4 — no infrastructure ahead of the gate.** Es el bloqueo principal. Ni
  el adaptador ni la pantalla tienen gate congelado.
- **Regla 8 — no coverage claims.** Un «Quality Monitor» sólo puede medir
  fragmentación *observada*; no puede sugerir completitud de mercado.
- **Regla 1 y Regla 3.** El adaptador puede ser offline y con fixtures porque los
  esquemas ISO 20022 son públicos; aun así, el corpus capturado sigue siendo
  local y CI no puede salir a la red.
- **Permisos (G0-L).** `docs/legal/artifact_classification.yaml` conserva cuatro
  celdas `UNRESOLVED` y una `FAIL`. Una superficie de replay puede exponer datos
  derivados de proveedor; no hay derecho de redistribución concedido.

## Siguiente paso

Nada de esto cambia el estado de G2. La forma correcta de avanzar el adaptador
ISO 20022 es redactar y congelar una especificación **G3** (alcance del adaptador
estructural contra los mensajes de la cinta de bonos) antes de escribir código.
La pantalla queda supeditada a la decisión de producto de **G4** y a resolver los
permisos aplicables.
