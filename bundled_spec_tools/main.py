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

from extractors import android_project, function_graph_extractor, ground_truth_builder, navigation_extractor, source_extractor, xml_extractor
from extractors.dependency_resolver import resolve_dependencies
from extractors.tab_structure_extractor import extract_tab_structure
from generate_specs import generate_all_specs


def _legacy_label_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", label.lower())


def _legacy_path_join(parts: list[str]) -> str:
    return " > ".join(p.strip() for p in parts if p and p.strip())


def _string_or(strings: dict, key: str, fallback: str) -> str:
    value = str(strings.get(key) or "").strip()
    return value or fallback


def _strip_non_feature_prefix(parts: list[str]) -> list[str]:
    cleaned = [str(p).strip() for p in parts if str(p).strip()]
    for marker in ("My Site",):
        marker_key = _legacy_label_key(marker)
        for idx, part in enumerate(cleaned):
            if _legacy_label_key(part) == marker_key:
                return cleaned[idx:]
    if cleaned and _legacy_label_key(cleaned[0]) in {"wplaunch", "app"}:
        return cleaned[1:]
    return cleaned


def _simplify_feature_base(parts: list[str]) -> list[str]:
    result = _strip_non_feature_prefix(parts)
    # WordPress groups Posts and Media under Content internally; the user-facing
    # feature list treats the concrete module as the next level under My Site.
    if len(result) >= 3 and _legacy_label_key(result[0]) in {"mysite", "我的站点"}:
        if _legacy_label_key(result[1]) == "content":
            return [result[0], *result[2:]]
    return result


def _iter_legacy_candidate_parts(effect_paths: dict, legacy_strings: list[str]):
    for row in effect_paths.get("paths", []) if isinstance(effect_paths, dict) else []:
        if not isinstance(row, dict):
            continue
        parts = [str(p) for p in row.get("path_parts", []) if str(p).strip()]
        if parts:
            yield parts
        legacy = str(row.get("path_display_legacy") or "")
        if legacy:
            yield [p.strip() for p in legacy.split(">") if p.strip()]
    for legacy in legacy_strings:
        if legacy:
            yield [p.strip() for p in legacy.split(">") if p.strip()]


