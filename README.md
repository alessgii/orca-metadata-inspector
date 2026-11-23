# La Orca — Inspector de metadatos

**La Orca** es una aplicación web de escritorio 100% local para inspeccionar metadatos, comparar archivos en profundidad (documentos e imágenes), calcular y verificar hashes criptográficos, generar manifiestos de integridad, limpiar metadatos con verificación posterior y gestionar expedientes de investigación portables.

- **Ejecución local y privada:** el servidor escucha únicamente en `127.0.0.1` sin servicios externos ni nube. La interfaz (Tailwind CSS 4), sus iconos SVG y sus scripts se sirven desde el mismo proceso.
- **Sin conexión:** una vez instaladas las dependencias iniciales, todos los análisis, comparaciones, limpiezas y exportaciones operan offline.
- **Formatos admitidos:** `PDF`, `DOCX`, `XLS`, `XLSX`, `PPTX`, `JPEG`, `PNG`, `WebP` y contenedores `ZIP` (validados por firma binaria real, nunca solo por extensión).

## Qué hace La Orca (resumen de módulos)

La barra lateral compacta expandible (estilo Supabase, con modo fijable y soporte de tema claro/oscuro) organiza la aplicación en siete secciones:

1. **Analizador (`#workspace`):** carga de hasta 100 archivos o ZIP (hasta 100 MB por carga directa), extracción con ExifTool, detección de correlaciones entre archivos, ficha detallada con **Normalizados**, **Técnicos**, **Cronología (Máquina del tiempo)**, **Hallazgos**, **Exposición («¿Qué estoy filtrando?»)**, **Copiar como evidencia**, **JSON** y **Antes / Después (Limpieza verificada)** — incluyendo verificación visual automática de píxeles y estructura tras limpiar imágenes JPEG/PNG.
2. **Duelo (`#duel`):** comparador universal automático para **2 o más archivos**. Detecta los formatos reales por firma binaria y activa únicamente los motores compatibles:
   - **Identidad binaria:** SHA-256 y diferencia de tamaño (siempre activo).
   - **Metadatos:** comparación semántica de roles, fechas UTC, coordenadas y etiquetas técnicas.
   - **Imágenes (`JPEG`, `PNG`, `WebP`):** equivalencia de muestras decodificadas (incluido **PNG de 16 bits** sin truncamiento), equivalencia visual normalizada (orientación EXIF + ICC→sRGB + canal alfa RGBA), similitud perceptual (`SSIM`, `pHash`, `dHash`, `MAE`, `MSE`, `PSNR`), inspección estructural defensiva **JPEG** (segmentos APPn/DQT/DHT/SOF/SOS/post-EOI) y **PNG** (chunks IHDR/IDAT/CRC32/APNG), y visor interactivo (lado a lado sincronizado, alternancia A/B, deslizador, zoom y **mapa de diferencias** PNG).
   - **Documentos (`PPTX`, `PDF`, `DOCX`, `XLS`, `XLSX` y cruzado `XLS ↔ XLSX`):** extracción de contenido real y comparación estructural/semántica (diapositivas y multimedia PPTX; páginas, texto, imágenes, anotaciones y detección sin ejecución de JavaScript en PDF; párrafos, tablas, encabezados/pies, notas, comentarios, enlaces e imágenes multiparte en DOCX; hojas, celdas pobladas, fórmulas vs valores en caché y hojas renombradas/agregadas en XLS/XLSX).
   - **N archivos (3 o más):** estrategia escalonada, matriz interactiva paginada, **modo Referencia (1 vs N)**, duplicados binarios, contenido equivalente, candidatos perceptuales no transitivos, análisis profundo por lotes cancelable y exportación JSON/HTML.
3. **Investigaciones (`#investigations`):** expedientes persistentes en SQLite identificados por **UUID v7**, con estados (`Abierta`, `Pausada`, `Cerrada`), auditoría de solo anexado, incorporación de archivos/carpetas (sin guardar originales), matriz de similitud, línea de tiempo documental, mapa de relaciones, observaciones versionadas y exportación/importación individual o masiva de paquetes `.zip` portables.
4. **Laboratorio de hashes (`#hashes`):** calculadora en bloques de 128 kB (`MD5`, `SHA-1`, `SHA-256`, `SHA-512`), comparador en tiempo constante e historial en SQLite con función «Verificar nuevamente».
5. **Historial (`#history`):** consulta y gestión de sesiones guardadas en `.orca-data/history.sqlite3`.
6. **Manifiestos (`#manifests`):** generación y verificación de manifiestos de integridad JSON v1 para archivos o carpetas, con informe HTML/JSON.
7. **Guía de uso (`#guide`):** referencia rápida de políticas de limpieza, similitud, hashes y retención.

