"""
xml_extractor.py
静态提取所有 XML 资源中的 UI 元素（layout / menu / navigation）
输出结构化 JSON，作为 ground truth 的基础层。

在 layout 扫描阶段会排除 **Android 桌面 App Widget（RemoteViews）** 相关布局：
``res/xml*/`` 中 ``<appwidget-provider>`` 声明的 ``initialLayout`` / ``previewLayout``
及其 ``<include>`` 闭包，以及少量通用命名启发式（如 ``^widget_``、``^stats_.*widget``），
避免把系统桌面小组件表面混进「应用内」静态 UI 清单。
"""
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from extractors import android_project

ANDROID_NS = "http://schemas.android.com/apk/res/android"
APP_NS     = "http://schemas.android.com/apk/res-auto"

# 明确可交互的 tag
INTERACTIVE_TAGS = {
    "Button", "ImageButton", "FloatingActionButton",
    "CheckBox", "RadioButton", "Switch", "ToggleButton",
    "EditText", "AutoCompleteTextView", "MultiAutoCompleteTextView",
    "Spinner", "SeekBar", "RatingBar",
    "TextView",           # 可能带 clickable / onClick
    "ImageView",          # 可能带 clickable
    "LinearLayout", "FrameLayout", "RelativeLayout",  # container 也可能 clickable
    "RecyclerView", "ViewPager2",
    "BottomNavigationView", "NavigationView",
    "TabLayout",
    "com.google.android.material.floatingactionbutton.FloatingActionButton",
    "com.google.android.material.appbar.MaterialToolbar",
    "com.google.android.material.chip.Chip",
    "com.google.android.material.chip.ChipGroup",
    "androidx.cardview.widget.CardView",
}

def _attr(elem, local_name, ns=ANDROID_NS):
    return elem.get(f"{{{ns}}}{local_name}", "")

def _short_tag(full_tag: str) -> str:
    """去掉包名，只保留类名"""
    return full_tag.split(".")[-1]

# ──────────────────────────────────────────────
# Layout XML
# ──────────────────────────────────────────────

def _resolve_include_path(include_layout: str, layout_dirs: list[Path]) -> Path | None:
    """Resolve @layout/foo or @layout/foo to the actual XML file path."""
    name = include_layout.replace("@layout/", "").replace("@+layout/", "").strip()
    if not name:
        return None
    for layout_dir in layout_dirs:
        candidate = layout_dir / f"{name}.xml"
        if candidate.is_file():
            return candidate
    return None


def extract_layout(xml_path: Path, source_prefix: str = "static_xml_layout",
                   file_prefix: str = "",
                   layout_dirs: list[Path] | None = None,
                   _visited: frozenset | None = None,
                   skip_layout_stems: frozenset[str] | None = None) -> list[dict]:
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        return []

    _visited = (_visited or frozenset()) | {xml_path}

    results = []
    for elem in tree.iter():
        tag = elem.tag
        short = _short_tag(tag)

        # Recursively expand <include layout="@layout/foo" /> tags.
        if short == "include":
            inc_layout = elem.get("layout", "")
            if inc_layout and layout_dirs:
                inc_path = _resolve_include_path(inc_layout, layout_dirs)
                if inc_path and inc_path not in _visited:
                    if skip_layout_stems and inc_path.stem in skip_layout_stems:
                        continue
                    results.extend(extract_layout(
                        inc_path,
                        source_prefix=source_prefix,
                        file_prefix=file_prefix,
                        layout_dirs=layout_dirs,
                        _visited=_visited,
                        skip_layout_stems=skip_layout_stems,
                    ))
            continue  # the <include> tag itself has no direct UI elements

        # <merge> root is transparent — its children are already visited by tree.iter()
        if short == "merge":
            continue

        view_id       = _attr(elem, "id").replace("@+id/", "").replace("@id/", "")
        on_click_attr = _attr(elem, "onClick")
        clickable     = _attr(elem, "clickable")
        checkable     = _attr(elem, "checkable")
        visibility    = _attr(elem, "visibility") or "visible"
        text          = _attr(elem, "text")
        hint          = _attr(elem, "hint")
        content_desc  = _attr(elem, "contentDescription")

        is_interactive = (
            short in INTERACTIVE_TAGS
            or on_click_attr
            or clickable == "true"
            or checkable == "true"
        )

        if not (is_interactive or view_id):
            continue

        try:
            file_rel = str(xml_path.relative_to(xml_path.parents[3]))
        except ValueError:
            file_rel = xml_path.name
        if file_prefix:
            file_rel = file_prefix + "/" + file_rel

        results.append({
            "source":       source_prefix,
            "file":         file_rel,
            "layout":       xml_path.stem,
            "tag":          short,
            "id":           view_id,
            "text":         text,
            "hint":         hint,
            "content_desc": content_desc,
            "on_click_attr": on_click_attr,
            "visibility":   visibility,
            "is_interactive": is_interactive,
        })

    return results


# ──────────────────────────────────────────────
# Menu XML
# ──────────────────────────────────────────────

