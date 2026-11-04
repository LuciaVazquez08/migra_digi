# Migración Digicorp

Analiza, valida, reorganiza y migra la documentación de clientes desde `E:\Raw Data` hacia la
estructura estandarizada de `E:\Digicorp`. La unidad de migración es el **cliente completo**: si
algo no es verificable con certeza, no se migra ningún archivo de ese cliente.

Las reglas exactas están en [docs/REGLAS.md](docs/REGLAS.md).

```
E:\Digicorp\<CUIT>\
├── Documentacion Corpo Fija\
│   ├── <contrato>\*.pdf
│   └── Documentacion Facultativa\   ← documentos originales (no URL) de Facultativa
└── Documentacion Movil\
    ├── <SDS>\*.pdf
    └── Documentacion Facultativa\   ← .url de Facultativa descargados como PDF
```

## Instalación (Windows)

Requisitos: Python 3.11 o superior y, opcionalmente, LibreOffice para convertir documentos Office a PDF.

```powershell
git clone <url-del-repo> C:\migracion-digicorp
cd C:\migracion-digicorp
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium        # navegador para los enlaces de Salesforce
copy config.example.toml config.local.toml   # y ajustar rutas
```

> `config.local.toml`, los Excel, los `.url` y la carpeta `trabajo\` **no se versionan**: contienen
> CUITs y enlaces que dan acceso a documentos personales.

## Uso

Todos los comandos se ejecutan desde la carpeta del repo con el entorno activado.

| Paso | Comando | Qué hace |
|---|---|---|
| 1 | `python -m migracion_digicorp inventario` | Recorre el origen (solo lectura) y lo contrasta con el Excel. El reporte incluye la hoja *Inventario* |
| 2 | `python -m migracion_digicorp diagnostico` | Aplica todas las reglas sin red ni escritura y clasifica cada cliente como APTO o REVISION_MANUAL |
| 2b | `python -m migracion_digicorp diagnostico --validar-urls` | Además descarga a la caché y valida los PDF. No prepara ni publica |
| 3 | `python -m migracion_digicorp migrar` | **Simulación** (por defecto): prepara el staging, ejecuta la validación final y lo descarta. Estado `SIMULADO_OK` |
| 4 | `modo = "real"` en `config.local.toml` + `python -m migracion_digicorp migrar --confirmar-ejecucion-real` | Publica. Pide escribir `MIGRAR`. Máximo 2000 clientes por ejecución |
| – | `python -m migracion_digicorp probar-url "ruta\DNI.url"` | Prueba la descarga de enlaces puntuales |
| – | `python herramientas\resumen_urls.py --desde 20000000000 --limite 200` | Resumen de dominios de los enlaces y de las copias, sin CUITs ni tokens |
| – | `python -m migracion_digicorp estado` | Muestra el resumen de estados persistidos |

Filtros disponibles en `inventario`, `diagnostico` y `migrar`:

- `--cuits 20123456786 30712345678`
- `--desde 30000000000`
- `--limite-clientes 100`
- `--sin-plan` (no genera la hoja de plan; conviene en corridas completas)

**Ejecución por etapas:**

- Para detener de forma ordenada, presioná Ctrl+C una vez o creá el archivo `trabajo\DETENER`.
  El cliente en curso se descarta entero.
- Al volver a ejecutar, los clientes ya publicados se omiten y las descargas válidas se toman de la
  caché.
- El failsafe detiene la ejecución real al llegar a 2000 clientes publicados. Para continuar,
  ejecutá el comando otra vez.

## Migrar ahora lo que no depende de un dominio bloqueado

Si un dominio no se puede descargar en este equipo (p. ej. s1gateway por la inspección SSL de
Fortinet), agregar en `config.local.toml`:

```toml
dominios_postergados = ["cdnva.s1gateway.com"]
```

Esos clientes quedan `POSTERGADO` y se migran después en otro equipo, sin esa línea. En el otro equipo
conviene usar **la misma carpeta `trabajo\`** (copiada o compartida): así los ya publicados figuran
como `OMITIDO_YA_PUBLICADO`. Con una carpeta de trabajo nueva tampoco se pierde ni se pisa nada, pero
los ya publicados aparecerían como `DESTINO_EXISTENTE` en el reporte.

## Salidas (en `trabajo\`)

| Ruta | Contenido |
|---|---|
| `reportes\reporte_<comando>_<id>.xlsx` | Hojas *Resumen por cliente*, *Detalle de incidencias*, *Plan de archivos* e *Inventario* |
| `logs\migracion_<id>.log` | Log técnico con fecha, etapa y error |
| `manifiestos\<CUIT>.json` | Archivos publicados con origen, SHA-256 y motivo de cada exclusión |
| `estado.sqlite` | Estado reanudable de clientes, descargas y ejecuciones |
| `cache_descargas\` | PDF descargados y validados |
| `staging\` | Preparación temporal (se vacía sola) |

## Estructura del código

| Módulo | Fase |
|---|---|
| `config.py` | Configuración centralizada |
| `inventario.py` | 1. Inventario y lectura del Excel |
| `reglas.py`, `diagnostico.py` | 2. Diagnóstico y plan por cliente |
| `descarga.py`, `validar_pdf.py`, `conversion.py`, `preparacion.py` | 3. Preparación temporal |
| `publicacion.py` | 4–5. Validación final y publicación atómica |
| `reporte.py` | 6. Reporte |
| `orquestador.py`, `estado.py`, `__main__.py` | Ejecución, reanudación y CLI |

## Tests

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

Cubren:

- el caso normal con la estructura exacta;
- carpetas SDS y de contrato vacías;
- CUIT incorrectos;
- diferencias con el Excel;
- URL rotas o truncadas;
- respuestas HTML;
- PDF corruptos;
- copias y duplicados;
- colisiones de nombres;
- plantillas con contenido;
- archivos sueltos;
- extensiones bloqueantes;
- SDS repetidas en varios clientes;
- conflicto de destino;
- error durante la publicación;
- idempotencia;
- failsafe;
- parada y reanudación.

GitHub Actions los ejecuta en Windows y Linux en cada push.

## Flujo de trabajo entre equipos

- Proteger `main` en GitHub (Settings → Branches): los cambios entran por pull request con los tests en verde.
- Un cambio de regla actualiza primero `docs/REGLAS.md` e incluye un test que lo cubra.
- Nunca subas datos reales (Excel, `.url`, PDF, reportes): el `.gitignore` los bloquea.

## Problemas de red

Si la ejecución se detiene con `ERROR_DE_ENTORNO` por un certificado HTTPS "no de confianza":

1. El programa ya valida con el almacén de certificados de Windows (`truststore`).
2. Si igual falla, la red corporativa suele usar un **proxy con script PAC** que el navegador sigue y
   Python no. Pedir a sistemas la dirección del proxy y definirla antes de ejecutar:
   ```powershell
   $env:HTTPS_PROXY = "http://proxy.empresa:8080"
   ```
3. Ver quién emite el certificado (solo lo lee, no descarga nada):
   ```powershell
   powershell -ExecutionPolicy Bypass -File herramientas\ver_certificado.ps1
   ```
   Si el emisor es de la empresa (firewall/proxy de inspección), pedir a sistemas el certificado raíz
   oficial en formato PEM (Base64) y configurarlo en `config.local.toml`:
   ```toml
   certificado_adicional = 'C:\ruta\raiz_empresa.pem'
   ```
   Se suma a las autoridades públicas; la verificación sigue activa.
4. Si el enlace abre a mano en el navegador sin advertencias: descargar esos dominios con el navegador automático, que
   usa su propia red y sus certificados (igual que cuando se abre el enlace a mano). En
   `config.local.toml`:
   ```toml
   dominios_via_navegador = ["cdnva.s1gateway.com"]
   ```
   Es más lento que la descarga directa, pero no cambia ninguna validación.

Nunca desactivar la verificación de certificados.

## Recuperar documentos de SDS problemáticas por CASE_ID

Para las SDS cuyos enlaces están postergados, cortados o no se pudieron descargar, se buscan los
documentos en la carpeta de casos (OneDrive) por su CASE_ID. **No migra nada**: copia, identifica y valida.

1. **Copiar la carpeta a E:** (queda en `E:\AMX Argentina S.A`, con la misma estructura):
   ```powershell
   python -m migracion_digicorp --config config.real.toml copiar-carpeta --solo-medir
   python -m migracion_digicorp --config config.real.toml copiar-carpeta
   ```
   Con `--casos casos.csv` (después de los pasos 2 y 3) copia **solo** los archivos cuyo nombre contiene
   los CASE_ID de las SDS problemáticas: mucho más rápido que bajar toda la carpeta de OneDrive.
   Reanudable (Ctrl+C o `trabajo\DETENER`). Los archivos "solo en la nube" se traen a C:, se copian
   y se devuelven a "solo en la nube"; si C: no tiene espacio se saltean y quedan en el manifiesto
   `trabajo\recuperacion\copia_manifiesto.csv`. El origen nunca se borra ni se modifica.
2. **Listar las SDS con problemas y generar la consulta:**
   ```powershell
   python -m migracion_digicorp --config config.real.toml sds-problematicas
   ```
   Genera `trabajo\recuperacion\sds_problematicas.csv` y `consulta_case_id.sql`.
3. **Obtener los CASE_ID desde Oracle** (lee la contraseña de la primera línea de `oracle_archivo_password`,
   por defecto `E:\Migracion Digicorp\Pswd.txt`; nunca se muestra ni se guarda):
   ```powershell
   python -m migracion_digicorp --config config.real.toml consultar-casos
   ```
   Genera `trabajo\recuperacion\casos.csv`. (Alternativa: ejecutar `consulta_case_id_unica.sql` a mano y
   exportar `SUBJECT`, `CASE_ID` a CSV.)
4. **Buscar y validar:**
   ```powershell
   python -m migracion_digicorp --config config.real.toml buscar-casos --casos "E:\Migracion Digicorp\casos.csv"
   ```
   Reporte `trabajo\recuperacion\recuperacion_<fecha>.xlsx`: por SDS, sus CASE_ID, archivos
   encontrados, válidos y corruptos.

### Número de caso por SDS (CNA.ACT_S1_DETALLE_CASOS)

```powershell
python -m migracion_digicorp --config config.real.toml consultar-sds --muestra 5   # prueba con 5 SDS
python -m migracion_digicorp --config config.real.toml consultar-sds              # todas (reanudable)
python -m migracion_digicorp --config config.real.toml buscar-en-original
```

`consultar-sds` ejecuta, por cada SDS pendiente, `SELECT * FROM CNA.ACT_S1_DETALLE_CASOS WHERE ASUNTO LIKE
'%<SDS>%'` (si no hay filas, repite con las letras en minúscula) y guarda `casos_por_sds.csv` y
`detalle_casos_por_sds.csv`. `buscar-en-original` cuenta en cuántas SDS el número de caso aparece en los
nombres de archivo de la carpeta original (sin descargar nada), con el número completo y sin el primer dígito.

### Recuperar documentos de las SDS y migrarlos

```powershell
python -m migracion_digicorp --config config.real.toml recuperar-sds --muestra --clientes 4
```

Para cada SDS con problema y número de caso, busca en la carpeta original los archivos cuyo nombre
contiene el número de caso, los copia a `trabajo\recuperacion\archivos\<SDS>\` (desde la nube si hace
falta, sin llenar C:) y los valida. Una SDS queda **RECUPERADA** si tiene al menos tantos archivos válidos
como enlaces `.url` hay en su carpeta (planillas, accesos directos y duplicados no cuentan).

Para migrar usando esos documentos, en la configuración:

```toml
archivo_recuperados = 'E:\Migracion Digicorp\trabajo\recuperacion\recuperados.csv'
```

En cada SDS RECUPERADA los enlaces se reemplazan por los documentos recuperados; el resto del cliente se
migra igual que siempre y siguen valiendo todos los bloqueos (un cliente con otro problema no se migra).
