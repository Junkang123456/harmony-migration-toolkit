"""
behavior_chain_extractor.py
Three-layer architecture:
  Layer 1 — Step Pipeline: pure functions for call classification, step building,
             call-graph traversal. No Android-specific assumptions.
  Layer 2 — Body Parser: text-based handler body extraction, condition splitting.
             No call-graph dependency.
  Layer 3 — Specialized Extractors: event-chain, lifecycle-hook extraction.
             Combines Layer 1 + 2 for concrete Android scenarios.

Extends to new behavior types by adding a Layer-3 extractor that reuses
the same classification and traversal pipeline.
"""
from __future__ import annotations

import re
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
# Layer 1 — Step Pipeline (reusable core, no Android specifics)
# ═══════════════════════════════════════════════════════════════════════════════

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

_LIFECYCLE_METHODS = frozenset({
    "onCreate", "onStart", "onResume", "onPause",
    "onStop", "onDestroy", "onRestart",
    "onCreateView", "onViewCreated", "onActivityCreated",
    "onAttach", "onDetach",
    "onSaveInstanceState", "onRestoreInstanceState",
})


def classify_call(method_name: str) -> str:
    """Classify a method call name into one of the known step types."""
    if _NAVIGATE_RE.match(method_name):
        return "navigate"
    if _UI_FEEDBACK_RE.match(method_name):
        return "ui_feedback"
    if _UI_UPDATE_RE.match(method_name):
        return "ui_update"
    if _ASYNC_RE.match(method_name):
        return "async"
    return "call"


# ── Brace matching ──────────────────────────────────────────────────────────

