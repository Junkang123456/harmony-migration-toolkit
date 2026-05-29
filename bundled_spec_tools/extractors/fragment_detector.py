"""
fragment_detector.py
Detect Fragment subclasses and their attachment patterns in Android source code.
Outputs structured JSON with fragment metadata.

Uses regex for attachment patterns and tree-sitter AST for comprehensive
class-declaration scanning with inheritance chain resolution.
"""
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from extractors import android_project
from extractors.ast_index import (
    build_class_hierarchy, _resolve_android_base, lookup_class,
    _walk as _ast_walk, _node_text as _ast_node_text,
    _line as _ast_line, _source_files as _ast_source_files,
    _language_for as _ast_language_for, _rel_path as _ast_rel_path,
    _class_name as _ast_class_name_node,
    _first_named_child as _ast_first_child,
)

try:
    from tree_sitter_language_pack import get_parser as _get_parser
except Exception:
    _get_parser = None

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

# Pattern 3: XML <fragment> tag (handled via XML parsing)

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


def _ast_fragment_declarations(project_root: str, dep_roots: list[str] | None = None,
                                file_prefix: str = "",
                                hierarchy: dict | None = None) -> tuple[list[dict], dict]:
    """使用 AST 继承链解析找到所有 Fragment 子类声明。返回 (results, hierarchy)。"""
    if hierarchy is None:
        hierarchy = build_class_hierarchy(project_root, dep_roots, file_prefix)
    results = []
    for fqn, info in hierarchy.items():
        base_type = _resolve_android_base(fqn, hierarchy)
        if base_type == "fragment":
            results.append({
                "class": info.name,
                "source_file": info.source_file,
                "line": info.line,
                "base_class": info.base_class or "",
                "detection": "ast",
            })
    return results, hierarchy


def _find_enclosing_class(source: bytes, node) -> str:
    cur = node.parent()
    while cur is not None:
        if cur.kind() in {"class_declaration", "object_declaration"}:
            return _ast_class_name_node(source, cur)
        cur = cur.parent()
    return ""


def _find_enclosing_function(source: bytes, node) -> str:
    cur = node.parent()
    while cur is not None:
        if cur.kind() in {"function_declaration", "method_declaration"}:
            name_node = cur.child_by_field_name("name")
            if name_node is not None:
                return _ast_node_text(source, name_node)
            child = _ast_first_child(cur, {"simple_identifier", "identifier"})
            return _ast_node_text(source, child) if child else ""
        cur = cur.parent()
    return ""


_FRAGMENT_TX_METHODS = {"replace", "add", "show", "commit", "beginTransaction"}
_TX_ATTACH_METHODS = {"replace", "add"}


def _resolve_fragment_arg(source: bytes, arg_node, scope_node, hierarchy) -> str | None:
    """Resolve a fragment argument to a class name.

    Handles:
      - Direct constructor: SomeFragment() / new SomeFragment()
      - Companion newInstance: SomeFragment.newInstance(...)
      - Variable reference: val f = SomeFragment(); tx.replace(..., f)
    """
    text = _ast_node_text(source, arg_node).strip()

    # 1. Direct constructor: SomeFragment() or SomeFragment.newInstance(...)
    if arg_node.kind() == "call_expression":
        callee = arg_node.named_child(0) if arg_node.named_child_count() > 0 else None
        if callee is not None:
            callee_text = _ast_node_text(source, callee)
            # SomeFragment.newInstance(...) or SomeFragment.Companion.newInstance(...)
            if ".newInstance" in callee_text or ".Companion." in callee_text:
                cls = callee_text.split(".")[0].strip()
                if cls and cls[0].isupper():
                    return cls
            # SomeFragment()
            if callee.kind() in {"simple_identifier", "identifier"}:
                cls = callee_text.strip()
                if cls and cls[0].isupper() and _is_fragment_class(cls, hierarchy):
                    return cls

    # Java: new SomeFragment()
    if arg_node.kind() == "object_creation_expression":
        type_node = arg_node.child_by_field_name("type") or _ast_first_child(
            arg_node, {"type_identifier", "scoped_type_identifier"}
        )
        if type_node is not None:
            cls = _ast_node_text(source, type_node).split(".")[-1].split("<")[0].strip()
            if cls and _is_fragment_class(cls, hierarchy):
                return cls

    # 2. Variable reference: look up declaration in scope
    if arg_node.kind() in {"simple_identifier", "identifier"}:
        var_name = text
        return _trace_variable_to_fragment(source, var_name, scope_node, hierarchy)

    # 3. Dotted expression ending in fragment-like name
    if "." in text:
        parts = text.split(".")
        last = parts[-1].split("(")[0].strip()
        if last and last[0].isupper() and _is_fragment_class(last, hierarchy):
            return last

    return None


