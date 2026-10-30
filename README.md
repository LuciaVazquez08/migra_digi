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
4. Alternativa sin pedir nada a sistemas: descargar esos dominios con el navegador automático, que
   usa su propia red y sus certificados (igual que cuando se abre el enlace a mano). En
   `config.local.toml`:
   ```toml
   dominios_via_navegador = ["cdnva.s1gateway.com"]
   ```
   Es más lento que la descarga directa, pero no cambia ninguna validación.

Nunca desactivar la verificación de certificados.
