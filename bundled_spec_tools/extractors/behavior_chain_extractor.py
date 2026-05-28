"""
behavior_chain_extractor.py
Extract event→effect chains by combining source_findings event_registrations
with call_graph data.

For each event_registration (e.g. btn.setOnClickListener { ... }):
  1. Extract the handler lambda/method body from source text
  2. Find method calls inside the handler body
  3. Follow each call through the call_graph (max 3 levels)
  4. Classify each call as navigate / ui_feedback / ui_update / async / call
  5. Detect condition branches in the handler source
"""
from __future__ import annotations

import re
from pathlib import Path

# ── Effect classifiers ────────────────────────────────────────────────────────

_NAVIGATE_RE = re.compile(
    r'^(?:startActivity|startActivityForResult|navigate|findNavController|'
    r'pushUrl|NavHostFragment|popBackStack|navigateUp|finish)$'
)

_UI_FEEDBACK_RE = re.compile(
    r'^(?:makeText|show|showDialog|Toast|AlertDialog|Snackbar|Builder|'
    r'showSnackbar|showToast|dismiss)$',
    re.IGNORECASE,
)

_UI_UPDATE_RE = re.compile(
    r'^(?:setVisibility|setText|setImageResource|setEnabled|setChecked|'
    r'isVisible|beVisible|beGone|beInvisible|setAlpha|animate|'
    r'notifyDataSetChanged|notifyItemChanged|submitList|invalidate)$'
)

_ASYNC_RE = re.compile(
    r'^(?:launch|async|withContext|enqueue|execute|postDelayed|'
    r'runOnUiThread|post|subscribeOn|observeOn)$'
)


def _classify_call(method_name: str) -> str:
    if _NAVIGATE_RE.match(method_name):
        return "navigate"
    if _UI_FEEDBACK_RE.match(method_name):
        return "ui_feedback"
    if _UI_UPDATE_RE.match(method_name):
        return "ui_update"
    if _ASYNC_RE.match(method_name):
        return "async"
    return "call"


# ── Navigate destination extraction ───────────────────────────────────────────

_INTENT_DEST_RE = re.compile(
    r'Intent\s*\(\s*\w+\s*,\s*(\w+)\s*(?:::\s*class\s*\.?\s*java|\.class)',
)

_NAV_DEST_RE = re.compile(
    r'navigate\s*\(\s*R\.id\.(\w+)',
)


def _extract_navigate_destination(body: str, call_line: int) -> str:
    for pat in (_INTENT_DEST_RE, _NAV_DEST_RE):
        m = pat.search(body)
        if m:
            return m.group(1)
    return ""


# ── Handler body extraction ───────────────────────────────────────────────────

def _extract_handler_body(source_lines: list[str], reg_line: int) -> tuple[str, int, int]:
    """Extract the lambda/handler body starting near the registration line.

    Returns (body_text, start_line_1based, end_line_1based).
    Handles both single-line `{ openSettings() }` and multi-line lambdas.
    """
    if reg_line < 1 or reg_line > len(source_lines):
        return "", 0, 0

    search_start = max(0, reg_line - 1)
    search_end = min(len(source_lines), reg_line + 5)
    joined = "\n".join(source_lines[search_start:search_end])

    brace_pos = joined.find("{")
    if brace_pos < 0:
        return "", 0, 0

    depth = 0
    body_start = brace_pos + 1
    for i in range(brace_pos, len(joined)):
        if joined[i] == "{":
            depth += 1
        elif joined[i] == "}":
            depth -= 1
            if depth == 0:
                body = joined[body_start:i].strip()
                start_1 = search_start + joined[:body_start].count("\n") + 1
                end_1 = search_start + joined[:i].count("\n") + 1
                return body, start_1, end_1

    full_text = "\n".join(source_lines[search_start:])
    for i in range(brace_pos, len(full_text)):
        if full_text[i] == "{":
            depth += 1
        elif full_text[i] == "}":
            depth -= 1
            if depth == 0:
                body = full_text[body_start:i].strip()
                start_1 = search_start + full_text[:body_start].count("\n") + 1
                end_1 = search_start + full_text[:i].count("\n") + 1
                return body, start_1, end_1

    return "", 0, 0


# ── Call extraction from handler body ─────────────────────────────────────────

_METHOD_CALL_RE = re.compile(r'(?:^|[^\w.])(\w+)\s*\(', re.MULTILINE)


def _extract_calls_from_body(body: str) -> list[str]:
    """Extract simple method call names from handler body text."""
    skip = {"if", "for", "while", "when", "switch", "catch", "return",
            "throw", "super", "this", "class", "fun", "val", "var",
            "let", "apply", "also", "run", "with", "Intent", "Bundle"}
    calls = []
    seen = set()
    for m in _METHOD_CALL_RE.finditer(body):
        name = m.group(1)
        if name not in skip and name not in seen and not name[0].isupper():
            seen.add(name)
            calls.append(name)
    return calls


