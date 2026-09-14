#!/usr/bin/env python3
"""Vendor the Textual TUI's third-party stack into ``alma_certify_tui/_vendor/``.

This runs on a packager's machine, never at runtime, so it may assume a current Python even though
the vendored code has to import on the suite's floor (3.9, on el9). It downloads the pinned,
pure-Python wheels from PyPI, verifies their hashes, extracts them, applies the prune and patch
transforms the manifest asks for, collects each package's license text, and prints the two things
the spec needs kept accurate:

  * the ``Provides: bundled(python3dist(NAME)) = VERSION`` block, and
  * the combined SPDX ``License:`` expression.

The real pygments is not vendored. Both Rich and Textual declare it and import it lazily from three
modules (rich.syntax, rich.traceback, textual.highlight), so instead the manifest injects our own
tiny MIT shim under the name ``pygments`` (packaging/vendor/patches/pygments/) that renders
everything as plain text. After vendoring, the script imports Rich and Textual against that shim and
renders a traceback through it in a subprocess; if an upstream bump reaches for pygments API the
shim does not cover, that import fails here at vendor time rather than on a user's machine.

Stdlib only: urllib for fetch, hashlib for verify, zipfile for the wheel, email.parser for METADATA.

Usage:
    python3 packaging/vendor/update_vendor.py           # download, vendor, print the spec lines
    python3 packaging/vendor/update_vendor.py --check    # fail if a fresh vendor would differ (CI)
"""
from __future__ import annotations

import argparse
import email.parser
import fnmatch
import hashlib
import io
import json
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
MANIFEST_PATH = HERE / "manifest.json"
LOCK_PATH = HERE / "vendor.lock.json"
PATCHES_DIR = HERE / "patches"

PYPI = "https://pypi.org/pypi"
UA = {"User-Agent": "alma-certify-vendor/1 (+https://github.com/AlmaLinux)"}


# --------------------------------------------------------------------------- fetch


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (https, fixed host)
        return json.loads(resp.read().decode("utf-8"))


def _get_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
        return resp.read()


# --------------------------------------------------------------------------- versions


def _parse_version(text: str) -> Optional[Tuple[int, ...]]:
    """A sortable key for a plain release version, or None for a pre-release.

    Pre-releases are skipped rather than ranked: vendoring a3/rc into a certification tool by
    accident is worse than missing a version, and these libraries all publish finals. Anything with
    a non-numeric segment (``1.0rc1``, ``1.0.dev0``, an epoch) returns None and is passed over.
    """
    parts = text.split(".")
    out: List[int] = []
    for part in parts:
        if not part.isdigit():
            return None
        out.append(int(part))
    return tuple(out)


def _requires_python_admits(spec: str, floor: Tuple[int, int]) -> bool:
    """Whether a ``Requires-Python`` string admits our floor interpreter.

    A deliberately small PEP 440 subset: comma-joined ``OP VERSION`` clauses, all of which must
    hold. Enough for the handful of libraries here, whose specifiers are ordinary lower and upper
    bounds. ``~=`` and epochs are treated as satisfied rather than parsed, because none of these
    packages use them to exclude 3.9 and guessing wrong would only ever be caught by the import
    check at the end anyway.
    """
    if not spec:
        return True
    fx, fy = floor
    floor_key = (fx, fy, 0)
    for raw in spec.split(","):
        clause = raw.strip()
        if not clause:
            continue
        m = re.match(r"(>=|<=|==|!=|<|>|~=)\s*([0-9][0-9.]*)", clause)
        if not m:
            continue
        op, ver = m.group(1), m.group(2)
        key = _parse_version(ver) or tuple(int(p) for p in ver.split(".") if p.isdigit())
        key = (key + (0, 0, 0))[:3]
        if op == "~=":
            continue
        if op == ">=" and not (floor_key >= key):
            return False
        if op == ">" and not (floor_key > key):
            return False
        if op == "<=" and not (floor_key <= key):
            return False
        if op == "<" and not (floor_key < key):
            return False
        if op == "==" and not (floor_key[: len(key)] == key):
            return False
        if op == "!=" and floor_key[: len(key)] == key:
            return False
    return True


