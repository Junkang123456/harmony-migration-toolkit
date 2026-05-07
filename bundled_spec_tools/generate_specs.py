"""
generate_specs.py
从 ground_truth + navigation_graph + ui_paths 生成每屏幕 spec。
用法：python generate_specs.py
"""
import json
import sys
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))


def generate_all_specs(nav, gt, paths, dag, specs_dir):
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
                "behaviors": [
                    {
                        "event": b.get("event", ""),
                        "method": b.get("method", b.get("handler", "")),
                        "file": b.get("file", ""),
                        "line": b.get("line", 0),
                    }
                    for b in e.get("behaviors", [])
                ],
            })

        # behaviors (from ground truth bindings)
        behaviors = []
        for e in elements:
            for b in e.get("behaviors", []):
                behaviors.append({
                    "trigger": f"{b.get('event', 'interaction')} on {e.get('id', '')}",
                    "element_id": e.get("id", ""),
                    "action": b.get("method", b.get("handler", "")),
                    "outcome": b.get("enclosing_fn", ""),
                    "file": b.get("file", ""),
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
            "screen_id": layout_name,
            "class": class_name,
            "layout": layout_name,
            "screen_type": screen_type,
            "source": "library" if any(
                e.get("source", "").startswith("library_") for e in elements
            ) else "project",
            "ui_elements": ui_elements,
            "behaviors": behaviors,
            "dynamic_ui": dynamic_ui,
            "navigation": {
                "entry_points": entry_points,
                "exit_points": navigation,
            },
            "stats": {
                "total_elements": len(elements),
                "interactive": sum(1 for e in elements if e.get("is_interactive")),
                "with_behavior": sum(1 for e in elements if e.get("behaviors")),
                "conditional_visibility": sum(1 for e in elements if e.get("conditional_visibility")),
                "dynamic_gaps": len(gaps),
                "nav_out": len(nav_out),
                "nav_in": len(nav_in),
            },
        }

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
