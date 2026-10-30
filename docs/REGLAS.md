# Reglas de migración

Este documento es la especificación que implementa el código. Cualquier cambio de regla se hace
primero acá, después en el código (`migracion_digicorp/reglas.py` y `diagnostico.py`) y se cubre
con un test.

**Principio:** la unidad de migración es el cliente completo. Si una regla bloqueante falla, no
se publica **ningún** archivo del cliente. Un cliente bloqueado no afecta a los demás.

## 1. Identificación del cliente

| Regla | Resultado |
|---|---|
| El nombre de la carpeta tiene exactamente 11 dígitos | requisito |
| El dígito verificador del CUIT (módulo 11) es correcto (`validar_digito_verificador`) | requisito |
| La carpeta del cliente figura en `Arbol_Carpetas` del Excel | requisito |
| El árbol físico es igual al del Excel (sin contar `desktop.ini`/`Thumbs.db`) | requisito |
| Archivo suelto en la raíz de `E:\Raw Data` | revisión manual |

## 2. Categorías (nivel 2)

La comparación ignora mayúsculas, tildes y espacios repetidos.

| Carpeta de origen | Categoría |
|---|---|
| `Documentación Móvil` | Móvil |
| `Contratos Fija` | Contratos |
| `Documentación Facultativa <CUIT>` | Facultativa. El número tiene que ser el CUIT del cliente; si falta o es otro, se bloquea |
| Cualquier otra (`Excepciones…`, `Cloud`, SDS sueltas…) | bloquea: `CARPETA_NO_RECONOCIDA` |
| Dos carpetas de la misma categoría | bloquea: `CATEGORIA_DUPLICADA` |
| Archivo suelto en la raíz del cliente | bloquea: `ARCHIVO_SUELTO` |

No es obligatorio que existan las tres categorías. Un cliente sin ningún documento migrable se
bloquea (`CLIENTE_SIN_DOCUMENTACION`).

## 3. Documentación Móvil → `Documentacion Movil\<SDS>\`

- SDS válida: exactamente `\d{8}[A-Z]{3}` (p. ej. `29067459SEC`, `00497937TAE`). Otro nombre bloquea
  (`NOMBRE_SDS_INVALIDO`).
- SDS sin documentos bloquea (`SDS_VACIA`).
- Subcarpeta dentro de una SDS bloquea (`SUBCARPETA_INESPERADA`): la pertenencia es ambigua.
- La misma SDS en más de un cliente (según el Excel) bloquea a todos (`SDS_EN_VARIOS_CLIENTES`).
- Plantilla `Numero de SDS`: si está vacía, se descarta; si tiene archivos, bloquea
  (`PLACEHOLDER_CON_CONTENIDO`): hay que renombrarla a mano.
- Archivo suelto en `Documentación Móvil`: si su nombre contiene exactamente una SDS, se ubica en
  esa carpeta (se crea si no existe). Si no, bloquea.

## 4. Contratos Fija → `Documentacion Corpo Fija\<contrato>\`

- Contrato válido: exactamente 8 dígitos. Nombres con varios números
  (`60399279 60399275 …`) o con texto bloquean (`NOMBRE_CONTRATO_INVALIDO`).
- Contrato sin documentos bloquea (`CONTRATO_VACIA`). Una subcarpeta bloquea.
- El mismo contrato en más de un cliente bloquea.
- Plantilla `Numero Os` (con su `Contrato Firmado`): si está vacía, se descarta; si tiene archivos,
  bloquea.
- Archivo suelto: si el nombre es exactamente `<8 dígitos>.ext` u `OS <8 dígitos>.ext`, va a esa
  carpeta de contrato. Si no, bloquea.

## 5. Documentación Facultativa

