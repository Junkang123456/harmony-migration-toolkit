"""
generate_specs.py
从 ground_truth + navigation_graph + ui_paths 生成每屏幕 spec。
用法：python generate_specs.py
"""
import json
import re
import sys
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))


def _normalize_layout(name: str) -> str:
    """Collapse underscores around digits for comparison: media3_video → media3video."""
    return re.sub(r'_+', '', name)


_SUMMARY_STEP_TYPES = frozenset({"navigate", "ui_feedback", "ui_update", "async"})


def _summarize_effects(steps: list[dict]) -> list[str]:
    """Flatten a nested effect_chain into deduplicated summary tags.

    Returns e.g. ["navigate:finish", "ui_feedback:Toast", "ui_update:setVisibility"].
    Skips generic 'call' and 'condition' steps — they don't carry translation-relevant info.
    """
    tags: list[str] = []
    seen: set[str] = set()

    def walk(slist: list[dict]) -> None:
        for s in slist:
            st = s.get("step", "")
            if st in _SUMMARY_STEP_TYPES:
                detail = s.get("target") or s.get("action") or s.get("destination") or s.get("via") or ""
                detail = detail.rstrip("()")
                tag = f"{st}:{detail}" if detail else st
                if tag not in seen:
                    seen.add(tag)
                    tags.append(tag)
            for key in ("nested", "then", "else"):
                child = s.get(key)
                if child and isinstance(child, list):
                    walk(child)

    walk(steps)
    return tags


def _dedupe_layout_variants(all_layouts: set[str], known_layouts: set[str]) -> set[str]:
    """Remove layout names that are snake_case variants of a known XML layout.

    When camelCase→snake_case produces 'media3video_player_activity' but the
    real XML file is 'media3_video_player_activity', drop the non-existent variant.
    """
    by_norm: dict[str, list[str]] = {}
    for name in all_layouts:
        norm = _normalize_layout(name)
        by_norm.setdefault(norm, []).append(name)

    result: set[str] = set()
    for norm, variants in by_norm.items():
        if len(variants) == 1:
            result.add(variants[0])
            continue
        in_known = [v for v in variants if v in known_layouts]
        if in_known:
            result.add(in_known[0])
        else:
            result.add(variants[0])
    return result


def _build_brief(ui_elements, event_bindings, nav_out_edges, nav_in_edges,
                 screen_fragments, screen_adapters, screen_lifecycle):
    """Build a concise LLM-friendly summary from v2 data."""
    eb_by_id: dict[str, list[str]] = {}
    for eb in event_bindings:
        eid = eb.get("element_id", "")
        if eid:
            eb_by_id.setdefault(eid, []).extend(eb.get("effect_summary", []))

    controls = []
    for e in ui_elements:
        eid = e.get("id", "")
        actions = list(dict.fromkeys(eb_by_id.get(eid, [])))
        if e.get("is_interactive") or actions:
            controls.append({
                "id": eid,
                "type": e.get("type", ""),
                "label": e.get("label", ""),
                "actions": actions,
            })

    return {
        "interactive_controls": controls,
        "nav_in": [f"{ep['from']} ({ep['type']})" for ep in nav_in_edges],
        "nav_out": [f"→ {ep['destination']} ({ep['trigger']})" for ep in nav_out_edges],
        "has_fragments": len(screen_fragments) > 0,
        "has_adapters": len(screen_adapters) > 0,
        "lifecycle_methods": list(screen_lifecycle.keys()) if screen_lifecycle else [],
    }