---

## Instalación detallada en Linux

### Requisitos previos
- **Python 3.12 o posterior** (`python3 --version`) con `venv` y `pip`.
- **ExifTool** (y los módulos Perl `Archive::Zip` y `Compress::Zlib` para inspección completa de Office/PDF).
- **Navegador moderno** (Firefox, Chromium, Chrome, Edge, etc.).
- *(Opcional, solo para desarrollo de estilos)* **Node.js 20+** para recompilar Tailwind CSS 4.

### Paso 1: Clonar el repositorio y crear el entorno virtual

```bash
git clone https://github.com/alessgii/orca-metadata-inspector.git
cd orca-metadata-inspector

python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

> En Debian/Ubuntu, si `python3 -m venv` indica que falta el módulo `ensurepip`, instala primero `sudo apt install python3-venv`.

### Paso 2: Instalar ExifTool

**Opción A — Paquetes del sistema (recomendada):**

```bash
# Fedora / RHEL
sudo dnf install perl-Image-ExifTool perl-Archive-Zip perl-IO-Compress

# Debian / Ubuntu
sudo apt update
sudo apt install libimage-exiftool-perl libarchive-zip-perl

# Arch Linux
sudo pacman -S perl-image-exiftool perl-archive-zip
```

Verifica que ExifTool y los módulos Perl respondan correctamente:

```bash
exiftool -ver
perl -MArchive::Zip -MCompress::Zlib -e 'print "Módulos Perl disponibles\n"'
```

**Opción B — Instalación local sin privilegios (`rootless`):**

Descarga la distribución Perl oficial desde [exiftool.org/install.html](https://exiftool.org/install.html), extráela en `.tools/exiftool-13.59/` (conservando juntos el ejecutable `exiftool` y su carpeta `lib/`) y apunta `ORCA_EXIFTOOL`:

```bash
export ORCA_EXIFTOOL="$PWD/.tools/exiftool-13.59/exiftool"
"$ORCA_EXIFTOOL" -ver
```

*(Si extraes la versión `13.59` en `.tools/exiftool-13.59/exiftool`, La Orca la detecta automáticamente incluso sin definir `ORCA_EXIFTOOL`.)*

### Paso 3: Iniciar La Orca en Linux

```bash
.venv/bin/python -m orca
```

Abre en tu navegador: [http://127.0.0.1:8000/](http://127.0.0.1:8000/)

- Para detener el servidor: `Ctrl+C`.
- Si el puerto `8000` está ocupado: `.venv/bin/python -m orca --port 8001` y abre `http://127.0.0.1:8001/`.

---

## Instalación detallada en Windows

