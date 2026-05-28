from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from bundled_spec_tools.extractors.android_project import ANDROID_NS, manifests


def manifest_activities(project_root: str | Path) -> dict[str, list[str]]:
    activities: dict[str, list[str]] = {}
    for mf in manifests(project_root):
        try:
            tree = ET.parse(mf)
        except ET.ParseError:
            continue
        package = tree.getroot().get("package", "") or ""
        for activity_elem in tree.iter("activity"):
            name_attr = activity_elem.get(f"{ANDROID_NS}name", "")
            if not name_attr:
                continue
            short = _short_name(name_attr, package)
            activities[short] = activities.get(short, []) + [mf.as_posix()]
    return activities


def _short_name(name_attr: str, package: str) -> str:
    if name_attr.startswith("."):
        name_attr = package + name_attr
    return name_attr.rsplit(".", 1)[-1]


def manifest_verifier(
    project_root: str | Path,
    ast_hierarchy: dict,
    resolve_android_base,
) -> dict:
    man_acts = manifest_activities(project_root)
    result = {
        "manifest_activity_count": len(man_acts),
        "ast_activity_count": 0,
        "ast_activity_matched": [],
        "ast_activity_missed": [],
        "manifest_only": [],
    }
    for short_name in sorted(man_acts):
        if short_name in ast_hierarchy:
            kind = resolve_android_base(short_name, ast_hierarchy)
            if kind == "activity":
                result["ast_activity_count"] += 1
                result["ast_activity_matched"].append(short_name)
            else:
                result["ast_activity_missed"].append({
                    "class": short_name,
                    "ast_kind": kind,
                    "manifest_files": man_acts[short_name],
                })
        else:
            result["manifest_only"].append({
                "class": short_name,
                "manifest_files": man_acts[short_name],
            })
    return result
