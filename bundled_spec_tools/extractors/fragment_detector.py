"""
fragment_detector.py
Detect Fragment subclasses and their attachment patterns in Android source code.
Outputs structured JSON with fragment metadata.
"""
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from extractors import android_project

ANDROID_NS = "http://schemas.android.com/apk/res/android"

# Pattern 1: FragmentTransaction.replace/add
_FRAGMENT_TX_RE = re.compile(
    r'\.(?:replace|add)\s*\(\s*R\.id\.(\w+)\s*,\s*'
    r'(?:new\s+)?(\w+)\s*[\(.]',
    re.MULTILINE,
)

# Pattern 2: Fragment adapter classes
_FRAGMENT_ADAPTER_CLASS_RE = re.compile(
    r'class\s+(\w+)\s*[^{]*(?:extends|:)\s*'
    r'(?:FragmentPagerAdapter|FragmentStateAdapter|FragmentStatePagerAdapter)',
)
_POSITION_BRANCH_RE = re.compile(
    r'(\d+)\s*(?:->|:)\s*(?:return\s+)?(?:new\s+)?(\w+)\s*\(',
)

# Pattern 3: XML <fragment> tag
# Handled via XML parsing, not regex

# Pattern 4: Fragment class declarations
_FRAGMENT_CLASS_RE = re.compile(
    r'class\s+(\w+)\s*[^{]*(?:extends|:)\s*'
    r'(?:Fragment|DialogFragment|BottomSheetDialogFragment|PreferenceFragmentCompat'
    r'|ListFragment|MapFragment|SupportMapFragment)\s*[\({]',
)

# Host class detection: find enclosing class name
_CLASS_DECL_RE = re.compile(
    r'class\s+(\w+)',
)

# Priority for dedup: lower index = higher priority
_ATTACH_PRIORITY = {
    "FragmentTransaction.replace": 0,
    "FragmentTransaction.add": 0,
    "ViewPager2+FragmentStateAdapter": 1,
    "xml_fragment_tag": 2,
    "class_declaration": 3,
}


def _find_host_class(content: str, line_idx: int) -> str:
    best = ""
    for m in _CLASS_DECL_RE.finditer(content):
        if m.start() <= _offset_for_line(content, line_idx):
            best = m.group(1)
    return best


def _offset_for_line(content: str, line_idx: int) -> int:
    offset = 0
    for i, line in enumerate(content.split("\n")):
        if i >= line_idx:
            break
        offset += len(line) + 1
    return offset


def _line_number(content: str, match_start: int) -> int:
    return content[:match_start].count("\n") + 1


def _is_fragment_name(name: str) -> bool:
    fragment_suffixes = ("Fragment", "BottomSheet", "DialogFragment",
                         "BottomSheetDialogFragment", "PreferenceFragment")
    return any(name.endswith(s) for s in fragment_suffixes) or "Fragment" in name


def _scan_source_files(project_root: str, dep_roots: list[str] | None,
                       file_prefix: str) -> list[tuple[Path, str, str]]:
    roots = [project_root] + (dep_roots or [])
    files = []
    for root in roots:
        prefix = file_prefix
        if root != project_root:
            prefix = Path(root).name
        for f in android_project.source_files(root):
            rel = android_project.relative_to_root(f, root, prefix)
            files.append((f, rel, root))
    return files