### Requisitos previos
- **Python 3.12 o posterior** instalado desde [python.org](https://www.python.org/downloads/windows/) (marca la opción **Add python.exe to PATH** durante la instalación).
- **ExifTool para Windows** (ejecutable standalone oficial).
- **PowerShell** (incluido en Windows 10/11) y un navegador moderno.

### Paso 1: Clonar o descargar el proyecto y crear el entorno virtual

Abre **PowerShell** en la carpeta del proyecto:

```powershell
git clone https://github.com/alessgii/orca-metadata-inspector.git
cd orca-metadata-inspector

py -3 --version
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> Si PowerShell bloquea scripts de activación, no es necesario activar el entorno: todos los comandos anteriores invocan directamente `.\.venv\Scripts\python.exe`.

### Paso 2: Instalar ExifTool en Windows

**Opción A — Con `winget` (Windows 10/11):**

```powershell
winget install OliverBetz.ExifTool
```

Abre una nueva ventana de PowerShell y verifica:

```powershell
exiftool -ver
```

**Opción B — Instalación manual portable (sin permisos de administrador):**

1. Descarga el archivo ZIP oficial de **Windows Executable** desde [exiftool.org](https://exiftool.org/).
2. Extrae todo su contenido en una carpeta local, por ejemplo `.\.tools\exiftool\` dentro del proyecto.
3. **Importante:** renombra `exiftool(-k).exe` a `exiftool.exe` y **mantén la carpeta `exiftool_files` en el mismo directorio** que `exiftool.exe`.
4. Configura la variable de entorno en PowerShell antes de iniciar La Orca (o agrega esa carpeta a tu `PATH` de usuario):

```powershell
$env:ORCA_EXIFTOOL = "$PWD\.tools\exiftool\exiftool.exe"
& $env:ORCA_EXIFTOOL -ver
```

### Paso 3: Iniciar La Orca en Windows

```powershell
.\.venv\Scripts\python.exe -m orca
```

Abre en tu navegador: [http://127.0.0.1:8000/](http://127.0.0.1:8000/)

- Para detener el servidor: `Ctrl+C`.
- Para usar otro puerto: `.\.venv\Scripts\python.exe -m orca --port 8001`.
- Si deseas que `ORCA_EXIFTOOL` persista entre sesiones de PowerShell, puedes añadir la carpeta de `exiftool.exe` a las *Variables de entorno del usuario → Path* en Windows.

## Uso

El botón de tema de la barra superior alterna entre claro y oscuro y conserva la preferencia en ese navegador.

1. Selecciona o arrastra archivos y ZIP. Puedes quitar elementos de la cola o vaciarla antes de analizar. Marca **Guardar en el historial** si quieres recuperar los resultados después de cerrar el servidor.
2. Pulsa **Analizar**. La barra cuenta los archivos terminados y muestra el nombre del archivo en curso cuando el servidor está extrayendo.
3. Filtra, ordena o busca en la tabla. Abre el nombre del archivo o **Detalles** para ver el SHA-256, los campos agrupados, el origen de cada propiedad, los metadatos técnicos, los hallazgos y el JSON. **Volver a resultados** recupera la tabla. **Nuevo análisis** vuelve a abrir la carga.
4. Abre las coincidencias entre archivos para ver el campo, el valor y los documentos asociados. Los nombres y el software se cotejan sin distinguir mayúsculas. Para rutas e identificadores se conserva la distinción de mayúsculas. Cada coincidencia describe valores registrados en los archivos. Verifica de forma independiente su autoría, procedencia e interpretación.
5. En **Antes / Después**, crea una copia, revisa los campos eliminados, modificados, persistentes y añadidos, y descárgala. **Exportar** descarga el informe de la sesión original en JSON, CSV o HTML.
6. Elimina una sesión desde el inspector o desde el historial. **Eliminar historial** vacía la base de resultados guardados. Las sesiones todavía activas siguen su plazo temporal.

Las fechas internas se muestran como las devuelve ExifTool, con zona horaria solo cuando está presente. Las rutas y los nombres de carpeta de usuario reconocidos en textos se presentan con el campo que los contiene. Los hallazgos identifican propiedades potencialmente sensibles. El informe deja al lector la interpretación y las conclusiones. Los archivos en `tests/` se generan con valores sintéticos para comprobar el programa.

## Herramientas del inspector

### Máquina del tiempo

Abre **Detalles → Cronología**. **Resumida** traza los instantes comparables en un eje proporcional y ofrece un selector para consultar eventos cercanos o simultáneos. Las fechas del sistema de archivos, cuando existen, usan su propio eje. **Avanzada** abre el detalle técnico vertical con los mismos datos. Puedes alternar con las flechas del teclado. Cada evento indica qué fecha se registró, su precisión, qué etiqueta la contiene y el valor original. Los instantes de segundo o fracción con un desplazamiento inequívoco se ordenan en UTC. Las horas locales, las fechas de calendario y los valores inválidos aparecen en secciones propias sin imponerles un orden absoluto. Orca conserva fuentes duplicadas y muestra discrepancias entre marcas del mismo tipo.

El intervalo expresa tiempo transcurrido entre una creación y una modificación internas comparables. Cuando varias fuentes se refieren al mismo instante, se conservan todas. Si las fuentes comparables discrepan, Orca evita elegir un intervalo único. Las fechas de sistema de archivos se identifican si están presentes en los datos almacenados. El extractor actual excluye las fechas del sistema de archivos de la copia temporal para evitar atribuirlas al original. La detección usa etiquetas conocidas y, para las demás, exige un nombre temporal y un valor con formato compatible. `LinksUpToDate` se conserva como propiedad de estado en los metadatos originales y en la comparación.

### Duelo de documentos y comparación profunda

Marca dos archivos de la misma sesión en la tabla y pulsa **Comparar archivos**, o entra desde **Duelo** en la barra lateral. Puedes intercambiar A y B, enviar ambos SHA-256 directamente al **Comparador de hashes**, filtrar diferencias y descargar el resultado completo en JSON o HTML.

El duelo evalúa tres dimensiones independientes:

1. **Identidad binaria:** compara los SHA-256 y la diferencia de tamaño. Aclara explícitamente que hashes distintos no implican contenidos visibles distintos y que nunca se compara similitud por caracteres del hash.
2. **Similitud de contenido (motor extensible por formato):** actualmente implementado para **PPTX**. Analiza el orden real de diapositivas desde `ppt/presentation.xml`, normaliza textos, tablas y notas, compara recursos multimedia por SHA-256 y evalúa estructura (diseño, formas, tablas, gráficos). Identifica diapositivas idénticas, modificadas (con diferencias de texto eliminado/agregado), reordenadas, agregadas, eliminadas, imágenes sustituidas y cambios únicamente de metadatos o empaquetado. No renderiza diapositivas ni declara equivalencia visual.
3. **Similitud de metadatos:** conserva la comparación semántica de roles, listas, fechas UTC y hemisferios EXIF, separando propiedades descriptivas de etiquetas técnicas.

Para PPTX con archivos activos disponibles, Orca calcula una puntuación determinista v1.0 (**50% contenido textual, 25% recursos multimedia, 15% estructura y 10% metadatos descriptivos**, redistribuyendo proporcionalmente dimensiones no aplicables y excluyendo etiquetas técnicas). Para formatos sin analizador profundo o sesiones expiradas del historial no se emite un porcentaje global engañoso.

### Laboratorio de hashes e historial

En **Laboratorio de hashes** dispones de tres herramientas:

- **Calculadora de hashes:** calcula MD5, SHA-1, SHA-256 (predeterminado) y SHA-512 en bloques de 128 kB sin cargar el archivo completo en memoria. Permite activar múltiples algoritmos para el mismo archivo sin volver a seleccionarlo y advierte sobre el carácter heredado de MD5 y SHA-1.
- **Comparador de hashes:** valida formato hexadecimal y longitud antes de comparar, acepta mayúsculas/minúsculas y espacios accidentales alrededor, y aclara que la igualdad de cadenas no demuestra autenticidad por sí sola.
- **Historial de hashes y «Verificar nuevamente»:** guarda registros ligeros en SQLite (`id`, nombre, tamaño, algoritmo, hash y fecha) sin almacenar copias de archivos. Permite buscar, filtrar, exportar a JSON/CSV, eliminar y volver a seleccionar un archivo para comprobar si su integridad se mantiene (sin usar el nombre de archivo como prueba de identidad).

### Manifiestos de integridad

En **Manifiestos** puedes seleccionar varios archivos o una carpeta para generar un manifiesto JSON (esquema v1) con rutas relativas normalizadas, tamaños y hashes. Al importar un manifiesto y volver a seleccionar los archivos, Orca clasifica cada elemento como **sin cambios**, **modificado**, **faltante**, **adicional** o **no analizable**, y permite exportar el resultado en JSON o HTML. Un manifiesto JSON verifica integridad respecto a esa referencia, pero no autenticidad criptográfica al no incorporar firma digital.

### Investigaciones (expedientes portables)

En **Investigaciones** puedes crear expedientes independientes persistidos en SQLite local (con migraciones versionadas) e identificados mediante **UUID v7** criptográficamente seguros e inmutables (sin numeración secuencial global). Cada expediente integra:

- **Gestión de estados y auditoría:** estados `Abierta`, `Pausada` y `Cerrada` con transiciones validadas, cierre con resumen y conclusión obligatorios, reapertura con motivo explícito y línea de tiempo de actividades de solo anexado.
- **Incorporación y análisis automático:** registra uno o varios archivos o una carpeta, ejecuta ExifTool y el extractor estructural (PPTX), detecta duplicados por SHA-256 conservando registros independientes y **no guarda copias permanentes de los archivos originales**.
- **Comparación múltiple y Matriz de similitud:** compara cualquier pareja de archivos registrados reutilizando la estructura persistida, diferencia identidad binaria de similitud documental y permite cálculo bajo demanda o en lotes acotados.
- **Línea de tiempo documental y Mapa de relaciones:** ordena documentos por incorporación, fechas internas declaradas o orden manual; visualiza un grafo interactivo de conexiones automáticas (hashes, recursos, similitud) y manuales (confirmadas o hipótesis).
- **Observaciones versionadas e informes:** notas humanas vinculables a la investigación, archivos, comparaciones, eventos o relaciones, con historial de versiones y exportación en HTML y JSON preparado para futuras integraciones locales de IA.
- **Portabilidad e importación masiva:** exporta expedientes como paquetes `.zip` versionados con manifiesto de integridad interno (sin archivos originales) e importa uno o múltiples paquetes (escenario profesor/estudiantes) con previsualización, detección de duplicados y conflictos por UUID, y creación de copias independientes conservando todas las relaciones internas.

### Copiar como evidencia

En **Normalizados**, **Técnicos**, **Hallazgos** o **Exposición**, abre **Evidencia** junto al dato. Puedes copiar el valor original, un bloque Markdown o JSON con el archivo, SHA-256 si existe, fecha de análisis, versión de ExifTool, etiqueta exacta y ruta dentro del JSON de extracción. La evidencia contextual incluye el valor interpretado cuando difiere del original y la explicación del hallazgo cuando corresponde.

Si el nombre y la etiqueta permiten una referencia segura, aparecen comandos para Linux o PowerShell. Ejecuta el comando desde el directorio que contiene el original con ExifTool en el PATH. El comando consulta campos y puede devolver varias instancias de una etiqueta. Los archivos contenidos en ZIP conservan su evidencia y su nombre de procedencia. Para esos miembros, la consulta por CLI requiere extraer el original y elegir su ruta.

### ¿Qué estoy filtrando?

Abre **Detalles → Exposición** para ver información examinada sobre identidad, ubicación, entorno de trabajo, historia documental, identificadores y texto descriptivo. Cada resultado indica la regla, su motivo y la procedencia. Cuando una regla detecta el mismo valor y el mismo tipo en varias fuentes, las agrupa y muestra todas.

| Regla | Clasificación orientativa | Campos |
| --- | --- | --- |
| `identity.attribution`, `identity.account` | Potencial | Autores, creadores, editores, organizaciones y cuentas. |
| `location.gps`, `location.place` | Potencial | Coordenadas o lugares declarados. |
| `environment.path`, `environment.host` | Potencial | Rutas y nombres de equipo. |
| `environment.tools`, `history.date` | Informativa | Software, versiones, equipos o fechas registradas. |
| `identifier.document` | Informativa | Identificadores documentales y UUID. |
| `identifier.serial` | Potencial | Números de serie reconocidos. |
| `additional.description`, `additional.comments` | Informativa para títulos y palabras clave. Potencial para comentarios. | Texto descriptivo. |

Selecciona **Abrir Antes / Después** para crear la copia si está disponible. Orca usa la extracción posterior para mostrar el recuento original, el de la copia y los valores eliminados, persistentes o añadidos. Los niveles son heurísticos. Pueden existir datos en el contenido visible o en estructuras fuera del alcance de ExifTool y del saneamiento.

### Ejemplo reproducible

Desde la raíz del proyecto y con las dependencias de pruebas instaladas, crea dos documentos propios de laboratorio:

```bash
.venv/bin/python - <<'PYTHON'
from datetime import datetime, timezone
from docx import Document

for name, editor in [("prueba-a.docx", "Editor A"), ("prueba-b.docx", "Editor B")]:
    document = Document()
    document.add_paragraph("Contenido sintético de prueba.")
    document.core_properties.author = "Cuenta de prueba"
    document.core_properties.last_modified_by = editor
    document.core_properties.created = datetime(2020, 2, 29, tzinfo=timezone.utc)
    document.core_properties.modified = datetime(2024, 2, 29, tzinfo=timezone.utc)
    document.save(name)
PYTHON
```

Analiza los dos archivos juntos. En la cronología verás un intervalo de 1461 días. En el duelo coincide el creador y difiere el último editor. Desde Exposición puedes copiar el campo de edición como evidencia y verificar su eliminación sobre una copia.

## Alcance de la limpieza

| Formato | Cambios comprobados | Partes que se conservan o requieren revisión |
| --- | --- | --- |
| JPEG | Reduce EXIF, XMP, IPTC, Photoshop y comentarios con ExifTool. Pillow valida dimensiones, perfil, orientación y píxeles decodificados. | Datos estructurales y de color. La orientación se conserva cuando está presente. |
| PNG | Reduce EXIF, XMP y textos descriptivos comunes. Pillow valida dimensiones, perfil y píxeles con transparencia. | Textos o bloques PNG no cubiertos por las reglas. Se rechazan animaciones y la limpieza de imágenes de más de 30 megapíxeles. |
| PDF | pikepdf/qpdf reescribe la copia completa tras eliminar el diccionario documental `Info` y el XMP del catálogo. Vuelve a abrirla y compara conteo, geometría, flujos de página y conteo de anotaciones. | Adjuntos, texto, anotaciones y metadatos de otros objetos. Se rechazan cifrado, firmas o campos de firma detectados y declaraciones reconocidas de conformidad o perfiles de salida. |
| DOCX, XLSX, PPTX | Elimina campos descriptivos estándar de `docProps/core.xml`. Comprueba legibilidad ZIP, nombres de piezas y el CRC de todas las demás piezas. | Comentarios de contenido, revisiones, propiedades avanzadas y personalizadas, plantillas, enlaces, adjuntos y recursos. Rechaza paquetes con indicios de macros o firmas. |
| WebP | Inspección. | Limpieza sin estrategia habilitada. |
| ZIP | Inspección de los archivos miembros admitidos. | La descarga de una copia opera sobre el miembro seleccionado. |

La comparación coteja los campos agrupados devueltos antes y después. Los cambios de geometría, contenedor y otros campos técnicos se identifican según sus etiquetas o grupos. Un resultado satisfactorio comprueba esta transformación y su alcance. Aún pueden persistir metadatos en bloques que el extractor desconoce, contenido visible u oculto, revisiones, adjuntos y perfiles. Guarda la descarga antes de cerrar la sesión. [Metadatos de PDF en pikepdf](https://pikepdf.readthedocs.io/en/stable/topics/metadata.html), [edición incremental de PDF en ExifTool](https://exiftool.org/TagNames/PDF.html), [alcance del Inspector de documentos de Office](https://learn.microsoft.com/en-us/office/vba/library-reference/concepts/using-the-document-inspector).

## Límites y retención

- Un MB son 1 000 000 bytes. Cada ZIP y cada archivo directo admiten hasta 100 000 000 bytes. Los archivos directos comparten una cuota de 100 000 000 bytes.
- Cada formulario tiene un máximo de 100 archivos de entrada y cada operación puede producir hasta 100 resultados. Las entradas no compatibles y ocultas también cuentan. Las carpetas tienen su propio límite de entradas. El presupuesto de expansión se comparte entre ZIP y partes de documentos Office revisadas.
- Para ZIP se admiten compresión almacenada y DEFLATE. Se comprueban ruta, volumen expandido, tamaño real, CRC y relación de compresión. ZIP64 en el directorio central, paquetes multipartes, contenedores ZIP anidados genéricos y métodos de compresión alternativos quedan fuera del alcance. Office cuenta como documento, con un presupuesto de expansión y validación para sus piezas internas.
- ExifTool se ejecuta con argumentos fijos, en serie y con un límite de tiempo y de salida. En Linux, los subprocesos también tienen límites de memoria virtual y de tamaño de archivo de salida. La revisión y la reescritura se ejecutan fuera del bucle HTTP. Una operación intensiva de carga o limpieza por instancia mantiene acotado el trabajo concurrente.
- El análisis crea archivos con nombres aleatorios en `.orca-data/temporary`. El servidor retira originales y copias al eliminar la sesión, reiniciar, alcanzar el máximo de ocho sesiones temporales o pasar el plazo de retención. Una limpieza posterior renueva el plazo temporal de esa sesión. Apagar inesperadamente el proceso puede dejar temporales hasta el siguiente arranque.
- Si eliges guardar resultados, `.orca-data/history.sqlite3` conserva nombres, hashes, fechas, errores, campos y hallazgos. Los informes descargados quedan donde los guardes. La base y sus exportaciones pueden contener datos sensibles en claro. SQLite utiliza eliminación segura para sus filas, aunque copias de seguridad o instantáneas del disco pueden conservarlas.
- El límite de salida del motor es 512 000 bytes por flujo y el presupuesto de resultados de una sesión es 16 MB. Los archivos que exceden los límites muestran un error. Los resultados ya obtenidos siguen disponibles.

### Variables de entorno

| Variable | Valor predeterminado | Función |
| --- | --- | --- |
| `ORCA_EXIFTOOL` | Distribución local detectada o `exiftool` en `PATH` | Ruta absoluta o relativa al ejecutable de confianza. |
| `ORCA_DATA` | `.orca-data` del proyecto | Directorio local de trabajo e historial. Usa uno por instancia. |
| `ORCA_UPLOAD_BYTES` | `250000000` | Tope acumulado de datos de archivo enviados, excluyendo la sobrecarga multipart. |
| `ORCA_EXPANDED_BYTES` | `200000000` | Tope total expandido durante la validación. |
| `ORCA_HASH_BYTES` | `1000000000` | Tope de tamaño en flujo para el Laboratorio de hashes y manifiestos. |
| `ORCA_TIMEOUT` | `25` | Segundos por invocación de la herramienta. |
| `ORCA_TTL` | `1800` | Segundos de retención temporal después del análisis o limpieza. |

Orca lee variables del entorno del proceso. Usa `export ORCA_TTL=900` en Linux o `$env:ORCA_TTL = "900"` en PowerShell antes de arrancar.

## Arquitectura

| Módulo | Responsabilidad |
| --- | --- |
| `orca/main.py` | FastAPI, modelos Pydantic, Jinja2, recursos locales, ciclo de vida y API REST. |
| `orca/uploads.py` | Cuotas, firmas binarias, validación de ZIP y Open XML, preparación de archivos. |
| `orca/extraction.py` | Protocolo `Extractor`, adaptador ExifTool, subprocess acotado y SHA-256. |
| `orca/analysis.py`, `orca/metadata.py` | Inspección, orígenes de campos normalizados, hallazgos, comparaciones y correlaciones. |
| `orca/timeline.py`, `orca/duel.py`, `orca/duel_engine.py` | Interpretación de fechas, orquestador universal por capacidades, matriz N archivos y lotes cancelables. |
| `orca/content.py`, `orca/doc_analyzers.py`, `orca/xls_reader.py`, `orca/similarity.py` | Extracción y comparación profunda (PPTX, PDF, DOCX, XLS OLE2/BIFF8, XLSX y `XLS ↔ XLSX`) y similitud determinista. |
| `orca/image_analysis.py` | Equivalencia de muestras (incluido PNG 16-bit), normalización EXIF/ICC/RGBA, similitud perceptual (`SSIM`, `pHash`, `dHash`, `MAE`, `MSE`, `PSNR`), parsers estructurales JPEG/PNG y mapas de diferencias. |
| `orca/hashes.py`, `orca/manifests.py` | Cálculo de hashes en bloques, validación/comparación, esquema v1 y verificación de manifiestos. |
| `orca/investigations.py`, `orca/portability.py` | Expedientes con UUIDv7, matriz de similitud, cronologías, grafo, notas versionadas y paquetes `.zip` portables. |
| `orca/evidence.py`, `orca/exposure.py` | Evidencia contextual, reglas de exposición y diferencias tras limpiar. |
| `orca/sessions.py`, `orca/storage.py` | Lotes, progreso, temporales, caché de perfiles y persistencia SQLite. |
| `orca/cleanup.py`, `orca/rewrite.py` | Políticas por formato, reescritura, reextracción, validación de copias y verificación visual JPEG/PNG. |
| `orca/reports.py`, `orca/templates/` | Informes y HTML escapado (sesiones, duelos A/B, matrices N archivos, investigaciones y manifiestos). |
| `orca/static/`, `frontend/` | Interfaz en español, Fetch, JavaScript sin framework (`app.js`, `inspection.js`, `timeline-summary.js`, `hash-lab.js`, `manifests.js`, `investigations.js`), SVG locales y Tailwind CSS 4. |

SQLite se usa mediante la biblioteca estándar para mantener la instalación ligera. El historial guarda metadatos agrupados tal como se entregan en el JSON de ExifTool. Se excluyen los campos del sistema de archivos que describirían el directorio y las marcas de tiempo temporales del servidor. Los archivos de entrada conservan su nombre y tamaño declarados en el registro de análisis, además del hash calculado cuando se leyó un archivo completo.

### API local

| Método y ruta | Resultado |
| --- | --- |
| `GET /api/health` | Disponibilidad y versión de ExifTool. |
| `POST /api/sessions` | Envía `files` repetido y `keep=1` opcional en multipart. Devuelve un identificador con 202. |
| `GET /api/sessions/{id}` | Estado, progreso y resumen con filas de archivos al terminar. |
| `GET /api/sessions/{id}/files/{file_id}` | JSON original, normalización con origen, hash y hallazgos. |
| `GET /api/sessions/{id}/files/{file_id}/context` | Cronología, exposición y comparación de exposición si hay una copia vigente. |
| `GET /api/sessions/{id}/files/{file_id}/evidence` | Evidencia. Usa `kind=raw&tag=...` o `kind=normalized`, `finding` o `exposure` con `index` desde cero en el orden del JSON. |
| `GET /api/sessions/{id}/files/{file_id}/preview` | Previsualización segura de imagen (`JPEG`, `PNG`, `WebP`) de una sesión temporal activa. |
| `GET /api/sessions/{id}/duel?file_a=…&file_b=…` | Duelo universal por capacidades (binario, metadatos, imágenes y documentos). Admite `download=true` y `format=json\|html`. |
| `GET /api/sessions/{id}/duel/diff-map?file_a=…&file_b=…` | Genera y devuelve un mapa PNG de diferencias exactas de píxeles (`mode=normalized\|raw`, `amplify=true\|false`). |
| `GET /api/sessions/{id}/duel/matrix` | Matriz N archivos, duplicados, grupos y modo referencia (`reference_id`, `download=true`, `format=json\|html`). |
| `POST /api/sessions/{id}/duel/matrix/batch`, `.../status`, `.../cancel` | Ejecuta, consulta progreso o cancela de forma segura comparaciones profundas por lotes. |
| `POST /api/sessions/{id}/files/{file_id}/clean` | Reescribe y verifica una copia, devuelve la diferencia y el JSON posterior. |
| `GET /api/sessions/{id}/files/{file_id}/copy` | Descarga la copia verificada vigente. |
| `GET /api/sessions/{id}/export/{json\|csv\|html}` | Descarga el informe. |
| `GET /api/history`, `DELETE /api/history` | Lista o borra los resultados persistentes. |
| `DELETE /api/sessions/{id}` | Borra una sesión, sus temporales y su entrada en el historial. |
| `POST /api/hashes/compute` | Calcula uno o varios hashes (MD5, SHA-1, SHA-256, SHA-512) en bloques; admite stream binario o multipart y `save=1`. |
| `POST /api/hashes/compare` | Valida formato/longitud y compara dos hashes hexadecimales en tiempo constante. |
| `GET /api/hashes/history`, `POST /api/hashes/history` | Lista (con `q` y `algorithm`) o guarda registros ligeros de hashes en SQLite. |
| `DELETE /api/hashes/history`, `DELETE /api/hashes/history/{id}` | Elimina un registro individual o limpia el historial de hashes. |
| `GET /api/hashes/history/export/{json\|csv}` | Exporta el historial de hashes (CSV protegido contra fórmulas). |
| `POST /api/hashes/history/{id}/verify` | Recalcula el hash de un archivo contra un registro guardado («Verificar nuevamente»). |
| `POST /api/manifests/generate` | Genera y valida un manifiesto de integridad JSON v1 (acepta JSON precalculado o multipart). |
| `POST /api/manifests/verify` | Verifica archivos contra un manifiesto JSON v1 y clasifica cambios, ausencias y adicionales. |
| `POST /api/manifests/report` | Exporta el resultado de verificación de un manifiesto como informe HTML seguro. |
| `GET /api/investigations`, `POST /api/investigations` | Lista (con búsqueda por nombre/responsable/UUID, estado y orden) o crea expedientes. |
| `GET /api/investigations/{uuid}`, `PATCH`, `DELETE`, `POST .../state` | Consulta, edita, elimina o cambia el estado (`pause`, `resume`, `close`, `reopen`) de un expediente. |
| `GET/POST /api/investigations/{uuid}/files`, `.../order`, `.../verify-hash` | Incorpora/analiza archivos, gestiona orden manual y re-verifica integridad SHA-256. |
| `GET/POST /api/investigations/{uuid}/comparisons`, `.../matrix`, `.../matrix/compute` | Ejecuta/consulta comparaciones persistentes y calcula la matriz de similitud bajo demanda o en lotes. |
| `GET /api/investigations/{uuid}/timeline/{activity\|documents}`, `.../graph`, `.../relations` | Auditoría de solo anexado, línea de tiempo documental, grafo y relaciones manuales. |
| `GET/POST /api/investigations/{uuid}/notes`, `PATCH .../notes/{note_uuid}` | Observaciones humanas con historial de versiones. |
| `GET /api/investigations/{uuid}/export`, `.../report/{json\|html}`, `POST .../manifest` | Exporta paquete portable `.zip`, informes completos y manifiestos SHA-256. |
| `POST /api/investigations/import/preview`, `POST /api/investigations/import` | Previsualiza conflictos e importa individualmente o por lotes paquetes de investigación `.zip`. |

## Desarrollo y pruebas

Linux:

```bash
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pytest -q
```

Si tienes Node.js, puedes verificar la escala gráfica con `node --test tests/timeline-summary.test.cjs`.

Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Los archivos de prueba se generan localmente con Pillow, pikepdf, python-docx, python-pptx y openpyxl. La suite comprueba valores conocidos, límites, respuestas parciales, persistencia, informes y conservación de contenido tras la limpieza. ExifTool debe estar operativo para ejecutar la integración completa.

Node.js solo se necesita al cambiar la interfaz o sus clases Tailwind. El CSS compilado forma parte del repositorio.

```bash
npm ci
npm run build
```

## Solución de problemas

- **ExifTool no disponible.** Ejecuta `exiftool -ver`. En Windows verifica el nombre del ejecutable y su carpeta auxiliar. Si usas una distribución extraída, configura `ORCA_EXIFTOOL` antes de reiniciar.
- **Office aparece como ZIP.** Revisa el aviso de ExifTool. Las instalaciones Perl necesitan `Archive::Zip` y sus dependencias para leer piezas Office comprimidas. `Compress::Zlib` también permite leer determinados flujos PDF.
- **Otra operación en curso.** Espera a que termine el análisis o la limpieza y vuelve a intentarlo.
- **La sesión del historial ya no admite limpieza.** Vuelve a cargar el archivo original. El historial conserva los resultados.
- **No se pudo abrir la copia.** La descarga debe seguir disponible dentro de la retención. Si se rechazó la transformación, el inspector muestra el motivo.
- **Puerto ocupado o directorio usado por otra instancia.** Elige otro puerto con `--port` y otro directorio con `ORCA_DATA`, o detén la instancia anterior. Usa una sola instancia por directorio de datos.