def extract_braced_block(text: str, brace_pos: int) -> tuple[str, int]:
    """Extract content inside the first matching brace pair starting at brace_pos.

    Returns (inner_content_stripped, end_pos_exclusive).
    Returns ("", 0) if no matching brace is found.
    """
    if brace_pos < 0 or brace_pos >= len(text) or text[brace_pos] != "{":
        return "", 0
    depth = 0
    for i in range(brace_pos, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[brace_pos + 1:i].strip(), i + 1
    return "", 0


def get_child_calls_from_graph(sym_id: str, calls_by_from: dict[str, list[dict]]) -> list[str]:
    """Return deduplicated callee names for a given symbol from the call graph."""
    return list(dict.fromkeys(
        c.get("callee_name", "")
        for c in calls_by_from.get(sym_id, [])
        if c.get("callee_name")
    ))


def resolve_symbol(
    call_name: str,
    handler_class: str,
    symbols_by_class_method: dict[tuple[str, str], dict],
) -> dict | None:
    """Resolve a call name to its function symbol, trying exact class match first."""
    sym = symbols_by_class_method.get((handler_class, call_name))
    if sym is None:
        for key, s in symbols_by_class_method.items():
            if key[1] == call_name:
                sym = s
                break
    return sym


# ── Navigate destination extraction ─────────────────────────────────────────

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


def _read_source_lines(root: Path, rel_path: str, file_cache: dict[str, list[str]]) -> list[str]:
    """Cached file reader used across all extractors."""
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


# ── Step builder (single call → step dict) ─────────────────────────────────

# Regex patterns for parameter extraction
_STRING_LITERAL_RE = re.compile(r"""["'](\w+)['"]""")
_MESSAGE_RE = re.compile(r"""["']([^"']{0,80})["']""")
_VISIBILITY_RE = re.compile(r'setVisibility\s*\(.*?\.(\w+)\)')
_SET_TEXT_RE = re.compile(r'setText\s*\(\s*["\']([^"\']{0,80})')


def _extract_call_params(call_name: str, context_body: str) -> dict[str, str]:
    """Extract common parameters from a call in the context body."""
    params: dict[str, str] = {}
    if not context_body:
        return params

    # Destination for navigation (Intent / startActivity patterns)
    if call_name in ("startActivity", "startActivityForResult"):
        m = re.search(r'Intent\s*\(\s*\w+\s*,\s*(\w+)\s*(?:\.class|::class\.java)', context_body)
        if m:
            params["destination"] = m.group(1)

    # String message for ui_feedback (Toast, Dialog, Snackbar)
    if call_name in ("makeText", "show", "showDialog", "showSnackbar"):
        ms = list(_MESSAGE_RE.finditer(context_body))
        if ms:
            params["message"] = ms[-1].group(1)

    # Visibility value
    if call_name == "setVisibility":
        m = _VISIBILITY_RE.search(context_body)
        if m:
            params["value"] = m.group(1).lower()

    # Text value
    if call_name == "setText":
        m = _SET_TEXT_RE.search(context_body)
        if m:
            params["text"] = m.group(1)

    return params


def build_step_for_call(
    call_name: str,
    context_body: str,
    handler_class: str,
    calls_by_from: dict[str, list[dict]],
    symbols_by_id: dict[str, dict],
    symbols_by_class_method: dict[tuple[str, str], dict],
    source_lines: list[str],
    depth: int,
    max_depth: int,
    visited: set[str],
    step_counter: dict[str, int],
) -> dict:
    """Classify a single call name and build a step dict, recursing into the
    call graph up to max_depth."""
    kind = classify_call(call_name)
    step_counter[kind] = step_counter.get(kind, 0) + 1
    params = _extract_call_params(call_name, context_body)

    if kind == "navigate":
        return {
            "step": "navigate",
            "target": call_name,
            "destination": params.get("destination", _extract_navigate_destination(context_body, 0)),
            "via": call_name,
            "confidence": "static_analysis",
        }

    if kind == "ui_feedback":
        step = {
            "step": "ui_feedback",
            "action": call_name,
            "confidence": "static_analysis",
        }
        if params.get("message"):
            step["message"] = params["message"]
        return step

    if kind == "ui_update":
        step = {
            "step": "ui_update",
            "action": call_name,
            "confidence": "static_analysis",
        }
        if params.get("value"):
            step["value"] = params["value"]
        if params.get("text"):
            step["text"] = params["text"]
        return step

    if kind == "async":
        return {
            "step": "async",
            "target": call_name,
            "confidence": "static_analysis",
        }

    # kind == "call"
    sym = resolve_symbol(call_name, handler_class, symbols_by_class_method)
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
        child_call_names = get_child_calls_from_graph(sym_id, calls_by_from)

        child_body = ""
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

        child_steps = _build_flat_chain(
            child_call_names,
            child_body or context_body,
            sym.get("class_name", handler_class) if sym else handler_class,
            calls_by_from,
            symbols_by_id,
            symbols_by_class_method,
            source_lines,
            depth=depth + 1,
            max_depth=max_depth,
            visited=visited,
            step_counter=step_counter,
        )
        if child_steps:
            step["nested"] = child_steps

    return step


def _build_flat_chain(
    call_names: list[str],
    context_body: str,
    handler_class: str,
    calls_by_from: dict[str, list[dict]],
    symbols_by_id: dict[str, dict],
    symbols_by_class_method: dict[tuple[str, str], dict],
    source_lines: list[str],
    depth: int = 0,
    max_depth: int = 3,
    visited: set[str] | None = None,
    step_counter: dict[str, int] | None = None,
) -> list[dict]:
    """Build a flat chain of steps from a list of call names (no condition branching)."""
    if visited is None:
        visited = set()
    if step_counter is None:
        step_counter = {}

    steps = []
    for name in call_names:
        step = build_step_for_call(
            name, context_body, handler_class,
            calls_by_from, symbols_by_id, symbols_by_class_method,
            source_lines, depth, max_depth, visited, step_counter,
        )
        steps.append(step)
    return steps


# ═══════════════════════════════════════════════════════════════════════════════
# Layer 2 — Body Parser (text → structured segments, no call-graph dependency)
# ═══════════════════════════════════════════════════════════════════════════════

_METHOD_CALL_RE = re.compile(r'(?:^|[^\w.])(\w+)\s*\(', re.MULTILINE)

_SKIP_CALLS = frozenset({
    "if", "for", "while", "when", "switch", "catch", "return",
    "throw", "super", "this", "class", "fun", "val", "var",
    "let", "apply", "also", "run", "with", "Intent", "Bundle",
})


def _extract_calls_from_body(body: str) -> list[str]:
    """Extract simple method call names from body text."""
    calls = []
    seen = set()
    for m in _METHOD_CALL_RE.finditer(body):
        name = m.group(1)
        if name not in _SKIP_CALLS and name not in seen and not name[0].isupper():
            seen.add(name)
            calls.append(name)
    return calls


_CONDITION_RE = re.compile(
    r'\b(if|when)\s*\(((?:[^()]|\([^()]*\))*)\)\s*\{',
    re.MULTILINE,
)


def _detect_conditions(body: str) -> list[dict]:
    """Detect if/when condition blocks in handler body. Returns sorted by offset."""
    conditions = []
    for m in _CONDITION_RE.finditer(body):
        conditions.append({
            "keyword": m.group(1),
            "expr": m.group(2).strip(),
            "offset": m.start(),
        })
    return conditions


def _find_next_non_whitespace(text: str, start: int) -> int:
    """Return index of first non-whitespace character at or after start."""
    for i in range(start, len(text)):
        if not text[i].isspace():
            return i
    return len(text)


def _scan_past_else_chain(text: str, else_pos: int) -> int:
    """Starting from an 'else' keyword at else_pos, scan past the entire
    else / else-if chain, returning the position right after the last '}'.

    Handles: else { }, else if (...) { }, else if ... else { }, etc.
    """
    pos = else_pos
    while pos < len(text):
        remainder = text[pos:].lstrip()
        ws_skip = len(text[pos:]) - len(remainder)
        pos = pos + ws_skip
        remainder = text[pos:]

        if remainder.startswith("else if"):
            # Skip "else if"
            cond_match = _CONDITION_RE.search(remainder)
            if not cond_match:
                pos += 4  # skip "else"
                continue
            # Find { for the body
            brace_at = remainder.find("{", cond_match.end())
            if brace_at < 0:
                pos += cond_match.end()
                continue
            _, body_end = extract_braced_block(remainder, brace_at)
            pos = pos + body_end
            # Continue to check for more else/else-if
        elif remainder.startswith("else"):
            # Find { for the body
            brace_at = remainder.find("{", 4)
            if brace_at >= 0:
                _, body_end = extract_braced_block(remainder, brace_at)
                return pos + body_end
            # No braces (Kotlin style), consume rest
            return len(text)
        else:
            # Not an else; we're done
            return pos
    return pos


def split_body_by_conditions(body: str) -> list[dict]:
    """Split handler body text into structured segments.

    Returns a list of segment dicts:
      {"type": "plain", "text": "..."}
      {"type": "condition", "expr": "...",
       "then_segments": [...],   (recursive)
       "else_segments": [...]}   (recursive, may be empty)

    Handles if-else, if-else if-else chains, and nested conditions recursively.
    """
    if not body:
        return []

    conditions = _detect_conditions(body)
    if not conditions:
        return [{"type": "plain", "text": body}]

    segments = []
    cursor = 0

    for cond in sorted(conditions, key=lambda c: c["offset"]):
        if cond["offset"] < cursor:
            continue

        # Plain text before this condition
        if cond["offset"] > cursor:
            pre = body[cursor:cond["offset"]].strip()
            if pre:
                segments.append({"type": "plain", "text": pre})

        # Find the '{' after the condition expression
        cond_text = body[cond["offset"]:]
        brace_pos = cond_text.find("{")
        if brace_pos < 0:
            cursor = cond["offset"] + 1
            continue

        then_body, then_end = extract_braced_block(cond_text, brace_pos)

        # Recurse into then body
        then_segments = split_body_by_conditions(then_body) if then_body else []

        # Consume else chain after the if-block
        else_segments = []
        scan_pos = then_end
        while scan_pos < len(cond_text):
            pos = _find_next_non_whitespace(cond_text, scan_pos)
            if pos >= len(cond_text):
                break
            remainder = cond_text[pos:]
            if remainder.startswith("else"):
                # Skip "else" and whitespace; split_body_by_conditions on
                # the remainder will naturally find if/when and produce
                # condition segments for the else chain.
                after_else = remainder[4:]
                tail_segs = split_body_by_conditions(after_else)
                # Remove any leading plain segment that's just whitespace
                clean = []
                for seg in tail_segs:
                    if seg["type"] == "plain" and not seg["text"].strip():
                        continue
                    clean.append(seg)
                else_segments = clean
                scan_pos = _scan_past_else_chain(cond_text, pos)
                break
            else:
                break

        if else_segments:
            then_end = scan_pos

        segments.append({
            "type": "condition",
            "expr": cond["expr"],
            "then_segments": then_segments,
            "else_segments": else_segments,
        })
        cursor = cond["offset"] + then_end

    # Plain text after last condition
    if cursor < len(body):
        post = body[cursor:].strip()
        if post:
            segments.append({"type": "plain", "text": post})

    return segments


def build_chain_from_segments(
    segments: list[dict],
    handler_class: str,
    calls_by_from: dict[str, list[dict]],
    symbols_by_id: dict[str, dict],
    symbols_by_class_method: dict[tuple[str, str], dict],
    source_lines: list[str],
    depth: int = 0,
    max_depth: int = 3,
    visited: set[str] | None = None,
    step_counter: dict[str, int] | None = None,
) -> list[dict]:
    """Convert structured segments from split_body_by_conditions into an effect chain.

    This is the condition-aware replacement for _build_flat_chain.
    Plain segments produce flat steps; condition segments produce
    {step: condition, then/else} nodes.
    """
    if visited is None:
        visited = set()
    if step_counter is None:
        step_counter = {}

    chain = []
    for seg in segments:
        if seg["type"] == "plain":
            call_names = _extract_calls_from_body(seg["text"])
            for name in call_names:
                step = build_step_for_call(
                    name, seg["text"], handler_class,
                    calls_by_from, symbols_by_id, symbols_by_class_method,
                    source_lines, depth, max_depth, visited, step_counter,
                )
                chain.append(step)

        elif seg["type"] == "condition":
            then_chain = build_chain_from_segments(
                seg["then_segments"], handler_class,
                calls_by_from, symbols_by_id, symbols_by_class_method,
                source_lines, depth + 1, max_depth, visited, step_counter,
            )
            else_chain = build_chain_from_segments(
                seg["else_segments"], handler_class,
                calls_by_from, symbols_by_id, symbols_by_class_method,
                source_lines, depth + 1, max_depth, visited, step_counter,
            ) if seg.get("else_segments") else []

            step_counter["condition"] = step_counter.get("condition", 0) + 1
            chain.append({
                "step": "condition",
                "expr": seg["expr"],
                "then": then_chain,
                "else": else_chain,
            })

    return chain


# ── Handler body extraction (event-registration specific) ───────────────────

def extract_handler_body(source_lines: list[str], reg_line: int) -> tuple[str, int, int]:
    """Extract the lambda/handler body starting near the registration line.

    Returns (body_text, start_line_1based, end_line_1based).
    Handles single-line `{ openSettings() }`, multi-line lambdas,
    and arrow lambdas without braces (`v -> doSomething()`).
    """
    if reg_line < 1 or reg_line > len(source_lines):
        return "", 0, 0

    search_start = max(0, reg_line - 1)
    search_end = min(len(source_lines), reg_line + 5)
    joined = "\n".join(source_lines[search_start:search_end])

    brace_pos = joined.find("{")

    # Check for arrow lambda without braces on the registration line itself
    reg_text = source_lines[reg_line - 1]
    arrow_m = re.search(r'->\s*(.+?)\s*\)\s*;', reg_text)
    if arrow_m:
        arrow_body = arrow_m.group(1).strip()
        if arrow_body and (brace_pos < 0 or joined.find("->") < brace_pos):
            return arrow_body, reg_line, reg_line

    if brace_pos < 0:
        return "", 0, 0

    body, end_pos = extract_braced_block(joined, brace_pos)
    if body:
        start_1 = search_start + joined[:brace_pos + 1].count("\n") + 1
        end_1 = search_start + joined[:end_pos].count("\n") + 1
        return body, start_1, end_1

    # If not found in limited window, try expanding to rest of file
    full_text = "\n".join(source_lines[search_start:])
    body, end_pos = extract_braced_block(full_text, brace_pos)
    if body:
        start_1 = search_start + full_text[:brace_pos + 1].count("\n") + 1
        end_1 = search_start + full_text[:end_pos].count("\n") + 1
        return body, start_1, end_1

    return "", 0, 0


def _try_method_reference_fallback(
    reg: dict,
    symbols_by_id: dict[str, dict],
    source_lines: list[str],
) -> str:
    """If the handler is a method reference (this::onClick), look up the
    target method's body from function_symbols and return it."""
    # Search for ::<methodName> pattern near the registration line
    reg_line = reg.get("line", 0)
    if not source_lines or reg_line < 1:
        return ""
    line_text = source_lines[reg_line - 1] if reg_line <= len(source_lines) else ""
    m = re.search(r'::(\w+)\b', line_text)
    if not m:
        return ""
    target_method = m.group(1)

    enclosing_sym_id = reg.get("enclosing_symbol_id", "")
    enclosing_sym = symbols_by_id.get(enclosing_sym_id, {})
    handler_class = enclosing_sym.get("class_name", "")
    if not handler_class:
        return ""

    # Find the target method's symbol and extract its body
    for sym in symbols_by_id.values():
        if (sym.get("class_name") == handler_class
                and sym.get("function_name") == target_method):
            try:
                sl = int(sym.get("start_line", 0))
                el = int(sym.get("end_line", 0))
                if sl and el:
                    raw = "\n".join(source_lines[sl - 1:el])
                    brace = raw.find("{")
                    if brace >= 0:
                        body, _ = extract_braced_block(raw, brace)
                        return body
            except (IndexError, ValueError):
                pass
    return ""


def _measure_max_depth(steps: list[dict]) -> int:
    """Measure the maximum nesting depth of an effect chain."""
    max_d = 0
    def walk(s, d):
        nonlocal max_d
        max_d = max(max_d, d)
        for step in s:
            for key in ("nested", "then", "else"):
                child = step.get(key)
                if child:
                    walk(child if isinstance(child, list) else [child], d + 1)
    walk(steps, 1)
    return max_d


# ═══════════════════════════════════════════════════════════════════════════════
# Layer 3 — Specialized Extractors
# ═══════════════════════════════════════════════════════════════════════════════

def build_indexes(call_graph: dict) -> tuple[
    dict[str, list[dict]],
    dict[str, dict],
    dict[tuple[str, str], dict],
]:
    """Build reusable index structures from call_graph.json data.

    Returns (calls_by_from, symbols_by_id, symbols_by_class_method).
    """
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

    return calls_by_from, symbols_by_id, symbols_by_class_method


def extract_event_chains(
    event_regs: list[dict],
    calls_by_from: dict[str, list[dict]],
    symbols_by_id: dict[str, dict],
    symbols_by_class_method: dict[tuple[str, str], dict],
    root: Path,
    file_cache: dict[str, list[str]],
    xml_ids: set[str] | None = None,
) -> tuple[list[dict], int, int]:
    """Extract event→effect chains from event registrations.

    Returns (behavior_chains, without_handler_count, max_depth_seen).
    """
    behavior_chains: list[dict] = []
    without_handler = 0
    max_depth_seen = 0

    for reg in event_regs:
        view_ref = reg.get("view_ref", "")
        event_type = reg.get("event_type", "click")
        reg_file = reg.get("file", "")
        reg_line = reg.get("line", 0)
        enclosing_sym_id = reg.get("enclosing_symbol_id", "")

        source_lines = _read_source_lines(root, reg_file, file_cache)
        if not source_lines:
            without_handler += 1
            continue

        viewref_map = _build_viewref_to_id("\n".join(source_lines))
        element_id = resolve_element_id(view_ref, viewref_map, xml_ids)

        handler_body, body_start, body_end = extract_handler_body(source_lines, reg_line)
        if not handler_body:
            # Try method reference fallback (this::onClick)
            handler_body = _try_method_reference_fallback(reg, symbols_by_id, source_lines)
            if not handler_body:
                without_handler += 1
                continue

        enclosing_sym = symbols_by_id.get(enclosing_sym_id, {})
        handler_class = enclosing_sym.get("class_name", "")

        step_counter: dict[str, int] = {}
        visited: set[str] = set()
        if enclosing_sym_id:
            visited.add(enclosing_sym_id)

        # Use condition-aware chain builder
        segments = split_body_by_conditions(handler_body)
        effect_chain = build_chain_from_segments(
            segments, handler_class,
            calls_by_from, symbols_by_id, symbols_by_class_method,
            source_lines,
            depth=0, max_depth=3,
            visited=visited, step_counter=step_counter,
        )

        chain_depth = _measure_max_depth(effect_chain)
        max_depth_seen = max(max_depth_seen, chain_depth)

        handler_info = {}
        if enclosing_sym_id:
            handler_info = {
                "symbol_id": enclosing_sym_id,
                "method": enclosing_sym.get("function_name", ""),
                "file": reg_file,
                "line": reg_line,
            }

        behavior_chains.append({
            "element_id": element_id,
            "view_ref": view_ref,
            "event_type": event_type,
            "handler": handler_info,
            "effect_chain": effect_chain,
            "chain_depth": chain_depth,
            "confidence": "static_analysis" if effect_chain else "no_chain",
        })

    return behavior_chains, without_handler, max_depth_seen


def extract_lifecycle_hooks(
    function_symbols: list[dict],
    calls_by_from: dict[str, list[dict]],
    symbols_by_id: dict[str, dict],
    symbols_by_class_method: dict[tuple[str, str], dict],
    source_lines_cache: dict[str, list[str]],
    root: Path,
    file_cache: dict[str, list[str]],
) -> dict[str, dict[str, list[str]]]:
    """Extract lifecycle method names per class (shallow: only direct calls).

    Returns {"ClassName": {"onCreate": ["initView", "initObserver"], ...}}
    """
    lifecycle_hooks: dict[str, dict[str, list[str]]] = {}

    for sym in function_symbols:
        cls = sym.get("class_name", "")
        fn = sym.get("function_name", "")
        if not cls or fn not in _LIFECYCLE_METHODS:
            continue

        sym_id = sym.get("symbol_id", "")
        if not sym_id:
            continue

        child_names = get_child_calls_from_graph(sym_id, calls_by_from)
        if not child_names:
            continue

        # Return just the method names, no deep traversal
        lifecycle_hooks.setdefault(cls, {})[fn] = child_names

    return lifecycle_hooks


# ── view_ref → element_id resolution ─────────────────────────────────────────

_ID_FIND_RE = re.compile(
    r'(?:(?:val|var)\s+)?(\w+)\s*(?::\s*\w+)?\s*=\s*'
    r'(?:\w+\s*\.\s*)?findViewById\w*\s*(?:<[^>]*>\s*)?\(\s*R\.id\.(\w+)',
)


from .view_ref_utils import camel_to_snake as _camel_to_snake


def _build_viewref_to_id(source: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for m in _ID_FIND_RE.finditer(source):
        mapping[m.group(1)] = m.group(2)
    return mapping


def resolve_element_id(view_ref: str, viewref_map: dict[str, str],
                       xml_ids: set[str] | None = None) -> str:
    """Resolve a view_ref to an XML element id.

    Priority:
      1. Explicit findViewById mapping (viewref_map)
      2. ViewBinding: camelCase view_ref → snake_case XML id
      3. Direct match (view_ref is already the XML id)
    """
    if not view_ref:
        return ""
    if view_ref in viewref_map:
        return viewref_map[view_ref]
    snake = _camel_to_snake(view_ref)
    if xml_ids is not None:
        if snake in xml_ids:
            return snake
        if view_ref in xml_ids:
            return view_ref
    else:
        if snake != view_ref:
            return snake
        return view_ref
    return ""


# ── Stats helper ─────────────────────────────────────────────────────────────

def _count_step_types(steps: list[dict]) -> dict[str, int]:
    """Recursively count step types from an effect chain."""
    counts: dict[str, int] = {}
    def walk(slist):
        for s in slist:
            st = s.get("step", "")
            counts[st] = counts.get(st, 0) + 1
            for key in ("nested", "then", "else"):
                child = s.get(key)
                if child:
                    walk(child if isinstance(child, list) else [child])
    walk(steps)
    return counts


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def run(
    source_findings: dict,
    call_graph: dict,
    project_root: str,
    file_prefix: str = "",
    xml_ids: set[str] | None = None,
) -> dict:
    """Run behavior chain extraction.

    Returns:
    {
      "behavior_chains": [...],     # per-event effect chains
      "lifecycle_hooks": {...},     # per-class lifecycle call chains
      "stats": {...}
    }
    """
    root = Path(project_root)
    file_cache: dict[str, list[str]] = {}

    # Build shared indexes (Layer 1)
    calls_by_from, symbols_by_id, symbols_by_class_method = build_indexes(call_graph)

    # ── Event chains (Layer 3) ──
    event_regs = source_findings.get("findings", {}).get("event_registrations", [])
    behavior_chains, without_handler, max_depth_seen = extract_event_chains(
        event_regs, calls_by_from, symbols_by_id, symbols_by_class_method,
        root, file_cache, xml_ids=xml_ids,
    )

    # ── Lifecycle hooks (Layer 3) ──
    function_symbols = call_graph.get("symbols", [])
    lifecycle_hooks = extract_lifecycle_hooks(
        function_symbols, calls_by_from, symbols_by_id, symbols_by_class_method,
        {}, root, file_cache,
    )

    # ── Stats ──
    total_by_step: dict[str, int] = {}
    for bc in behavior_chains:
        _merge_counts(total_by_step, _count_step_types(bc.get("effect_chain", [])))
    total_lifecycle_calls = sum(
        len(hooks) for cls_hooks in lifecycle_hooks.values() for hooks in cls_hooks.values()
    )

    return {
        "behavior_chains": behavior_chains,
        "lifecycle_hooks": lifecycle_hooks,
        "stats": {
            "total_bindings": len(event_regs),
            "with_effect_chain": sum(1 for bc in behavior_chains if bc["effect_chain"]),
            "without_handler": without_handler,
            "max_chain_depth": max_depth_seen,
            "by_step_type": total_by_step,
            "lifecycle_classes": len(lifecycle_hooks),
            "lifecycle_calls": total_lifecycle_calls,
        },
    }


def _merge_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for k, v in source.items():
        target[k] = target.get(k, 0) + v