def _build_multilevel_completions(
    strings: dict,
    effect_paths: dict,
    legacy_strings: list[str],
    tab_structure: dict | None = None,
) -> list[str]:
    """
    Complete known user-facing module roots with tab/filter labels.

    Uses dynamically-detected tab structure (tab_structure.json) when available;
    falls back to hardcoded WordPress-Android defaults for backward compatibility.
    """
    completions: list[str] = []

    # ── Determine module → [tab_labels] mapping ──
    # Priority 1: tab_structure (mined from artifacts)
    # Priority 2: hardcoded defaults (existing behavior)

    labels_by_module: dict[str, list[str]] = {}

    if tab_structure and tab_structure.get("screens"):
        # Build reverse map: screen_class → module name
        screen_to_module: dict[str, str] = {}
        # WordPress-specific known mapping
        KNOWN: dict[str, str] = {
            "PostsListActivity": "posts",
            "PagesFragment": "pages",
            "MediaBrowserActivity": "media",
        }
        for screen_cls, meta in tab_structure.get("screens", {}).items():
            module = str(KNOWN.get(screen_cls, screen_cls.lower()))
            tabs = meta.get("tabs", [])
            labels = [str(t["label"]) for t in tabs if t.get("label")]
            if labels:
                labels_by_module[module] = labels

    # Hardcoded fallback for when tab_structure isn't available
    if not labels_by_module:
        labels_by_module = {
            "posts": [
                _string_or(strings, "post_list_tab_published_posts", "Published"),
                _string_or(strings, "post_list_tab_drafts", "Drafts"),
                _string_or(strings, "post_list_tab_scheduled_posts", "Scheduled"),
                _string_or(strings, "post_list_tab_trashed_posts", "Trashed"),
                _string_or(strings, "search", "Search"),
            ],
            "pages": [
                _string_or(strings, "pages_published", "Published"),
                _string_or(strings, "pages_drafts", "Drafts"),
                _string_or(strings, "pages_scheduled", "Scheduled"),
                _string_or(strings, "pages_trashed", "Trashed"),
                _string_or(strings, "search", "Search"),
            ],
            "media": [
                _string_or(strings, "media_all", "All"),
                _string_or(strings, "media_images", "Images"),
                _string_or(strings, "media_documents", "Documents"),
                _string_or(strings, "media_videos", "Videos"),
                _string_or(strings, "search", "Search"),
            ],
        }

    # ── Find existing module bases in legacy paths ──
    known_modules = set(labels_by_module.keys())
    module_bases: dict[str, list[str]] = {}

    for parts in _iter_legacy_candidate_parts(effect_paths, legacy_strings):
        base = _simplify_feature_base(parts)
        if not base or _legacy_label_key(base[0]) != "mysite":
            continue
        for idx, part in enumerate(base):
            key = _legacy_label_key(part)
            if key in known_modules and idx > 0:
                module_bases.setdefault(key, base[: idx + 1])

    # ── Generate completions ──
    for module in sorted(known_modules):
        base = module_bases.get(module)
        if not base:
            continue
        tab_labels = labels_by_module.get(module, [])
        for label in tab_labels:
            path = _legacy_path_join([*base, label])
            if path:
                completions.append(path)

    return completions


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
    nav["meta"]["analyzed_variant"] = android_project.analyzed_variant_meta(project_root)
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
    from extractors.ui_dag_assembler import (
        assemble,
        assemble_all_flat_paths,
        assemble_exploration_legacy_paths,
        assemble_flat_paths,
        set_facts_dir,
    )
    from extractors.app_model_builder import build_and_write
    from extractors.app_model_schema import build_path_record, path_display_report_from_segments, user_click_path_quality
    from extractors.ui_paths_nav_enumerator import enumerate_nav_paths

    set_facts_dir(out_dir)

    launcher_class = navigation_extractor.get_launcher_activity_class(project_root)
    if not launcher_class:
        launcher_class = str((nav.get("meta") or {}).get("launcher_activity") or "")
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
        if launcher_class:
            for cname, cnode in nav.get("nodes", {}).items():
                if cname == launcher_class or cname.lower() == launcher_class.lower():
                    launcher_layout = cnode.get("layout", "") or nav.get("class_layouts", {}).get(cname, "")
                    launcher_class = cname
                    break
    launcher_warning = ""
    if not launcher_layout or not launcher_class:
        launcher_warning = "missing_launcher_activity"
        print("  Root screen: <unknown> (using app-wide runtime entries)")
    else:
        print(f"  Root screen: {launcher_class} (layout: {launcher_layout})")

    dag = (
        assemble(launcher_layout, max_depth=8)
        if launcher_layout
        else {
            "warning": launcher_warning,
            "screen": "",
            "screen_class": "",
            "screen_type": "unknown",
            "ui_elements": [],
            "aggregate_stats": {
                "screens": 0,
                "elements": 0,
                "interactive": 0,
                "with_behavior": 0,
                "with_navigation": 0,
            },
        }
    )
    (out_dir / "ui_dag.json").write_text(
        json.dumps(dag, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    ag = dag.get("aggregate_stats", {})
    print(f"  Reachable screens:       {ag.get('screens', 0)}")
    print(f"  Total elements:          {ag.get('elements', 0)}")
    print(f"  Interactive:             {ag.get('interactive', 0)}")
    print(f"  With behavior:           {ag.get('with_behavior', 0)}")
    print(f"  With navigation target:  {ag.get('with_navigation', 0)}")

    launcher_flat = assemble_flat_paths(launcher_layout, max_depth=8) if launcher_layout else []
    flat = list(launcher_flat)
    all_flat, coverage_report = assemble_all_flat_paths(include_report=True)
    if launcher_warning:
        coverage_report.setdefault("warnings", []).append(launcher_warning)
    if all_flat:
        seen_flat = {p.get("path_id", "") for p in flat if isinstance(p, dict)}
        flat.extend(
            p for p in all_flat
            if isinstance(p, dict) and p.get("path_id", "") not in seen_flat
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
        nav_graph=nav,
    )
    (out_dir / "ui_effect_paths.json").write_text(
        json.dumps(effect_paths, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    def _layout_for_class(class_name: str) -> str:
        return (
            (nav.get("nodes", {}).get(class_name, {}) or {}).get("layout", "")
            or nav.get("class_layouts", {}).get(class_name, "")
        )

    def _effect_row_to_path(row: dict) -> dict | None:
        target_class = str(row.get("target_class") or row.get("target") or "")
        if row.get("display_channel") != "ui" or row.get("user_visible") is not True:
            return None
        parts = [str(p) for p in row.get("path_parts", []) if str(p).strip()]
        if len(parts) < 2:
            return None
        has_target = bool(target_class and row.get("effect_kind") in {"activity", "dialog"})
        action_label = parts[-2] if has_target else parts[-1]
        screen_parts = parts[:-2] if has_target else parts[:-1]
        segments = [
            {
                "kind": "screen",
                "layout": "_effect_root" if idx == 0 else "",
                "screen_class": launcher_class if idx == 0 else "",
                "label": label,
                "display_role": "user_screen",
                "display_source": "source_effect_context" if idx == 0 else "ui",
            }
            for idx, label in enumerate(screen_parts)
        ]
        segments.append(
            {
                "kind": "action",
                "layout": "",
                "screen_class": "",
                "element_id": "",
                "tag": "virtual_effect_action",
                "interaction": "tap",
                "resolved_label": action_label,
                "trigger": row.get("action_token") or None,
                "virtual": True,
                "user_visible": True,
                "display_role": "user_action",
                "display_source": "ui",
                "effect_path_id": row.get("path_id", ""),
            }
        )
        if has_target:
            segments.append(
                {
                    "kind": "screen",
                    "layout": _layout_for_class(target_class),
                    "screen_class": target_class,
                    "label": parts[-1],
                    "display_role": "user_screen",
                    "display_source": "navigation_target",
                }
            )
        rec = build_path_record(segments)
        rec["source"] = "resolved_ui_effect"
        rec["effect_path_id"] = row.get("path_id", "")
        return rec

    resolved_effect_paths = []
    seen_flat = {p.get("path_id", "") for p in flat if isinstance(p, dict)}
    for row in effect_paths.get("paths", []):
        if not isinstance(row, dict):
            continue
        rec = _effect_row_to_path(row)
        if not rec:
            continue
        pid = rec.get("path_id", "")
        if pid and pid not in seen_flat:
            seen_flat.add(pid)
            resolved_effect_paths.append(rec)
    if resolved_effect_paths:
        flat.extend(resolved_effect_paths)

    (out_dir / "ui_paths.json").write_text(
        json.dumps(flat, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    exploration_stats: dict = {}
    exploration_chains: list[str] = []
    if launcher_layout:
        exploration_chains, exploration_stats = assemble_exploration_legacy_paths(
            launcher_layout, max_depth=8, max_chains=80000
        )

    # Eligible single-step path_display_legacy (broad coverage when static DAG has few deep edges)
    single_step_legacy: list[str] = []
    for p in flat:
        if not isinstance(p, dict):
            continue
        quality = user_click_path_quality(p)
        if not quality.get("eligible"):
            continue
        value = p.get("path_display_legacy", p.get("path_display", ""))
        if value:
            single_step_legacy.append(value)

    seen_legacy: set[str] = set()
    legacy_strings: list[str] = []
    for s in exploration_chains:
        if s not in seen_legacy:
            seen_legacy.add(s)
            legacy_strings.append(s)
    single_step_merged = 0
    for s in single_step_legacy:
        if s not in seen_legacy:
            seen_legacy.add(s)
            legacy_strings.append(s)
            single_step_merged += 1
    # Mine tab structure from artifacts for data-driven multilevel completions
    tab_structure = extract_tab_structure(out_dir)
    (out_dir / "tab_structure.json").write_text(
        json.dumps(tab_structure, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    multilevel_completions = _build_multilevel_completions(
        strings_map,
        effect_paths,
        legacy_strings,
        tab_structure=tab_structure,
    )
    multilevel_completion_merged = 0
    for s in multilevel_completions:
        if s not in seen_legacy:
            seen_legacy.add(s)
            legacy_strings.append(s)
            multilevel_completion_merged += 1

    dropped_paths: list = []
    bad_segment_count = 0
    dropped_path_count = 0
    eligible_single_step = 0
    for p in flat:
        if not isinstance(p, dict):
            continue
        quality = user_click_path_quality(p)
        if quality.get("eligible"):
            eligible_single_step += 1
        else:
            dropped_path_count += 1
        bad_count = len(quality.get("dropped_segments") or [])
        if bad_count:
            bad_segment_count += bad_count
            if len(dropped_paths) < 500:
                dropped_paths.append(quality)

    display_quality = {
        "schema_version": "2.0",
        "policy": (
            "ui_paths_legacy is the deduplicated union of (1) root-to-leaf exploration chains "
            "from the launcher-assembled UI DAG (UI-visible segments joined by ' > ') and "
            "(2) eligible single-step path_display_legacy strings from structured ui_paths and "
            "(3) static multi-level feature completions from discovered module roots plus "
            "string-resource tab/filter labels. "
            "Union covers apps where static analysis yields few deep chains but many discrete controls."
        ),
        "summary": {
            "structured_path_count": len(flat),
            "legacy_exploration_chain_count": len(exploration_chains),
            "legacy_single_step_merged_count": single_step_merged,
            "legacy_multilevel_completion_merged_count": multilevel_completion_merged,
            "legacy_exploration_truncated": bool(exploration_stats.get("truncated")),
            "legacy_exploration_cap": exploration_stats.get("cap", 80000),
            "legacy_exported_count": len(legacy_strings),
            "single_step_paths_eligible_count": eligible_single_step,
            "dropped_path_count": dropped_path_count,
            "bad_segment_count": bad_segment_count,
            "resolved_effect_path_count": len(resolved_effect_paths),
        },
        "dropped_paths": dropped_paths,
    }

    effect_legacy_total = 0
    effect_legacy_included = 0
    for p in effect_paths.get("paths", []):
        if not isinstance(p, dict) or not p.get("path_display_legacy"):
            continue
        effect_legacy_total += 1
        if p.get("display_channel") == "ui" and p.get("user_visible") is True:
            effect_legacy_included += 1
    coverage_report["effect_paths_legacy_filter"] = {
        "total": effect_legacy_total,
        "included": effect_legacy_included,
        "filtered": effect_legacy_total - effect_legacy_included,
    }
    effect_stats = effect_paths.get("stats", {}) if isinstance(effect_paths, dict) else {}
    coverage_report["dynamic_option_coverage"] = {
        "provider_option_group_count": effect_stats.get("provider_option_group_count", 0),
        "provider_option_item_count": effect_stats.get("provider_option_item_count", 0),
        "legacy_excluded_dynamic_option_count": effect_stats.get("legacy_excluded_dynamic_option_count", 0),
        "by_items_source": effect_stats.get("by_items_source", {}),
    }
    coverage_report["legacy_path_filter"] = {
        "launcher_reachable_path_count": len(launcher_flat),
        "inventory_path_count": len(all_flat),
        "legacy_path_count": len(legacy_strings),
        "legacy_multilevel_completion_count": multilevel_completion_merged,
        "inventory_excluded_from_legacy_count": display_quality["summary"]["dropped_path_count"],
        "policy": (
            "ui_paths_legacy merges exploration chains, eligible single-step legacy strings, "
            "and static multi-level feature completions; "
            "see ui_paths_display_quality_report summary for counts."
        ),
    }
    (out_dir / "ui_paths_display_quality_report.json").write_text(
        json.dumps(display_quality, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "ui_paths_coverage_report.json").write_text(
        json.dumps(coverage_report, indent=2, ensure_ascii=False), encoding="utf-8"
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
        ) if launcher_class else [],
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
