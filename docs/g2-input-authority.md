# Validar insumos G2 sin alterar el corpus G1

G2-A1 proporciona funciones offline para importar el origen G1 verificado y
planificar la admisión de ventanas prospectivas. La autoridad semántica sigue
siendo [G2-A](gates/G2.md#G2-A); estos puntos de entrada no adquieren datos,
activan builds ni adjudican resultados operativos.

## Orden de uso

```python
from pathlib import Path
from metrics.g2_origin import import_origin
from metrics.g2_ingest import admit_window

origin = import_origin(Path("data/adjudication/g0-a3-bundle-20260924T181127Z"))

# profile_doc y bundle_doc son metadatos del operador, no payloads.
# La primera ventana prospectiva se prepara después de verificar A1/B1/C1/C2.
planned = admit_window(
    Path("data/g2/windows/<window-id>"),
    Path("data/raw"),
    profile_doc,
    bundle_doc,
    origin["registry"],
)
```

`registry` contiene exclusivamente autoridad, identidades, tiempos, membresías
y spans entre ventanas. La función devuelve un nuevo valor; no escribe un
registro aceptado ni cambia el anterior. La persistencia y activación atómicas
pertenecen a G2-C1. `selection["versions"]` incluye rutas locales para el
procesamiento posterior; no se debe publicar el resultado completo.

## Esquemas y comprobaciones

| Entrada | Validación y localización |
| --- | --- |
| Perfil `g2-window-profile-v1` | Campos exactos y validaciones en `metrics.g2_authority.profile`; su huella se calcula con `digest` |
| Bundle `g2-window-bundle-v1` | Perfil, manifiestos y journal autenticados; nombres relativos únicos y ordenados; revisiones y hora de sellado |
| Descriptores | Bytes en `config/sources/bme_apa.yaml` y/o `config/sources/bloomberg_apae.yaml`, según las fuentes del perfil |
| Manifiestos y journal | Formatos del colector G0-A3; identidad, observación, estado y modo reconciliados para cada intento elegible |
| Raw local | Namespace y sidecar originales de `RawStore`; cada copia se comprueba por identidad y SHA-256 |
| Origen G1 | Bundle original exacto, verificación de todos los archivos declarados y comparación con el selector G1 inclusivo |

Los ejemplos sintéticos ejecutables están en `tests/g2/`. Los errores de
autoridad rechazan la admisión. Las capturas no parseables y registros sin
identificador siguen presentes en el censo con diagnósticos; no se convierten
silenciosamente en un corpus vacío satisfactorio. Un replay exacto conserva
el digest del registro. Las incorporaciones retroactivas requieren el
contrato de corrección separado previsto por la especificación.

```bash
python -m pytest tests/g2 --basetemp <directorio-temporal-del-runner>
```

La admisión técnica devuelve `operational_verdict: NOT_ASSESSED`; los spans
también permanecen `NOT_ASSESSED`. G2-A3 adjudicará la evidencia acumulada.
Las referencias heredadas conservan `LEGACY_BUILD_SNAPSHOT`; esta importación
no las transforma en observaciones temporales. Ninguna de estas funciones
declara cobertura de mercado ni el PASS de G2.