def _trace_variable_to_fragment(source: bytes, var_name: str, scope_node, hierarchy) -> str | None:
    """Walk backwards through scope to find where var_name was assigned a Fragment."""
    for node in _ast_walk(scope_node):
        if node.kind() == "property_declaration":
            text = _ast_node_text(source, node)
            m = re.match(r'(?:val|var)\s+' + re.escape(var_name) + r'\s*(?::\s*\w+)?\s*=', text)
            if m:
                eq_pos = text.find("=")
                if eq_pos >= 0:
                    init = text[eq_pos + 1:].strip()
                    ctor = re.match(r'([A-Z]\w*)\s*[.(]', init)
                    if ctor:
                        cls = ctor.group(1)
                        if _is_fragment_class(cls, hierarchy):
                            return cls
                    ni = re.match(r'([A-Z]\w*)\.(?:newInstance|Companion)', init)
                    if ni:
                        cls = ni.group(1)
                        if _is_fragment_class(cls, hierarchy):
                            return cls

        elif node.kind() in {"local_variable_declaration", "variable_declarator"}:
            for i in range(node.named_child_count()):
                child = node.named_child(i)
                if child.kind() == "variable_declarator":
                    name_node = child.child_by_field_name("name") or _ast_first_child(
                        child, {"identifier"}
                    )
                    if name_node and _ast_node_text(source, name_node) == var_name:
                        val_node = child.child_by_field_name("value")
                        if val_node:
                            return _resolve_fragment_arg(source, val_node, scope_node, hierarchy)
    return None


def _is_fragment_class(name: str, hierarchy) -> bool:
    if _is_fragment_name(name):
        return True
    if hierarchy:
        info = lookup_class(hierarchy, name)
        if info is not None:
            return _resolve_android_base(info.fqn, hierarchy) == "fragment"
    return False


def _extract_container_id(source: bytes, args_node) -> str:
    """Extract R.id.xxx from the first argument of replace/add."""
    if args_node is None or args_node.named_child_count() == 0:
        return ""
    first_arg = args_node.named_child(0)
    text = _ast_node_text(source, first_arg)
    m = re.search(r'R\.id\.(\w+)', text)
    return m.group(1) if m else ""


def _ast_fragment_transactions(project_root: str, dep_roots: list[str] | None = None,
                                file_prefix: str = "",
                                hierarchy: dict | None = None) -> list[dict]:
    """Use AST to find FragmentTransaction.replace/add calls and resolve fragment arguments."""
    if _get_parser is None:
        return []

    if hierarchy is None:
        hierarchy = build_class_hierarchy(project_root, dep_roots, file_prefix)

    results: list[dict] = []
    roots = [(project_root, file_prefix)]
    if dep_roots:
        roots.extend((d, Path(d).name) for d in dep_roots)

    for root_path, prefix in roots:
        root = Path(root_path)
        for src_path in _ast_source_files(root):
            language = _ast_language_for(src_path)
            if not language:
                continue
            try:
                parser = _get_parser(language)
                source = src_path.read_bytes()
                tree = parser.parse(source.decode("utf-8"))
            except Exception:
                continue
            root_node = tree.root_node()
            rel = _ast_rel_path(src_path, root, prefix)

            for node in _ast_walk(root_node):
                if node.kind() not in {"call_expression", "method_invocation"}:
                    continue
                call_text = _ast_node_text(source, node)

                # Match .replace(...) / .add(...) on fragment transaction
                method_name = _get_call_method_name(source, node)
                if method_name not in _TX_ATTACH_METHODS:
                    continue

                # Check receiver looks like a FragmentTransaction
                if not _looks_like_fragment_transaction(source, node, call_text):
                    continue

                # Get arguments
                args_node = _get_args_node(node)
                if args_node is None or args_node.named_child_count() < 2:
                    continue

                container_id = _extract_container_id(source, args_node)
                frag_arg = args_node.named_child(1)

                # Find the enclosing function/class to use as scope
                scope = _find_enclosing_function_node(node)
                if scope is None:
                    scope = root_node

                frag_class = _resolve_fragment_arg(source, frag_arg, scope, hierarchy)
                if not frag_class:
                    continue

                host_class = _find_enclosing_class(source, node)
                line = _ast_line(node)
                results.append({
                    "class": frag_class,
                    "container_id": container_id,
                    "attach_method": f"FragmentTransaction.{method_name}",
                    "host_class": host_class,
                    "host_file": rel,
                    "line": line,
                    "source_file": rel,
                    "detection": "ast_dataflow",
                })

    return results