def extract_menu(xml_path: Path, source_prefix: str = "static_xml_menu",
                 file_prefix: str = "") -> list[dict]:
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        return []

    results = []
    for elem in tree.iter():
        if _short_tag(elem.tag) not in ("item", "Item"):
            continue

        item_id    = _attr(elem, "id").replace("@+id/", "").replace("@id/", "")
        title      = _attr(elem, "title")
        show_as    = _attr(elem, "showAsAction", ns=APP_NS)

        file_rel = str(xml_path.relative_to(xml_path.parents[2]))
        if file_prefix:
            file_rel = file_prefix + "/" + file_rel

        results.append({
            "source":         source_prefix,
            "file":           file_rel,
            "menu":           xml_path.stem,
            "tag":            "MenuItem",
            "id":             item_id,
            "text":           title,
            "show_as_action": show_as,
            "is_interactive": True,
        })

    return results


# ──────────────────────────────────────────────
# Navigation Graph XML
# ──────────────────────────────────────────────

def extract_navigation(xml_path: Path) -> list[dict]:
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        return []

    results = []
    root = tree.getroot()

    def _walk(node, parent_id=""):
        node_id    = _attr(node, "id").replace("@+id/", "")
        node_label = _attr(node, "label")
        short      = _short_tag(node.tag)

        if short in ("fragment", "activity", "dialog", "navigation"):
            results.append({
                "source":     "static_xml_nav",
                "file":       str(xml_path.relative_to(xml_path.parents[2])),
                "nav_graph":  xml_path.stem,
                "tag":        short,
                "id":         node_id,
                "label":      node_label,
                "is_interactive": False,
            })

        for action in node:
            if _short_tag(action.tag) == "action":
                action_id   = _attr(action, "id").replace("@+id/", "")
                destination = _attr(action, "destination").replace("@id/", "")
                results.append({
                    "source":      "static_xml_nav_action",
                    "file":        str(xml_path.relative_to(xml_path.parents[2])),
                    "nav_graph":   xml_path.stem,
                    "tag":         "NavAction",
                    "id":          action_id,
                    "from":        node_id,
                    "destination": destination,
                    "is_interactive": True,
                })
            _walk(action, node_id)

    _walk(root)
    return results


# ──────────────────────────────────────────────
# String resource loader
# ──────────────────────────────────────────────

def load_strings(project_root: str) -> dict:
    """
    Parse values/strings.xml (and values-en/ fallback) from the project.
    Returns {key: value} for use in resolving @string/xxx references.
    Only reads the default locale (values/strings.xml) — no translation variants.
    """
    strings: dict = {}
    # Collect all candidate strings.xml files; prefer app/src/main/res/values/
    candidates = [res / "values" / "strings.xml" for res in android_project.res_dirs(project_root)]
    candidates = [p for p in candidates if p.is_file()]
    if not candidates:
        root = Path(project_root)
        candidates = list(root.rglob("values/strings.xml"))
    for path in candidates:
        try:
            tree = ET.parse(path)
        except ET.ParseError:
            continue
        for elem in tree.iter("string"):
            name = elem.get("name", "")
            # Collapse whitespace and strip inner markup
            text = (elem.text or "").strip()
            if name and text:
                strings[name] = text
    return strings


_HOME_WIDGET_LAYOUT_STEM_HEURISTIC = re.compile(
    r"(?i)"
    r"^widget_"  # common RemoteViews root layout prefix
    r"|_widget_preview$"
    r"|^glance_"  # Jetpack Glance home widgets
)


def _stem_from_android_layout_ref(raw: str) -> str | None:
    """Strip @layout/foo or @+layout/foo down to the layout resource stem."""
    v = (raw or "").strip()
    if "/layout/" not in v:
        return None
    stem = v.rsplit("/", 1)[-1]
    return stem or None


def _xml_dirs_under_res(res: Path) -> list[Path]:
    if not res.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(res.iterdir()):
        if not child.is_dir():
            continue
        name = child.name.lower()
        if name == "xml" or name.startswith("xml-"):
            out.append(child)
    return out


def _included_layout_stems_from_file(layout_path: Path) -> list[str]:
    """@layout stems referenced by <include> inside a layout XML."""
    try:
        tree = ET.parse(layout_path)
    except ET.ParseError:
        return []
    stems: list[str] = []
    for elem in tree.iter():
        if _short_tag(elem.tag) != "include":
            continue
        lay = elem.get("layout", "")
        st = _stem_from_android_layout_ref(lay)
        if st:
            stems.append(st)
    return stems


def _find_layout_path(layout_stem: str, layout_dirs: list[Path]) -> Path | None:
    for d in layout_dirs:
        candidate = d / f"{layout_stem}.xml"
        if candidate.is_file():
            return candidate
    return None


