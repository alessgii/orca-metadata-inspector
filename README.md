# Orca Metadata Inspector

Orca es una aplicación de escritorio web local para inspeccionar, comparar y reducir los metadatos de archivos propios. El servidor escucha en `127.0.0.1`. La interfaz, sus iconos y su CSS se sirven desde el mismo proceso. La instalación inicial descarga dependencias. El análisis, la limpieza y las exportaciones funcionan sin conexión.

## Inicio en Linux

Requisitos de ejecución: Python 3.12 o posterior, ExifTool y un navegador moderno. En un clon nuevo, abre una terminal en la carpeta del proyecto.

```bash
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Para ExifTool, usa el paquete de tu distribución si puedes instalar paquetes del sistema.

```bash
# Fedora
sudo dnf install perl-Image-ExifTool perl-Archive-Zip perl-IO-Compress

# Debian o Ubuntu
sudo apt install libimage-exiftool-perl libarchive-zip-perl
```

Si trabajas sin privilegios, extrae la distribución Perl oficial en una carpeta local. Mantén juntos el ejecutable `exiftool` y su directorio `lib`. Indica la ruta al ejecutable desde la raíz de Orca. Si los módulos Perl ya están disponibles y la distribución 13.59 está en la ruta del ejemplo, puedes omitir la variable `ORCA_EXIFTOOL`.

```bash
export ORCA_EXIFTOOL="$PWD/.tools/exiftool-13.59/exiftool"
"$ORCA_EXIFTOOL" -ver
perl -MArchive::Zip -MCompress::Zlib -e 'print "Módulos Perl disponibles\n"'
.venv/bin/python -m orca
```

Si tu Perl es mínimo, instala sus módulos por los medios admitidos por la distribución o configura `PERL5LIB` con sus directorios locales. En el entorno de desarrollo Fedora 44, se usó ExifTool 13.59 extraído de su distribución oficial. Los RPM necesarios de Perl se descargaron y extrajeron con `rpm2cpio` y `cpio` bajo `.tools/perl`, sin instalarlos globalmente. Orca detecta esta distribución y esos módulos al encontrarla en `.tools/exiftool-13.59/exiftool` sin `ORCA_EXIFTOOL`.

Abre [Orca en 127.0.0.1:8000](http://127.0.0.1:8000/). Detén el servidor con Ctrl+C. Si el puerto está ocupado, usa `.venv/bin/python -m orca --port 8001` y abre el puerto correspondiente. [Instalación de ExifTool](https://exiftool.org/install.html).

## Inicio en Windows

En PowerShell, dentro de la carpeta del proyecto:

```powershell
py -3 --version
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Descarga y extrae la versión ejecutable de ExifTool para Windows. Cambia el nombre de `exiftool(-k).exe` a `exiftool.exe`, conserva `exiftool_files` junto al ejecutable y ajusta la ruta de ejemplo.

```powershell
$env:ORCA_EXIFTOOL = "$PWD\.tools\exiftool\exiftool.exe"
& $env:ORCA_EXIFTOOL -ver
.\.venv\Scripts\python.exe -m orca
```

Abre [Orca en 127.0.0.1:8000](http://127.0.0.1:8000/). También puedes dejar `exiftool.exe` en el `PATH`. [Distribución oficial y archivos auxiliares](https://exiftool.org/install.html).

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

### Duelo de documentos

Marca dos archivos de la misma sesión en la tabla y pulsa **Comparar archivos**. Puedes intercambiar A y B, filtrar por estado, incluir campos técnicos y descargar el resultado como JSON o HTML. En **Historial**, abre una sesión guardada para comparar sus archivos con el mismo flujo.

La comparación agrupa etiquetas con un significado compatible y conserva los valores registrados junto con sus fuentes. Autor, creador, último editor, herramienta de creación y productor se presentan como roles distintos. Una lista semántica se coteja por sus valores, y las listas de etiquetas técnicas conservan el orden. Una cadena vacía, un nulo y un campo ausente tienen significados distintos. Las fechas con un instante UTC equivalente pueden coincidir aunque sus representaciones difieran. En coordenadas EXIF, Orca usa la referencia de hemisferio para comparar el signo y conserva ambas etiquetas. Una magnitud sin hemisferio queda como no comparable. Las coincidencias describen valores registrados y los hashes describen las huellas calculadas.

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
| `orca/timeline.py`, `orca/duel.py` | Interpretación de fechas y comparación semántica de registros de una sesión. |
| `orca/evidence.py`, `orca/exposure.py` | Evidencia contextual, reglas de exposición y diferencias tras limpiar. |
| `orca/sessions.py`, `orca/storage.py` | Lotes, progreso, temporales y persistencia SQLite con dos tablas vinculadas para sesiones y documentos. |
| `orca/cleanup.py`, `orca/rewrite.py` | Políticas por formato, reescritura, reextracción y validación de copias. |
| `orca/reports.py`, `orca/templates/` | Informes y HTML escapado. El CSV protege contra celdas interpretadas como fórmulas. |
| `orca/static/`, `frontend/` | Interfaz en español, Fetch, JavaScript sin framework, SVG locales y Tailwind compilado. |

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
| `GET /api/sessions/{id}/duel?file_a=…&file_b=…` | Comparación de dos archivos de una sesión. Usa `download=true` para descargar JSON o añade `format=html` para el informe. |
| `POST /api/sessions/{id}/files/{file_id}/clean` | Reescribe y verifica una copia, devuelve la diferencia y el JSON posterior. |
| `GET /api/sessions/{id}/files/{file_id}/copy` | Descarga la copia verificada vigente. |
| `GET /api/sessions/{id}/export/{json\|csv\|html}` | Descarga el informe. |
| `GET /api/history`, `DELETE /api/history` | Lista o borra los resultados persistentes. |
| `DELETE /api/sessions/{id}` | Borra una sesión, sus temporales y su entrada en el historial. |

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