_LOAD_FRAGMENT_METHODS = {"loadFragment", "loadChildFragment"}


def _ast_load_fragment_calls(project_root: str, dep_roots: list[str] | None = None,
                              file_prefix: str = "",
                              hierarchy: dict | None = None) -> list[dict]:
    """Detect loadFragment(Fragment)/loadChildFragment(Fragment) calls via AST.

    Covers patterns like:
      - loadChildFragment(new SomeFragment())
      - loadChildFragment(SomeFragment.newInstance(...))
      - ((MainActivity) getActivity()).loadChildFragment(someFragment)
    Host is resolved from the class that defines loadFragment/loadChildFragment.
    """
    if _get_parser is None:
        return []
    if hierarchy is None:
        hierarchy = build_class_hierarchy(project_root, dep_roots, file_prefix)

    # Find which classes define loadFragment/loadChildFragment
    host_classes = _find_load_fragment_hosts(project_root, dep_roots, file_prefix, hierarchy)

    results: list[dict] = []
    roots = [(project_root, file_prefix)]
    if dep_roots:
        roots.extend((d, Path(d).name) for d in dep_roots)

    for root_path, prefix in roots:
        root = Path(root_path)
        for src_path in _ast_source_files(root):
            language = _ast_language_for(src_path)
            if not language:
                continue
            try:
                parser = _get_parser(language)
                source = src_path.read_bytes()
                tree = parser.parse(source.decode("utf-8"))
            except Exception:
                continue
            root_node = tree.root_node()
            rel = _ast_rel_path(src_path, root, prefix)
            source_text = source.decode("utf-8", errors="ignore")

            for node in _ast_walk(root_node):
                if node.kind() not in {"call_expression", "method_invocation"}:
                    continue
                method_name = _get_call_method_name(source, node)
                if method_name not in _LOAD_FRAGMENT_METHODS:
                    continue

                args_node = _get_args_node(node)
                if args_node is None or args_node.named_child_count() < 1:
                    continue

                frag_arg = args_node.named_child(0)
                scope = _find_enclosing_function_node(node) or root_node
                frag_class = _resolve_fragment_arg(source, frag_arg, scope, hierarchy)
                if not frag_class:
                    continue

                # Determine host: cast target or class defining the method
                host = _resolve_load_fragment_host(source, node, host_classes)
                if not host:
                    host = _find_enclosing_class(source, node)

                line = _ast_line(node)
                results.append({
                    "class": frag_class,
                    "container_id": "",
                    "attach_method": f"FragmentTransaction.replace",
                    "host_class": host,
                    "host_file": rel,
                    "line": line,
                    "source_file": rel,
                    "detection": "ast_load_fragment",
                })

    return results


def _find_load_fragment_hosts(project_root: str, dep_roots: list[str] | None,
                               file_prefix: str, hierarchy: dict) -> set[str]:
    """Find classes that define loadFragment/loadChildFragment methods."""
    hosts: set[str] = set()
    if _get_parser is None:
        return hosts
    roots = [(project_root, file_prefix)]
    if dep_roots:
        roots.extend((d, Path(d).name) for d in dep_roots)
    for root_path, prefix in roots:
        root = Path(root_path)
        for src_path in _ast_source_files(root):
            language = _ast_language_for(src_path)
            if not language:
                continue
            try:
                parser = _get_parser(language)
                source = src_path.read_bytes()
                tree = parser.parse(source.decode("utf-8"))
            except Exception:
                continue
            for node in _ast_walk(tree.root_node()):
                if node.kind() not in {"function_declaration", "method_declaration"}:
                    continue
                name_node = node.child_by_field_name("name")
                if name_node is None:
                    name_node = _ast_first_child(node, {"simple_identifier", "identifier"})
                if name_node and _ast_node_text(source, name_node) in _LOAD_FRAGMENT_METHODS:
                    cls = _find_enclosing_class(source, node)
                    if cls:
                        hosts.add(cls)
    return hosts