| Archivo | Destino |
|---|---|
| `.url` cuyo nombre no es de copia | descarga → `Documentacion Movil\Documentacion Facultativa\<nombre>.pdf` |
| `Copia X.url` / `Copia de X.url` / `X - copia.url` cuyo original `X.url` existe en Documentación Móvil del cliente | **se excluye**, aunque apunte a otro destino (p. ej. SharePoint) o no tenga URL legible: la copia se genera a partir de lo cargado en Móvil. Se registra qué original la representa y con qué criterio |
| Copia sin original con ese nombre, pero con un enlace idéntico a uno de Móvil | se excluye |
| Copia sin original ni enlace coincidente en Móvil | bloquea (`COPIA_SIN_ORIGINAL`) |
| Cualquier otro formato | `Documentacion Corpo Fija\Documentacion Facultativa\` (con conversión, ver §6) |
| Subcarpeta | bloquea |

`X (1).pdf` **no** se considera copia.

## 6. Archivos

| Tipo | Tratamiento |
|---|---|
| `.url` | Se lee `URL=` de `[InternetShortcut]` (también la variante mal escrita `[InternetSortcut]` presente en las copias), se valida y se descarga a `<nombre>.pdf` |
| `.pdf` | Se valida (§7) y se copia; si es inválido, bloquea (`PDF_CORRUPTO`) |
| Imágenes (`jpg jpeg jfif png bmp tif tiff gif`) | Se convierten a PDF (todas las páginas). Si no se puede, se copia el original |
| Office (`doc docx ppt pptx rtf odt`) | Se convierten a PDF con LibreOffice si está instalado. Si no, se copia el original |
| `msg eml mht mhtml zip rar 7z txt` | Se copian tal cual (no hay conversión sin pérdida) |
| Planillas (`xls xlsx xlsm xlsb xlt xltx xltm ods csv`) | **bloquean** (`PLANILLA_EXCEL`): no son documentación y no se migran |
| `desktop.ini`, `Thumbs.db` | Se ignoran y se registran |
| `.crdownload .partial .part .tmp .lnk`, sin extensión, 0 bytes, extensión desconocida, enlace simbólico | bloquean |

## 7. Enlaces y PDF

- Solo se descargan dominios de `dominios_permitidos` (hoy `cdnva.s1gateway.com`, solo HTTPS). Los
  enlaces de **Salesforce** (`claro.my.salesforce.com/sfc/p/#…`) bloquean al cliente
  (`URL_INVALIDA: Dominio no soportado`) hasta que se defina cómo descargarlos.
- s1gateway: la ruta es `/file/get/<token>` y el token es `base64(base64(bloque cifrado))`. Si alguna
  capa no decodifica o el bloque no es múltiplo de 16 bytes, el enlace está **truncado** y se
  bloquea sin descargar.
- HTTP 401/403 (autenticación), 404/410 y otros 4xx son errores permanentes. 429/5xx, caídas de
  conexión y descargas incompletas (`Content-Length`) se reintentan con backoff exponencial.
- El contenido se valida por sus bytes, no por la extensión. Tiene que tener firma `%PDF-` y
  `%%EOF`, abrirse con pypdf, no estar protegido con contraseña y tener al menos una página. Una
  respuesta HTML (login o error) bloquea. Si el servidor devuelve una imagen, se convierte a PDF y se
  valida (`aceptar_imagen_descargada`).
- Las descargas válidas quedan en caché por SHA-256 de la URL y no se repiten en ejecuciones
  siguientes.

## 8. Nombres, duplicados y colisiones

- Se conserva el nombre original; solo cambia la extensión a `.pdf` cuando hay conversión o descarga.
- Dentro de una misma carpeta de destino, dos archivos con **contenido idéntico** (SHA-256): se
  conserva el primero en orden alfabético del origen y se excluye el resto, con registro.
- Dentro de una misma carpeta de destino, dos archivos distintos con el **mismo nombre** (sin
  distinguir mayúsculas): el primero en orden alfabético del origen conserva el nombre; los
  siguientes reciben ` (2)`, ` (3)`… Queda registrado en `RENOMBRADO_POR_COLISION`.
- Nunca se deduplican archivos entre carpetas distintas (otra SDS u otro contrato).

## 9. Validación final y publicación

Antes de publicar se verifica:

- CUIT válido y plan sin incidencias bloqueantes.
- El destino `E:\Digicorp\<CUIT>` **no existe**. Si existe y no lo publicó este proceso, el cliente
  se bloquea (`DESTINO_EXISTENTE`).
- El staging coincide exactamente con el manifiesto (tamaño y SHA-256 de cada archivo).
- La estructura es `<Categoría>\<SDS|contrato|Documentacion Facultativa>\<archivo>`, sin carpetas
  vacías.
- Todos los PDF del resultado se vuelven a validar.
- Cada archivo de origen está migrado o excluido con un motivo registrado.

La publicación es un único `rename` del staging al destino, en el mismo volumen. Si la
verificación posterior falla, el resultado se retira del destino.

## 10. Operación

- `modo = "simulacion"` por defecto. `migrar` arma y valida el staging, y lo borra sin publicar.
- Para publicar hace falta `modo = "real"` y `--confirmar-ejecucion-real`, y escribir `MIGRAR`.
- **Failsafe:** como máximo 2000 clientes publicados por ejecución. La configuración solo puede
  bajar ese tope.
- **Parada manual:** Ctrl+C una vez (o crear el archivo `trabajo\DETENER`) termina el cliente en
  curso y cierra de forma ordenada. Un cliente interrumpido no se publica y se reprocesa en la
  próxima ejecución.
- **Reanudación:** `estado.sqlite` registra los clientes publicados (no se reprocesan) y las
  descargas (no se repiten).
- El origen nunca se modifica.
