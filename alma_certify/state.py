"""Run directory layout and the resume journal.

Layout of ``<run_dir>/<run_id>/``:

    report.json     final report (written by report.py)
    state.json      resume journal, rewritten after every test
    inventory/      raw collector outputs
    artifacts/      per-test raw logs
    alma-certify.log   suite log
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional


class RunState:
    def __init__(self, run_dir: str, data: Dict[str, Any]):
        self.run_dir = run_dir
        self.data = data

    # -- creation / loading -------------------------------------------------
    @classmethod
    def create(cls, base_dir: str, run_id: str, meta: Dict[str, Any]) -> RunState:
        run_dir = os.path.join(base_dir, run_id)
        os.makedirs(os.path.join(run_dir, "inventory"), exist_ok=True)
        os.makedirs(os.path.join(run_dir, "artifacts"), exist_ok=True)
        data = {
            "run_id": run_id,
            "meta": meta,
            "completed": {},   # test_id -> result dict
            "plan": [],        # ordered test ids
            "resumed": False,
        }
        state = cls(run_dir, data)
        state.save()
        return state

    @classmethod
    def load(cls, base_dir: str, run_id: str) -> RunState:
        run_dir = os.path.join(base_dir, run_id)
        with open(os.path.join(run_dir, "state.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return cls(run_dir, data)

    @classmethod
    def find(cls, base_dir: str, run_id_prefix: str) -> RunState:
        """Load by full id or unique prefix."""
        if os.path.isdir(os.path.join(base_dir, run_id_prefix)):
            return cls.load(base_dir, run_id_prefix)
        matches = [
            d for d in sorted(os.listdir(base_dir))
            if d.startswith(run_id_prefix)
            and os.path.isfile(os.path.join(base_dir, d, "state.json"))
        ]
        if len(matches) != 1:
            raise FileNotFoundError(
                "run %r: %s" % (run_id_prefix, "not found" if not matches else "ambiguous")
            )
        return cls.load(base_dir, matches[0])

    # -- journal -------------------------------------------------------------
    def save(self) -> None:
        path = os.path.join(self.run_dir, "state.json")
        fd, tmp = tempfile.mkstemp(dir=self.run_dir, prefix=".state-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=1)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def set_plan(self, test_ids: List[str]) -> None:
        self.data["plan"] = list(test_ids)
        self.save()

    def record_result(self, result_dict: Dict[str, Any]) -> None:
        self.data["completed"][result_dict["id"]] = result_dict
        self.save()

    def is_completed(self, test_id: str) -> bool:
        return test_id in self.data["completed"]

    def mark_resumed(self) -> None:
        self.data["resumed"] = True
        self.save()

    # -- accessors -----------------------------------------------------------
    @property
    def run_id(self) -> str:
        return self.data["run_id"]

    @property
    def meta(self) -> Dict[str, Any]:
        return self.data["meta"]

    def results(self) -> List[Dict[str, Any]]:
        ordered = [t for t in self.data["plan"] if t in self.data["completed"]]
        extra = [t for t in self.data["completed"] if t not in ordered]
        return [self.data["completed"][t] for t in ordered + sorted(extra)]

    def inventory_path(self, name: str) -> str:
        return os.path.join(self.run_dir, "inventory", name)

    def load_inventory(self) -> Optional[Dict[str, Any]]:
        path = self.inventory_path("inventory.json")
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