def run(project_root: str, dep_roots: list[str] | None = None,
        file_prefix: str = "") -> dict:
    fragments: list[dict] = {}.__class__([])  # type: list[dict]
    files_scanned = 0

    source_files = _scan_source_files(project_root, dep_roots, file_prefix)

    for fpath, rel_path, root in source_files:
        try:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        files_scanned += 1

        # Pattern 1: FragmentTransaction.replace/add
        for m in _FRAGMENT_TX_RE.finditer(content):
            container_id = m.group(1)
            frag_class = m.group(2)
            if not _is_fragment_name(frag_class):
                continue
            line = _line_number(content, m.start())
            host = _find_host_class(content, line - 1)
            method = "replace" if ".replace" in m.group(0) else "add"
            fragments.append({
                "class": frag_class,
                "container_id": container_id,
                "attach_method": f"FragmentTransaction.{method}",
                "host_class": host,
                "host_file": rel_path,
                "line": line,
                "source_file": rel_path,
            })

        # Pattern 2: FragmentPagerAdapter/FragmentStateAdapter
        for m_cls in _FRAGMENT_ADAPTER_CLASS_RE.finditer(content):
            adapter_class = m_cls.group(1)
            adapter_start = m_cls.start()
            # Find the body of getItem/createFragment
            body_re = re.compile(
                r'(?:getItem|createFragment)\s*\([^)]*\)\s*[:{]',
            )
            body_match = body_re.search(content, adapter_start)
            if not body_match:
                continue
            body_start = body_match.end()
            # Scan for position branches
            search_end = min(body_start + 2000, len(content))
            body_text = content[body_start:search_end]
            for m_pos in _POSITION_BRANCH_RE.finditer(body_text):
                frag_class = m_pos.group(2)
                if not _is_fragment_name(frag_class):
                    continue
                position = int(m_pos.group(1))
                line = _line_number(content, body_start + m_pos.start())
                fragments.append({
                    "class": frag_class,
                    "container_id": "",
                    "attach_method": "ViewPager2+FragmentStateAdapter",
                    "host_class": adapter_class,
                    "host_file": rel_path,
                    "line": line,
                    "position": position,
                    "source_file": rel_path,
                })

        # Pattern 4: Fragment class declarations
        for m in _FRAGMENT_CLASS_RE.finditer(content):
            frag_class = m.group(1)
            line = _line_number(content, m.start())
            fragments.append({
                "class": frag_class,
                "container_id": "",
                "attach_method": "class_declaration",
                "host_class": "",
                "host_file": "",
                "line": line,
                "source_file": rel_path,
            })

    # Pattern 3: XML <fragment> tags
    roots_to_scan = [project_root] + (dep_roots or [])
    for root in roots_to_scan:
        prefix = file_prefix if root == project_root else Path(root).name
        for res_dir in android_project.res_dirs(root):
            for layout_dir in res_dir.glob("layout*"):
                for xml_file in layout_dir.glob("*.xml"):
                    try:
                        tree = ET.parse(xml_file)
                    except ET.ParseError:
                        continue
                    for elem in tree.iter():
                        tag = elem.tag
                        if tag == "fragment" or (isinstance(tag, str) and tag.endswith("}fragment")):
                            name_attr = elem.get(f"{{{ANDROID_NS}}}name", "") or elem.get("android:name", "") or elem.get("class", "")
                            if not name_attr:
                                continue
                            frag_class = name_attr.rsplit(".", 1)[-1]
                            container_id = (elem.get(f"{{{ANDROID_NS}}}id", "") or "").replace("@+id/", "").replace("@id/", "")
                            rel = android_project.relative_to_root(xml_file, root, prefix)
                            fragments.append({
                                "class": frag_class,
                                "container_id": container_id,
                                "attach_method": "xml_fragment_tag",
                                "host_class": "",
                                "host_file": rel,
                                "line": 0,
                                "source_file": rel,
                            })

    # Dedup: keep highest-priority attach_method per fragment class
    deduped: dict[str, dict] = {}
    for frag in fragments:
        cls = frag["class"]
        if cls not in deduped:
            deduped[cls] = frag
        else:
            existing_priority = _ATTACH_PRIORITY.get(deduped[cls]["attach_method"], 99)
            new_priority = _ATTACH_PRIORITY.get(frag["attach_method"], 99)
            if new_priority < existing_priority:
                deduped[cls] = frag
            elif new_priority == existing_priority and frag["attach_method"] != "class_declaration":
                # Keep both if same priority but different contexts
                key = f"{cls}@{frag.get('container_id', '')}@{frag.get('host_class', '')}"
                if key not in deduped:
                    deduped[key] = frag

    result_fragments = sorted(deduped.values(), key=lambda f: (f["source_file"], f["line"]))

    by_method: dict[str, int] = {}
    for f in result_fragments:
        m = f["attach_method"]
        by_method[m] = by_method.get(m, 0) + 1

    return {
        "fragments": result_fragments,
        "stats": {
            "total": len(result_fragments),
            "by_attach_method": by_method,
            "source_files_scanned": files_scanned,
        },
    }