def _resolve_load_fragment_host(source: bytes, call_node, host_classes: set[str]) -> str:
    """Resolve the host from cast expressions like ((MainActivity) getActivity())."""
    text = _ast_node_text(source, call_node)
    # Java cast: ((MainActivity) getActivity()).loadChildFragment(...)
    m = re.search(r'\(\((\w+)\)\s*(?:getActivity|requireActivity)\s*\(\)\)', text)
    if m and m.group(1) in host_classes:
        return m.group(1)
    # Kotlin cast: (activity as MainActivity).loadChildFragment(...)
    m = re.search(r'as\s+(\w+)', text)
    if m and m.group(1) in host_classes:
        return m.group(1)
    # Direct call inside host class: loadChildFragment(...)
    for host in host_classes:
        if f"{host}." in text or text.startswith("loadChildFragment") or text.startswith("loadFragment"):
            return host
    return ""


def _ast_show_fragment_calls(project_root: str, dep_roots: list[str] | None = None,
                              file_prefix: str = "",
                              hierarchy: dict | None = None) -> list[dict]:
    """Detect DialogFragment.show(fragmentManager, tag) calls via AST.

    Covers patterns like:
      - new SomeDialog().show(getSupportFragmentManager(), tag)
      - SomeDialog.newInstance(...).show(getChildFragmentManager(), tag)
    """
    if _get_parser is None:
        return []
    if hierarchy is None:
        hierarchy = build_class_hierarchy(project_root, dep_roots, file_prefix)

    results: list[dict] = []
    roots = [(project_root, file_prefix)]
    if dep_roots:
        roots.extend((d, Path(d).name) for d in dep_roots)

    for root_path, prefix in roots:
        root = Path(root_path)
        for src_path in _ast_source_files(root):
            language = _ast_language_for(src_path)
            if not language:
                continue
            try:
                parser = _get_parser(language)
                source = src_path.read_bytes()
                tree = parser.parse(source.decode("utf-8"))
            except Exception:
                continue
            root_node = tree.root_node()
            rel = _ast_rel_path(src_path, root, prefix)

            for node in _ast_walk(root_node):
                if node.kind() not in {"call_expression", "method_invocation"}:
                    continue
                method_name = _get_call_method_name(source, node)
                if method_name != "show":
                    continue

                text = _ast_node_text(source, node)
                lower = text.lower()
                if "fragmentmanager" not in lower and "getsupportfragmentmanager" not in lower \
                        and "getchildfragmentmanager" not in lower \
                        and "getparentfragmentmanager" not in lower:
                    continue

                frag_class = _extract_show_receiver_class(source, node, hierarchy)
                if not frag_class:
                    continue

                host = _find_enclosing_class(source, node)
                line = _ast_line(node)
                results.append({
                    "class": frag_class,
                    "container_id": "",
                    "attach_method": "FragmentTransaction.add",
                    "host_class": host,
                    "host_file": rel,
                    "line": line,
                    "source_file": rel,
                    "detection": "ast_show",
                })

    return results


def _extract_show_receiver_class(source: bytes, show_node, hierarchy) -> str | None:
    """Extract the fragment class from the receiver of .show().

    Patterns:
      - new SomeDialog().show(...)  →  SomeDialog
      - SomeDialog.newInstance(...).show(...)  →  SomeDialog
      - dialog.show(...)  →  trace variable
    """
    text = _ast_node_text(source, show_node)

    # new SomeDialog().show(...)
    m = re.search(r'new\s+(\w+)\s*\([^)]*\)\s*\.show', text)
    if m:
        cls = m.group(1)
        if _is_fragment_class(cls, hierarchy):
            return cls

    # SomeDialog.newInstance(...).show(...)
    m = re.search(r'(\w+)\.newInstance\s*\([^)]*\)\s*\.show', text)
    if m:
        cls = m.group(1)
        if _is_fragment_class(cls, hierarchy):
            return cls

    # SomeDialog(...).show(...) — Kotlin constructor
    m = re.search(r'(\w+)\s*\([^)]*\)\s*\.show', text)
    if m:
        cls = m.group(1)
        if cls[0].isupper() and _is_fragment_class(cls, hierarchy):
            return cls

    # variable.show(...) — try to trace
    m = re.match(r'(\w+)\.show', text)
    if m:
        var_name = m.group(1)
        scope = _find_enclosing_function_node(show_node)
        if scope:
            return _trace_variable_to_fragment(source, var_name, scope, hierarchy)

    return None


