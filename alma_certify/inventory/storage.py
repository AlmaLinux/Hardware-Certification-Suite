"""Block devices: lsblk JSON, per-disk smartctl, NVMe identify/smart."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from .. import procutil


def collect(inv_dir: str) -> Dict[str, Any]:
    disks: List[Dict[str, Any]] = []
    try:
        res = procutil.run_cmd(
            [
                "lsblk", "-J", "-d", "-b",
                "-o", "NAME,MODEL,SERIAL,SIZE,ROTA,TYPE,TRAN,RM",
            ],
            timeout=30,
            tee_path=os.path.join(inv_dir, "lsblk.json.txt"),
        )
        if res.ok:
            data = json.loads(res.stdout)
            for dev in data.get("blockdevices", []):
                if dev.get("type") != "disk" or dev.get("rm"):
                    continue
                name = dev.get("name", "")
                if name.startswith(("zram", "loop", "ram", "dm-")):
                    continue
                disks.append(
                    {
                        "name": dev.get("name"),
                        "model": (dev.get("model") or "").strip() or None,
                        "serial": (dev.get("serial") or "").strip() or None,
                        "bytes": int(dev.get("size") or 0),
                        "rotational": bool(int(dev.get("rota") or 0)),
                        "transport": dev.get("tran") or _infer_transport(dev.get("name", "")),
                    }
                )
    except (procutil.CommandNotFound, json.JSONDecodeError, ValueError):
        pass

    for disk in disks:
        disk.update(_smart_info(inv_dir, disk["name"]))
        if disk.get("transport") == "nvme":
            disk.update(_nvme_info(inv_dir, disk["name"]))
        disk.setdefault("driver", _sysfs_driver(disk["name"]))

    return {"disks": disks}


def _infer_transport(name: str) -> str:
    if name.startswith("nvme"):
        return "nvme"
    if name.startswith("vd"):
        return "virtio"
    if name.startswith("md"):
        return "md"
    return "unknown"


def _sysfs_driver(name: str) -> Any:
    # /sys/block/<dev>/device/driver -> symlink to the driver
    link = "/sys/block/%s/device/driver" % name
    try:
        return os.path.basename(os.readlink(link))
    except OSError:
        return None


def _smart_info(inv_dir: str, name: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        res = procutil.run_cmd(
            ["smartctl", "-j", "-i", "-H", "/dev/%s" % name],
            timeout=60,
            tee_path=os.path.join(inv_dir, "smartctl-%s.json.txt" % name),
        )
        data = json.loads(res.stdout) if res.stdout.strip() else {}
    except (procutil.CommandNotFound, json.JSONDecodeError):
        return out
    if data.get("firmware_version"):
        out["firmware"] = data["firmware_version"]
    smart = data.get("smart_status")
    if isinstance(smart, dict) and "passed" in smart:
        out["smart_passed"] = bool(smart["passed"])
    if not out.get("model") and data.get("model_name"):
        out["model"] = data["model_name"]
    return out


def _nvme_info(inv_dir: str, name: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    ctrl = name.rstrip("0123456789").rstrip("n")  # nvme0n1 -> nvme0
    try:
        res = procutil.run_cmd(
            ["nvme", "id-ctrl", "/dev/%s" % ctrl, "-o", "json"],
            timeout=30,
            tee_path=os.path.join(inv_dir, "nvme-id-ctrl-%s.json.txt" % ctrl),
        )
        if res.ok:
            data = json.loads(res.stdout)
            if data.get("fr"):
                out["firmware"] = str(data["fr"]).strip()
            if data.get("mn"):
                out["model"] = str(data["mn"]).strip()
        procutil.run_cmd(
            ["nvme", "smart-log", "/dev/%s" % ctrl, "-o", "json"],
            timeout=30,
            tee_path=os.path.join(inv_dir, "nvme-smart-log-%s.json.txt" % ctrl),
        )
    except (procutil.CommandNotFound, json.JSONDecodeError):
        pass
    return out
