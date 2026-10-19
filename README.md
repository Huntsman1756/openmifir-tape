# OpenMiFIR Tape

Laboratorio reproducible y auditable de transparencia post-mercado para datos
de bonos bajo MiFIR. Primer perfil: **bonos corporativos españoles**
(`es-corporate-bonds`), observados a través de dos fuentes de transparencia
independientes.

No es un proveedor de cinta consolidada ni compite con fairCT: cuando la CTP
oficial de bonos de la UE esté operativa, será una fuente más y un punto de
comparación.

## Qué ofrece

- **Captura verificable**: descarga los bytes exactos publicados por cada
  fuente, write-once, con SHA-256 registrado en el momento de la captura.
- **Reproducibilidad**: repetir la ingesta de los mismos bytes no produce
  ningún cambio de estado no intencionado (idempotencia verificada).
- **Clasificación del universo**: resolución determinista de los bonos
  corporativos españoles (tipo `CRPB` + LEI de emisor con jurisdicción
  legal `ES`) a partir de los registros oficiales de ESMA y GLEIF. Ante
  cualquier ambigüedad, el registro se marca para revisión en lugar de
  adivinar.
- **Historial completo de cada operación**: reconstrucción del ciclo de vida
  de cada trade (ejecución, publicación diferida, enmienda, cancelación) a
  partir de las publicaciones sucesivas, sin sobrescribir nada.
- **Medición de divergencia entre fuentes**: informe de fragmentación
  observada entre las fuentes, sin pretender medir la completitud del
  mercado.

## Fuentes de datos

| Fuente | Formato | Frecuencia de publicación | Enlace |
|---|---|---|---|
| BME APA (transparencia post-mercado) | JSON | fichero diario | [bolsasymercados.es](https://www.bolsasymercados.es/en/other-services/regulatory-services/post-trade-data.html) |
| Bloomberg APAE (informes diferidos de su APA europea) | CSV | fichero por intervalo de publicación | [bloombergapa.com](https://www.bloombergapa.com/) · [histórico](https://www.bloombergapa.com/historyfiles) |

Los recolectores consultan cada fuente como máximo cada hora y reescanean la
ventana de las últimas 24 h a diario (intervalos configurados en
[`config/sources/`](config/sources)). Los endpoints viven en descriptores de
configuración, nunca en el código.

Para clasificar el universo se usan además tres registros oficiales, que el
operador aporta localmente:

- [ESMA FITRS](https://data.esma.europa.eu/) — tipo de instrumento no
  volumétrico (campo de transparencia no-equity).
- [ESMA FIRDS](https://data.esma.europa.eu/) — LEI del emisor.
- [GLEIF](https://api.gleif.org/) — jurisdicción legal del LEI.

## Cómo funciona

```
   BME APA ─┐
            ├─► captura (bytes crudos inmutables + SHA-256)
   Bloomberg APAE ┘            │
                         ▼
             normalización sin pérdida
                         │
                         ▼
   ESMA + GLEIF ──► universo (bonos corporativos ES, fail-closed)
                         │
                         ▼
        libro de eventos (ciclo de vida, append-only)
                         │
                         ▼
     cinta auditable local (Parquet) ──► verificación y
                                         divergencia entre fuentes
```

Cada paso guarda en `evidence/` solo metadatos (hashes, recuentos, logs);
los datos de las fuentes nunca se suben a este repositorio. CI es offline y
trabaja con fixtures sintéticas.

## Uso local

Requiere Python 3.11+.

```bash
pip install -e ".[dev]"        # instala con pytest y ruff
python -m pytest               # suite de tests offline
ruff check .                   # lint
python tools/check_hygiene.py  # guarda de higiene del repositorio
```

Los comandos de adquisición se ejecutan en el entorno del operador (hacen
peticiones reales a las fuentes) y escriben en `data/` (gitignored) y
`evidence/` (solo metadatos):

```bash
omt-g0a1 --source all --max-objects 1   # captura puntual de prueba
omt-g0a2                                # idempotencia: repetir y verificar
omt-g0a3 --mode poll                    # captura programada (cron/systemd)
```

Todo comando acepta `--help`. Para operar la captura de forma continua,
consulta el runbook de [`ops/A3-runbook.md`](ops/A3-runbook.md) y las
unidades de referencia de [`ops/systemd/`](ops/systemd).

La especificación detallada del proyecto está en
[`docs/gates/`](docs/gates); el flujo de trabajo de contribución, en
[`CONTRIBUTING.md`](CONTRIBUTING.md). Cuestiones de seguridad, en
[`SECURITY.md`](SECURITY.md).

## Licencia

MIT — ver [`LICENSE`](LICENSE). La licencia cubre solo el código, las
fixtures y la documentación de este repositorio; los datos y materiales de
terceros (BME, Bloomberg, ESMA, GLEIF, …) están sujetos a sus propios
términos de uso — ver [`NOTICE`](NOTICE) y [`docs/legal/`](docs/legal).
