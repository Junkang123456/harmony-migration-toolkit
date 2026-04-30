from __future__ import annotations

import json
import shutil
from pathlib import Path

from stages._util import load_json, toolkit_root


def export_feature_tree_view(
    feature_tree_path: Path,
    out_viewer_dir: Path,
    *,
    framework_map_path: Path | None = None,
    harmony_arch_path: Path | None = None,
) -> None:
    out_viewer_dir.mkdir(parents=True, exist_ok=True)
    vendor_src = toolkit_root() / "viewer" / "vendor"
    vendor_dst = out_viewer_dir / "vendor"
    if vendor_src.is_dir():
        shutil.copytree(vendor_src, vendor_dst, dirs_exist_ok=True)

    shutil.copy2(feature_tree_path, out_viewer_dir / "feature_tree.v1.json")
    for sidecar_name in ("feature_spec_evidence.json", "verify_report.json", "taxonomy_report.json"):
        sidecar = feature_tree_path.parent / sidecar_name
        if sidecar.is_file():
            shutil.copy2(sidecar, out_viewer_dir / sidecar_name)

    if framework_map_path and framework_map_path.is_file():
        fm = load_json(framework_map_path)
        side = {"rules_version": fm.get("rules_version"), "gap_items": fm.get("gap_items") or []}
        (out_viewer_dir / "framework_map_sidecar.json").write_text(
            json.dumps(side, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    if harmony_arch_path and harmony_arch_path.is_file():
        ha = load_json(harmony_arch_path)
        side = {
            "bundle_name": ha.get("bundle_name"),
            "abilities": ha.get("abilities") or [],
            "routes": (ha.get("routes") or [])[:128],
        }
        (out_viewer_dir / "harmony_arch_sidecar.json").write_text(
            json.dumps(side, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    html_src = toolkit_root() / "viewer" / "feature_tree.html"
    shutil.copy2(html_src, out_viewer_dir / "feature_tree.html")

    readme = out_viewer_dir / "README_viewer.txt"
    readme.write_text(
        "Feature tree viewer (read-only)\n"
        "================================\n"
        "Open a local HTTP server from this directory so the browser can load JSON:\n"
        "  python -m http.server 8765\n"
        "Then visit http://127.0.0.1:8765/feature_tree.html\n"
        "file:// URLs often block fetch() for JSON; use http.server as above.\n",
        encoding="utf-8",
        newline="\n",
    )


def export_feature_tree_view_cli() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Export static feature tree viewer")
    p.add_argument("--tree", type=Path, required=True, help="feature_tree.v1.json")
    p.add_argument("--out", type=Path, required=True, help="Output viewer directory")
    p.add_argument("--framework-map", type=Path, default=None)
    p.add_argument("--harmony-arch", type=Path, default=None)
    args = p.parse_args()
    export_feature_tree_view(
        args.tree.resolve(),
        args.out.resolve(),
        framework_map_path=args.framework_map.resolve() if args.framework_map else None,
        harmony_arch_path=args.harmony_arch.resolve() if args.harmony_arch else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(export_feature_tree_view_cli())
