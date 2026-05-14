# -*- mode: python ; coding: utf-8 -*-
# Build:  python -m PyInstaller packaging/pipeline.spec --noconfirm
# Output: dist/harmony-migration-toolkit/

import sys
from pathlib import Path

try:
    from PyInstaller.utils.hooks import collect_submodules
except ImportError as e:
    raise SystemExit(
        "PyInstaller is required. Install with:\n"
        "  pip install pyinstaller"
    ) from e

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent

pathex = [str(ROOT), str(ROOT / "bundled_spec_tools")]

datas = [
    (str(ROOT / "schemas"), "schemas"),
    (str(ROOT / "data"), "data"),
    (str(ROOT / "bundled_spec_tools"), "bundled_spec_tools"),
]
_viewer = ROOT / "viewer"
if _viewer.is_dir():
    datas.append((str(_viewer), "viewer"))

_hidden = list(collect_submodules("stages"))
_hidden += [
    "yaml",
    "jsonschema",
    "jsonschema.validators",
    "generate_specs",
]
for _py in (ROOT / "bundled_spec_tools" / "extractors").glob("*.py"):
    if _py.name == "__init__.py":
        _hidden.append("extractors")
    elif _py.suffix == ".py":
        _hidden.append("extractors." + _py.stem)


def _extra_windows_runtime_dlls():
    if sys.platform != "win32":
        return []
    names = (
        "libexpat.dll",
        "libcrypto-3-x64.dll",
        "libssl-3-x64.dll",
        "liblzma.dll",
        "libmpdec-4.dll",
        "LIBBZ2.dll",
        "ffi.dll",
        "zlib.dll",
    )
    search_dirs = [
        Path(sys.base_prefix) / "DLLs",
        Path(sys.base_prefix) / "Library" / "bin",
    ]
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for d in search_dirs:
        if not d.is_dir():
            continue
        for n in names:
            p = (d / n).resolve()
            key = str(p).lower()
            if p.is_file() and key not in seen:
                seen.add(key)
                out.append((str(p), "."))
    return out


a = Analysis(
    [str(ROOT / "pipeline.py")],
    pathex=pathex,
    binaries=_extra_windows_runtime_dlls(),
    datas=datas,
    hiddenimports=_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="harmony-migration-toolkit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="harmony-migration-toolkit",
)
