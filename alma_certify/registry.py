"""Test base class, run context, and the explicit test registry."""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import procutil
from .config import Config
from .result import Metric, Severity, Status, TestResult  # noqa: F401 (re-export)


class RunContext:
    """Everything a test needs: config, run dir, inventory, helpers."""

    def __init__(
        self,
        config: Config,
        run_dir: str,
        inventory: Optional[Dict[str, Any]] = None,
        pkg: Optional[Any] = None,
        peer: Optional[str] = None,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.config = config
        self.run_dir = run_dir
        self.inventory = inventory or {}
        self.pkg = pkg
        self.peer = peer
        self._log = log or (lambda msg: None)
        self._current_test_id: Optional[str] = None

    def log(self, msg: str) -> None:
        self._log(msg)

    @property
    def current_test_id(self) -> Optional[str]:
        """Whichever test is running, or None outside the test loop.

        Public so the debug tracer can label each command with the test that ran
        it without reaching into a private attribute. The Runner sets the
        underlying value as it steps through the plan.
        """
        return self._current_test_id

    # -- artifacts ---------------------------------------------------------
    def artifact_path(self, name: str, test_id: Optional[str] = None) -> str:
        """Absolute path for an artifact; auto-creates the directory.

        The relative path (``artifacts/<test-id>/<name>``) is what goes in
        the result's ``artifacts`` list and in the manifest.
        """
        tid = test_id or self._current_test_id or "misc"
        rel = os.path.join("artifacts", tid, name)
        abs_path = os.path.join(self.run_dir, rel)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        return abs_path

    def rel_artifact(self, name: str, test_id: Optional[str] = None) -> str:
        tid = test_id or self._current_test_id or "misc"
        return os.path.join("artifacts", tid, name)

    # -- commands ----------------------------------------------------------
    def cmd(
        self,
        argv: List[str],
        timeout: Optional[float] = None,
        artifact: Optional[str] = None,
        check: bool = False,
    ) -> procutil.CmdResult:
        tee = self.artifact_path(artifact) if artifact else None
        return procutil.run_cmd(argv, timeout=timeout, tee_path=tee, check=check)

    # -- inventory shortcuts -------------------------------------------------
    @property
    def summary(self) -> Dict[str, Any]:
        return self.inventory.get("summary", {})


class Test:
    """Base class. Subclasses set the class attributes and implement run()."""

    id: str = ""
    category: str = ""
    run_type: str = ""            # "validate" | "benchmark"
    severity: str = Severity.REQUIRED   # validate tests only
    interactive: bool = False
    default_timeout: int = 600
    packages: Tuple[str, ...] = ()
    repos: Tuple[str, ...] = ()

    def applicable(self, ctx: RunContext) -> Optional[str]:
        """Return a skip reason, or None to run."""
        return None

    def setup(self, ctx: RunContext) -> None:
        pass

    def run(self, ctx: RunContext) -> TestResult:
        raise NotImplementedError

    def teardown(self, ctx: RunContext) -> None:
        pass

    # helper for subclasses
    def result(self, status: str, **kw: Any) -> TestResult:
        kw.setdefault("severity", self.severity if self.run_type == "validate" else None)
        return TestResult(
            id=self.id,
            run_type=self.run_type,
            category=self.category,
            status=status,
            **kw,
        )


class Registry:
    def __init__(self) -> None:
        self._tests: Dict[str, type] = {}

    def register(self, cls: type) -> type:
        if not getattr(cls, "id", None):
            raise ValueError("test class %r has no id" % cls)
        if cls.id in self._tests:
            raise ValueError("duplicate test id %s" % cls.id)
        self._tests[cls.id] = cls
        return cls

    def all(self) -> List[type]:
        return [self._tests[k] for k in sorted(self._tests)]

    def get(self, test_id: str) -> Optional[type]:
        return self._tests.get(test_id)

    def select(
        self,
        run_type: str,
        only: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        interactive: bool = False,
    ) -> List[type]:
        chosen = []
        for cls in self.all():
            if cls.run_type != run_type:
                continue
            if only and cls.id not in only:
                continue
            if exclude and cls.id in exclude:
                continue
            if categories and cls.category not in categories:
                continue
            if cls.interactive and not interactive:
                continue
            chosen.append(cls)
        return chosen


REGISTRY = Registry()


VALIDATE_MODULES = (
    # ``nvidia`` and ``gpuapi`` sit beside ``gpu`` rather than inside it. ``gpu`` records what is
    # present for any vendor and gates nothing. ``nvidia`` proves a card computes through CUDA,
    # which needs that vendor's toolchain. ``gpuapi`` does the same through OpenCL and Vulkan,
    # which work on every vendor's hardware and are the only runtime some cards have.
    "cpu", "gpu", "gpuapi", "ipmi", "kernel", "memory", "network", "nvidia",
    "platform", "power", "reboot", "storage", "usb_interactive", "virt",
)
BENCHMARK_MODULES = (
    # No storage benchmarks. Certification compares machines, not drive models,
    # and a disk figure from whatever drive happens to be installed says more
    # about the drive than about the system being certified. Storage is still
    # *validated* - validate.storage.smart and validate.storage.io-sanity - and
    # that is the question the suite is asking about a disk.
    "compilebench", "compress", "cpu", "crypto", "gpu", "memory", "network",
    "sched",
)

_loaded = False


def load_all_tests() -> None:
    """Import every test module so registration side effects run."""
    global _loaded
    if _loaded:
        return
    import importlib

    for package, modules in (
        ("alma_certify.validate", VALIDATE_MODULES),
        ("alma_certify.benchmarks", BENCHMARK_MODULES),
    ):
        for name in modules:
            importlib.import_module("%s.%s" % (package, name))
    _loaded = True