def _pick_wheel(files: List[dict]) -> Optional[dict]:
    """The pure-Python wheel among a release's files, if it has one.

    ``py3-none-any`` or ``py2.py3-none-any``: an ``any`` platform tag and no ABI, which is what
    lets a vendored copy run on every arch the suite targets. A package that ships only
    arch-specific wheels is not vendorable this way and the caller reports that.
    """
    for f in files:
        if f.get("packagetype") != "bdist_wheel" or f.get("yanked"):
            continue
        name = f.get("filename", "")
        if name.endswith("-py3-none-any.whl") or name.endswith("-py2.py3-none-any.whl"):
            return f
    return None


def _resolve(name: str, pin: str, floor: Tuple[int, int]) -> Tuple[str, dict]:
    """Return ``(version, wheel_file)`` for a package: the pin if given, else newest that fits."""
    if pin and pin != "auto":
        data = _get_json(f"{PYPI}/{name}/{pin}/json")
        wheel = _pick_wheel(data["urls"])
        if wheel is None:
            raise SystemExit(f"{name} {pin}: no pure-Python wheel on PyPI")
        return pin, wheel

    data = _get_json(f"{PYPI}/{name}/json")
    releases: Dict[str, List[dict]] = data["releases"]
    candidates = sorted(
        (v for v in releases if _parse_version(v) is not None),
        key=lambda v: _parse_version(v),
        reverse=True,
    )
    for version in candidates:
        files = releases[version]
        wheel = _pick_wheel(files)
        if wheel is None:
            continue
        if not _requires_python_admits(wheel.get("requires_python") or "", floor):
            continue
        return version, wheel
    raise SystemExit(f"{name}: no release with a pure-Python wheel admitting {floor[0]}.{floor[1]}")


# --------------------------------------------------------------------------- metadata / license


def _read_metadata(zf: zipfile.ZipFile, distinfo: str) -> email.message.Message:
    with zf.open(f"{distinfo}/METADATA") as fh:
        return email.parser.BytesParser().parse(fh)


def _license_from_metadata(meta: email.message.Message) -> Optional[str]:
    """The wheel's own SPDX license, if it states one we can trust.

    Prefer PEP 639 ``License-Expression`` (already SPDX). Fall back to a tiny classifier map for the
    licenses in this tree. A bare freeform ``License:`` is ignored: it is prose as often as an id,
    and the manifest is the source of truth precisely so the spec is deterministic.
    """
    expr = meta.get("License-Expression")
    if expr:
        return expr.strip()
    classifier_map = {
        "License :: OSI Approved :: MIT License": "MIT",
        "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
        "License :: OSI Approved :: BSD License": "BSD-2-Clause",
    }
    for classifier in meta.get_all("Classifier") or []:
        if classifier.strip() in classifier_map:
            return classifier_map[classifier.strip()]
    return None


def _requires_dist_names(meta: email.message.Message) -> List[str]:
    """Bare names of the wheel's *unconditional* runtime dependencies.

    Anything gated on ``extra ==`` is an optional extra we did not ask for and is dropped;
    everything else (including plain ``python_version`` conditionals) is a hard dependency whose
    absence from the manifest means we would under-vendor. This is the drift check's raw input.
    """
    names: List[str] = []
    for raw in meta.get_all("Requires-Dist") or []:
        spec, _, marker = raw.partition(";")
        if "extra ==" in marker:
            continue
        m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
        if m:
            names.append(_canonical(m.group(1)))
    return names


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# --------------------------------------------------------------------------- extract / place


def _wheel_top_levels(zf: zipfile.ZipFile, distinfo: str) -> List[str]:
    """Top-level importable entries in the wheel: package dirs and single-module files.

    Read from the archive rather than guessed from the project name, because the two differ
    (``markdown-it-py`` imports as ``markdown_it``) and a wheel can carry more than one.
    """
    tops = set()
    for info in zf.namelist():
        first = info.split("/", 1)[0]
        if first.endswith(".dist-info") or first.endswith(".data"):
            continue
        tops.add(first)
    return sorted(tops)


def _place_package(zf: zipfile.ZipFile, tops: List[str], vendor_dir: Path) -> None:
    for top in tops:
        target = vendor_dir / top
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
    for info in zf.infolist():
        first = info.filename.split("/", 1)[0]
        if first not in tops or info.is_dir():
            continue
        dest = vendor_dir / info.filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)


