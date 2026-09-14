"""Configuration: INI file merged with CLI flags.

Precedence: built-in defaults < /etc/alma-certify/alma-certify.conf < --config file
< command-line flags.
"""

from __future__ import annotations

import configparser
import os
from typing import Dict, Optional

# Where results go unless told otherwise. A default rather than an empty string, so a run from a
# source checkout, with no config file installed, submits to the same place a packaged one does.
DEFAULT_SERVER = "https://catalog.almalinux.org"

# The staging catalog, reached with ``--dev``. Named here rather than typed into a --server each
# time: it is the one alternative anybody uses, and a URL retyped from memory is a URL that
# eventually gets a typo and uploads nothing.
DEV_SERVER = "https://lumina.almalinux.dev"

DEFAULT_CONFIG_PATH = "/etc/alma-certify/alma-certify.conf"
DEFAULT_RUN_DIR = "/var/lib/alma-certify/runs"
# Not /etc: the token is not configuration. It is a short-lived credential the machine fetches for
# itself and can fetch again with `alma-certify register`, which is what /var/cache is for. Keeping
# it out of /etc also keeps it out of configuration backups and etckeeper history.
DEFAULT_TOKEN_PATH = "/var/cache/alma-certify/token.json"

DEFAULTS: Dict[str, Dict[str, str]] = {
    "general": {
        "run_dir": DEFAULT_RUN_DIR,
        "server": DEFAULT_SERVER,
        "token_path": DEFAULT_TOKEN_PATH,
        # Accept the catalog's TLS certificate without verifying it. For a dev or staging server
        # with a self-signed certificate. See ``alma_certify.submit.http.allow_self_signed`` for
        # what it actually turns off, which is more than the name suggests.
        "allow_self_signed": "false",
    },
    "validate": {
        # short functional smoke durations (seconds); validation verifies
        # that things work, not endurance
        "cpu_smoke_seconds": "60",
        "memory_smoke_seconds": "60",
        "io_smoke_seconds": "60",
        "network_smoke_seconds": "30",
        "network_min_fraction": "0.5",
    },
    "benchmark": {
        "cooldown_seconds": "8",
        # Where validate.storage.io-sanity writes its test file. Kept in
        # this section because --disk has always written it here; the
        # storage benchmarks that used to share it are gone.
        "storage_target_dir": "",
        "storage_file_size_mb": "4096",
        "corpus_size_mb": "512",
    },
    "network": {
        "peer": "",
        "iperf3_port": "5201",
    },
    "timeouts": {},  # per-test-id overrides: "validate.cpu.stress = 600"
}


class Config:
    def __init__(self, parser: configparser.ConfigParser):
        self._cp = parser

    @classmethod
    def load(cls, path: Optional[str] = None) -> Config:
        cp = configparser.ConfigParser()
        cp.read_dict(DEFAULTS)
        candidates = [DEFAULT_CONFIG_PATH]
        if path:
            candidates.append(path)
        for candidate in candidates:
            if os.path.isfile(candidate):
                cp.read(candidate)
        return cls(cp)

    def get(self, section: str, key: str, fallback: str = "") -> str:
        return self._cp.get(section, key, fallback=fallback)

    def getint(self, section: str, key: str, fallback: int = 0) -> int:
        return self._cp.getint(section, key, fallback=fallback)

    def getfloat(self, section: str, key: str, fallback: float = 0.0) -> float:
        return self._cp.getfloat(section, key, fallback=fallback)

    def getbool(self, section: str, key: str, fallback: bool = False) -> bool:
        """configparser's own boolean parsing, so "1", "yes", and "on" mean what a reader expects.

        Named ``getbool`` rather than ``getboolean`` to match ``getint`` and ``getfloat`` above.
        """
        return self._cp.getboolean(section, key, fallback=fallback)

    def set(self, section: str, key: str, value: str) -> None:
        if not self._cp.has_section(section) and section != "DEFAULT":
            self._cp.add_section(section)
        self._cp.set(section, key, value)

    def timeout_for(self, test_id: str, default: int) -> int:
        return self._cp.getint("timeouts", test_id, fallback=default)
