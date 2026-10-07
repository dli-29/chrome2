#!/usr/bin/env python3
"""Build the Chrome 2 engine bundle: PyQt6 + Qt WebEngine *with* H.264/AAC, relocatable, for Apple Silicon Macs.

Runs on a macOS arm64 machine (GitHub's macos-15 runner) after

    brew install pyqt qtimageformats

and turns Homebrew's bottles (qtwebengine is built with -DFEATURE_webengine_proprietary_codecs=ON) into one
self-contained folder that works without Homebrew and without admin rights:

    chrome2-engine/
      site/PyQt6/...                 the PyQt6 modules Chrome 2 imports (+ sip), plus the .pth helpers
      qt/lib/Qt*.framework           only the Qt frameworks those modules need (QtWebEngineCore with its
                                     QtWebEngineProcess helper app, resources, locales, ICU data)
      qt/plugins/<kind>/*.dylib      platforms (cocoa, offscreen, ...), imageformats, iconengines, tls, styles, ...
      lib/*.dylib                    every other non-system library they load (ICU, OpenSSL, libpng, ...)
      share/selftest-h264-aac.mp4    a tiny H.264+AAC clip for the installer's self-test
      licenses/<formula>/...         license files of everything bundled
      manifest.json

Every Mach-O load command, install id and LC_RPATH is rewritten to @rpath / @loader_path, so nothing refers to
/opt/homebrew (or any other absolute non-system path); everything is re-signed ad hoc. The build fails if otool
still finds /opt/homebrew, /usr/local or Cellar anywhere in a load command.

Usage (on the runner):
    "$(brew --prefix python@3.14)/bin/python3.14" tools/engine/build_bundle.py --work build --out dist \
        --selftest-media media/selftest-h264-aac.mp4 --build-number 7
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

BUNDLE_NAME = "chrome2-engine"
TARBALL = "chrome2-engine-arm64.tar.gz"
RELEASE_MANIFEST = "chrome2-engine-manifest.json"
HELPER_CANDIDATES = ("qt/lib/QtWebEngineCore.framework/Versions/A/Helpers/QtWebEngineProcess.app",
                     "qt/lib/QtWebEngineCore.framework/Helpers/QtWebEngineProcess.app")

# The PyQt6 modules foxglove.py imports (directly or through the modules it imports - the closure is taken from
# sys.modules after importing these with Homebrew's Python).
REQUIRED_MODULES = ["sip", "QtCore", "QtGui", "QtWidgets", "QtNetwork", "QtPrintSupport", "QtSvg",
                    "QtWebEngineCore", "QtWebEngineWidgets", "QtWebChannel"]
# Cheap extras: included only when they need no Qt framework beyond the ones the required modules already pull in.
OPTIONAL_MODULES = ["QtSvgWidgets", "QtOpenGL", "QtOpenGLWidgets", "QtQml", "QtQuick", "QtQuickWidgets",
                    "QtPositioning", "QtWebEngineQuick", "QtConcurrent", "QtXml", "QtTest", "QtDBus", "QtStateMachine"]
# Plugin kinds to ship. A plugin is skipped when it would drag in a Qt framework the modules don't already need.
PLUGIN_KINDS = ["platforms", "imageformats", "iconengines", "tls", "styles", "networkinformation", "generic",
                "permissions", "position", "printsupport", "platforminputcontexts", "platformthemes"]

FORBIDDEN = ("/opt/homebrew", "/usr/local", "Cellar")
SYSTEM_PREFIXES = ("/usr/lib/", "/System/Library/")
PYTHON_ORG_FRAMEWORK = "/Library/Frameworks/Python.framework/Versions/3.14/Python"
FRAMEWORK_IGNORE = {"Headers", ".DS_Store"}

# ── Mach-O parsing (just what relocation needs) ──────────────────────────────────────────────────────────────────────
MH_MAGIC_64 = 0xFEEDFACF
FAT_MAGIC, FAT_MAGIC_64 = 0xCAFEBABE, 0xCAFEBABF
CPU_TYPE_ARM64 = 0x0100000C
MH_EXECUTE, MH_DYLIB, MH_BUNDLE = 2, 6, 8
LC_LOAD_DYLIB, LC_ID_DYLIB, LC_LOAD_WEAK_DYLIB = 0xC, 0xD, 0x80000018
LC_REEXPORT_DYLIB, LC_LAZY_LOAD_DYLIB, LC_LOAD_UPWARD_DYLIB = 0x8000001F, 0x20, 0x80000023
LC_RPATH, LC_BUILD_VERSION, LC_VERSION_MIN_MACOSX = 0x8000001C, 0x32, 0x24
DEP_COMMANDS = {LC_LOAD_DYLIB: "load", LC_LOAD_WEAK_DYLIB: "weak", LC_REEXPORT_DYLIB: "reexport",
                LC_LAZY_LOAD_DYLIB: "lazy", LC_LOAD_UPWARD_DYLIB: "upward"}


class MachO:
    def __init__(self, path: str, filetype: int, install_id: str | None, deps: list[tuple[str, str]],
                 rpaths: list[str], minos: str | None):
        self.path, self.filetype, self.install_id = path, filetype, install_id
        self.deps, self.rpaths, self.minos = deps, rpaths, minos


def _arm64_offset(fh) -> int | None:
    """Offset of the arm64 Mach-O image in the file, or None when it isn't a Mach-O file."""
    head = fh.read(8)
    if len(head) < 8:
        return None
    if struct.unpack_from("<I", head)[0] == MH_MAGIC_64:
        return 0
    magic, count = struct.unpack_from(">II", head)
    if magic not in (FAT_MAGIC, FAT_MAGIC_64) or not 0 < count < 20:  # (Java class files share FAT_MAGIC)
        return None
    size = 20 if magic == FAT_MAGIC else 32
    table = fh.read(size * count)
    for i in range(count):
        if magic == FAT_MAGIC:
            cputype, _sub, offset, _size, _align = struct.unpack_from(">iiIII", table, i * size)
        else:
            cputype, _sub, offset, _size, _align, _res = struct.unpack_from(">iiQQII", table, i * size)
        if cputype == CPU_TYPE_ARM64:
            return offset
    raise SystemExit(f"error: {fh.name} is a universal binary without an arm64 slice")


