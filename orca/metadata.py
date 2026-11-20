import re
import unicodedata
from collections import defaultdict
from itertools import islice

FIELDS = {
    "identity": {
        "author": "author authors artist creator creators byline originator photographer lastmodifiedby lastauthor",
        "organization": "company organization organisation credit publisher institution",
        "username": "username useraccount logonname ownername cameraownername",
    },
    "dates": {
        "created": "createdate datecreated creationdate created datetimeoriginal mediacreatedate",
        "modified": "modifydate datemodified modificationdate modified metadatadate mediamodifydate",
    },
    "software": {
        "software": "software creatorsw creatortool producer application originatingprogram generatingsoftware",
        "version": "appversion applicationversion softwareversion creatorversion programversion",
        "operating_system": "operatingsystem operatingsystemversion osversion platform",
    },
    "device": {"make": "make manufacturer devicemanufacturer",
               "model": "model devicemodel cameramodelname lensmodel"},
    "location": {"latitude": "gpslatitude", "longitude": "gpslongitude", "gps": "gpsposition gpscoordinates"},
    "document": {"title": "title documenttitle", "subject": "subject headline",
                 "comments": "comment comments description caption usercomment imagedescription instructions",
                 "keywords": "keywords tags"},
    "other": {"path": "filepath sourcefilepath path originalpath documentpath templatepath",
              "identifier": "id identifier documentid instanceid originaldocumentid imageuniqueid serialnumber "
                            "internalserialnumber lensserialnumber bodyserialnumber uuid"},
}
LOOKUP = {tag: (group, field) for group, fields in FIELDS.items()
          for field, tags in fields.items() for tag in tags.split()}
PATH = re.compile(r"(?<![\w:/])(?:[A-Za-z]:[\\/][^\r\n<>|\"*?]+|\\\\[^\r\n<>|\"*?]+|/(?:[^\s/]+/)+[^\r\n<>|\"*?]+)")
USER = re.compile(r"(?:^|[\\/])(?:users|home|documents and settings)[\\/]([^\\/]+)", re.I)
STRUCTURAL = set("""
 filetype filetypeextension mimetype exiftoolversion fileinodechangedate fileaccessdate filemodifydate filesize
 filepermissions encodingprocess exifbyteorder vp8version webpflags horizontalscale verticalscale bitdepth bitspercomponent bitspersample colorcomponents colorspace colortransform
 compression filter interlace width height imagewidth imageheight imagesize megapixels ycbcrsubsampling
 resolutionunit xresolution yresolution orientation measurementunit samplesperpixel colortype ycbcrpositioning
 pdfversion linearized pagecount zipfilename zipcompression zipcompressedsize zipuncompressedsize zipmodifydate
 thumbnailimage thumbnailoffset thumbnaillength renditionclass flashpixversion exifversion componentsconfiguration
 profilecmmtype profileversion profileclass profileconnectionspace profilefileSignature applicationrecordversion
 adobeunit flags dctencodeversion app14flags0 app14flags1 jfifversion hasrealmergeddata
""".lower().split())
STRUCTURAL_GROUPS = {"System", "ExifTool", "ICC_Profile", "ICC-header", "JFIF", "Adobe", "ZIP"}


def tag_name(source):
    return re.sub(r"[^a-z\d]", "", source.rsplit(":", 1)[-1].lower())


def technical(source):
    parts = source.split(":")
    return len(parts) > 1 and (
        any(part in STRUCTURAL_GROUPS for part in parts[:-1]) or tag_name(source) in STRUCTURAL
    )


def leaves(value, source):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield from leaves(nested, (*source, key))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from leaves(nested, (*source, index))
    elif value is not None and value != "":
        yield value, list(source)


def normalize(raw):
    normalized = defaultdict(list)
    count = 0
    for source, original in raw.items():
        if technical(source):
            continue
        for value, pointer in leaves(original, (source,)):
            count += 1
            if count > 4096:
                raise ValueError("Too many metadata values")
            names = [tag_name(part) for part in reversed(pointer) if isinstance(part, str)]
            match = next((LOOKUP[name] for name in names if name in LOOKUP), ("other", "property"))
            group, field = match
            normalized[group].append({"field": field, "value": value, "source": pointer})
            if not isinstance(value, str):
                continue
            for match_path in islice(PATH.finditer(value), 32):
                path = match_path.group()
                if field != "path":
                    normalized["other"].append(
                        {"field": "path", "value": path, "source": pointer})
                user = USER.search(path)
                if user:
                    normalized["identity"].append(
                        {"field": "username", "value": user[1], "source": pointer})
    return dict(normalized)


SENSITIVE = {
    "author": "Puede identificar un nombre atribuido al documento.",
    "organization": "Puede revelar una organización mencionada en las propiedades.",
    "username": "Un nombre de cuenta o de carpeta de usuario puede revelar información del entorno.",
    "path": "Una ruta puede exponer directorios internos o nombres de proyectos.",
    "identifier": "Un identificador puede vincular archivos o dispositivos.",
    "latitude": "Una coordenada puede revelar dónde se capturó o creó el archivo.",
    "longitude": "Una coordenada puede revelar dónde se capturó o creó el archivo.",
    "gps": "Las coordenadas pueden revelar una ubicación asociada con el archivo.",
    "comments": "El texto descriptivo podría contener información interna.",
}
CORRELATE = {"author", "organization", "software", "version", "make", "model", "path", "identifier"}


def properties(normalized):
    return (item for group in normalized.values() for item in group)


def findings(normalized):
    seen = set()
    result = []
    for item in properties(normalized):
        field = item["field"]
        key = (field, str(item["value"]), tuple(item["source"]))
        if field in SENSITIVE and key not in seen:
            seen.add(key)
            result.append({**item, "level": "potential", "description": SENSITIVE[field]})
    return result


def correlation(records):
    groups = defaultdict(dict)
    for record in records:
        for item in properties(record["normalized"]):
            field, value = item["field"], item["value"]
            if field in CORRELATE:
                text = unicodedata.normalize("NFC", str(value)).strip()
                key = text if field in {"path", "identifier"} else text.casefold()
                if key:
                    groups[(field, key)].setdefault(record["id"], {
                        "id": record["id"], "name": record["name"], "value": value, "source": item["source"]
                    })
    return [
        {"field": field, "value": next(iter(files.values()))["value"], "files": list(files.values())}
        for (field, _), files in groups.items() if len(files) > 1
    ]


def compare(before, after):
    result = {"removed": [], "changed": [], "retained": [], "added": []}
    for source in sorted(before.keys() | after.keys()):
        if source not in after:
            kind = "removed"
        elif source not in before:
            kind = "added"
        elif before[source] == after[source]:
            kind = "retained"
        else:
            kind = "changed"
        result[kind].append({"source": source, "before": before.get(source), "after": after.get(source),
                             "technical": technical(source)})
    return result