# ── Condition branch detection ────────────────────────────────────────────────

_CONDITION_RE = re.compile(
    r'\b(if|when)\s*\(([^)]{1,120})\)\s*\{',
    re.MULTILINE,
)


def _detect_conditions(body: str) -> list[dict]:
    """Detect if/when condition blocks in handler body."""
    conditions = []
    for m in _CONDITION_RE.finditer(body):
        keyword = m.group(1)
        expr = m.group(2).strip()
        line_in_body = body[:m.start()].count("\n")
        conditions.append({
            "keyword": keyword,
            "expr": expr,
            "offset": m.start(),
            "line_offset": line_in_body,
        })
    return conditions


# ── Core chain builder ────────────────────────────────────────────────────────

def _build_effect_chain(
    method_calls: list[str],
    handler_body: str,
    calls_by_from: dict[str, list[dict]],
    symbols_by_id: dict[str, dict],
    symbols_by_class_method: dict[tuple[str, str], dict],
    handler_class: str,
    source_lines: list[str],
    source_offset: int,
    depth: int = 0,
    max_depth: int = 3,
    visited: set | None = None,
    step_counter: dict | None = None,
) -> list[dict]:
    if visited is None:
        visited = set()
    if step_counter is None:
        step_counter = {}

    steps: list[dict] = []

    for call_name in method_calls:
        kind = _classify_call(call_name)

        if kind == "navigate":
            dest = _extract_navigate_destination(handler_body, 0)
            step = {
                "step": "navigate",
                "target": call_name,
                "destination": dest,
                "via": call_name,
                "confidence": "static_analysis",
            }
            steps.append(step)
            step_counter["navigate"] = step_counter.get("navigate", 0) + 1

        elif kind == "ui_feedback":
            step = {
                "step": "ui_feedback",
                "action": call_name,
                "confidence": "static_analysis",
            }
            steps.append(step)
            step_counter["ui_feedback"] = step_counter.get("ui_feedback", 0) + 1

        elif kind == "ui_update":
            step = {
                "step": "ui_update",
                "action": call_name,
                "confidence": "static_analysis",
            }
            steps.append(step)
            step_counter["ui_update"] = step_counter.get("ui_update", 0) + 1

        elif kind == "async":
            step = {
                "step": "async",
                "target": call_name,
                "confidence": "static_analysis",
            }
            steps.append(step)
            step_counter["async"] = step_counter.get("async", 0) + 1

        else:
            sym = symbols_by_class_method.get((handler_class, call_name))
            if sym is None:
                for key, s in symbols_by_class_method.items():
                    if key[1] == call_name:
                        sym = s
                        break

            sym_id = sym["symbol_id"] if sym else ""

            step: dict = {
                "step": "call",
                "target": f"{call_name}()",
                "confidence": "static_analysis" if sym else "inferred",
            }
            if sym_id:
                step["symbol_id"] = sym_id

            if sym_id and sym_id not in visited and depth < max_depth:
                visited.add(sym_id)
                child_calls_raw = calls_by_from.get(sym_id, [])
                child_call_names = [c.get("callee_name", "") for c in child_calls_raw if c.get("callee_name")]

                child_body = ""
                child_lines = source_lines
                if sym:
                    try:
                        f = sym.get("file", "")
                        sl = int(sym.get("start_line", 0))
                        el = int(sym.get("end_line", 0))
                        if f and sl and el:
                            raw = "\n".join(source_lines[sl - 1:el])
                            brace = raw.find("{")
                            child_body = raw[brace + 1:].strip() if brace >= 0 else raw
                    except (IndexError, ValueError):
                        pass

                if child_body:
                    body_calls = _extract_calls_from_body(child_body)
                    seen_names = set(child_call_names)
                    for bc_name in body_calls:
                        if bc_name not in seen_names:
                            child_call_names.append(bc_name)
                            seen_names.add(bc_name)

                child_steps = _build_effect_chain(
                    child_call_names,
                    child_body or handler_body,
                    calls_by_from,
                    symbols_by_id,
                    symbols_by_class_method,
                    sym.get("class_name", handler_class) if sym else handler_class,
                    source_lines,
                    source_offset,
                    depth=depth + 1,
                    max_depth=max_depth,
                    visited=visited,
                    step_counter=step_counter,
                )
                if child_steps:
                    step["nested"] = child_steps

            steps.append(step)
            step_counter["call"] = step_counter.get("call", 0) + 1

    conditions = _detect_conditions(handler_body) if depth == 0 else []
    if conditions:
        for cond in conditions:
            step_counter["condition"] = step_counter.get("condition", 0) + 1

    return steps


# ── view_ref → element_id resolution ─────────────────────────────────────────