def is_macho(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return _arm64_offset(fh) is not None
    except (OSError, SystemExit):
        return False


def _version(value: int) -> str:
    major, minor, patch = value >> 16, (value >> 8) & 0xFF, value & 0xFF
    return f"{major}.{minor}.{patch}" if patch else f"{major}.{minor}"


_CACHE: dict[str, MachO] = {}


def parse_macho(path: str) -> MachO:
    path = os.path.realpath(path)
    if path in _CACHE:
        return _CACHE[path]
    with open(path, "rb") as fh:
        base = _arm64_offset(fh)
        if base is None:
            raise SystemExit(f"error: not a Mach-O file: {path}")
        fh.seek(base)
        header = fh.read(32)
        magic, _cpu, _sub, filetype, ncmds, sizeofcmds, _flags, _res = struct.unpack("<IiiIIIII", header)
        if magic != MH_MAGIC_64:
            raise SystemExit(f"error: unsupported Mach-O (magic {magic:#x}): {path}")
        blob = fh.read(sizeofcmds)
    install_id, deps, rpaths, minos = None, [], [], None
    pos = 0
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", blob, pos)

        def cstring(offset_field: int) -> str:
            start = pos + struct.unpack_from("<I", blob, pos + offset_field)[0]
            end = blob.index(b"\0", start, pos + cmdsize) if b"\0" in blob[start:pos + cmdsize] else pos + cmdsize
            return blob[start:end].decode("utf-8")

        if cmd == LC_ID_DYLIB:
            install_id = cstring(8)
        elif cmd in DEP_COMMANDS:
            deps.append((DEP_COMMANDS[cmd], cstring(8)))
        elif cmd == LC_RPATH:
            rpaths.append(cstring(8))
        elif cmd == LC_BUILD_VERSION:
            platform, value = struct.unpack_from("<II", blob, pos + 8)
            if platform == 1:  # PLATFORM_MACOS
                minos = _version(value)
        elif cmd == LC_VERSION_MIN_MACOSX:
            minos = _version(struct.unpack_from("<I", blob, pos + 8)[0])
        pos += cmdsize
    info = MachO(path, filetype, install_id, deps, rpaths, minos)
    _CACHE[path] = info
    return info


def is_system(name: str) -> bool:
    return name.startswith(SYSTEM_PREFIXES)


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)) or (0,)


# ── Helpers ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def run(cmd: list[str], check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"error: command failed ({result.returncode}): {' '.join(cmd)}\n{result.stdout}")
    if not quiet and result.stdout.strip() and result.returncode != 0:
        print(result.stdout.rstrip())
    return result


def log(message: str) -> None:
    print(message, flush=True)


def framework_root(path: str) -> str | None:
    """'/x/lib/QtCore.framework/Versions/A/QtCore' -> '/x/lib/QtCore.framework'."""
    parts = path.split("/")
    for i, part in enumerate(parts):
        if part.endswith(".framework"):
            return "/".join(parts[: i + 1])
    return None


def cellar_formula(path: str) -> str | None:
    match = re.search(r"/Cellar/([^/]+)/([^/]+)/", path)
    return match.group(1) if match else None


def brew(*args: str) -> str:
    return run(["brew", *args]).stdout.strip()


