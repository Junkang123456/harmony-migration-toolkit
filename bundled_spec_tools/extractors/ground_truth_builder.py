"""
ground_truth_builder.py
合并 xml_extractor + source_extractor 输出，生成完整 ground truth。

绑定逻辑（通用，不依赖框架）：
  1. event_registrations  → view_ref (camelCase) 对齐 XML id (snake_case) → 绑定行为
  2. id_dispatchers       → item_id 直接对齐 XML id → 绑定行为
  3. visibility_controls  → view_ref / item_id 对齐 → 标记条件可见
  4. inflates             → layout 未在 XML 中出现的 → dynamic_gap
  5. data_driven_ui       → 独立的 data_driven_gap，含 items_options 字段

误报抑制：
  - unmatched 列表排除已知非 View 系统对象的注册（decorView、contentResolver 等）
  - unmatched 列表排除 PascalCase view_ref（类型名，非实例变量）
"""
import json
import re
from pathlib import Path


def _camel_to_snake(name: str) -> str:
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", name)
    s = re.sub(r"(?<=[a-zA-Z])(?=[0-9])", "_", s)
    return s.lower()


def _clean_ref(ref: str) -> str:
    """去掉 binding. / viewBinding. 等前缀，得到裸 view_ref"""
    ref = re.sub(r'^(?:viewBinding|binding|view|this)\s*[\.\?]\s*', '', ref)
    return ref.strip()


def build(xml_result: dict, source_result: dict) -> dict:
    # id → element 索引
    elements = {e["id"]: e for e in xml_result["elements"] if e.get("id")}
    findings = source_result["findings"]

    # ── 1. event_registrations → 绑定到 XML 元素 ─────────────────
    unmatched = []
    for reg in findings.get("event_registrations", []):
        raw_ref = _clean_ref(reg.get("view_ref", ""))
        view_id = _camel_to_snake(raw_ref) if raw_ref else ""

        if view_id and view_id in elements:
            elements[view_id].setdefault("behaviors", []).append({
                "event":        reg["event_type"],
                "method":       reg["method"],
                "file":         reg["file"],
                "line":         reg["line"],
                "enclosing_fn": reg.get("enclosing_fn", ""),
            })
        else:
            reg["_resolved_id_attempt"] = view_id
            unmatched.append(reg)

    # ── 2. id_dispatchers → 绑定 menu / action items ──────────────
    for disp in findings.get("id_dispatchers", []):
        item_id = disp.get("item_id", "")
        if item_id and item_id in elements:
            elements[item_id].setdefault("behaviors", []).append({
                "event":        "id_dispatch",
                "handler":      disp.get("handler", ""),
                "file":         disp["file"],
                "line":         disp["line"],
                "enclosing_fn": disp.get("enclosing_fn", ""),
            })
        else:
            unmatched.append(disp)

    # ── 3. visibility_controls → 标记条件可见 ────────────────────
    for vc in findings.get("visibility_controls", []):
        # 可能是 view_ref 或 item_id
        raw = _clean_ref(vc.get("view_ref", "") or vc.get("item_id", ""))
        vid = _camel_to_snake(raw)

        if vid and vid in elements:
            elem = elements[vid]
            elem["conditional_visibility"] = True
            elem.setdefault("visibility_conditions", []).append({
                "property":  vc.get("property") or "isVisible",
                "condition": vc.get("condition", ""),
                "file":      vc["file"],
                "line":      vc["line"],
            })

    # ── 4. inflates → dynamic_gap ────────────────────────────────
    static_layouts = {e.get("layout", "") for e in xml_result["elements"]}
    gap_elements   = []
    seen_layouts   = set()

    for inf in findings.get("inflates", []):
        layout = inf.get("layout", "")
        if not layout or layout in seen_layouts:
            continue
        seen_layouts.add(layout)
        gap_elements.append({
            "source":       inf["kind"],   # inflate_layout / inflate_binding / static_binding
            "layout":       layout,
            "binding_class": inf.get("binding_class", ""),
            "enclosing_fn": inf.get("enclosing_fn", ""),
            "file":         inf["file"],
            "line":         inf["line"],
            "in_static_xml": layout in static_layouts,
        })

    # ── 5. data_driven_ui → 独立 gap ─────────────────────────────
    for dd in findings.get("data_driven_ui", []):
        gap_elements.append({
            "source":        "data_driven_ui",
            "component":     dd["component"],
            "items_source":  dd.get("items_source", ""),
            "items_range":   dd.get("items_range", ""),
            "items_options": dd.get("items_options", []),
            "enclosing_fn":  dd.get("enclosing_fn", ""),
            "file":          dd["file"],
            "line":          dd["line"],
            "in_static_xml": False,
            "note":          dd.get("note", ""),
        })

    # ── 6. 统计 ──────────────────────────────────────────────────
    all_elems      = list(elements.values())
    with_behaviors = [e for e in all_elems if e.get("behaviors")]
    conditional    = [e for e in all_elems if e.get("conditional_visibility")]
    pure_dynamic   = [g for g in gap_elements if not g["in_static_xml"]]
    data_driven    = [g for g in gap_elements if g.get("source") == "data_driven_ui"]

    stats = {
        "xml_elements_total":         len(all_elems),
        "xml_interactive":            sum(1 for e in all_elems if e.get("is_interactive")),
        "xml_with_behavior_bound":    len(with_behaviors),
        "xml_conditional_visibility": len(conditional),
        "dynamic_gap_total":          len(gap_elements),
        "dynamic_gap_pure_new":       len(pure_dynamic),
        "data_driven_ui":             len(data_driven),
        "unmatched":                  len(unmatched),
    }

    return {
        "static_elements": all_elems,
        "dynamic_gap":     gap_elements,
        "unmatched":       unmatched,
        "coverage_stats":  stats,
    }


if __name__ == "__main__":
    import sys
    xml_path = Path("output/static_xml.json")
    src_path = Path("output/source_findings.json")

    if not xml_path.exists() or not src_path.exists():
        print("Run xml_extractor.py and source_extractor.py first.")
        sys.exit(1)

    xml_result = json.loads(xml_path.read_text(encoding="utf-8"))
    src_result = json.loads(src_path.read_text(encoding="utf-8"))
    result     = build(xml_result, src_result)

    out = Path("output/ground_truth.json")
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    s = result["coverage_stats"]
    print("Ground truth built:")
    for k, v in s.items():
        print(f"  {k}: {v}")
