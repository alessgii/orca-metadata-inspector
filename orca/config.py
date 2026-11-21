import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_FILES = 100
MAX_BYTES = 100_000_000
CHUNK = 128 * 1024


@dataclass(frozen=True)
class Settings:
    data: Path = field(default_factory=lambda: Path(os.getenv("ORCA_DATA", ROOT / ".orca-data")))
    expanded_limit: int = field(default_factory=lambda: int(os.getenv("ORCA_EXPANDED_BYTES", "200000000")))
    upload_limit: int = field(default_factory=lambda: int(os.getenv("ORCA_UPLOAD_BYTES", "250000000")))
    hash_limit: int = field(default_factory=lambda: int(os.getenv("ORCA_HASH_BYTES", "1000000000")))
    timeout: int = field(default_factory=lambda: int(os.getenv("ORCA_TIMEOUT", "25")))
    ttl: int = field(default_factory=lambda: int(os.getenv("ORCA_TTL", "1800")))
    max_live: int = 8

    def __post_init__(self):
        if min(self.expanded_limit, self.upload_limit, self.hash_limit, self.timeout, self.ttl, self.max_live) < 1:
            raise ValueError("Los límites y tiempos de Orca deben ser positivos.")
