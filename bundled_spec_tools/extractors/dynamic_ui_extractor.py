"""
dynamic_ui_extractor.py
Detect dynamically-created UI components in Android source code.

Three detection patterns:
  1. addView  — container.addView(new XxxView(ctx)) or container.addView(v)
  2. inflate+addView — val v = inflater.inflate(R.layout.xxx); container.addView(v)
  3. setAdapter — recyclerView.adapter = XxxAdapter(); find inflate in adapter class

Only scans initialisation methods (onCreate, onCreateView, initView, setupView, etc.)
to avoid tracking runtime-dynamic state.
"""
from __future__ import annotations

import re
from pathlib import Path

from extractors import android_project

# Methods considered "initialisation" — only scan these for dynamic UI
_INIT_METHOD_RE = re.compile(
    r'(?:fun|override\s+fun|void|private\s+fun|public\s+void)\s+'
    r'(onCreate|onCreateView|onViewCreated|initView|initUI|setupView|setupUI|initViews|bindViews)\s*\(',
    re.MULTILINE,
)

# Pattern 1: addView calls
_ADD_VIEW_RE = re.compile(
    r'(\w+)\s*\.\s*addView\s*\(\s*(\w+)',
    re.MULTILINE,
)

# Track variable construction: val xxx = XxxView(context) or new XxxView(context)
_VIEW_CTOR_RE = re.compile(
    r'(?:val|var|final)\s+(\w+)\s*(?::\s*\w+)?\s*=\s*(?:new\s+)?(\w+(?:View|Layout|Button|Text|Image|EditText|Switch|Checkbox|Radio|Spinner|Progress|Seek|Rating|Chip|Card|Toolbar|AppBar|FloatingAction|RecyclerView|ListView|GridView|ScrollView|WebView))\s*\(',
    re.MULTILINE,
)

# Pattern 2: inflate into variable, then addView
_INFLATE_VAR_RE = re.compile(
    r'(?:val|var|final)\s+(\w+)\s*(?::\s*\w+)?\s*=\s*\w+\.inflate\s*\(\s*R\.layout\.(\w+)',
    re.MULTILINE,
)

# Pattern 3: setAdapter / adapter = XxxAdapter
_SET_ADAPTER_RE = re.compile(
    r'(\w+)\s*\.\s*(?:adapter\s*=\s*|setAdapter\s*\(\s*)(\w+Adapter)\s*[\(.]',
    re.MULTILINE,
)

# In adapter class: inflate(R.layout.item_xxx) in onCreateViewHolder/getView
_ADAPTER_CLASS_RE = re.compile(
    r'class\s+(\w+Adapter)\s*[^{]*(?:extends|:)\s*'
    r'(?:RecyclerView\.Adapter|ArrayAdapter|BaseAdapter|ListAdapter|PagingDataAdapter'
    r'|CursorAdapter|SimpleCursorAdapter|PagedListAdapter)',
    re.MULTILINE,
)
_ADAPTER_INFLATE_RE = re.compile(
    r'inflate\s*\(\s*R\.layout\.(\w+)',
    re.MULTILINE,
)

# Property setter extraction for dynamically created views
_SETTER_PATTERNS = {
    "text": re.compile(r'(\w+)\s*\.\s*(?:text\s*=|setText\s*\()\s*["\']([^"\']{1,80})', re.MULTILINE),
    "hint": re.compile(r'(\w+)\s*\.\s*(?:hint\s*=|setHint\s*\()\s*["\']([^"\']{1,80})', re.MULTILINE),
    "content_description": re.compile(r'(\w+)\s*\.\s*(?:contentDescription\s*=|setContentDescription\s*\()\s*["\']([^"\']{1,80})', re.MULTILINE),
}

_CLASS_DECL_RE = re.compile(r'class\s+(\w+)', re.MULTILINE)


def _find_host_class(content: str, offset: int) -> str:
    best = ""
    for m in _CLASS_DECL_RE.finditer(content):
        if m.start() <= offset:
            best = m.group(1)
    return best


def _line_number(content: str, offset: int) -> int:
    return content[:offset].count("\n") + 1


def _find_method_body(content: str, method_match: re.Match) -> str:
    start = method_match.end()
    depth = 0
    body_start = -1
    for i in range(start, min(start + 5000, len(content))):
        ch = content[i]
        if ch == '{':
            if depth == 0:
                body_start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and body_start >= 0:
                return content[body_start:i + 1]
    return content[start:start + 3000]


def _extract_properties(body: str, var_name: str) -> dict:
    props: dict[str, str] = {}
    for prop_name, pat in _SETTER_PATTERNS.items():
        for m in pat.finditer(body):
            if m.group(1) == var_name:
                props[prop_name] = m.group(2)
    return props


def _find_view_id_for_variable(body: str, var_name: str) -> str:
    """Try to find R.id.xxx assignment: val v = findViewById(R.id.xxx) or findViewById<T>(R.id.xxx)."""
    pat = re.compile(
        rf'(?:val|var|final)\s+{re.escape(var_name)}\s*(?::\s*\w+)?\s*=\s*\w*\.?findViewById\w*\s*(?:<[^>]*>\s*)?\(\s*R\.id\.(\w+)',
    )
    m = pat.search(body)
    return m.group(1) if m else ""


