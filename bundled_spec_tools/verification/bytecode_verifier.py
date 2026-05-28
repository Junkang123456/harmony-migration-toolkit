from __future__ import annotations

from pathlib import Path

from extractors.class_parser import parse_class

_ANDROID_FRAGMENT_BASES_SHORT = {
    "Fragment", "DialogFragment", "BottomSheetDialogFragment",
    "PreferenceFragmentCompat", "ListFragment",
    "MapFragment", "SupportMapFragment",
    "AppCompatDialogFragment",
}

_ANDROID_ACTIVITY_BASES_SHORT = {
    "Activity", "AppCompatActivity", "FragmentActivity",
    "ComponentActivity", "ListActivity", "PreferenceActivity",
}


def _class_dirs(project_root: str | Path) -> list[Path]:
    root = Path(project_root)
    dirs: list[Path] = []
    build_roots = [root] + [e for e in root.iterdir() if e.is_dir() and not e.name.startswith(".")]
    for br in build_roots:
        for candidate in ("intermediates/javac", "tmp/kotlin-classes"):
            parent = br / "build" / candidate
            if parent.is_dir():
                for variant in parent.iterdir():
                    if variant.is_dir():
                        dirs.append(variant)
    return dirs


def bytecode_hierarchy(project_root: str | Path) -> dict[str, str | None]:
    hierarchy: dict[str, str | None] = {}
    seen: set[str] = set()
    for class_dir in _class_dirs(project_root):
        for cf in class_dir.rglob("*.class"):
            try:
                cls = parse_class(cf)
            except Exception:
                continue
            full_name: str = cls.get("class", "")
            name = full_name.rsplit(".", 1)[-1] if "." in full_name else full_name
            if name in seen:
                continue
            seen.add(name)
            super_full: str = cls.get("super", "") or ""
            if super_full and "." in super_full:
                hierarchy[name] = super_full.rsplit(".", 1)[-1]
            else:
                hierarchy[name] = super_full or None
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

    super_short = bc_hierarchy.get(name)
    # class not found in bytecode at all — unknown, not a guess
    if super_short is None:
        return "unknown"

    if super_short in _ANDROID_FRAGMENT_BASES_SHORT:
        return "fragment"
    if super_short in _ANDROID_ACTIVITY_BASES_SHORT:
        return "activity"
    return _resolve_bytecode_base(super_short, bc_hierarchy, visited)


def bytecode_verifier(
    project_root: str | Path,
    ast_hierarchy: dict,
    resolve_android_base,
) -> dict:
    bc_hierarchy = bytecode_hierarchy(project_root)

    result: dict = {
        "bytecode_available": bool(bc_hierarchy),
        "bytecode_class_count": len(bc_hierarchy),
    }

    if not bc_hierarchy:
        result["bytecode_fragment_count"] = 0
        result["bytecode_activity_count"] = 0
        result["bytecode_fragments"] = []
        result["bytecode_activities"] = []
        result["ast_vs_bytecode_fragment_diff"] = {
            "note": "no bytecode data — build project first or check find_class_dir()",
            "ast_only": [], "bytecode_only": [], "matched": [],
        }
        result["ast_vs_bytecode_activity_diff"] = {
            "note": "no bytecode data — build project first or check find_class_dir()",
            "ast_only": [], "bytecode_only": [], "matched": [],
        }
        return result

    bc_fragments: set[str] = set()
    bc_activities: set[str] = set()
    for name in bc_hierarchy:
        kind = _resolve_bytecode_base(name, bc_hierarchy)
        if kind == "fragment":
            bc_fragments.add(name)
        elif kind == "activity":
            bc_activities.add(name)

    result["bytecode_fragment_count"] = len(bc_fragments)
    result["bytecode_activity_count"] = len(bc_activities)
    result["bytecode_fragments"] = sorted(bc_fragments)
    result["bytecode_activities"] = sorted(bc_activities)

    ast_fragments = {
        info.name for fqn, info in ast_hierarchy.items()
        if resolve_android_base(fqn, ast_hierarchy) == "fragment"
    }
    ast_activities = {
        info.name for fqn, info in ast_hierarchy.items()
        if resolve_android_base(fqn, ast_hierarchy) == "activity"
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
