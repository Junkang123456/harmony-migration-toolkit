from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path


ANDROID_NS = "{http://schemas.android.com/apk/res/android}"

# Cache so detect_default_flavor is only computed once per project root.
_FLAVOR_CACHE: dict[str, str | None] = {}


def _is_ignored(path: Path) -> bool:
    ignored = {".git", ".gradle", ".idea", "build", ".dep_cache"}
    return any(part in ignored for part in path.parts)


def _source_class_key(path: Path) -> str:
    """Return package+class path (after the 'java' or 'kotlin' segment) for dedup."""
    parts = path.parts
    for i, part in enumerate(parts):
        if part in ("java", "kotlin"):
            return "/".join(parts[i + 1:])
    return path.name


# ── Flavor auto-detection ─────────────────────────────────────────────────────

def detect_default_flavor(project_root: str | Path) -> str | None:
    """
    Auto-detect the active product flavor by parsing build.gradle / build.gradle.kts.

    Search order: every non-ignored build file under *project_root*.
    Returns the flavor marked ``isDefault true`` (Groovy/Kotlin DSL), or the first
    flavor defined inside a ``productFlavors`` block, or ``None`` when no
    ``productFlavors`` block is found at all.

    Result is cached per resolved project root path.
    """
    key = str(Path(project_root).resolve())
    if key in _FLAVOR_CACHE:
        return _FLAVOR_CACHE[key]

    root = Path(project_root)
    gradle_files = sorted(
        f for f in root.rglob("build.gradle*") if not _is_ignored(f) and f.is_file()
    )
    result: str | None = None
    for gradle_file in gradle_files:
        try:
            text = gradle_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "productFlavors" not in text:
            continue
        # isDefault true / isDefault = true (Groovy or Kotlin DSL)
        m = re.search(r'(\w+)\s*\{[^{}]*?\bisDefault\b\s*[=\s]*true', text, re.S)
        if m:
            result = m.group(1)
            break
        # First flavor name: productFlavors { flavorName { ... } }
        # Also handles Kotlin DSL: productFlavors { create("flavorName") { ... } }
        m = re.search(
            r'productFlavors\s*[\{\(][^}]*?(?:create\s*\(\s*"(\w+)"|\b(\w+)\s*\{)',
            text, re.S,
        )
        if m:
            result = m.group(1) or m.group(2)
            break

    _FLAVOR_CACHE[key] = result
    return result


def source_dirs_for_variant(module_src: Path, flavor: str | None) -> list[Path]:
    """Return existing source directories in priority order: [src/main, src/<flavor>]."""
    candidates = ["main"]
    if flavor:
        candidates.append(flavor)
    return [module_src / d for d in candidates if (module_src / d).exists()]


# ── Core scanning functions ───────────────────────────────────────────────────

def source_files(project_root: str | Path) -> list[Path]:
    """
    Return Kotlin/Java files under src/main and the auto-detected flavor source set.

    For projects without product flavors, behaviour is identical to before
    (only ``src/main`` is scanned).  When a default flavor is detected (e.g.
    ``jetpack`` in WordPress-Android), ``src/<flavor>`` files are added and
    flavor files take precedence over same-package files from ``src/main``.
    """
    root = Path(project_root)
    flavor = detect_default_flavor(root)

    patterns = ["src/main/**/*.java", "src/main/**/*.kt"]
    if flavor:
        patterns += [f"src/{flavor}/**/*.java", f"src/{flavor}/**/*.kt"]

    files: list[Path] = []
    for pattern in patterns:
        files.extend(p for p in root.rglob(pattern) if not _is_ignored(p))

    if not flavor:
        return sorted(files)

    # Dedup: flavor file wins over main file for the same package+class.
    # Patterns are ordered main-first, so iterating in order means the flavor
    # file (appearing later) naturally overwrites the main entry.
    seen: dict[str, Path] = {}
    for f in files:
        seen[_source_class_key(f)] = f
    return sorted(seen.values())


def source_dirs(project_root: str | Path) -> list[Path]:
    root = Path(project_root)
    dirs: set[Path] = set()
    for src in source_files(root):
        for parent in src.parents:
            p = parent.as_posix()
            if p.endswith("/src/main/java") or p.endswith("/src/main/kotlin"):
                dirs.add(parent)
                break
            # Also recognise flavor source dirs (e.g. src/jetpack/java)
            flavor = detect_default_flavor(root)
            if flavor and (
                p.endswith(f"/src/{flavor}/java") or p.endswith(f"/src/{flavor}/kotlin")
            ):
                dirs.add(parent)
                break
    return sorted(dirs)


def res_dirs(project_root: str | Path) -> list[Path]:
    root = Path(project_root)
    flavor = detect_default_flavor(root)
    result = sorted(
        p for p in root.rglob("src/main/res") if p.is_dir() and not _is_ignored(p)
    )
    if flavor:
        flavor_res = sorted(
            p for p in root.rglob(f"src/{flavor}/res")
            if p.is_dir() and not _is_ignored(p)
        )
        result = result + flavor_res
    return result


