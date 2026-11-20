import io
import time
import zipfile

import openpyxl
import pikepdf
import pytest
from docx import Document
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
from pptx import Presentation

from orca.config import Settings
from orca.main import create_app

AUTHOR = "Ada Fixture"
BODY = "Contenido sintético de Orca"


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    root = tmp_path_factory.mktemp("documents")
    pdf = root / "ejemplo.pdf"
    with pikepdf.new() as doc:
        page = doc.add_blank_page()
        page.obj.Contents = doc.make_stream(b"1 0 0 rg 20 20 100 80 re f")
        doc.docinfo["/Author"] = AUTHOR
        doc.docinfo["/Title"] = "Fixture PDF"
        doc.docinfo["/Subject"] = "/home/ada/proyectos/orca.txt"
        doc.docinfo["/CreationDate"] = "D:20240205111002Z"
        doc.docinfo["/Producer"] = "Orca Fixtures"
        doc.save(pdf)
    word = root / "ejemplo.docx"
    doc = Document()
    doc.core_properties.author = AUTHOR
    doc.core_properties.comments = "Comentario de control"
    doc.add_paragraph(BODY)
    doc.save(word)

    slides = root / "ejemplo.pptx"
    deck = Presentation()
    deck.core_properties.author = AUTHOR
    slide = deck.slides.add_slide(deck.slide_layouts[0])
    slide.shapes.title.text = BODY
    deck.save(slides)

    sheet = root / "ejemplo.xlsx"
    book = openpyxl.Workbook()
    book.properties.creator = AUTHOR
    book.active["A1"] = BODY
    book.save(sheet)

    jpeg = root / "ejemplo.jpg"
    exif = Image.Exif()
    exif[315] = AUTHOR
    exif[274] = 6
    exif[305] = "Orca Fixtures"
    Image.new("RGB", (32, 18), (56, 128, 199)).save(jpeg, exif=exif)
    png = root / "ejemplo.png"
    text = PngImagePlugin.PngInfo()
    text.add_text("Author", AUTHOR)
    text.add_text("Description", BODY)
    Image.new("RGBA", (21, 11), (56, 128, 199, 128)).save(png, pnginfo=text)
    webp = root / "ejemplo.webp"
    Image.new("RGB", (21, 11), (56, 128, 199)).save(webp, exif=exif)
    return {path.suffix[1:]: path for path in root.iterdir()}


@pytest.fixture
def settings(tmp_path):
    return Settings(data=tmp_path / "data")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as web:
        yield web


def zip_bytes(entries, compression=zipfile.ZIP_STORED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def upload(client, files, keep=False):
    return client.post("/api/sessions", files=[("files", item) for item in files], data={"keep": str(int(keep))})


def complete(client, response):
    assert response.status_code == 202, response.text
    base = "/api/sessions/" + response.json()["id"]
    until = time.monotonic() + 30
    while time.monotonic() < until:
        result = client.get(base).json()
        if result.get("state") == "complete":
            assert result["processed"] == result["total"]
            assert result["duration_ms"] >= 0
            return base, result
        assert result.get("state") != "failed", result
        time.sleep(.02)
    pytest.fail("El análisis no terminó a tiempo.")
