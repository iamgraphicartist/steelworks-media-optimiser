"""SHA-1 helpers. Figma's imageHash and videoHash are the SHA-1 of the original
file's bytes, so a SHA-1 match proves a source file is the exact original."""
from __future__ import annotations

import hashlib
import re
from typing import BinaryIO, Callable, Optional

CHUNK = 8 << 20
_HEX = re.compile(r"^[0-9a-f]{8,40}$")


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha1_stream(f: BinaryIO, progress: Optional[Callable[[int], None]] = None) -> str:
    h = hashlib.sha1()
    done = 0
    while True:
        chunk = f.read(CHUNK)
        if not chunk:
            break
        h.update(chunk)
        done += len(chunk)
        if progress:
            progress(done)
    return h.hexdigest()


def sha1_file(path: str, progress: Optional[Callable[[int], None]] = None) -> str:
    with open(path, "rb") as f:
        return sha1_stream(f, progress)


def is_figma_hash(value: str) -> bool:
    return bool(value) and bool(_HEX.match(value.lower()))


def hash_matches(sha1_hex: str, figma_hash: str) -> bool:
    """True when a full SHA-1 matches a Figma hash. Figma hashes are full 40-char
    SHA-1s; shorter prefixes (>= 8 chars) are accepted from older exports."""
    if not (sha1_hex and figma_hash):
        return False
    a, b = sha1_hex.lower(), figma_hash.lower()
    if not is_figma_hash(b) or len(a) != 40:
        return False
    return a == b if len(b) == 40 else a.startswith(b)


class HashingWriter:
    """File-like wrapper that hashes everything written through it, so a
    download is verified without a second read of the file."""

    def __init__(self, f: BinaryIO):
        self._f = f
        self._h = hashlib.sha1()
        self.bytes_written = 0

    def write(self, data: bytes) -> int:
        self._h.update(data)
        self.bytes_written += len(data)
        return self._f.write(data)

    def hexdigest(self) -> str:
        return self._h.hexdigest()