_ID_FIND_RE = re.compile(
    r'(?:val|var)\s+(\w+)\s*(?::\s*\w+)?\s*=\s*'
    r'(?:\w+\s*\.\s*)?findViewById\w*\s*(?:<[^>]*>\s*)?\(\s*R\.id\.(\w+)',
)


def _build_viewref_to_id(source: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for m in _ID_FIND_RE.finditer(source):
        mapping[m.group(1)] = m.group(2)
    return mapping


# ── Entry point ───────────────────────────────────────────────────────────────

def run(
    source_findings: dict,
    call_graph: dict,
    project_root: str,
    file_prefix: str = "",
) -> dict:
    root = Path(project_root)

    calls_by_from: dict[str, list[dict]] = {}
    symbols_by_id: dict[str, dict] = {}
    symbols_by_class_method: dict[tuple[str, str], dict] = {}

    for sym in call_graph.get("symbols", []):
        sid = sym.get("symbol_id", "")
        if sid:
            symbols_by_id[sid] = sym
            cls = sym.get("class_name", "")
            fn = sym.get("function_name", "")
            if cls and fn:
                symbols_by_class_method[(cls, fn)] = sym

    for edge in call_graph.get("calls", []):
        from_id = edge.get("from_symbol_id", "")
        if from_id:
            calls_by_from.setdefault(from_id, []).append(edge)

    event_regs = source_findings.get("findings", {}).get("event_registrations", [])

    file_cache: dict[str, list[str]] = {}

    def _read_lines(rel_path: str) -> list[str]:
        if rel_path in file_cache:
            return file_cache[rel_path]
        fp = root / rel_path
        if not fp.is_file():
            fp = root / rel_path.replace("\\", "/")
        if not fp.is_file():
            file_cache[rel_path] = []
            return []
        try:
            lines = fp.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            lines = []
        file_cache[rel_path] = lines
        return lines

    behavior_chains: list[dict] = []
    without_handler = 0
    max_depth_seen = 0

    for reg in event_regs:
        view_ref = reg.get("view_ref", "")
        event_type = reg.get("event_type", "click")
        reg_file = reg.get("file", "")
        reg_line = reg.get("line", 0)
        enclosing_sym_id = reg.get("enclosing_symbol_id", "")

        source_lines = _read_lines(reg_file)
        if not source_lines:
            without_handler += 1
            continue

        viewref_map = _build_viewref_to_id("\n".join(source_lines))
        element_id = viewref_map.get(view_ref, "")

        handler_body, body_start, body_end = _extract_handler_body(source_lines, reg_line)
        if not handler_body:
            without_handler += 1
            continue

        enclosing_sym = symbols_by_id.get(enclosing_sym_id, {})
        handler_class = enclosing_sym.get("class_name", "")

        handler_calls = _extract_calls_from_body(handler_body)

        step_counter: dict[str, int] = {}
        visited: set[str] = set()
        if enclosing_sym_id:
            visited.add(enclosing_sym_id)

        effect_chain = _build_effect_chain(
            handler_calls,
            handler_body,
            calls_by_from,
            symbols_by_id,
            symbols_by_class_method,
            handler_class,
            source_lines,
            body_start,
            depth=0,
            max_depth=3,
            visited=visited,
            step_counter=step_counter,
        )

        chain_depth = 0
        def _measure_depth(steps, d=1):
            nonlocal chain_depth
            chain_depth = max(chain_depth, d)
            for s in steps:
                if "nested" in s:
                    _measure_depth(s["nested"], d + 1)
        _measure_depth(effect_chain)
        max_depth_seen = max(max_depth_seen, chain_depth)

        handler_info = {}
        if enclosing_sym_id:
            handler_info = {
                "symbol_id": enclosing_sym_id,
                "method": enclosing_sym.get("function_name", ""),
                "file": reg_file,
                "line": reg_line,
            }

        chain_record = {
            "element_id": element_id,
            "view_ref": view_ref,
            "event_type": event_type,
            "handler": handler_info,
            "effect_chain": effect_chain,
            "chain_depth": chain_depth,
            "confidence": "static_analysis" if effect_chain else "no_chain",
        }
        behavior_chains.append(chain_record)

    total_by_step: dict[str, int] = {}
    for bc in behavior_chains:
        def _count_steps(steps):
            for s in steps:
                st = s.get("step", "")
                total_by_step[st] = total_by_step.get(st, 0) + 1
                if "nested" in s:
                    _count_steps(s["nested"])
                if "then" in s:
                    _count_steps(s["then"])
                if "else" in s:
                    _count_steps(s["else"])
        _count_steps(bc.get("effect_chain", []))

    return {
        "behavior_chains": behavior_chains,
        "stats": {
            "total_bindings": len(event_regs),
            "with_effect_chain": sum(1 for bc in behavior_chains if bc["effect_chain"]),
            "without_handler": without_handler,
            "max_chain_depth": max_depth_seen,
            "by_step_type": total_by_step,
        },
    }
