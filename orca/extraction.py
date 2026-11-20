import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread
from typing import Protocol

from .config import CHUNK, ROOT

READ_OPTIONS = ("-json", "-G0:1:4", "-a", "-s", "-struct", "-u", "-n", "--System:all")


class EngineError(Exception):
    pass


def run_command(command: list[str], timeout: int, limit: int = 512_000, env=None, cwd=ROOT):
    """Bound each pipe while continuing to drain it after termination."""
    overflow = Event()
    if sys.platform == "linux":
        limits = Path(__file__).with_name("process_limits.py").resolve()
        command = [sys.executable, str(limits), *command]
    try:
        with subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, shell=False, env=env,
        ) as process:
            buffers = [bytearray(), bytearray()]

            def drain(pipe, output):
                while chunk := pipe.read1(CHUNK):
                    if len(output) + len(chunk) > limit:
                        overflow.set()
                        process.kill()
                        return
                    output.extend(chunk)

            threads = [
                Thread(target=drain, args=(pipe, output), daemon=True)
                for pipe, output in zip((process.stdout, process.stderr), buffers)
            ]
            for thread in threads:
                thread.start()
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                raise EngineError("La herramienta excedió el tiempo permitido.") from exc
            finally:
                for thread in threads:
                    thread.join()
            if overflow.is_set():
                raise EngineError("La salida de la herramienta excedió el límite permitido.")
            return code, bytes(buffers[0]), bytes(buffers[1])
    except OSError as exc:
        raise EngineError("No se pudo iniciar la herramienta local.") from exc


class Extractor(Protocol):
    @property
    def info(self) -> dict: ...
    def extract(self, path: Path) -> dict: ...


class ExifTool:
    def __init__(self, timeout: int):
        bundled = ROOT / ".tools" / "exiftool-13.59" / "exiftool"
        binary = os.getenv("ORCA_EXIFTOOL") or (bundled if bundled.is_file() else shutil.which("exiftool"))
        self.command = [str(Path(binary).resolve()), "-config", ""] if binary else []
        self.timeout = timeout
        self.env = None
        local_perl = ROOT / ".tools" / "perl" / "usr"
        if binary == bundled and local_perl.is_dir():
            paths = [local_perl / part for part in (
                "share/perl5", "share/perl5/vendor_perl", "lib64/perl5/vendor_perl",
            )]
            self.env = {**os.environ, "PERL5LIB": os.pathsep.join(map(str, paths))}
        self.version = None
        if self.command:
            try:
                code, output, _ = run_command([*self.command, "-ver"], 5, env=self.env)
                version = output.decode("ascii").strip()
                if code == 0 and version.replace(".", "").isdigit():
                    self.version = version
            except (EngineError, UnicodeError):
                pass

    @property
    def info(self):
        return {"name": "ExifTool", "version": self.version, "available": self.version is not None,
                "options": " ".join(READ_OPTIONS)}

    def extract(self, path):
        path = path.resolve()
        if not self.version:
            raise EngineError("ExifTool no está disponible. Consulta las instrucciones de instalación.")
        code, output, _ = run_command([
            *self.command, *READ_OPTIONS,
            "-charset", "UTF8", "--", path.name,
        ], self.timeout, env=self.env, cwd=path.parent)
        try:
            records = json.loads(output)
            record = records[0]
            if not isinstance(record, dict):
                raise ValueError
            json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, IndexError, KeyError, TypeError, RecursionError) as exc:
            raise EngineError("ExifTool no devolvió un registro JSON válido.") from exc
        # ExifTool includes the service's temporary path. The report has its own original-name field.
        record.pop("SourceFile", None)
        if code and not any(key.endswith(":Error") for key in record):
            raise EngineError("ExifTool no pudo completar la extracción.")
        return record

    def strip_image(self, path):
        path = path.resolve()
        code, _, _ = run_command([
            *self.command, "-overwrite_original", "-EXIF:all=", "-XMP:all=", "-IPTC:all=",
            "-Photoshop:all=", "-Comment=", "-PNG:Title=", "-PNG:Author=",
            "-PNG:Description=", "-PNG:Copyright=", "-PNG:Software=", "-PNG:CreationTime=",
            "-PNG:Disclaimer=", "-PNG:Warning=", "-PNG:Source=", "-tagsFromFile", "@", "-IFD0:Orientation",
            "--", path.name,
        ], self.timeout, env=self.env, cwd=path.parent)
        if code:
            raise EngineError("ExifTool no pudo limpiar los grupos descriptivos de la imagen.")


def sha256(path: Path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()
