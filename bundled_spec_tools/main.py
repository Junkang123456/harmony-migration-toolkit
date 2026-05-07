"""
main.py
Static ground truth 生成的入口脚本。
用法：python main.py <android_project_root>
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from extractors import function_graph_extractor, ground_truth_builder, navigation_extractor, source_extractor, xml_extractor
from extractors.dependency_resolver import resolve_dependencies
from generate_specs import generate_all_specs


def _merge_dict(base, extra):
    for k, v in extra.items():
        if k in base and isinstance(base[k], (int, float)):
            base[k] += v
        elif k in base and isinstance(base[k], dict):
            _merge_dict(base[k], v)
        elif k in base and isinstance(base[k], list):
            base[k].extend(v)
        else:
            base[k] = v


def detect_include_builds(project_root):
    paths = []
    root = Path(project_root).resolve()
    for name in ("settings.gradle.kts", "settings.gradle"):
        sf = root / name
        if not sf.exists():
            continue
        content = sf.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r'includeBuild\(["\']([^"\']+)["\']', content):
            dep = (root / m.group(1)).resolve()
            if dep.exists():
                paths.append(str(dep))
    return paths


def _print_spec_report(flat: list, dag: dict) -> None:
    print("\n" + "=" * 70)
    print("SPEC REPORT — UI Paths")
    print("=" * 70)

    ag = dag.get("aggregate_stats", {})
    print(f"  Reachable screens   : {ag.get('screens', 0)}")
    print(f"  Total paths         : {len(flat)}")

    print("\n" + "-" * 70)
    print("PATH DETAILS")
    print("-" * 70)
    for p in flat:
        if isinstance(p, dict):
            print(f"  {p.get('path_display', p.get('path_id', ''))}")
        else:
            print(f"  {p}")

    print("\n" + "=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Generate static Android facts for harmony-migration-toolkit")
    parser.add_argument("android_project_root", nargs="?", default=".", help="Android project root")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: bundled_spec_tools/output)",
    )
    args = parser.parse_args()

    project_root = args.android_project_root
    out_dir = (args.out or (Path(__file__).parent / "output")).resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    dep_roots = resolve_dependencies(project_root)
    print(f"Project: {project_root}")
    if dep_roots:
        print(f"Detected dependencies ({len(dep_roots)}):")
        for d in dep_roots:
            print(f"  {d}")
    print("=" * 50)

    # ── 阶段一：静态提取 ──

    # Step 1: XML 静态提取
    print("\n[1/7] Extracting XML resources...")
    xml_result = xml_extractor.run(project_root)
    for dep in dep_roots:
        dep_name = Path(dep).name
        dep_xml = xml_extractor.run(
            dep,
            source_prefix="library_xml_layout",
            menu_prefix="library_xml_menu",
            file_prefix=dep_name,
        )
        xml_result["elements"].extend(dep_xml["elements"])
        xml_result["stats"]["total"] += dep_xml["stats"].get("total", 0)
        xml_result["stats"]["interactive"] += dep_xml["stats"].get("interactive", 0)
        xml_result["stats"]["hidden_by_default"] += dep_xml["stats"].get("hidden_by_default", 0)
        dep_strings = dep_xml.get("strings", {})
        xml_result.get("strings", {}).update(dep_strings)
    (out_dir / "static_xml.json").write_text(
        json.dumps(xml_result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    s = xml_result["stats"]
    print(f"  {s['total']} elements, {s['interactive']} interactive, "
          f"{s['hidden_by_default']} hidden by default")

    # Step 2: Source 静态扫描
    print("\n[2/7] Scanning source code...")
    src_result = source_extractor.run(project_root)
    symbol_payload, call_graph_payload = function_graph_extractor.run(project_root)
    for dep in dep_roots:
        dep_name = Path(dep).name
        dep_src = source_extractor.run(
            dep,
            file_prefix=dep_name,
            scan_events=True,
        )
        dep_symbols, dep_call_graph = function_graph_extractor.run(dep, file_prefix=dep_name)
        _merge_dict(src_result["findings"], dep_src["findings"])
        _merge_dict(src_result["stats"], dep_src["stats"])
        symbol_payload["symbols"].extend(dep_symbols.get("symbols") or [])
        call_graph_payload["symbols"].extend(dep_call_graph.get("symbols") or [])
        call_graph_payload["calls"].extend(dep_call_graph.get("calls") or [])
        call_graph_payload["unresolved_calls"].extend(dep_call_graph.get("unresolved_calls") or [])
    (out_dir / "source_findings.json").write_text(
        json.dumps(src_result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    symbol_payload["symbols"].sort(key=lambda s: (s.get("file", ""), s.get("start_line", 0), s.get("symbol_id", "")))
    symbol_payload["stats"] = {
        "source_files_scanned": src_result["stats"].get("source_files_scanned", 0),
        "symbol_count": len(symbol_payload["symbols"]),
        "ast_symbol_count": sum(1 for s in symbol_payload["symbols"] if s.get("confidence") == "ast"),
    }
    call_graph_payload["symbols"] = symbol_payload["symbols"]
    call_graph_payload["calls"].sort(key=lambda c: (c.get("callsite_file", ""), c.get("callsite_line", 0), c.get("from_symbol_id", "")))
    call_graph_payload["unresolved_calls"].sort(key=lambda c: (c.get("callsite_file", ""), c.get("callsite_line", 0), c.get("from_symbol_id", "")))
    call_graph_payload["stats"] = {
        "symbol_count": len(call_graph_payload["symbols"]),
        "call_count": len(call_graph_payload["calls"]),
        "unresolved_call_count": len(call_graph_payload["unresolved_calls"]),
        "ast_call_count": sum(1 for c in call_graph_payload["calls"] if str(c.get("confidence", "")).startswith("ast")),
    }
    (out_dir / "function_symbols.json").write_text(
        json.dumps(symbol_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "call_graph.json").write_text(
        json.dumps(call_graph_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    for k, v in src_result["stats"].items():
        print(f"  {k}: {v}")
    print(f"  function_symbols: {symbol_payload['stats']['symbol_count']}")
    print(f"  call_edges: {call_graph_payload['stats']['call_count']}")

    # Step 3: 合并 → ground truth
    print("\n[3/7] Building ground truth...")
    gt = ground_truth_builder.build(xml_result, src_result)
    gt_path = out_dir / "ground_truth.json"
    gt_path.write_text(json.dumps(gt, indent=2, ensure_ascii=False), encoding="utf-8")

    s = gt["coverage_stats"]
    print(f"\nGround truth saved to {gt_path}")
    print(f"  XML elements:            {s['xml_elements_total']}")
    print(f"  Interactive:             {s['xml_interactive']}")
    print(f"  Behavior bound:          {s['xml_with_behavior_bound']}")
    print(f"  Conditional visibility:  {s['xml_conditional_visibility']}")
    print(f"  Dynamic gap (total):     {s['dynamic_gap_total']}")
    print(f"  Dynamic gap (new):       {s['dynamic_gap_pure_new']}")
    print(f"  Unmatched:               {s['unmatched']}")

    total = s["xml_interactive"] + s["dynamic_gap_pure_new"]
    bound = s["xml_with_behavior_bound"]
    if total > 0:
        pct = bound / total * 100
        print(f"\n  Behavior coverage (static): {bound}/{total} = {pct:.1f}%")

    # ── 阶段二：导航与关联 ──

    # Step 4: 导航图提取
    print("\n[4/7] Extracting navigation graph...")
    nav = navigation_extractor.run(project_root, dep_roots=dep_roots)
    nav_path = out_dir / "navigation_graph.json"
    nav_path.write_text(json.dumps(nav, indent=2, ensure_ascii=False), encoding="utf-8")

    from extractors import nav_pipeline as _nav_pipeline

    cand_payload = _nav_pipeline.build_candidates_payload(project_root, dep_roots)
    (out_dir / "navigation_candidates.json").write_text(
        json.dumps(cand_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    ns = nav.get("stats", {})
    total_nodes = ns.get("total_nodes", 0)
    activity_nodes = ns.get("activity_nodes", 0)
    dialog_nodes = ns.get("dialog_nodes", 0)
    external_nodes = ns.get("external_nodes", 0)
    total_edges = ns.get("total_edges", 0)
    by_type = ns.get("by_type", {})
    print(f"\nNavigation graph saved to {nav_path}")
    print(
        f"  Navigation candidates (L1): {cand_payload['stats']['total']} "
        f"(kinds: {cand_payload['stats'].get('by_kind', {})})"
    )
    print(f"  Inferred class→layout mappings: {len(nav.get('class_layouts', {}))}")
    print(f"  Total nodes:     {total_nodes} "
          f"({activity_nodes} activities, {dialog_nodes} dialogs, {external_nodes} external)")
    print(f"  Total edges:     {total_edges}")
    for t, c in by_type.items():
        print(f"    {t}: {c}")

    # Step 5: Gap 合并
    gap_path = out_dir / "gap_analysis.json"
    gap = {"stats": {"total_resolved": 0, "by_gap_type": {}, "merged_into_gt": False}, "resolved": []}
    if gap_path.exists():
        gap = json.loads(gap_path.read_text(encoding="utf-8"))
        print(f"\n[5/7] Gap analysis loaded: {gap['stats']['total_resolved']} resolved items")
        for k, v in gap["stats"].get("by_gap_type", {}).items():
            print(f"  {k}: {v}")
        # 将 gap 条目合并到 ground_truth 的 dynamic_gap 中
        for item in gap.get("resolved", []):
            gt.setdefault("dynamic_gap", []).append({
                "layout": item.get("resolved_layout", ""),
                "gap_type": item.get("gap_type", ""),
                "source": item.get("file", ""),
                "file": item.get("file", ""),
                "enclosing_fn": item.get("trigger", ""),
                "view_ref": item.get("view_ref", ""),
                "behavior": item.get("behavior", ""),
                "resolved_xml_id": item.get("resolved_xml_id", ""),
            })
        gap["stats"]["merged_into_gt"] = len(gap.get("resolved", []))
        print(f"  Merged {gap['stats']['merged_into_gt']} items into ground_truth.dynamic_gap")
    else:
        print("\n[5/7] No gap_analysis.json found — run merge_gap.py or sub-agent first.")

    # Step 6: 动态组装 UI DAG — launcher from AndroidManifest MAIN/LAUNCHER
    print("\n[6/7] Assembling UI DAG...")
    from extractors.ui_dag_assembler import assemble, assemble_all_flat_paths, assemble_flat_paths
    from extractors.app_model_builder import build_and_write
    from extractors.app_model_schema import path_display_report_from_segments
    from extractors.ui_paths_nav_enumerator import enumerate_nav_paths

    launcher_class = navigation_extractor.get_launcher_activity_class(project_root)
    launcher_layout = ""
    if launcher_class:
        node = nav.get("nodes", {}).get(launcher_class, {})
        launcher_layout = node.get("layout", "") or nav.get("class_layouts", {}).get(
            launcher_class, ""
        )
        if not launcher_layout:
            for cname, cnode in nav.get("nodes", {}).items():
                if cname.lower() == launcher_class.lower():
                    launcher_layout = cnode.get("layout", "") or nav.get(
                        "class_layouts", {}
                    ).get(cname, "")
                    launcher_class = cname
                    break
    if not launcher_layout or not launcher_class:
        launcher_class = launcher_class or "BrowserActivity"
        launcher_layout = launcher_layout or "browser_activity"
        for cname, cnode in nav.get("nodes", {}).items():
            if cname == launcher_class or cname.lower() == launcher_class.lower():
                launcher_layout = cnode.get("layout", launcher_layout) or launcher_layout
                launcher_class = cname
                break
    print(f"  Root screen: {launcher_class} (layout: {launcher_layout})")

    dag = assemble(launcher_layout, max_depth=8)
    (out_dir / "ui_dag.json").write_text(
        json.dumps(dag, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    ag = dag.get("aggregate_stats", {})
    print(f"  Reachable screens:       {ag.get('screens', 0)}")
    print(f"  Total elements:          {ag.get('elements', 0)}")
    print(f"  Interactive:             {ag.get('interactive', 0)}")
    print(f"  With behavior:           {ag.get('with_behavior', 0)}")
    print(f"  With navigation target:  {ag.get('with_navigation', 0)}")

    flat = assemble_flat_paths(launcher_layout, max_depth=8)
    all_flat, coverage_report = assemble_all_flat_paths(include_report=True)
    if all_flat:
        seen_flat = {p.get("path_id", "") for p in flat if isinstance(p, dict)}
        flat.extend(
            p for p in all_flat
            if isinstance(p, dict) and p.get("path_id", "") not in seen_flat
        )
    (out_dir / "ui_paths_coverage_report.json").write_text(
        json.dumps(coverage_report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    strings_map = xml_result.get("strings") or {}
    for p in flat:
        if isinstance(p, dict) and p.get("segments"):
            p["path_display_report"] = path_display_report_from_segments(p["segments"], strings_map)

    effect_paths = _nav_pipeline.build_ui_effect_paths(
        project_root,
        dep_roots,
        strings_map,
        launcher_class=launcher_class,
    )
    (out_dir / "ui_effect_paths.json").write_text(
        json.dumps(effect_paths, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    (out_dir / "ui_paths.json").write_text(
        json.dumps(flat, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    legacy_strings = [
        p.get("path_display_legacy", p.get("path_display", ""))
        if isinstance(p, dict)
        else str(p)
        for p in flat
    ]
    legacy_strings.extend(
        p.get("path_display_legacy", "")
        for p in effect_paths.get("paths", [])
        if isinstance(p, dict) and p.get("path_display_legacy")
    )
    (out_dir / "ui_paths_legacy.json").write_text(
        json.dumps(legacy_strings, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    report_rows = [
        {
            "path_id": p.get("path_id", ""),
            "path_display_legacy": p.get("path_display_legacy", p.get("path_display", "")),
            "path_display_report": p.get("path_display_report", ""),
        }
        for p in flat
        if isinstance(p, dict)
    ]
    report_rows.extend(
        {
            "path_id": p.get("path_id", ""),
            "path_display_legacy": p.get("path_display_legacy", ""),
            "path_display_report": p.get("path_display_report", ""),
            "effect_kind": p.get("effect_kind", ""),
            "report_only": True,
            "source_file": p.get("source_file", ""),
            "line": p.get("line", 0),
        }
        for p in effect_paths.get("paths", [])
        if isinstance(p, dict)
    )
    (out_dir / "ui_paths_report.json").write_text(
        json.dumps(report_rows, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    enum_payload = {
        "schema_version": "1.0",
        "start_class": launcher_class,
        "max_depth": 8,
        "max_paths_cap": 800,
        "paths": enumerate_nav_paths(
            nav,
            start_class=launcher_class,
            start_layout=launcher_layout,
            max_depth=8,
            max_paths=800,
        ),
    }
    enum_payload["path_count"] = len(enum_payload["paths"])
    (out_dir / "ui_paths_enumerated.json").write_text(
        json.dumps(enum_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    am_counts = build_and_write(out_dir, project_root, flat, nav, gt, xml_result)
    print(
        f"  UI effect paths: {effect_paths.get('path_count', 0)} "
        f"(kinds: {effect_paths.get('stats', {}).get('by_effect_kind', {})})"
    )
    print(f"  App model: {am_counts}")

    # ── Spec 报告 ──────────────────────────────────────────────────────────────
    _print_spec_report(flat, dag)

    # ── 阶段三：Spec 生成 ──

    # Step 7: 为导航图中的每个屏幕生成 HarmonyOS 迁移 spec
    print("\n[7/7] Generating HarmonyOS migration specs...")
    specs_dir = out_dir / "specs"
    specs_dir.mkdir(exist_ok=True)

    generate_all_specs(nav, gt, flat, dag, specs_dir)

    generated = len(list(specs_dir.glob("*_spec.json")))
    print(f"  Generated {generated} specs in output/specs/")

    print("\nDone. Output files:")
    for name in ["static_xml.json", "source_findings.json", "ground_truth.json",
                 "function_symbols.json", "call_graph.json",
                 "navigation_graph.json", "navigation_candidates.json",
                 "gap_analysis.json", "ui_dag.json",
                 "ui_paths.json", "ui_paths_legacy.json", "ui_paths_report.json",
                 "ui_effect_paths.json",
                 "ui_paths_enumerated.json"]:
        exists = (out_dir / name).exists()
        marker = "OK" if exists else "MISSING"
        print(f"  [{marker}] output/{name}")
    am_index = out_dir / "app_model" / "index.json"
    print(f"  [{'OK' if am_index.exists() else 'MISSING'}] output/app_model/index.json")


if __name__ == "__main__":
    main()
