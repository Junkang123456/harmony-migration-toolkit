from __future__ import annotations

from pathlib import Path

from bundled_spec_tools.extractors.bytecode_navigation import find_class_dir
from bundled_spec_tools.extractors.class_parser import parse_class

_ANDROID_FRAGMENT_BASES = {
    "androidx.fragment.app.Fragment", "android.app.Fragment",
    "androidx.fragment.app.DialogFragment", "android.app.DialogFragment",
    "androidx.fragment.app.BottomSheetDialogFragment",
    "androidx.preference.PreferenceFragmentCompat",
    "android.app.ListFragment",
    "com.google.android.gms.maps.MapFragment",
    "com.google.android.gms.maps.SupportMapFragment",
    "android.preference.PreferenceFragment",
    "androidx.appcompat.app.AppCompatDialogFragment",
}

_ANDROID_ACTIVITY_BASES = {
    "android.app.Activity",
    "androidx.appcompat.app.AppCompatActivity",
    "androidx.fragment.app.FragmentActivity",
    "androidx.activity.ComponentActivity",
    "android.app.ListActivity",
    "android.preference.PreferenceActivity",
}


def bytecode_hierarchy(project_root: str | Path) -> dict[str, str | None]:
    hierarchy: dict[str, str | None] = {}
    class_dir = find_class_dir(str(project_root))
    if not class_dir:
        return hierarchy
    root = Path(class_dir)
    for cf in root.rglob("*.class"):
        try:
            cls = parse_class(cf)
        except Exception:
            continue
        full_name: str = cls.get("class", "")
        short = full_name.rsplit(".", 1)[-1] if "." in full_name else full_name
        super_full: str = cls.get("super", "") or ""
        if super_full and "." in super_full:
            hierarchy[short] = super_full.rsplit(".", 1)[-1]
        else:
            hierarchy[short] = super_full or None
    return hierarchy


def _resolve_bytecode_base(
    name: str,
    bc_hierarchy: dict[str, str | None],
    visited: set[str] | None = None,
) -> str:
    if visited is None:
        visited = set()
    if name in visited:
        return "other"
    visited.add(name)

    super_name = bc_hierarchy.get(name)
    if super_name is None:
        if name.endswith("Fragment"):
            return "fragment"
        if name.endswith("Activity"):
            return "activity"
        return "other"

    if super_name in _ANDROID_FRAGMENT_BASES:
        return "fragment"
    if super_name in _ANDROID_ACTIVITY_BASES:
        return "activity"
    return _resolve_bytecode_base(super_name, bc_hierarchy, visited)


def bytecode_verifier(
    project_root: str | Path,
    ast_hierarchy: dict,
    resolve_android_base,
) -> dict:
    bc_hierarchy = bytecode_hierarchy(project_root)
    result = {
        "bytecode_class_count": len(bc_hierarchy),
        "bytecode_fragment_count": 0,
        "bytecode_activity_count": 0,
        "bytecode_fragments": [],
        "bytecode_activities": [],
        "ast_vs_bytecode_fragment_diff": [],
        "ast_vs_bytecode_activity_diff": [],
    }

    bc_fragments: set[str] = set()
    bc_activities: set[str] = set()
    for name in bc_hierarchy:
        kind = _resolve_bytecode_base(name, bc_hierarchy)
        if kind == "fragment":
            bc_fragments.add(name)
            result["bytecode_fragments"].append(name)
        elif kind == "activity":
            bc_activities.add(name)
            result["bytecode_activities"].append(name)

    result["bytecode_fragment_count"] = len(bc_fragments)
    result["bytecode_activity_count"] = len(bc_activities)

    ast_fragments = {
        name for name, info in ast_hierarchy.items()
        if resolve_android_base(name, ast_hierarchy) == "fragment"
    }
    ast_activities = {
        name for name, info in ast_hierarchy.items()
        if resolve_android_base(name, ast_hierarchy) == "activity"
    }

    result["ast_vs_bytecode_fragment_diff"] = {
        "ast_only": sorted(ast_fragments - bc_fragments),
        "bytecode_only": sorted(bc_fragments - ast_fragments),
        "matched": sorted(ast_fragments & bc_fragments),
    }
    result["ast_vs_bytecode_activity_diff"] = {
        "ast_only": sorted(ast_activities - bc_activities),
        "bytecode_only": sorted(bc_activities - ast_activities),
        "matched": sorted(ast_activities & bc_activities),
    }

    return result
