"""Deterministic compression-benchmark corpus generator.

Produces a byte-identical file on every machine (seeded PRNG, no downloads)
mixing compressible text, semi-random binary, and zero runs - so compression
speed/ratio results are comparable across systems.
"""

from __future__ import annotations

import random

_WORDS = (
    b"alma linux certification hardware suite kernel memory storage network "
    b"performance benchmark validation enterprise server system release "
).split()


def write_corpus(path: str, size_mb: int = 512, seed: int = 20260101) -> str:
    rng = random.Random(seed)
    remaining = size_mb * 1024 * 1024
    with open(path, "wb") as fh:
        while remaining > 0:
            kind = rng.randrange(4)
            if kind == 0:  # text
                chunk = b" ".join(rng.choices(_WORDS, k=4096))
            elif kind == 1:  # binary
                chunk = rng.getrandbits(8 * 65536).to_bytes(65536, "little")
            elif kind == 2:  # zeros
                chunk = b"\0" * 262144
            else:  # repeated structure
                record = rng.getrandbits(8 * 64).to_bytes(64, "little")
                chunk = record * 2048
            fh.write(chunk[:remaining])
            remaining -= len(chunk)
    return path