def manifests(project_root: str | Path) -> list[Path]:
    root = Path(project_root)
    flavor = detect_default_flavor(root)
    result = sorted(
        p for p in root.rglob("src/main/AndroidManifest.xml")
        if p.is_file() and not _is_ignored(p)
    )
    if flavor:
        flavor_manifests = sorted(
            p for p in root.rglob(f"src/{flavor}/AndroidManifest.xml")
            if p.is_file() and not _is_ignored(p)
        )
        result = result + flavor_manifests
    return result


def relative_to_root(path: Path, root: str | Path, file_prefix: str = "") -> str:
    rel = path.relative_to(Path(root)).as_posix()
    return f"{file_prefix}/{rel}" if file_prefix else rel


def module_root_from_manifest(manifest_path: Path) -> Path:
    # module/src/main/AndroidManifest.xml -> module
    return manifest_path.parents[2]


def _manifest_has_launcher(activity_elem: ET.Element) -> bool:
    for intent_filter in activity_elem.iter("intent-filter"):
        actions = {
            action.get(f"{ANDROID_NS}name", "")
            for action in intent_filter.iter("action")
        }
        categories = {
            category.get(f"{ANDROID_NS}name", "")
            for category in intent_filter.iter("category")
        }
        if "android.intent.action.MAIN" in actions and "android.intent.category.LAUNCHER" in categories:
            return True
    return False


def _short_activity_name(name_attr: str, package_name: str) -> str:
    if name_attr.startswith("."):
        name_attr = package_name + name_attr
    return name_attr.rsplit(".", 1)[-1]


# Cache so gradle_namespace is only computed once per project root.
_NAMESPACE_CACHE: dict[str, str] = {}


def gradle_namespace(project_root: str | Path) -> str:
    """
    Read the app package namespace from Gradle build files as a fallback for
    projects that no longer declare ``package=`` in AndroidManifest.xml
    (Gradle 7+ AGP convention: namespace is in build.gradle.kts / build.gradle).

    Returns the first namespace found, or "" if none.
    """
    key = str(Path(project_root).resolve())
    if key in _NAMESPACE_CACHE:
        return _NAMESPACE_CACHE[key]

    root = Path(project_root)
    result = ""
    for gradle_file in sorted(root.rglob("build.gradle*")):
        if _is_ignored(gradle_file) or not gradle_file.is_file():
            continue
        try:
            text = gradle_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Kotlin DSL:  namespace = "com.example.app"
        m = re.search(r'\bnamespace\s*=\s*"([^"]+)"', text)
        if m:
            result = m.group(1)
            break
        # Groovy DSL:  namespace 'com.example.app'
        m = re.search(r"\bnamespace\s+'([^']+)'", text)
        if m:
            result = m.group(1)
            break
        # Fallback: applicationId / applicationIdSuffix
        m = re.search(r'\bapplicationId\s*[=\s]\s*["\']([^"\']+)["\']', text)
        if m:
            result = m.group(1)
            break

    _NAMESPACE_CACHE[key] = result
    return result


def _manifest_package(manifest_root_elem, project_root: str | Path) -> str:
    """Return the app package from the manifest element, with Gradle fallback."""
    pkg = manifest_root_elem.get("package", "") or ""
    if not pkg:
        pkg = gradle_namespace(project_root)
    return pkg


def launcher_activity_class(project_root: str | Path) -> str:
    """Return the short MAIN/LAUNCHER Activity from any module manifest."""
    for manifest_path in manifests(project_root):
        try:
            tree = ET.parse(manifest_path)
        except ET.ParseError:
            continue
        package_name = _manifest_package(tree.getroot(), project_root)
        for activity_elem in tree.iter("activity"):
            name_attr = activity_elem.get(f"{ANDROID_NS}name", "")
            if name_attr and _manifest_has_launcher(activity_elem):
                return _short_activity_name(name_attr, package_name)
    return ""


def manifest_action_map(project_root: str | Path) -> dict[str, list[str]]:
    """Build android.intent.action.* -> short Activity names across all manifests."""
    action_map: dict[str, list[str]] = {}
    for manifest_path in manifests(project_root):
        try:
            tree = ET.parse(manifest_path)
        except ET.ParseError:
            continue
        package_name = _manifest_package(tree.getroot(), project_root)
        for activity_elem in tree.iter("activity"):
            name_attr = activity_elem.get(f"{ANDROID_NS}name", "")
            if not name_attr:
                continue
            short_name = _short_activity_name(name_attr, package_name)
            for intent_filter in activity_elem.iter("intent-filter"):
                for action in intent_filter.iter("action"):
                    action_name = action.get(f"{ANDROID_NS}name", "")
                    if action_name and action_name.startswith("android.intent.action."):
                        action_map.setdefault(action_name, []).append(short_name)
    return action_map


def class_name_for_source(path: Path) -> str:
    return path.stem


def package_name_for_source(source: str) -> str:
    match = re.search(r"^\s*package\s+([\w.]+)", source, re.MULTILINE)
    return match.group(1) if match else ""


def analyzed_variant_meta(project_root: str | Path) -> dict:
    """Return a small metadata dict describing which flavor was auto-detected."""
    flavor = detect_default_flavor(project_root)
    return {"flavor": flavor, "build_type": None, "auto_detected": flavor is not None}