def _scan_init_methods(content: str, rel_path: str,
                       dynamic_elements: list, adapter_usages: dict) -> None:
    """Scan init methods in a source file for dynamic UI patterns."""
    for method_match in _INIT_METHOD_RE.finditer(content):
        method_name = method_match.group(1)
        body = _find_method_body(content, method_match)
        host_class = _find_host_class(content, method_match.start())
        body_offset = content.find(body, method_match.end() - 20)
        if body_offset < 0:
            body_offset = method_match.start()

        # Collect variable→type mappings from constructor patterns
        var_types: dict[str, str] = {}
        for m in _VIEW_CTOR_RE.finditer(body):
            var_types[m.group(1)] = m.group(2)

        # Collect variable→layout from inflate patterns
        var_layouts: dict[str, str] = {}
        for m in _INFLATE_VAR_RE.finditer(body):
            var_layouts[m.group(1)] = m.group(2)

        # Pattern 1 & 2: addView calls
        for m in _ADD_VIEW_RE.finditer(body):
            container_var = m.group(1)
            added_var = m.group(2)
            container_id = _find_view_id_for_variable(body, container_var)
            line = _line_number(content, body_offset + m.start())

            if added_var in var_types:
                # Pattern 1: addView with direct construction
                dynamic_elements.append({
                    "view_type": var_types[added_var],
                    "variable_name": added_var,
                    "container_variable": container_var,
                    "container_id": container_id,
                    "creation_method": "addView",
                    "host_class": host_class,
                    "host_method": method_name,
                    "file": rel_path,
                    "line": line,
                    "properties": _extract_properties(body, added_var),
                })
            elif added_var in var_layouts:
                # Pattern 2: inflate + addView
                dynamic_elements.append({
                    "view_type": "inflated_layout",
                    "layout_name": var_layouts[added_var],
                    "variable_name": added_var,
                    "container_variable": container_var,
                    "container_id": container_id,
                    "creation_method": "inflate+addView",
                    "host_class": host_class,
                    "host_method": method_name,
                    "file": rel_path,
                    "line": line,
                    "properties": {},
                })

        # Pattern 3: setAdapter
        for m in _SET_ADAPTER_RE.finditer(body):
            host_var = m.group(1)
            adapter_class = m.group(2)
            host_id = _find_view_id_for_variable(body, host_var)
            line = _line_number(content, body_offset + m.start())
            adapter_usages[adapter_class] = {
                "host_class": host_class,
                "host_variable": host_var,
                "host_id": host_id,
                "file": rel_path,
                "line": line,
            }


def _scan_adapter_classes(content: str, rel_path: str,
                          adapter_usages: dict, adapter_layouts: list) -> None:
    """Find adapter class declarations and extract item layout inflate calls."""
    for m in _ADAPTER_CLASS_RE.finditer(content):
        adapter_class = m.group(1)
        class_start = m.start()
        # Find class body
        depth = 0
        body_start = -1
        for i in range(class_start, min(class_start + 20000, len(content))):
            if content[i] == '{':
                if depth == 0:
                    body_start = i
                depth += 1
            elif content[i] == '}':
                depth -= 1
                if depth == 0 and body_start >= 0:
                    class_body = content[body_start:i + 1]
                    break
        else:
            class_body = content[class_start:class_start + 5000]

        for inflate_m in _ADAPTER_INFLATE_RE.finditer(class_body):
            item_layout = inflate_m.group(1)
            usage = adapter_usages.get(adapter_class, {})
            line = _line_number(content, class_start + inflate_m.start())
            adapter_layouts.append({
                "adapter_class": adapter_class,
                "item_layout": item_layout,
                "host_class": usage.get("host_class", ""),
                "host_variable": usage.get("host_variable", ""),
                "host_id": usage.get("host_id", ""),
                "file": rel_path,
                "line": line,
            })


def run(project_root: str, dep_roots: list[str] | None = None,
        file_prefix: str = "") -> dict:
    roots = [(project_root, file_prefix)] + [
        (dep, Path(dep).name) for dep in (dep_roots or [])
    ]

    dynamic_elements: list[dict] = []
    adapter_usages: dict[str, dict] = {}
    adapter_layouts: list[dict] = []
    files_scanned = 0

    for root, prefix in roots:
        for fpath in android_project.source_files(root):
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            files_scanned += 1
            rel = android_project.relative_to_root(fpath, root, prefix)
            _scan_init_methods(content, rel, dynamic_elements, adapter_usages)
            _scan_adapter_classes(content, rel, adapter_usages, adapter_layouts)

    by_method: dict[str, int] = {}
    for elem in dynamic_elements:
        m = elem["creation_method"]
        by_method[m] = by_method.get(m, 0) + 1

    return {
        "dynamic_elements": dynamic_elements,
        "adapter_layouts": adapter_layouts,
        "stats": {
            "total_dynamic_elements": len(dynamic_elements),
            "total_adapter_layouts": len(adapter_layouts),
            "by_creation_method": by_method,
            "source_files_scanned": files_scanned,
        },
    }