def _collect_licenses(
    zf: zipfile.ZipFile, distinfo: str, name: str, licenses_dir: Path
) -> List[str]:
    """Copy a package's license texts into ``_vendor/licenses/<name>/`` and return their names.

    Fedora ships the actual texts under ``%license``, not just the tag. Newer wheels put them under
    ``<dist-info>/licenses/``; older ones drop a ``LICENSE``/``COPYING`` at the dist-info root.
    """
    dest_dir = licenses_dir / name
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    found: List[str] = []
    for entry in zf.namelist():
        if entry.endswith("/"):
            continue
        base = entry.rsplit("/", 1)[-1]
        in_licenses = entry.startswith(f"{distinfo}/licenses/")
        at_root = entry.startswith(f"{distinfo}/") and re.match(
            r"(LICENSE|LICENCE|COPYING|NOTICE|AUTHORS)", base, re.IGNORECASE
        )
        if not (in_licenses or at_root):
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / base
        with zf.open(entry) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
        found.append(base)
    return found


_LICENSE_NAME = re.compile(r"(LICENSE|LICENCE|COPYING|NOTICE|AUTHORS)", re.IGNORECASE)


def _in_tree_licenses(vendor_dir: Path) -> List[str]:
    """License texts that sit inside a vendored package rather than in ``licenses/``.

    mdit-py-plugins keeps one per plugin for code it vendored in turn - separate copyrights, which
    the RPM has to ship too. Found by scanning rather than listed by hand, so a version bump that
    adds or drops one cannot pass unnoticed. Source files are skipped: a module named ``license.py``
    would otherwise match on its name alone.
    """
    licenses_dir = vendor_dir / "licenses"
    found: List[str] = []
    for path in sorted(vendor_dir.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts or licenses_dir in path.parents:
            continue
        if path.suffix in {".py", ".pyc", ".pyi"}:
            continue
        if _LICENSE_NAME.match(path.name):
            found.append(path.relative_to(vendor_dir).as_posix())
    return found


# --------------------------------------------------------------------------- transforms


def _apply_prune(vendor_dir: Path, globs: List[str]) -> None:
    """Delete anything whose *name* matches a pattern's final component, at any depth.

    The patterns are basename globs written with a leading ``**/`` for readability (``**/tests``,
    ``**/__pycache__``); only that final component is matched, against ``path.name``, which is all
    these rules need and avoids pretending ``fnmatch`` understands ``**``.
    """
    names = [pattern.rsplit("/", 1)[-1] for pattern in globs]
    for path in sorted(vendor_dir.rglob("*"), reverse=True):
        if not path.exists():
            continue
        if any(fnmatch.fnmatch(path.name, name) for name in names):
            shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink()


def _apply_patch(vendor_dir: Path, target_rel: str, patch_rel: str) -> None:
    patch = PATCHES_DIR / patch_rel
    if not patch.is_file():
        raise SystemExit(f"patch source missing: {patch}")
    dest = vendor_dir / target_rel
    if not dest.exists():
        raise SystemExit(f"patch target not vendored: {target_rel} (did the layout change?)")
    dest.write_bytes(patch.read_bytes())


SMOKE = r"""
import sys
sys.path.insert(0, {vendor!r})

# The shim must win over any real pygments the packager happens to have installed; the sentinel
# proves the copy on the path is ours and not the multi-megabyte library we are avoiding.
import pygments
assert getattr(pygments, "__vendored_stub__", False), "pygments on the path is not our shim"

# The normal path, and every module that reaches for pygments lazily (syntax, tracebacks, Textual's
# highlighter). If the shim is missing a name any of these import, this raises here at vendor time.
import rich.console, rich.syntax, rich.traceback  # noqa: E401,F401
import textual.app, textual.widgets, textual.highlight  # noqa: E401,F401

# Exercise the two paths that actually construct highlighted output, not just import it: a rendered
# traceback (Textual's worker/driver error path) and Textual's highlighter. Rendering, not just
# importing, is what proves the shim's Lexer/Style/token surface is enough.
import io
from rich.console import Console
from rich.traceback import Traceback
try:
    raise ValueError("vendor smoke test")
except ValueError:
    Console(file=io.StringIO(), force_terminal=True).print(Traceback())
from textual.highlight import highlight
highlight("print('hello')\n", language="python")
print("SMOKE_OK")
"""


def _smoke_import(vendor_dir: Path) -> None:
    """Prove the vendored forest imports and renders with only the shim standing in for pygments.

    A subprocess actually imports Rich and Textual and renders a traceback through the shim, rather
    than grepping for ``import pygments`` (which could not tell our shim from the real thing, now
    that the shim is on the path on purpose). ``-S`` drops system site-packages so nothing can be
    silently satisfied by a package we forgot to vendor; the only importables are the stdlib and
    this tree. It runs on the packager's interpreter, so it checks the API surface, not 3.9 syntax;
    the spec's %%check is what exercises the floor interpreter on el9.
    """
    import subprocess

    proc = subprocess.run(
        # -B so importing does not scatter __pycache__ into the tree we are about to ship; -S drops
        # site-packages (self-containment); -I isolates env and user site.
        [sys.executable, "-S", "-I", "-B", "-c", SMOKE.format(vendor=str(vendor_dir))],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or "SMOKE_OK" not in proc.stdout:
        raise SystemExit(
            "vendored forest failed to import/render with only the pygments shim:\n"
            + (proc.stderr or proc.stdout).strip()
            + "\n\nUpstream likely reaches for more of pygments (or a dep we did not vendor). "
            "Extend patches/pygments/ to cover the new API, or add the missing package to the "
            "manifest."
        )


# --------------------------------------------------------------------------- main


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def vendor(manifest: dict, *, vendor_dir: Path) -> dict:
    floor = tuple(int(p) for p in manifest["min_python"].split("."))[:2]
    licenses_dir = vendor_dir / "licenses"
    vendor_dir.mkdir(parents=True, exist_ok=True)

    # Wipe the importable packages but keep licenses/ (rebuilt per package) and this dir's marker.
    for child in sorted(vendor_dir.iterdir()):
        if child.name in ("licenses", "__init__.py", "README.md"):
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()

    records: List[dict] = []
    all_requires: set = set()
    manifest_names = {_canonical(p["name"]) for p in manifest["packages"]}

    for pkg in manifest["packages"]:
        name = pkg["name"]
        version, wheel = _resolve(name, pkg.get("version", "auto"), floor)
        sha = wheel["digests"]["sha256"]
        print(f"  {name} {version}  ({wheel['filename']})")
        blob = _get_bytes(wheel["url"])
        got = hashlib.sha256(blob).hexdigest()
        if got != sha:
            raise SystemExit(f"{name} {version}: sha256 mismatch (PyPI {sha}, downloaded {got})")

        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            distinfo = next(
                n.split("/", 1)[0] for n in zf.namelist() if n.endswith(".dist-info/METADATA")
            )
            meta = _read_metadata(zf, distinfo)
            tops = _wheel_top_levels(zf, distinfo)
            _place_package(zf, tops, vendor_dir)
            license_files = _collect_licenses(zf, distinfo, name, licenses_dir)

        declared = pkg["license"]
        found_spdx = _license_from_metadata(meta)
        if found_spdx and _canonical_spdx(found_spdx) != _canonical_spdx(declared):
            raise SystemExit(
                f"{name} {version}: manifest license {declared!r} disagrees with wheel "
                f"metadata {found_spdx!r}. Update the manifest deliberately if upstream relicensed."
            )
        all_requires.update(_requires_dist_names(meta))
        records.append({
            "name": name,
            "version": version,
            "sha256": sha,
            "license": declared,
            "requires_python": wheel.get("requires_python") or "",
            "top_levels": tops,
            "license_files": license_files,
        })

    for pkg in manifest["packages"]:
        for target_rel, patch_rel in (pkg.get("patch") or {}).items():
            _apply_patch(vendor_dir, target_rel, patch_rel)

    # Local shims we author (the pygments null-highlighter), copied in over nothing: they are our
    # own code, so they carry no Provides and no third-party license, and the manifest treats them
    # separately from the wheels above.
    injected: List[dict] = []
    for inj in manifest.get("inject", []):
        src = HERE / inj["src"]
        dest = vendor_dir / inj["dest"]
        if dest.exists():
            shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
        shutil.copytree(src, dest)
        injected.append({"dest": inj["dest"], "note": inj.get("note", "")})

    _apply_prune(vendor_dir, manifest.get("prune_globs", []))
    _smoke_import(vendor_dir)

    # Drift: hard deps a wheel declares that we neither vendor nor deliberately shim. The ignore
    # list carries the on-purpose omissions (pygments, shimmed) with their reason, so this warning
    # stays meaningful for the case it is for: a version bump quietly adding a brand-new dependency.
    ignore = {_canonical(n) for n in (manifest.get("drift_ignore") or {})}
    missing = sorted(n for n in all_requires if n not in manifest_names and n not in ignore)
    return {
        "packages": records,
        "injected": injected,
        "drift": missing,
        "in_tree_licenses": _in_tree_licenses(vendor_dir),
    }


def _canonical_spdx(expr: str) -> str:
    return re.sub(r"\s+", " ", expr).strip().upper()


def _emit(result: dict) -> None:
    records = result["packages"]
    print("\n# --- paste into alma-certify.spec, %package tui ---\n")

    # The tag first, in the order the spec carries it: the one expression covering the whole
    # bundled set, then what each line in that set contributes to it.
    spdx = sorted({r["license"] for r in records})
    print("# License tag for the tui subpackage (its own MIT AND the bundled set):")
    print("License:        " + " AND ".join(["MIT", *[s for s in spdx if s != "MIT"]]))

    # A license above each line, because the tag above says PSF-2.0 is in there somewhere without
    # saying which package brings it, and a reviewer asking that should not have to open nine
    # wheels to find out.
    print()
    for r in records:
        print(f"# {r['license']}")
        print(f"Provides:       bundled(python3dist({_canonical(r['name'])})) = {r['version']}")

    # The texts themselves are not listed: %install stages _vendor/licenses/ into bundled/ and
    # %files tui marks it with `%license bundled/`, so there is nothing to paste and the shipped
    # set is `rpm -qp --licensefiles`. Only the case that needs a person is printed.
    bare = [r["name"] for r in records if not r["license_files"]]
    if bare:
        print("\n# WARNING: no license text in the wheel, so the RPM would ship none for:")
        for name in bare:
            print(f"#   {name}")

    injected = result.get("injected") or []
    if injected:
        print("\n# injected local shims (our own code, covered by the suite's MIT; no Provides):")
        for inj in injected:
            print(f"#   {inj['dest']}  - {inj['note']}")

    if result["drift"]:
        print("\n# WARNING: wheels declare runtime deps not in the manifest (under-vendoring?):")
        for name in result["drift"]:
            print(f"#   {name}")
    else:
        print("\n# drift check: clean (every hard dependency is in the manifest)")


def _tree_digest(root: Path) -> Dict[str, str]:
    """sha256 of every file under ``root`` (bytecode aside), keyed by POSIX relpath, for --check."""
    out: Dict[str, str] = {}
    if not root.exists():
        return out
    for path in root.rglob("*"):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="fail if a fresh vendor would differ from the committed tree (CI)")
    args = parser.parse_args(argv)

    manifest = _load(MANIFEST_PATH)
    vendor_dir = REPO_ROOT / manifest["vendor_dir"]

    if args.check:
        # Vendor into a throwaway dir and diff it against what is committed, touching nothing.
        # Catches an edited manifest nobody re-vendored, and an "auto" pin upstream has since moved.
        import tempfile
        before = _tree_digest(vendor_dir)
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / "vendor"
            vendor(manifest, vendor_dir=fresh)
            after = _tree_digest(fresh)
        changed = sorted(
            (set(before) ^ set(after))
            | {p for p in set(before) & set(after) if before[p] != after[p]}
        )
        if changed:
            print(f"re-vendoring would change {len(changed)} file(s); run update_vendor.py:")
            for rel in changed[:40]:
                print(f"  {rel}")
            return 1
        print("vendored tree is up to date")
        return 0

    print(f"vendoring into {vendor_dir.relative_to(REPO_ROOT)} (floor {manifest['min_python']}):")
    result = vendor(manifest, vendor_dir=vendor_dir)
    LOCK_PATH.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _emit(result)
    print(f"\nwrote {LOCK_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