def _get_call_method_name(source: bytes, node) -> str:
    """Extract the method name from a call_expression or method_invocation."""
    if node.kind() == "method_invocation":
        name_node = node.child_by_field_name("name")
        return _ast_node_text(source, name_node) if name_node else ""
    # Kotlin call_expression: navigation_member_expression . simple_identifier
    if node.named_child_count() > 0:
        first = node.named_child(0)
        text = _ast_node_text(source, first)
        if "." in text:
            return text.rsplit(".", 1)[-1].strip()
    return ""


def _looks_like_fragment_transaction(source: bytes, node, call_text: str) -> bool:
    """Heuristic: receiver contains 'transaction', 'beginTransaction', 'childFragmentManager',
    'supportFragmentManager', or 'fragmentManager'."""
    lower = call_text.lower()
    keywords = ("transaction", "fragmentmanager", "begintransaction",
                "childfragmentmanager", "supportfragmentmanager")
    if any(k in lower for k in keywords):
        return True
    # Also match: variable.replace(...) where variable was assigned from beginTransaction
    if node.kind() == "method_invocation":
        obj = node.child_by_field_name("object")
        if obj is not None:
            obj_text = _ast_node_text(source, obj).lower()
            return any(k in obj_text for k in keywords)
    return False


def _get_args_node(node):
    """Get the arguments/value_arguments node from a call."""
    args = node.child_by_field_name("arguments")
    if args is not None:
        return args
    return _ast_first_child(node, {"value_arguments", "argument_list"})


def _find_enclosing_function_node(node):
    cur = node.parent()
    while cur is not None:
        if cur.kind() in {"function_declaration", "method_declaration",
                          "function_body", "class_body"}:
            return cur
        cur = cur.parent()
    return None


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
    fragments: list[dict] = []
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
            body_re = re.compile(
                r'(?:getItem|createFragment)\s*\([^)]*\)\s*[:{]',
            )
            body_match = body_re.search(content, adapter_start)
            if not body_match:
                continue
            body_start = body_match.end()
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

    # Build class hierarchy once, share between AST passes
    hierarchy = build_class_hierarchy(project_root, dep_roots, file_prefix)

    # Pattern 4: AST-based fragment class declarations (inheritance chain aware)
    ast_decls, hierarchy = _ast_fragment_declarations(project_root, dep_roots, file_prefix,
                                                       hierarchy=hierarchy)

    # Pattern 5: AST data-flow fragment transactions
    ast_tx = _ast_fragment_transactions(project_root, dep_roots, file_prefix,
                                         hierarchy=hierarchy)
    for tx in ast_tx:
        fragments.append(tx)

    # Pattern 6: loadFragment/loadChildFragment calls
    load_frags = _ast_load_fragment_calls(project_root, dep_roots, file_prefix,
                                           hierarchy=hierarchy)
    for lf in load_frags:
        fragments.append(lf)

    # Pattern 7: DialogFragment.show() calls
    show_frags = _ast_show_fragment_calls(project_root, dep_roots, file_prefix,
                                           hierarchy=hierarchy)
    for sf in show_frags:
        fragments.append(sf)

    attached_names = {f["class"] for f in fragments}
    for decl in ast_decls:
        if decl["class"] not in attached_names:
            fragments.append({
                "class": decl["class"],
                "container_id": "",
                "attach_method": "class_declaration",
                "host_class": "",
                "host_file": "",
                "line": decl["line"],
                "source_file": decl["source_file"],
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
                key = f"{cls}@{frag.get('container_id', '')}@{frag.get('host_class', '')}"
                if key not in deduped:
                    deduped[key] = frag

    result_fragments = sorted(deduped.values(), key=lambda f: (f["source_file"], f["line"]))

    by_method: dict[str, int] = {}
    for f in result_fragments:
        m = f["attach_method"]
        by_method[m] = by_method.get(m, 0) + 1

    # Coverage: all Fragment classes from AST hierarchy
    ast_class_names = {d["class"] for d in ast_decls}
    attached_classes = {f["class"] for f in result_fragments if f["attach_method"] != "class_declaration"}
    orphan_classes = sorted(ast_class_names - attached_classes) if ast_decls else []

    coverage: dict = {
        "ast_available": bool(ast_decls),
        "declared_fragment_count": len(ast_class_names),
        "attached_fragment_count": len(attached_classes),
        "orphan_classes": orphan_classes,
    }

    return {
        "fragments": result_fragments,
        "stats": {
            "total": len(result_fragments),
            "by_attach_method": by_method,
            "source_files_scanned": files_scanned,
            "coverage": coverage,
        },
    }