# ── The bundle ───────────────────────────────────────────────────────────────────────────────────────────────────────
class Bundle:
    def __init__(self, root: Path, brew_prefix: str):
        self.root = root
        self.brew_prefix = brew_prefix
        self.fallback_dirs = [os.path.join(brew_prefix, "lib"), os.path.join(brew_prefix, "Frameworks")]
        self.placed: dict[str, str] = {}       # source realpath (Mach-O) -> destination path in the bundle
        self.identity: dict[str, str] = {}     # source realpath -> name after "@rpath/" (frameworks and lib/)
        self.kind: dict[str, str] = {}         # source realpath -> "framework" | "lib" | "module" | "plugin"
        self.frameworks: dict[str, str] = {}   # framework source root -> destination root
        self.lib_names: dict[str, str] = {}    # lib/ file name -> source realpath
        self.edges: dict[str, dict[str, str]] = {}  # source realpath -> {dependency name as written: target realpath}
        self.python_refs: list[str] = []
        self.formulae: set[str] = set()
        self.notes: list[str] = []

    # Resolving dependencies the way dyld would on the build machine
    def resolve(self, name: str, info: MachO) -> str | None:
        here = os.path.dirname(info.path)
        executable = info.filetype == MH_EXECUTE

        def expand(path: str) -> str | None:
            if path.startswith("@loader_path"):
                return here + path[len("@loader_path"):]
            if path.startswith("@executable_path"):
                return here + path[len("@executable_path"):] if executable else None
            return path

        candidates: list[str] = []
        if name.startswith("@rpath/"):
            rest = name[len("@rpath/"):]
            for rpath in info.rpaths:
                base = expand(rpath)
                if base:
                    candidates.append(os.path.join(base, rest))
            candidates += [os.path.join(d, rest) for d in self.fallback_dirs]
        elif name.startswith(("@loader_path/", "@executable_path/")):
            path = expand(name)
            if path:
                candidates.append(path)
        else:
            candidates.append(name)
            # A missing absolute path: maybe a renamed keg (opt/ link) - look for the same leaf in the fallbacks
            leaf = name.split(".framework/")[-1] if ".framework/" in name else os.path.basename(name)
            if ".framework/" in name:
                fw = os.path.basename(framework_root(name) or "")
                candidates += [os.path.join(d, fw, leaf) for d in self.fallback_dirs]
            else:
                candidates += [os.path.join(d, leaf) for d in self.fallback_dirs]
        for candidate in candidates:
            if os.path.isfile(candidate):
                return os.path.realpath(candidate)
        return None

    def closure(self, roots: list[str]) -> dict[str, MachO]:
        """Every Mach-O file *roots* load, transitively (non-system only), plus everything inside the frameworks
        they touch (QtWebEngineCore's helper app). Doesn't copy anything."""
        seen: dict[str, MachO] = {}
        frameworks_done: set[str] = set()
        stack = [os.path.realpath(r) for r in roots]
        while stack:
            path = stack.pop()
            if path in seen:
                continue
            info = parse_macho(path)
            seen[path] = info
            fw = framework_root(path)
            if fw and fw not in frameworks_done:
                frameworks_done.add(fw)
                for dirpath, dirnames, filenames in os.walk(fw):
                    dirnames[:] = [d for d in dirnames if d not in FRAMEWORK_IGNORE and not d.endswith(".dSYM")]
                    for filename in filenames:
                        full = os.path.join(dirpath, filename)
                        if not os.path.islink(full) and is_macho(full):
                            stack.append(os.path.realpath(full))
            for _kind, name in info.deps:
                if is_system(name):
                    continue
                target = self.resolve(name, info)
                if target is None:
                    if _kind == "weak":
                        self.notes.append(f"weak dependency not found (left as @rpath leaf): {name} in {path}")
                        continue
                    raise SystemExit(f"error: can't resolve {name}\n  needed by {path}\n  rpaths: {info.rpaths}")
                self.edges.setdefault(path, {})[name] = target
                if "Python.framework" in target:
                    continue  # the interpreter: never bundled (see place_all)
                stack.append(target)
        return seen

    @staticmethod
    def qt_frameworks(files: dict[str, MachO]) -> set[str]:
        return {os.path.basename(framework_root(p)) for p in files if framework_root(p)}

    # Copying
    def copy_file(self, src: str, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        mode = os.stat(src).st_mode
        os.chmod(dst, (stat.S_IMODE(mode) | stat.S_IWUSR | stat.S_IRUSR) & 0o755 | (0o111 if mode & 0o111 else 0))
        formula = cellar_formula(src)
        if formula:
            self.formulae.add(formula)

    def place_framework(self, fw_src: str) -> None:
        if fw_src in self.frameworks:
            return
        name = os.path.basename(fw_src)
        dst = self.root / "qt" / "lib" / name

        def ignore(directory: str, names: list[str]) -> set[str]:
            return {n for n in names if n in FRAMEWORK_IGNORE or n.endswith((".prl", ".dSYM"))}

        shutil.copytree(fw_src, dst, symlinks=True, ignore=ignore)
        for dirpath, _dirs, filenames in os.walk(dst):
            for filename in filenames:
                full = os.path.join(dirpath, filename)
                if not os.path.islink(full):
                    os.chmod(full, os.stat(full).st_mode | stat.S_IWUSR | stat.S_IRUSR)
        self.frameworks[fw_src] = str(dst)
        formula = cellar_formula(fw_src)
        if formula:
            self.formulae.add(formula)

    def place_all(self, files: dict[str, MachO], roles: dict[str, tuple[str, Path]]) -> None:
        """Copy every file of the closure into the bundle. *roles* maps the roots (PyQt6 modules, plugins) to their
        destination; everything else goes to qt/lib (frameworks) or lib/ (plain dylibs)."""
        for src in sorted(files):
            if src in roles:
                kind, dst = roles[src]
                self.copy_file(src, dst)
                self.placed[src], self.kind[src] = str(dst), kind
                continue
            fw = framework_root(src)
            if fw:
                self.place_framework(fw)
                rel = os.path.relpath(src, fw)
                self.placed[src] = os.path.join(self.frameworks[fw], rel)
                self.identity[src] = f"{os.path.basename(fw)}/{rel}"
                self.kind[src] = "framework"
                continue
            info = files[src]
            leaf = os.path.basename(info.install_id) if info.install_id else os.path.basename(src)
            other = self.lib_names.get(leaf)
            if other and other != src:
                raise SystemExit(f"error: two different libraries named {leaf}: {other} and {src}")
            self.lib_names[leaf] = src
            dst = self.root / "lib" / leaf
            self.copy_file(src, dst)
            self.placed[src], self.identity[src], self.kind[src] = str(dst), leaf, "lib"

    # Rewriting load commands
    def rewrite(self, files: dict[str, MachO]) -> None:
        qt_lib = self.root / "qt" / "lib"
        lib = self.root / "lib"
        for src, dst in sorted(self.placed.items()):
            info = files[src]
            args: list[str] = []
            if info.filetype == MH_DYLIB and info.install_id:
                new_id = "@rpath/" + (self.identity.get(src) or os.path.basename(dst))
                if new_id != info.install_id:
                    args += ["-id", new_id]
            needs_fw = needs_lib = False
            for _kind, name in info.deps:
                if is_system(name):
                    continue
                target = self.edges.get(src, {}).get(name)
                if target is None:  # an unresolvable weak dependency
                    new = "@rpath/" + os.path.basename(name)
                elif "Python.framework" in target:
                    new = PYTHON_ORG_FRAMEWORK  # the running interpreter's own library (python.org's path)
                    self.python_refs.append(f"{os.path.relpath(dst, self.root)}: {name}")
                else:
                    new = "@rpath/" + self.identity[target]
                    if self.kind[target] == "framework":
                        needs_fw = True
                    else:
                        needs_lib = True
                if new != name:
                    args += ["-change", name, new]
            for rpath in dict.fromkeys(info.rpaths):
                args += ["-delete_rpath", rpath]
            if args:
                run(["install_name_tool", *args, dst], quiet=True)
            # duplicated LC_RPATHs need one -delete_rpath each
            for _ in range(5):
                left = parse_macho_uncached(dst).rpaths
                if not left:
                    break
                run(["install_name_tool", *[a for r in dict.fromkeys(left) for a in ("-delete_rpath", r)], dst],
                    quiet=True)
            here = os.path.dirname(os.path.realpath(dst))
            add: list[str] = []
            for needed, folder in ((needs_fw, qt_lib), (needs_lib, lib)):
                if needed:
                    rel = os.path.relpath(folder, here)
                    add.append("@loader_path" if rel == "." else f"@loader_path/{rel}")
            if add:
                run(["install_name_tool", *[a for r in add for a in ("-add_rpath", r)], dst], quiet=True)


def parse_macho_uncached(path: str) -> MachO:
    _CACHE.pop(os.path.realpath(path), None)
    return parse_macho(path)


# ── Steps ────────────────────────────────────────────────────────────────────────────────────────────────────────────
QUERY = r"""
import importlib, json, os, sys, sysconfig
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
for name in sys.argv[1].split(","):
    importlib.import_module("PyQt6." + name)
from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, QLibraryInfo
from PyQt6.QtWebEngineCore import qWebEngineChromiumVersion
print(json.dumps({
    "modules": {n: getattr(m, "__file__", None) for n, m in sys.modules.items() if n.startswith("PyQt6")},
    "plugins": QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath),
    "prefix": QLibraryInfo.path(QLibraryInfo.LibraryPath.PrefixPath),
    "qt": QT_VERSION_STR, "pyqt": PYQT_VERSION_STR, "chromium": qWebEngineChromiumVersion(),
    "python": sys.version.split()[0], "soabi": sysconfig.get_config_var("SOABI"),
}))
"""


def query_homebrew_pyqt(python: str) -> dict:
    result = run([python, "-c", QUERY, ",".join(REQUIRED_MODULES)])
    for line in reversed(result.stdout.splitlines()):  # (Qt may print warnings around it)
        if line.startswith("{"):
            return json.loads(line)
    raise SystemExit(f"error: no answer from {python}:\n{result.stdout}")


def copy_pyqt_package(src_dir: str, dst_dir: Path, keep_so: set[str]) -> None:
    """The PyQt6 package without the extension modules nobody imports (and without the .sip bindings)."""
    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = {n for n in names if n in ("bindings", "__pycache__", "Qt6")}
        if os.path.realpath(directory) == os.path.realpath(src_dir):
            dropped = {n for n in names if n.endswith(".so") and n not in keep_so}
            stems = {n.split(".")[0] for n in dropped}
            skipped |= dropped | {n for n in names if n.endswith(".pyi") and n[:-4] in stems}
        return skipped

    shutil.copytree(src_dir, dst_dir, ignore=ignore, symlinks=False)
    for so in keep_so:
        os.chmod(dst_dir / so, 0o755)


def strip_binaries(paths: list[str]) -> int:
    saved = 0
    for path in paths:
        before = os.path.getsize(path)
        result = run(["strip", "-x", path], check=False, quiet=True)
        if result.returncode != 0:
            log(f"  note: strip -x failed for {path} (kept unstripped): {result.stdout.strip()[:200]}")
            continue
        saved += before - os.path.getsize(path)
    return saved


def capture_entitlements(path: str, folder: Path) -> str | None:
    """The entitlements *path* is signed with now (saved to a file), so re-signing keeps them."""
    for flags in (["--entitlements", "-", "--xml"], ["--entitlements", ":-"]):
        result = subprocess.run(["codesign", "-d", *flags, path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        data = result.stdout.strip()
        if result.returncode == 0 and b"<key>" in data:
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / (hashlib.sha256(path.encode()).hexdigest()[:16] + ".plist")
            target.write_bytes(data)
            return str(target)
    return None


def sign_all(root: Path, machos: list[str], entitlements: dict[str, str], helper_rel: str) -> None:
    helper_app = root / helper_rel
    inside_helper = [p for p in machos if p.startswith(str(helper_app) + "/")]
    others = [p for p in machos if p not in inside_helper]
    for path in inside_helper:
        extra = ["--entitlements", entitlements[path]] if path in entitlements else []
        run(["codesign", "--force", "--sign", "-", "--timestamp=none", *extra, path])
    if helper_app.is_dir():
        main_exe = str(helper_app / "Contents" / "MacOS" / "QtWebEngineProcess")
        extra = ["--entitlements", entitlements[main_exe]] if main_exe in entitlements else []
        run(["codesign", "--force", "--deep", "--sign", "-", "--timestamp=none", *extra, str(helper_app)])
    for path in others:
        result = run(["codesign", "--force", "--sign", "-", "--timestamp=none", path], check=False)
        if result.returncode != 0:
            fw = framework_root(path)
            if not fw:
                raise SystemExit(f"error: codesign failed for {path}:\n{result.stdout}")
            log(f"  codesign of {path} failed ({result.stdout.strip()}); signing {fw}/Versions/A instead")
            run(["codesign", "--force", "--sign", "-", "--timestamp=none", os.path.join(fw, "Versions", "A")])


def all_machos(root: Path) -> list[str]:
    found = []
    for dirpath, _dirs, filenames in os.walk(root):
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            if not os.path.islink(full) and is_macho(full):
                found.append(full)
    return sorted(found)


def verify(root: Path, machos: list[str]) -> list[str]:
    """Fails (returns the problems) if anything still points outside the bundle. Uses otool, as the spec says."""
    problems: list[str] = []
    root_real = os.path.realpath(root)
    for path in machos:
        listing = run(["otool", "-l", path]).stdout + run(["otool", "-L", path]).stdout
        for word in FORBIDDEN:
            if word in listing:
                bad = [line.strip() for line in listing.splitlines() if word in line]
                problems.append(f"{os.path.relpath(path, root)}: otool shows {word!r}: {bad[:3]}")
        info = parse_macho_uncached(path)
        here = os.path.dirname(os.path.realpath(path))
        for rpath in info.rpaths:
            if not rpath.startswith("@loader_path"):
                problems.append(f"{os.path.relpath(path, root)}: LC_RPATH {rpath} is not @loader_path-relative")
        if info.install_id and not (info.install_id.startswith("@rpath/") or info.install_id.startswith("@loader_path")):
            problems.append(f"{os.path.relpath(path, root)}: install id {info.install_id}")
        for _kind, name in info.deps:
            if is_system(name) or name == PYTHON_ORG_FRAMEWORK:
                continue
            if name.startswith("@rpath/"):
                rest = name[len("@rpath/"):]
                hits = [os.path.realpath(os.path.join(here + r[len("@loader_path"):], rest)) for r in info.rpaths
                        if r.startswith("@loader_path")]
                hits = [h for h in hits if os.path.isfile(h)]
                if not hits and _kind != "weak":
                    problems.append(f"{os.path.relpath(path, root)}: {name} not found via its rpaths {info.rpaths}")
                for hit in hits:
                    if not hit.startswith(root_real + "/"):
                        problems.append(f"{os.path.relpath(path, root)}: {name} resolves outside the bundle: {hit}")
            elif name.startswith("@loader_path/"):
                hit = os.path.realpath(here + name[len("@loader_path"):])
                if not (os.path.isfile(hit) and hit.startswith(root_real + "/")):
                    problems.append(f"{os.path.relpath(path, root)}: {name} -> {hit}")
            else:
                problems.append(f"{os.path.relpath(path, root)}: absolute non-system dependency {name}")
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                target = os.readlink(full)
                resolved = os.path.realpath(full)
                if os.path.isabs(target) or not (resolved == root_real or resolved.startswith(root_real + "/")):
                    problems.append(f"symlink {os.path.relpath(full, root)} -> {target} leaves the bundle")
                elif not os.path.exists(full):
                    problems.append(f"dangling symlink {os.path.relpath(full, root)} -> {target}")
    return problems


def scan_text_references(root: Path, machos: set[str]) -> tuple[list[str], list[str]]:
    """Files whose *contents* mention /opt/homebrew: (text files, binaries). Load commands are already clean; these
    are compiled-in fallback strings (e.g. Qt's configure-time prefix), reported for information."""
    text_hits, binary_hits = [], []
    needle = b"/opt/homebrew"
    for dirpath, _dirs, filenames in os.walk(root):
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            if os.path.islink(full):
                continue
            with open(full, "rb") as fh:
                data = fh.read()
            if needle in data:
                (binary_hits if full in machos or b"\0" in data[:8192] else text_hits).append(
                    os.path.relpath(full, root))
    return text_hits, binary_hits


def copy_licenses(root: Path, formulae: set[str], cellar: str) -> list[str]:
    copied = []
    for formula in sorted(formulae):
        versions = sorted(Path(cellar, formula).glob("*"), key=lambda p: version_key(p.name))
        if not versions:
            continue
        keg = versions[-1]
        for item in sorted(keg.iterdir()):
            if re.match(r"(LICEN[CS]E|COPYING|NOTICE|LGPL|GPL|AUTHORS)", item.name, re.I) and item.is_file():
                dst = root / "licenses" / formula / item.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, dst)
                copied.append(f"{formula}/{item.name}")
    return copied


def dir_size(root: Path) -> tuple[int, int]:
    total = count = 0
    for dirpath, _dirs, filenames in os.walk(root):
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            if not os.path.islink(full):
                total += os.path.getsize(full)
                count += 1
    return total, count


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_tarball(root: Path, target: Path) -> None:
    def clean(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    with tarfile.open(target, "w:gz", compresslevel=6, format=tarfile.PAX_FORMAT) as tar:
        tar.add(root, arcname=BUNDLE_NAME, filter=clean)


def patch_installer(source: Path, target: Path, tag: str, min_macos: str, repo: str) -> None:
    text = source.read_text(encoding="utf-8")
    replacements = {
        r'^DEFAULT_ENGINE_TAG=.*$': f'DEFAULT_ENGINE_TAG="{tag}"  # set by the release workflow',
        r'^DEFAULT_MIN_MACOS=.*$': f'DEFAULT_MIN_MACOS="{min_macos}"  # set by the release workflow',
        r'^DEFAULT_REPO=.*$': f'DEFAULT_REPO="{repo}"  # set by the release workflow',
    }
    for pattern, line in replacements.items():
        text, count = re.subn(pattern, line, text, flags=re.M)
        if count != 1:
            raise SystemExit(f"error: {source} must contain exactly one line matching {pattern}")
    target.write_text(text, encoding="utf-8")
    os.chmod(target, 0o755)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work", default="build", help="folder for the unpacked bundle")
    parser.add_argument("--out", default="dist", help="folder for the release files")
    parser.add_argument("--python", default=None, help="Homebrew's python3.14 (default: brew --prefix python@3.14)")
    parser.add_argument("--selftest-media", default=None, help="small H.264+AAC .mp4 for the installer's self-test")
    parser.add_argument("--build-number", default=os.environ.get("GITHUB_RUN_NUMBER", "0"))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "dli-29/chrome2"))
    parser.add_argument("--commit", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--no-strip", action="store_true", help="keep local symbols")
    args = parser.parse_args()

    if sys.platform != "darwin" or os.uname().machine != "arm64":
        raise SystemExit("error: build on an Apple Silicon Mac (the GitHub macos-15 runner)")
    brew_prefix = brew("--prefix")
    python = args.python or os.path.join(brew("--prefix", "python@3.14"), "bin", "python3.14")
    work, out = Path(args.work).resolve(), Path(args.out).resolve()
    root = work / BUNDLE_NAME
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    out.mkdir(parents=True, exist_ok=True)

    # 1. What Homebrew's PyQt6 loads
    log("==> Querying Homebrew's PyQt6")
    query = query_homebrew_pyqt(python)
    log(f"    Qt {query['qt']}, PyQt6 {query['pyqt']}, Chromium {query['chromium']}, Python {query['python']} "
        f"({query['soabi']}), plugins in {query['plugins']}")
    modules = {name.split(".", 1)[1]: os.path.realpath(path) for name, path in query["modules"].items()
               if path and path.endswith(".so") and "." in name}
    pyqt_dir = os.path.dirname(modules["QtCore"])
    log(f"    PyQt6 package: {pyqt_dir}")
    log(f"    modules loaded by the required imports: {', '.join(sorted(modules))}")
    for name in REQUIRED_MODULES:
        if name not in modules:
            raise SystemExit(f"error: PyQt6.{name} wasn't loaded (does Homebrew's pyqt still ship it?)")

    bundle = Bundle(root, brew_prefix)
    required = bundle.closure(list(modules.values()))
    allowed = bundle.qt_frameworks(required)
    log(f"    Qt frameworks needed: {', '.join(sorted(allowed))}")
    if "QtWebEngineCore.framework" not in allowed:
        raise SystemExit("error: QtWebEngineCore.framework isn't among the dependencies")

    # 2. Optional modules and plugins that don't need more Qt
    for name in OPTIONAL_MODULES:
        if name in modules:
            continue
        found = sorted(Path(pyqt_dir).glob(f"{name}.*so"))
        if not found:
            continue
        try:
            extra = bundle.qt_frameworks(bundle.closure([str(found[0])])) - allowed
        except SystemExit as exc:
            log(f"    skipping PyQt6.{name}: {str(exc).splitlines()[0]}")
            continue
        if extra:
            log(f"    skipping PyQt6.{name}: would add {', '.join(sorted(extra))}")
        else:
            modules[name] = os.path.realpath(found[0])
    plugin_src = Path(query["plugins"])
    plugins: dict[str, str] = {}
    for kind in PLUGIN_KINDS:
        folder = plugin_src / kind
        if not folder.is_dir():
            continue
        for item in sorted(folder.iterdir()):
            if not item.name.endswith(".dylib") or not is_macho(str(item)):
                continue
            try:
                extra = bundle.qt_frameworks(bundle.closure([str(item)])) - allowed
            except SystemExit as exc:
                log(f"    skipping plugin {kind}/{item.name}: {str(exc).splitlines()[0]}")
                continue
            if extra:
                log(f"    skipping plugin {kind}/{item.name}: would add {', '.join(sorted(extra))}")
                continue
            plugins[f"{kind}/{item.name}"] = os.path.realpath(item)
    for must in ("platforms/libqcocoa.dylib", "platforms/libqoffscreen.dylib"):
        if must not in plugins:
            raise SystemExit(f"error: plugin {must} missing")
    log(f"    plugins: {', '.join(sorted(plugins))}")

    # 3. Copy
    log("==> Copying into the bundle")
    keep_so = {os.path.basename(path) for path in modules.values()}
    copy_pyqt_package(pyqt_dir, root / "site" / "PyQt6", keep_so)
    roles: dict[str, tuple[str, Path]] = {}
    for path in modules.values():
        roles[path] = ("module", root / "site" / "PyQt6" / os.path.basename(path))
    for rel, path in plugins.items():
        roles[path] = ("plugin", root / "qt" / "plugins" / rel)
    files = bundle.closure(list(modules.values()) + list(plugins.values()))
    bundle.place_all(files, roles)
    for helper in ("chrome2_engine_env.py", "chrome2_engine_selftest.py"):
        shutil.copyfile(HERE / helper, root / "site" / helper)
    if args.selftest_media:
        (root / "share").mkdir(exist_ok=True)
        shutil.copyfile(args.selftest_media, root / "share" / "selftest-h264-aac.mp4")
    helpers = [os.path.relpath(os.path.join(d, "QtWebEngineProcess.app"), root)
               for d, dirs, _files in os.walk(root / "qt" / "lib" / "QtWebEngineCore.framework")
               if "QtWebEngineProcess.app" in dirs and not os.path.islink(os.path.join(d, "QtWebEngineProcess.app"))]
    if len(helpers) != 1 or helpers[0] not in HELPER_CANDIDATES or \
            not (root / helpers[0] / "Contents" / "MacOS" / "QtWebEngineProcess").is_file():
        raise SystemExit(f"error: expected one helper app at {' or '.join(HELPER_CANDIDATES)} (where "
                         f"chrome2_engine_env.py looks), found {helpers}")
    helper_rel = helpers[0]
    log(f"    helper: {helper_rel}")
    resources = root / "qt/lib/QtWebEngineCore.framework/Versions/A/Resources"
    log(f"    QtWebEngineCore resources: {', '.join(sorted(p.name for p in resources.iterdir()))}")

    # 4. Relocate, strip, sign
    entitlements: dict[str, str] = {}
    for path in all_machos(root / helper_rel):
        saved_to = capture_entitlements(path, work / "entitlements")
        if saved_to:
            entitlements[path] = saved_to
            log(f"    keeping the entitlements of {os.path.relpath(path, root)}: "
                f"{', '.join(re.findall(r'<key>([^<]+)</key>', Path(saved_to).read_text()))}")
    log("==> Rewriting load commands")
    bundle.rewrite(files)
    machos = all_machos(root)
    unknown = sorted(set(os.path.realpath(p) for p in machos) - set(os.path.realpath(p) for p in bundle.placed.values()))
    if unknown:
        raise SystemExit("error: Mach-O files in the bundle that weren't relocated:\n  " + "\n  ".join(unknown))
    size_before, _ = dir_size(root)
    saved = 0 if args.no_strip else strip_binaries(machos)
    log(f"    strip -x saved {saved / 1e6:.1f} MB")
    log("==> Signing (ad hoc)")
    sign_all(root, machos, entitlements, helper_rel)
    bad_signatures = []
    for path in machos:
        result = run(["codesign", "--verify", "--strict", path], check=False, quiet=True)
        if result.returncode != 0:
            bad_signatures.append(f"{os.path.relpath(path, root)}: {result.stdout.strip()[:200]}")
    result = run(["codesign", "--verify", "--deep", "--strict", str(root / helper_rel)], check=False, quiet=True)
    if result.returncode != 0:
        raise SystemExit(f"error: the helper app's signature doesn't verify:\n{result.stdout}")
    for line in bad_signatures:
        log(f"    note: codesign --verify: {line}")

    # 5. Verify: nothing may point outside the bundle
    log("==> Verifying (otool)")
    problems = verify(root, machos)
    if problems:
        for problem in problems:
            log(f"    PROBLEM: {problem}")
        raise SystemExit(f"error: {len(problems)} load-command problems (see above)")
    text_hits, binary_hits = scan_text_references(root, set(machos))
    bad_text = [hit for hit in text_hits if hit.endswith((".py", ".pth", ".conf", ".plist", ".json"))]
    if bad_text:
        raise SystemExit(f"error: files mention /opt/homebrew: {bad_text}")
    log(f"    OK: {len(machos)} Mach-O files; no /opt/homebrew, /usr/local or Cellar in any load command")
    if binary_hits:
        log(f"    (compiled-in strings mentioning /opt/homebrew, unused when QT_PLUGIN_PATH etc. are set: "
            f"{', '.join(binary_hits[:12])}{' ...' if len(binary_hits) > 12 else ''})")

    # 6. Manifest
    minos_by_file = {os.path.relpath(p, root): parse_macho_uncached(p).minos or "?" for p in machos}
    known = [v for v in minos_by_file.values() if v != "?"]
    min_macos = max(known, key=version_key)
    counts: dict[str, int] = {}
    for value in minos_by_file.values():
        counts[value] = counts.get(value, 0) + 1
    licenses = copy_licenses(root, bundle.formulae, brew("--cellar"))
    size, count = dir_size(root)
    versions = dict(line.split(" ", 1) for line in brew("list", "--versions").splitlines() if " " in line)
    qtwebengine = versions.get("qtwebengine", query["qt"]).split()[-1]
    qt_plain = re.sub(r"_\d+$", "", qtwebengine)
    tag = f"engine-qt{qt_plain}-arm64"
    version = f"{qt_plain}-b{args.build_number}"
    sw_vers = run(["sw_vers", "-productVersion"]).stdout.strip()
    manifest = {
        "name": BUNDLE_NAME,
        "version": version,
        "release_tag": tag,
        "arch": "arm64",
        "min_macos": min_macos,
        "python": "3.14",
        "qt_version": query["qt"],
        "pyqt_version": query["pyqt"],
        "chromium_version": query["chromium"],
        "proprietary_codecs": True,
        "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "build": {"runner_macos": sw_vers, "repo": args.repo, "commit": args.commit,
                  "build_number": args.build_number, "homebrew": brew("--version").splitlines()[0],
                  "homebrew_python": query["python"], "homebrew_python_soabi": query["soabi"]},
        "homebrew_versions": {k: versions[k] for k in sorted(versions)},
        "bundled_formulae": sorted(bundle.formulae),
        "layout": {"site": "site", "qt_lib": "qt/lib", "plugins": "qt/plugins", "lib": "lib",
                   "webengine_helper": helper_rel, "env_module": "site/chrome2_engine_env.py",
                   "selftest": "site/chrome2_engine_selftest.py"},
        "pyqt_modules": sorted(modules),
        "qt_frameworks": sorted(os.path.basename(p) for p in bundle.frameworks),
        "plugins": sorted(plugins),
        "libs": sorted(bundle.lib_names),
        "minos_counts": dict(sorted(counts.items(), key=lambda kv: version_key(kv[0]))),
        "minos_max_files": sorted(p for p, v in minos_by_file.items() if v == min_macos),
        "python_framework_refs": bundle.python_refs,
        "compiled_in_homebrew_strings": binary_hits,
        "codesign_verify_notes": bad_signatures,
        "notes": bundle.notes,
        "licenses": licenses,
        "size_bytes": size,
        "size_before_strip_bytes": size_before,
        "file_count": count,
        "macho_count": len(machos),
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (root / "README.txt").write_text(
        f"Chrome 2 engine {version}: PyQt6 {query['pyqt']} + Qt WebEngine {query['qt']} (Chromium "
        f"{query['chromium']}) with H.264/AAC, built from Homebrew bottles on macOS {sw_vers} (arm64).\n"
        f"Needs macOS {min_macos} or newer on Apple Silicon and python.org Python 3.14.\n"
        "Install it with install-engine.sh (no admin rights needed); see manifest.json for what's inside.\n",
        encoding="utf-8")

    # 7. Package
    log("==> Packaging")
    tarball = out / TARBALL
    make_tarball(root, tarball)
    digest = sha256_of(tarball)
    (out / f"{TARBALL}.sha256").write_text(f"{digest}  {TARBALL}\n", encoding="utf-8")
    release_manifest = dict(manifest, tarball=TARBALL, tarball_sha256=digest, tarball_bytes=tarball.stat().st_size)
    (out / RELEASE_MANIFEST).write_text(json.dumps(release_manifest, indent=2) + "\n", encoding="utf-8")
    patch_installer(REPO / "tools" / "install-engine.sh", out / "install-engine.sh", tag, min_macos, args.repo)

    log(f"    version {version}, tag {tag}")
    log(f"    min macOS (largest LC_BUILD_VERSION minos): {min_macos}   counts: {manifest['minos_counts']}")
    log(f"    bundle: {size / 1e6:.1f} MB in {count} files ({len(machos)} Mach-O; {size_before / 1e6:.1f} MB before strip)")
    log(f"    tarball: {tarball.stat().st_size / 1e6:.1f} MB  sha256 {digest}")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"tag={tag}\nversion={version}\nmin_macos={min_macos}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
