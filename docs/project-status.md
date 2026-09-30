# Estado de OpenMiFIR Tape

Revisión del 30 de septiembre de 2026. Autoridad: [G1 congelado](gates/G1.md),
especialmente §7; contrato de evaluación: issue #41. Este documento describe
el estado y las siguientes decisiones; no amplía el alcance de ningún gate.

La evaluación puede repetirse offline con los recibos registrados:

```bash
python -m metrics.g1_gate --ci evidence/g1-d3/CI_verified.json \
  --current-tape evidence/g1-d3/CURRENT_TAPE_verified.json \
  --out data/g1/gate-evaluation
```

G0 dispone de una ventana sellada y verificada. G1 completa el laboratorio
local de auditoría sobre ese corpus: universo observado, disposiciones,
tape Parquet, reconstrucción temporal, cápsulas y divergencia entre fuentes.
El cierre formal `PASS` se registra en
[la evaluación de los diez criterios](../evidence/g1-d3/G1-D3_gate_evaluation.yaml).

| Área | Estado comprobado | Continuación útil |
| --- | --- | --- |
| Backend y datos | Python, Parquet y DuckDB; ledger append-only y reconstrucción as-of | Conservar esta base; diseñar G2 antes de incorporar ventanas nuevas o referencias temporales |
| Auditoría | Explain, verificación propia, replay de procedencia y divergencia offline | Utilizar las CLI con autoridad explícita y conservar los insumos privados que permiten el replay |
| Frontend y API | Fuera del alcance de G1 | Definir usuarios, consultas y gate G4 antes de construir una superficie de producto |
| Infraestructura | Captura local con dos timers; evidencia operativa separada de la versionada | Desplegar sólo revisiones verificadas y registrar cada migración; las capturas nuevas no heredan la verificación de septiembre |
| QA | Matriz offline Linux/Windows y Python 3.11–3.13; controles de wheel instalado | Mantener contraejemplos de identidad, futuro, autoridad alterada e idempotencia; temp de pytest en el runner |
| Desarrollo | Issues con autoridad congelada; PRs pequeños y evidencia por trabajo | Resolver cada línea con su contrato, sin introducir plataforma antes del gate |

El [reporte G1-D2](../evidence/g1-d2/G1-D2_observed_fragmentation.yaml)
clasifica los 8.077 identificadores observados: 135 INCLUDE, 2.764 EXCLUDE,
760 QUARANTINE y 4.418 CONFLICT. Estos últimos tienen motivo
`NON_ES_LEGAL_JURISDICTION` según la regla congelada; no son una medida de
errores de identidad. La tape contiene 52.008 publicaciones; el ledger de
divergencia contiene 245 cadenas de instrumentos INCLUDE al cierre declarado.
No aparecen candidatos económicos entre fuentes en ese corte: esto describe
el corpus, sin implicar ausencia de negociación ni representatividad del mercado.

La separación entre tiempo de negociación, publicación y primera observación
es parte del contrato. La clasificación regulatoria usa `BUILD_SNAPSHOT`;
G1 no proporciona historia de referencias ni una tape que se actualice sola.
Un objeto BME vacío consta explícitamente como diagnóstico no parseable.

La [clasificación de permisos](legal/artifact_classification.yaml) es una
fotografía del 15 de septiembre, con cuatro celdas UNRESOLVED y una FAIL.
El cierre técnico de G1 no cambia esos estados ni concede redistribución.
Antes de una publicación de datos derivados habría que resolver los permisos
aplicables y definir el producto autorizado.

Conviene mantener lo construido y continuar por gates. La siguiente línea
con más utilidad es especificar G2: nuevas ventanas selladas, conservación de
referencias por fecha, continuidad y tratamiento explícito de gaps. G3 puede
añadir comparaciones cuando exista una fuente y un contrato verificables.
G4 requiere una decisión de producto; una interfaz ahora no resolvería las
limitaciones temporales ni los permisos pendientes.

La evidencia de CI comprueba código y fixtures; la salud del VPS requiere su
propio readback. El [despliegue verificado](../evidence/g0-a3-h2/G0-A3-H2_deployment.json)
del 30 de septiembre instaló `2cedea5`, migró 1.402 manifiestos con SHA-256,
conservó originales y journal, y mantuvo ambos timers activos. La primera
ronda posterior terminó `SUCCEEDED` para ambas fuentes y escribió en
`data/a3/evidence/`. Este readback no declara otra ventana de captura verificada.
Esta revisión no constituye un pentest ni una auditoría de
seguridad exhaustiva.
