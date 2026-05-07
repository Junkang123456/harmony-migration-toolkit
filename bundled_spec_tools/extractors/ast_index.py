"""
Tree-sitter backed Kotlin/Java source index.

The index owns language-structure facts only: packages, classes, functions,
call sites, source ranges, and a small set of navigation call candidates. Higher
level extractors still interpret business DSLs and project-specific rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from tree_sitter_language_pack import get_parser
except Exception:  # pragma: no cover - optional runtime dependency
    get_parser = None  # type: ignore[assignment]


_SKIP_CALLS = {
    "if",
    "for",
    "while",
    "when",
    "switch",
    "catch",
    "return",
    "throw",
    "super",
    "this",
    "class",
    "fun",
}


@dataclass
class AstProjectIndex:
    project_root: str
    file_prefix: str = ""
    ast_available: bool = False
    symbols: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    unresolved_calls: list[dict[str, Any]] = field(default_factory=list)
    navigation_edges: list[dict[str, Any]] = field(default_factory=list)

    def find_enclosing_symbol(self, file_path: str, line: int) -> dict[str, Any] | None:
        norm = _norm_path(file_path)
        candidates = [
            s
            for s in self.symbols
            if _norm_path(s.get("file")) == norm
            and int(s.get("start_line", 0)) <= line <= int(s.get("end_line", 0))
        ]
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda s: (int(s.get("end_line", 0)) - int(s.get("start_line", 0)), str(s.get("symbol_id"))),
        )[0]

    def calls_in_symbol(self, symbol_id: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c.get("from_symbol_id") == symbol_id]

    def calls_by_name(self, name: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c.get("callee_name") == name]


def _norm_path(value: Any) -> str:
    return str(value or "").replace("\\", "/")


def _source_files(root: Path) -> list[Path]:
    return sorted(list(root.rglob("src/main/**/*.java")) + list(root.rglob("src/main/**/*.kt")))


def _rel_path(path: Path, root: Path, file_prefix: str = "") -> str:
    rel = path.relative_to(root).as_posix()
    return f"{file_prefix}/{rel}" if file_prefix else rel


def _language_for(path: Path) -> str:
    if path.suffix == ".kt":
        return "kotlin"
    if path.suffix == ".java":
        return "java"
    return ""


def _node_text(source: bytes, node: Any) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _line(node: Any) -> int:
    return int(node.start_point[0]) + 1


def _end_line(node: Any) -> int:
    return int(node.end_point[0]) + 1


def _walk(node: Any):
    yield node
    for child in node.children:
        yield from _walk(child)


def _first_named_child(node: Any, types: set[str]) -> Any | None:
    for child in node.named_children:
        if child.type in types:
            return child
    return None


def _last_identifier_text(source: bytes, node: Any) -> str:
    names: list[str] = []
    for child in _walk(node):
        if child.type in {"identifier", "simple_identifier", "type_identifier"}:
            names.append(_node_text(source, child))
    return names[-1] if names else ""


def _package_name(source: bytes, root: Any, language: str) -> str:
    for child in root.named_children:
        if language == "kotlin" and child.type == "package_header":
            simples = [_node_text(source, n) for n in _walk(child) if n.type == "simple_identifier"]
            if simples:
                return ".".join(simples)
            ident = _first_named_child(child, {"identifier"})
            return _node_text(source, ident) if ident is not None else ""
        if language == "java" and child.type == "package_declaration":
            names = [_node_text(source, n) for n in _walk(child) if n.type == "identifier"]
            return ".".join(names)
    return ""


def _owner_stack(source: bytes, node: Any, fallback: str) -> list[str]:
    owners: list[str] = []
    cur = node.parent
    while cur is not None:
        if cur.type in {"class_declaration", "object_declaration", "interface_declaration", "enum_declaration"}:
            name = _class_name(source, cur)
            if name:
                owners.append(name)
        cur = cur.parent
    return list(reversed(owners)) or [fallback]


def _class_name(source: bytes, node: Any) -> str:
    by_field = node.child_by_field_name("name")
    if by_field is not None:
        return _node_text(source, by_field)
    child = _first_named_child(node, {"type_identifier", "identifier", "simple_identifier"})
    return _node_text(source, child) if child is not None else ""


def _function_name(source: bytes, node: Any, language: str) -> str:
    by_field = node.child_by_field_name("name")
    if by_field is not None:
        return _node_text(source, by_field)
    if language == "kotlin":
        child = _first_named_child(node, {"simple_identifier", "identifier"})
        return _node_text(source, child) if child is not None else ""
    child = _first_named_child(node, {"identifier"})
    return _node_text(source, child) if child is not None else ""


def _signature(source: bytes, node: Any) -> str:
    params = node.child_by_field_name("parameters")
    if params is None:
        params = _first_named_child(node, {"function_value_parameters", "formal_parameters"})
    return " ".join(_node_text(source, params).strip("()").split()) if params is not None else ""


def _arity(signature: str) -> int:
    if not signature.strip():
        return 0
    depth = 0
    count = 1
    for ch in signature:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            count += 1
    return count


def _symbol_id(package: str, owners: list[str], function_name: str, signature: str) -> str:
    owner = ".".join([x for x in [package, *owners] if x])
    return f"fn:{owner}.{function_name}/{_arity(signature)}" if owner else f"fn:{function_name}/{_arity(signature)}"


def _is_function_node(node: Any) -> bool:
    return node.type in {"function_declaration", "method_declaration", "constructor_declaration"}


def _call_name(source: bytes, node: Any) -> str:
    if node.type == "method_invocation":
        name = node.child_by_field_name("name")
        return _node_text(source, name) if name is not None else _last_identifier_text(source, node)
    if node.type == "object_creation_expression":
        typ = node.child_by_field_name("type") or _first_named_child(node, {"type_identifier", "scoped_type_identifier"})
        return _last_identifier_text(source, typ) if typ is not None else ""
    if node.type == "call_expression":
        first = node.named_children[0] if node.named_children else None
        return _last_identifier_text(source, first) if first is not None else ""
    return ""


def _call_arg_count(source: bytes, node: Any) -> int:
    args = node.child_by_field_name("arguments")
    if args is None:
        args = _first_named_child(node, {"value_arguments", "argument_list"})
    if args is None:
        return 0
    return sum(1 for c in args.named_children if c.type not in {",", "(", ")"})


def _call_receiver(source: bytes, node: Any) -> str:
    if node.type != "call_expression" or not node.named_children:
        return ""
    first = node.named_children[0]
    text = _node_text(source, first)
    if "." not in text:
        return ""
    return text.rsplit(".", 1)[0].strip()


def _symbol_for_call(caller: dict[str, Any], name: str, by_name: dict[str, list[dict[str, Any]]]) -> tuple[str, str]:
    candidates = by_name.get(name) or []
    if not candidates:
        return "", "unresolved"
    same_class = [s for s in candidates if s.get("class_name") == caller.get("class_name")]
    if len(same_class) == 1:
        return str(same_class[0]["symbol_id"]), "ast_same_class"
    if len(candidates) == 1:
        return str(candidates[0]["symbol_id"]), "ast_unique_name"
    return "", "ambiguous"


def _extract_target_class(text: str) -> str:
    for pattern in (
        r"\b([A-Z]\w*)::class(?:\.java)?",
        r"\b([A-Z]\w*(?:Activity|Fragment|Dialog|BottomSheet))\s*\(",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return ""


def _is_dialog_like(name: str) -> bool:
    return bool(re.search(r"(Dialog|DialogFragment|BottomSheet)$", name))


def _layout_for_class(class_name: str, resolver: Any | None) -> str:
    if resolver:
        try:
            return str(resolver(class_name))
        except Exception:
            return ""
    return ""


def build_project_index(
    project_root: str,
    file_prefix: str = "",
    *,
    layout_resolver: Any | None = None,
) -> AstProjectIndex:
    index = AstProjectIndex(project_root=str(project_root), file_prefix=file_prefix)
    if get_parser is None:
        return index

    root = Path(project_root)
    file_roots: list[tuple[Path, bytes, str, str, Any]] = []
    for src_path in _source_files(root):
        language = _language_for(src_path)
        if not language:
            continue
        try:
            parser = get_parser(language)
            source = src_path.read_bytes()
            tree = parser.parse(source)
        except Exception:
            continue
        rel = _rel_path(src_path, root, file_prefix)
        file_roots.append((src_path, source, language, rel, tree.root_node))
        package = _package_name(source, tree.root_node, language)
        fallback_owner = src_path.stem
        for node in _walk(tree.root_node):
            if not _is_function_node(node):
                continue
            function_name = _function_name(source, node, language)
            if not function_name:
                continue
            signature = _signature(source, node)
            owners = _owner_stack(source, node, fallback_owner)
            symbol_id = _symbol_id(package, owners, function_name, signature)
            class_name = owners[-1] if owners else fallback_owner
            index.symbols.append(
                {
                    "symbol_id": symbol_id,
                    "kind": "function",
                    "language": language,
                    "package": package,
                    "class_name": class_name,
                    "owner_chain": owners,
                    "function_name": function_name,
                    "signature": signature,
                    "file": rel,
                    "start_line": _line(node),
                    "end_line": _end_line(node),
                    "confidence": "ast",
                }
            )

    by_name: dict[str, list[dict[str, Any]]] = {}
    by_file: dict[str, list[dict[str, Any]]] = {}
    for sym in index.symbols:
        by_name.setdefault(str(sym.get("function_name")), []).append(sym)
        by_file.setdefault(str(sym.get("file")), []).append(sym)

    seen_calls: set[tuple[str, str, int, str]] = set()
    for _src_path, source, _language, rel, root_node in file_roots:
        file_symbols = by_file.get(rel, [])
        for node in _walk(root_node):
            if node.type not in {"call_expression", "method_invocation", "object_creation_expression"}:
                continue
            callee_name = _call_name(source, node)
            if not callee_name or callee_name in _SKIP_CALLS:
                continue
            line = _line(node)
            caller = next(
                (
                    s
                    for s in file_symbols
                    if int(s.get("start_line", 0)) <= line <= int(s.get("end_line", 0))
                    and s.get("function_name") != callee_name
                ),
                None,
            )
            if caller is None:
                continue
            to_symbol, confidence = _symbol_for_call(caller, callee_name, by_name)
            if to_symbol:
                key = (str(caller["symbol_id"]), to_symbol, line, callee_name)
                if key not in seen_calls:
                    seen_calls.add(key)
                    index.calls.append(
                        {
                            "from_symbol_id": caller["symbol_id"],
                            "to_symbol_id": to_symbol,
                            "callee_name": callee_name,
                            "receiver": _call_receiver(source, node),
                            "argument_count": _call_arg_count(source, node),
                            "callsite_file": rel,
                            "callsite_line": line,
                            "confidence": confidence,
                        }
                    )
            else:
                index.unresolved_calls.append(
                    {
                        "from_symbol_id": caller["symbol_id"],
                        "callee_name": callee_name,
                        "receiver": _call_receiver(source, node),
                        "argument_count": _call_arg_count(source, node),
                        "callsite_file": rel,
                        "callsite_line": line,
                        "reason": confidence,
                    }
                )

            text = _node_text(source, node)
            from_class = str(caller.get("class_name") or "")
            trigger = f"fn: {caller.get('function_name')}"
            if callee_name in {"startActivity", "startActivityForResult"}:
                target = _extract_target_class(text)
                if target:
                    index.navigation_edges.append(
                        {
                            "from": from_class,
                            "to": target,
                            "to_layout": _layout_for_class(target, layout_resolver),
                            "type": "activity",
                            "via": f"{callee_name} [ast]",
                            "trigger": trigger,
                            "line": line,
                            "source_file": rel,
                            "source": "ast_index",
                            "confidence": "ast",
                        }
                    )
            elif _is_dialog_like(callee_name):
                index.navigation_edges.append(
                    {
                        "from": from_class,
                        "to": callee_name,
                        "to_layout": _layout_for_class(callee_name, layout_resolver),
                        "type": "dialog",
                        "via": "Dialog() [ast]",
                        "trigger": trigger,
                        "line": line,
                        "source_file": rel,
                        "source": "ast_index",
                        "confidence": "ast",
                    }
                )

    index.ast_available = bool(index.symbols)
    index.symbols.sort(key=lambda s: (s["file"], s["start_line"], s["symbol_id"]))
    index.calls.sort(key=lambda c: (c["callsite_file"], c["callsite_line"], c["from_symbol_id"], c["callee_name"]))
    index.unresolved_calls.sort(key=lambda c: (c["callsite_file"], c["callsite_line"], c["from_symbol_id"], c["callee_name"]))
    return index


def symbols_payload(index: AstProjectIndex) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "symbols": index.symbols,
        "stats": {
            "source_files_scanned": len(_source_files(Path(index.project_root))),
            "symbol_count": len(index.symbols),
            "ast_symbol_count": sum(1 for s in index.symbols if s.get("confidence") == "ast"),
        },
    }


def call_graph_payload(index: AstProjectIndex) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "symbols": index.symbols,
        "calls": index.calls,
        "unresolved_calls": index.unresolved_calls,
        "stats": {
            "symbol_count": len(index.symbols),
            "call_count": len(index.calls),
            "unresolved_call_count": len(index.unresolved_calls),
            "ast_call_count": sum(1 for c in index.calls if str(c.get("confidence", "")).startswith("ast")),
        },
    }