def collect_home_widget_surface_layout_stems(
    res_dirs: list[Path],
    layout_dirs: list[Path],
) -> set[str]:
    """
    Layout stems used for Android App Widget (home-screen / OS launcher) surfaces.

    Collects ``initialLayout`` / ``initialKeyguardLayout`` / ``previewLayout`` from
    every ``<appwidget-provider>`` under ``res/xml*``, then expands ``<include>``
    chains so RemoteViews subtrees stay out of in-app static UI inventory.

    Heuristic stems (e.g. ``^stats_.*widget`` for WordPress stats widgets) cover
    widget configuration dialogs inflated only from code, which never appear in
    ``appwidget-provider`` attributes.
    """
    queue: list[str] = []
    for res in res_dirs:
        for xd in _xml_dirs_under_res(res):
            for f in xd.glob("*.xml"):
                try:
                    tree = ET.parse(f)
                except ET.ParseError:
                    continue
                root = tree.getroot()
                local = root.tag.split("}")[-1] if root.tag else ""
                if local != "appwidget-provider":
                    continue
                for _k, v in root.attrib.items():
                    lk = _k.split("}")[-1]
                    if lk not in ("initialLayout", "initialKeyguardLayout", "previewLayout"):
                        continue
                    stem = _stem_from_android_layout_ref(v)
                    if stem:
                        queue.append(stem)

    excluded: set[str] = set()

    def _drain_include_queue() -> None:
        while queue:
            stem = queue.pop()
            if stem in excluded:
                continue
            excluded.add(stem)
            path = _find_layout_path(stem, layout_dirs)
            if not path:
                continue
            for inc in _included_layout_stems_from_file(path):
                if inc not in excluded:
                    queue.append(inc)

    _drain_include_queue()

    # Heuristic families not listed on appwidget-provider (e.g. widget config dialogs).
    for res in res_dirs:
        for layout_dir in res.glob("layout*"):
            for f in layout_dir.glob("*.xml"):
                stem = f.stem
                if stem in excluded:
                    continue
                if _HOME_WIDGET_LAYOUT_STEM_HEURISTIC.search(stem) or re.match(
                    r"(?i)^stats_.*widget", stem
                ):
                    queue.append(stem)

    _drain_include_queue()

    return excluded


def resolve_text(raw: str, strings: dict) -> str:
    """
    Resolve an @string/xxx reference to its human-readable value.
    If the key is not found (e.g. it lives in an external library), convert the
    key itself to Title Case words so labels stay readable.
    """
    if raw.startswith("@string/"):
        key = raw[len("@string/"):]
        if key in strings:
            return strings[key]
        # Key not in local strings (external library) — make it human-readable
        return key.replace("_", " ").title()
    return raw


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def run(project_root: str, source_prefix: str = "static_xml_layout",
        menu_prefix: str = "static_xml_menu",
        file_prefix: str = "") -> dict:
    res_dirs = android_project.res_dirs(project_root)

    strings = load_strings(project_root)
    all_elements = []

    # Collect all layout directories up-front so <include> expansion can resolve paths.
    all_layout_dirs: list[Path] = []
    for res in res_dirs:
        all_layout_dirs.extend(res.glob("layout*"))

    excluded_home_widget_stems = collect_home_widget_surface_layout_stems(res_dirs, all_layout_dirs)
    skip_stems = frozenset(excluded_home_widget_stems)

    for res in res_dirs:
        for layout_dir in res.glob("layout*"):
            for f in layout_dir.glob("*.xml"):
                if f.stem in skip_stems:
                    continue
                all_elements.extend(extract_layout(
                    f, source_prefix=source_prefix, file_prefix=file_prefix,
                    layout_dirs=all_layout_dirs, skip_layout_stems=skip_stems))

        menu_dir = res / "menu"
        if menu_dir.exists():
            for f in menu_dir.glob("*.xml"):
                all_elements.extend(extract_menu(
                    f, source_prefix=menu_prefix, file_prefix=file_prefix))

        nav_dir = res / "navigation"
        if nav_dir.exists():
            for f in nav_dir.glob("*.xml"):
                all_elements.extend(extract_navigation(f))

    # Resolve @string/xxx references in text / hint / content_desc
    for e in all_elements:
        for field in ("text", "hint", "content_desc"):
            raw = e.get(field, "")
            if raw:
                e[field] = resolve_text(raw, strings)

    # 统计
    stats = {
        "total":          len(all_elements),
        "interactive":    sum(1 for e in all_elements if e.get("is_interactive")),
        "hidden_by_default": sum(1 for e in all_elements if e.get("visibility") in ("gone", "invisible")),
        "by_source": {},
        "excluded_os_home_widget_layouts": len(excluded_home_widget_stems),
    }
    for e in all_elements:
        s = e["source"]
        stats["by_source"][s] = stats["by_source"].get(s, 0) + 1

    return {"elements": all_elements, "stats": stats, "strings": strings}


if __name__ == "__main__":
    import sys
    project = sys.argv[1] if len(sys.argv) > 1 else "."
    result = run(project)
    out = Path("output/static_ground_truth.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"✓ {result['stats']['total']} elements extracted "
          f"({result['stats']['interactive']} interactive)")
    print(f"  hidden_by_default: {result['stats']['hidden_by_default']}")
    print(f"  by source: {result['stats']['by_source']}")
