"""
Enumerate simple navigation chains from navigation_graph.json (read-only).

Produces many short human-readable paths for reporting / coverage vs DAG-based
ui_paths.json (which dedupes by path_id and respects layout-backed controls).
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any


def _screen_label(screen_class: str, layout: str) -> str:
    name = screen_class or layout
    for suffix in ("Activity", "Fragment", "Dialog"):
        if name.endswith(suffix) and name != suffix:
            name = name[: -len(suffix)]
            break
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return spaced or (layout or screen_class or "Screen")


def _edge_label(trigger: str, via: str) -> str:
    t = (trigger or "").strip()
    if t.lower().startswith("fn:"):
        return t[3:].strip().replace("_", " ").title()
    if t.lower().startswith("menu "):
        return t[5:].strip().replace("_", " ").title()
    if t:
        return t.replace("_", " ").title()[:96]
    v = (via or "nav").strip()
    return v.replace("_", " ").title()[:96]


def enumerate_nav_paths(
    nav: dict[str, Any],
    *,
    start_class: str,
    start_layout: str = "",
    max_depth: int = 8,
    max_paths: int = 800,
) -> list[dict[str, Any]]:
    """
    DFS over navigation edges from start_class. Emits path prefixes with at least
    two display segments (screen + one step). Cycles on the current stack are skipped.
    """
    edges_by_from: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in nav.get("edges") or []:
        f = e.get("from")
        t = e.get("to")
        if not f or not t or f == t:
            continue
        edges_by_from[str(f)].append(e)
    for k in edges_by_from:
        edges_by_from[k].sort(
            key=lambda x: (str(x.get("to", "")), str(x.get("trigger", "")), int(x.get("line") or 0))
        )

    out: list[dict[str, Any]] = []
    root_label = _screen_label(start_class, start_layout)

    def dfs(cur_class: str, stack: list[str], parts: list[str]) -> None:
        if len(out) >= max_paths:
            return
        if len(parts) >= 2:
            out.append(
                {
                    "path_display": " > ".join(parts),
                    "depth": len(stack),
                    "leaf_class": cur_class,
                }
            )
        if len(stack) >= max_depth:
            return
        for edge in edges_by_from.get(cur_class, []):
            dest = str(edge.get("to", ""))
            if dest in stack:
                continue
            lbl = _edge_label(str(edge.get("trigger", "")), str(edge.get("via", "")))
            dest_lbl = _screen_label(dest, str(edge.get("to_layout", "") or ""))
            seg = [lbl, dest_lbl] if lbl else [dest_lbl]
            dfs(dest, stack + [dest], parts + seg)

    dfs(start_class, [start_class], [root_label])
    out.sort(key=lambda r: r.get("path_display", ""))
    return out