def generate_all_specs(nav, gt, paths, dag, specs_dir, *,
                       layout_trees=None, fragments=None,
                       dynamic_elements=None, behavior_chains=None,
                       lifecycle_hooks=None, adapter_layouts=None,
                       spec_version="1.0"):
    """为导航图中的每个屏幕生成 HarmonyOS 迁移 spec。"""
    specs_dir = Path(specs_dir)
    specs_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. 按 layout 分组 static_elements ──
    by_layout = defaultdict(list)
    for e in gt["static_elements"]:
        layout = e.get("layout", "")
        if layout:
            by_layout[layout].append(e)

    # ── 2. 按 layout 分组 dynamic_gap ──
    gap_by_layout = defaultdict(list)
    for g in gt["dynamic_gap"]:
        layout = g.get("layout", "")
        if layout:
            gap_by_layout[layout].append(g)

    # ── 3. 从 ui_paths 提取唯一 screen → layout 映射 ──
    screen_layouts = {}
    if paths and isinstance(paths[0], dict):
        for p in paths:
            screen = p.get("screen", "")
            eid = p.get("element_id", "")
            prim = p.get("primary_layout", "")
            if eid:
                for e in gt["static_elements"]:
                    if e.get("id") == eid:
                        layout = e.get("layout", "")
                        if layout and layout not in screen_layouts.values():
                            screen_layouts[screen] = layout
                        break
            elif screen and prim and prim not in screen_layouts.values():
                screen_layouts[screen] = prim
    else:
        # paths 是形如 "Browser > Ondestroy" 的字符串列表
        for p in paths:
            parts = p.split(">", 1)
            screen = parts[0].strip() if parts else p
            if screen and screen not in screen_layouts:
                screen_layouts[screen] = ""

    for name, node in dag.get("nodes", {}).items():
        layout = node.get("layout", "")
        if layout and layout not in screen_layouts.values():
            screen_layouts[name] = layout

    # ── 4. 从 navigation_graph 提取边 ──
    edges_from = defaultdict(list)
    edges_to = defaultdict(list)
    for edge in nav.get("edges", []):
        edges_from[edge["from"]].append(edge)
        edges_to[edge["to"]].append(edge)

    # ── 5. 从 class_layouts 反查 ──
    class_to_layout = nav.get("class_layouts", {})

    # ── 6. 从 navigation_graph 节点提取 layout ──
    nav_layouts = set()
    for name, node in nav.get("nodes", {}).items():
        layout = node.get("layout", "")
        if layout:
            nav_layouts.add(layout)

    # ── 7. 构建所有屏幕的 layout 集合 ──
    all_layouts = (
        set(by_layout.keys())
        | set(gap_by_layout.keys())
        | set(screen_layouts.values())
        | set(class_to_layout.values())
        | nav_layouts
    )

    # ── 7b. 去重：合并仅因 snake_case 转换差异（数字边界等）产生的变体 ──
    known_layouts = set(by_layout.keys())
    if layout_trees:
        known_layouts |= set(layout_trees.keys())
    all_layouts = _dedupe_layout_variants(all_layouts, known_layouts)

    # ── 8. 为每个 layout 生成 spec ──
    generated = 0
    for layout_name in sorted(all_layouts):
        if not layout_name:
            continue

        elements = by_layout.get(layout_name, [])
        gaps = gap_by_layout.get(layout_name, [])

        # 查找对应的 class 名（先查 class_to_layout，再查 nav nodes）
        class_name = ""
        for cn, cl in class_to_layout.items():
            if cl == layout_name:
                class_name = cn
                break
        if not class_name:
            for cn, node in nav.get("nodes", {}).items():
                if node.get("layout", "") == layout_name:
                    class_name = cn
                    break

        # 查找导航边
        nav_out = []
        nav_in = []
        for cn in [class_name] if class_name else []:
            nav_out = edges_from.get(cn, [])
            nav_in = edges_to.get(cn, [])

        # ui_elements
        ui_elements = []
        for e in elements:
            ui_elements.append({
                "id": e.get("id", ""),
                "type": e.get("tag", ""),
                "label": e.get("text", "") or e.get("hint", "") or e.get("content_desc", ""),
                "visibility": "conditional" if e.get("conditional_visibility") else "always",
                "condition": "; ".join(
                    vc.get("condition", "")
                    for vc in e.get("visibility_conditions", [])
                ),
                "is_interactive": e.get("is_interactive", False),
            })

        # dynamic_ui (from gaps)
        dynamic_ui = []
        for g in gaps:
            entry = {
                "source": g.get("source", ""),
                "layout": g.get("layout", ""),
                "enclosing_fn": g.get("enclosing_fn", ""),
                "file": g.get("file", ""),
            }
            if g.get("items_options"):
                entry["options"] = g["items_options"]
                entry["items_source"] = g.get("items_source", "")
            dynamic_ui.append(entry)

        # navigation
        navigation = []
        for edge in nav_out:
            navigation.append({
                "trigger": edge.get("trigger", ""),
                "destination": edge.get("to", ""),
                "destination_layout": edge.get("to_layout", ""),
                "type": edge.get("type", ""),
                "via": edge.get("via", ""),
            })

        entry_points = []
        for edge in nav_in:
            entry_points.append({
                "from": edge.get("from", ""),
                "trigger": edge.get("trigger", ""),
                "type": edge.get("type", ""),
            })

        # screen_type（优先从 nav node type 推断）
        screen_type = "unknown"
        if class_name:
            node_type = nav.get("nodes", {}).get(class_name, {}).get("type", "")
            if node_type:
                screen_type = node_type
            elif "Activity" in class_name:
                screen_type = "activity"
            elif "Fragment" in class_name:
                screen_type = "fragment"
            elif "Dialog" in class_name:
                screen_type = "dialog"
            elif "Adapter" in class_name:
                screen_type = "adapter_item"
        if screen_type == "unknown" and layout_name.startswith("dialog_"):
            screen_type = "dialog"
        elif screen_type == "unknown" and (layout_name.startswith("item_") or layout_name.startswith("editor_")):
            screen_type = "adapter_item"

        spec = {
            "class": class_name,
            "layout": layout_name,
            "screen_type": screen_type,
            "source": "library" if any(
                e.get("source", "").startswith("library_") for e in elements
            ) else "project",
            "ui_elements": ui_elements,
            "dynamic_ui": dynamic_ui,
            "navigation": {
                "entry_points": entry_points,
                "exit_points": navigation,
            },
            "stats": {
                "conditional_visibility": sum(1 for e in elements if e.get("conditional_visibility")),
                "dynamic_gaps": len(gaps),
                "nav_out": len(nav_out),
                "nav_in": len(nav_in),
            },
        }

        # ── v2 extensions ──
        if spec_version >= "2.0":
            spec["spec_version"] = "2.1"

            # L0_structure
            ui_tree = None
            if layout_trees and layout_name in layout_trees:
                ui_tree = layout_trees[layout_name]

            screen_fragments = []
            if fragments:
                all_ids = {e.get("id", "") for e in elements if e.get("id")}
                for frag in fragments:
                    cid = frag.get("container_id", "")
                    host = frag.get("host_class", "")
                    if cid in all_ids or (class_name and host == class_name):
                        screen_fragments.append({
                            "class": frag.get("class", ""),
                            "container_id": cid,
                            "attach_method": frag.get("attach_method", ""),
                        })

            screen_dynamic = []
            if dynamic_elements:
                for de in dynamic_elements:
                    if class_name and de.get("host_class", "") == class_name:
                        screen_dynamic.append({
                            "view_type": de.get("view_type", ""),
                            "creation_method": de.get("creation_method", ""),
                            "container_id": de.get("container_id", ""),
                            "properties": de.get("properties", {}),
                        })

            spec["L0_structure"] = {
                "ui_tree": ui_tree,
                "fragments": screen_fragments,
                "dynamic_elements": screen_dynamic,
            }

            # L1_behavior
            event_bindings = []
            if behavior_chains:
                all_ids = {e.get("id", "") for e in elements if e.get("id")}
                for bc in behavior_chains:
                    eid = bc.get("element_id", "")
                    if eid in all_ids or (class_name and bc.get("handler", {}).get("file", "").replace("\\", "/").find(class_name) >= 0):
                        chain = bc.get("effect_chain", [])
                        event_bindings.append({
                            "element_id": eid,
                            "event_type": bc.get("event_type", ""),
                            "handler_method": bc.get("handler", {}).get("method", ""),
                            "effect_chain": chain,
                            "effect_summary": _summarize_effects(chain),
                            "chain_depth": bc.get("chain_depth", 0),
                        })

            # lifecycle_hooks
            screen_lifecycle = {}
            if lifecycle_hooks and class_name in lifecycle_hooks:
                screen_lifecycle = lifecycle_hooks[class_name]

            # adapter_bindings
            screen_adapters = []
            if adapter_layouts:
                for al in adapter_layouts:
                    if al.get("host_class", "") == class_name:
                        screen_adapters.append({
                            "container_id": al.get("host_id", ""),
                            "adapter_class": al.get("adapter_class", ""),
                            "item_layout": al.get("item_layout", ""),
                        })

            l1_entry = {}
            if event_bindings:
                l1_entry["event_bindings"] = event_bindings
            if screen_lifecycle:
                l1_entry["lifecycle_hooks"] = screen_lifecycle
            if screen_adapters:
                l1_entry["adapter_bindings"] = screen_adapters
            if l1_entry:
                spec["L1_behavior"] = l1_entry

            spec["stats"]["fragments"] = len(screen_fragments)
            spec["stats"]["dynamic_elements"] = len(screen_dynamic)
            spec["stats"]["event_bindings"] = len(event_bindings)
            spec["stats"]["event_bindings_with_chain"] = sum(
                1 for eb in event_bindings if eb.get("effect_chain")
            )

            spec["brief"] = _build_brief(
                ui_elements, event_bindings, navigation, entry_points,
                screen_fragments, screen_adapters, screen_lifecycle,
            )

        out_path = specs_dir / f"{layout_name}_spec.json"
        out_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
        generated += 1
        print(f"  [{screen_type:12s}] {layout_name:50s}  elems={len(elements):3d}  gaps={len(gaps):2d}  nav={len(nav_out):2d}in/{len(nav_in):2d}out")

    print(f"\nGenerated {generated} specs in {specs_dir}")


if __name__ == "__main__":
    BASE = Path(__file__).parent / "output"
    OUT = BASE / "specs"

    gt = json.loads((BASE / "ground_truth.json").read_text(encoding="utf-8"))
    nav = json.loads((BASE / "navigation_graph.json").read_text(encoding="utf-8"))
    dag = json.loads((BASE / "ui_dag.json").read_text(encoding="utf-8"))
    paths = json.loads((BASE / "ui_paths.json").read_text(encoding="utf-8"))

    generate_all_specs(nav, gt, paths, dag, OUT)
