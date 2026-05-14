from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUNDLED_TOOLS = ROOT / "bundled_spec_tools"


def run_pipeline(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(ROOT / "pipeline.py"), *args]
    return subprocess.run(cmd, cwd=str(ROOT), check=check, capture_output=True, text=True)


def _ensure_bundled_import_path() -> None:
    bundled = str(BUNDLED_TOOLS)
    if bundled not in sys.path:
        sys.path.insert(0, bundled)


def test_pipeline_minimal_fixture_schema():
    out = ROOT / "out" / "pytest_minimal"
    if out.exists():
        shutil.rmtree(out)
    run_pipeline(
        [
            "--android-root",
            str(ROOT / "fixtures" / "minimal_android"),
            "--facts-source",
            str(ROOT / "fixtures" / "minimal_facts"),
            "--out",
            str(out),
            "--stages",
            "0,1,2,3,5,4,7",
        ]
    )

    inter = out / "intermediate"
    manifest = json.loads((inter / "0_android_facts" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifact_checks"]["call_graph"]["symbol_count"] == 3
    assert manifest["artifact_checks"]["warnings"] == []
    assert manifest["spec_tools"]["main_py_sha256"]

    af = json.loads((inter / "1_android_facts" / "android_facts.v1.json").read_text(encoding="utf-8"))
    assert af["schema_version"] == "1.0"
    assert any(s["class_name"] == "MainActivity" for s in af["screens"])

    fm = json.loads((inter / "2_framework_map" / "framework_map.v1.json").read_text(encoding="utf-8"))
    assert fm["rules_version"]
    assert isinstance(fm["gap_items"], list)

    ha = json.loads((inter / "3_harmony_arch" / "harmony_arch.v1.json").read_text(encoding="utf-8"))
    assert ha["bundle_name"] == "com.verifyfix.minimal"
    assert ha["abilities"][0]["name"] == "EntryAbility"

    ft = json.loads((inter / "5_feature_tree" / "feature_tree.v1.json").read_text(encoding="utf-8"))
    assert ft["schema_version"] == "1.0"
    assert ft["taxonomy_version"] == "1.0"
    assert any(n.get("node_id") == "product_root" for n in ft["nodes"])
    assert any(n.get("node_id") == "screen:MainActivity" for n in ft["nodes"])
    nav_edges = [e for e in ft["edges"] if e.get("rel") == "presents_modal"]
    assert len(nav_edges) >= 1
    behavior = next(n for n in ft["nodes"] if n.get("node_id") == "behavior:effect:ep:minimal-settings")
    assert behavior["evidence"]["source_file"] == "app/src/main/java/com/verifyfix/minimal/MainActivity.kt"
    assert behavior["evidence"]["line"] == 12
    assert behavior["logical_feature_id"].startswith("generated.settings")
    assert behavior["evidence"]["entry_symbol_id"] == "fn:com.verifyfix.minimal.MainActivity.openSettings/0"
    assert any(n.get("node_id") == "function_symbol:fn:com.verifyfix.minimal.MainActivity.openSettings/0" for n in ft["nodes"])
    assert any(e.get("rel") == "enters" for e in ft["edges"])
    assert any(e.get("rel") == "calls" for e in ft["edges"])
    assert ft["meta"]["coverage"]["features_with_line_evidence"] >= 1
    assert ft["meta"]["coverage"]["effect_path_attached_to_feature"] == 1
    assert ft["meta"]["coverage"]["behaviors_with_entry_symbol"] >= 1

    spec_ev = json.loads((inter / "5_feature_tree" / "feature_spec_evidence.json").read_text(encoding="utf-8"))
    assert spec_ev["features"][0]["source_anchors"]
    verify = json.loads((inter / "5_feature_tree" / "verify_report.json").read_text(encoding="utf-8"))
    assert verify["status"] in {"pass", "warn"}
    taxonomy = json.loads((inter / "5_feature_tree" / "taxonomy_report.json").read_text(encoding="utf-8"))
    assert taxonomy["summary"]["matched_screen_count"] >= 1
    assert taxonomy["summary"]["generated_feature_count"] >= 1
    assert taxonomy["summary"]["unmatched_screen_count"] == 0

    bundle = json.loads((out / "agent_bundle.v1.json").read_text(encoding="utf-8"))
    assert bundle["bundle_kind"] == "harmony_migration_agent_bundle"
    assert bundle["summary"]["feature_count"] >= 1
    assert bundle["summary"]["screen_count"] >= 1
    assert bundle["outline"]["verification"]["status"] in {"pass", "warn"}
    assert bundle["outline"]["artifacts"]["feature_tree"]["path"] == "intermediate/5_feature_tree/feature_tree.v1.json"
    assert "feature_tree" not in bundle
    assert bundle["intermediate_manifest"]["artifact_count"] >= 7
    assert not (out / "viewer").exists()


def test_stage0_bundled_scanner_uses_isolated_output(tmp_path: Path):
    out = tmp_path / "stage0"
    shared_output = ROOT / "bundled_spec_tools" / "output"
    stale = shared_output / "app_model" / "features" / "stale_from_previous_project.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"stale": true}\n', encoding="utf-8")

    try:
        run_pipeline(
            [
                "--android-root",
                str(ROOT / "fixtures" / "minimal_android"),
                "--out",
                str(out),
                "--stages",
                "0",
            ]
        )

        facts = out / "intermediate" / "0_android_facts"
        assert (facts / "static_xml.json").is_file()
        assert (facts / "navigation_graph.json").is_file()
        assert (facts / "function_symbols.json").is_file()
        assert (facts / "call_graph.json").is_file()
        assert (facts / "app_model" / "index.json").is_file()
        assert (facts / "manifest.json").is_file()
        legacy = json.loads((facts / "ui_paths_legacy.json").read_text(encoding="utf-8"))
        assert isinstance(legacy, list)
        assert all(isinstance(p, str) for p in legacy)
        assert not any("L2:" in p or "l2:" in p for p in legacy)
        assert not any("Runtime Entry" in p for p in legacy)
        assert not any("Unmapped Layouts" in p for p in legacy)
        assert not any("Builder" in p or "View Model" in p for p in legacy)
        assert not any("Backup > WordPress Themes" in p for p in legacy)
        ui_paths = json.loads((facts / "ui_paths.json").read_text(encoding="utf-8"))
        assert isinstance(ui_paths, list)
        assert all(isinstance(p, dict) for p in ui_paths)
        effect_paths = json.loads((facts / "ui_effect_paths.json").read_text(encoding="utf-8"))
        assert isinstance(effect_paths.get("paths", []), list)
        quality = json.loads((facts / "ui_paths_display_quality_report.json").read_text(encoding="utf-8"))
        assert quality["summary"]["legacy_exported_count"] == len(legacy)
        assert quality["summary"]["legacy_exploration_chain_count"] <= len(legacy)
        assert quality["summary"]["legacy_single_step_merged_count"] >= 0
        assert "union" in quality["policy"].lower()
        coverage = json.loads((facts / "ui_paths_coverage_report.json").read_text(encoding="utf-8"))
        effect_filter = coverage["effect_paths_legacy_filter"]
        assert effect_filter["filtered"] == effect_filter["total"] - effect_filter["included"]
        legacy_filter = coverage["legacy_path_filter"]
        assert legacy_filter["legacy_path_count"] == len(legacy)
        assert legacy_filter["inventory_excluded_from_legacy_count"] >= 0
        assert "exploration" in legacy_filter["policy"].lower()
        assert not (facts / "app_model" / "features" / stale.name).exists()
        assert not (out / "intermediate" / "0_android_facts.__scan_tmp").exists()
    finally:
        if stale.exists():
            stale.unlink()


def test_exploration_legacy_segment_and_join():
    _ensure_bundled_import_path()
    from extractors.app_model_schema import exploration_legacy_join, is_exploration_legacy_segment

    assert exploration_legacy_join([" Browser ", "OK"]) == "Browser > OK"
    screen = {
        "kind": "screen",
        "label": "Main",
        "layout": "activity_main",
        "screen_class": "MainActivity",
        "display_role": "user_screen",
        "display_source": "class_name",
    }
    assert is_exploration_legacy_segment(screen)
    action_bad = {
        "kind": "action",
        "resolved_label": "L2:Localintent:L5",
        "element_id": "",
        "user_visible": True,
        "display_source": "ui",
        "display_role": "user_action",
    }
    assert not is_exploration_legacy_segment(action_bad)


def test_l2_navigation_evidence_is_not_legacy_ui_text():
    _ensure_bundled_import_path()
    from extractors import nav_pipeline
    from extractors.app_model_schema import build_path_record

    source = textwrap.dedent(
        """
        class MainActivity {
            private fun openDetails() {
                val intent = Intent(this, DetailActivity::class.java)
                startActivity(intent)
            }

            private fun openFactory() {
                startActivity(SettingsActivity.createIntent(this))
            }
        }
        """
    )

    layout_for = lambda name: name.replace("Activity", "").lower()
    local_edges = nav_pipeline.extract_l2_variable_intent_edges(
        source,
        "MainActivity",
        "app/src/main/java/MainActivity.kt",
        layout_for,
    )
    factory_edges = nav_pipeline.extract_l2_create_intent_edges(
        source,
        "MainActivity",
        "app/src/main/java/MainActivity.kt",
        layout_for,
    )
    edges = local_edges + factory_edges

    assert {e["trigger_kind"] for e in edges} == {"l2_local_intent", "l2_create_intent"}
    assert all(e["display_source"] == "synthetic_navigation" for e in edges)
    assert all(e["user_visible"] is False for e in edges)
    assert all(e["source_file"] == "app/src/main/java/MainActivity.kt" for e in edges)
    assert {e["enclosing_fn"] for e in edges} == {"openDetails", "openFactory"}

    record = build_path_record(
        [
            {"kind": "screen", "layout": "activity_main", "screen_class": "MainActivity", "label": "Main"},
            {
                "kind": "action",
                "layout": "activity_main",
                "screen_class": "MainActivity",
                "element_id": "",
                "tag": "virtual",
                "interaction": "tap",
                "resolved_label": "L2:Localintent:L5",
                "trigger": "l2:localIntent:L5",
                "virtual": True,
                "user_visible": False,
                "display_source": "synthetic_navigation",
            },
            {"kind": "screen", "layout": "detail", "screen_class": "DetailActivity", "label": "Detail"},
        ]
    )
    assert "L2:" in record["path_display"]
    assert "L2:" not in record["path_display_legacy"]
    assert record["path_display_legacy"] == "Main > Detail"


def test_wordpress_multilevel_legacy_completions_from_static_labels():
    _ensure_bundled_import_path()
    from main import _build_multilevel_completions

    strings = {
        "post_list_tab_published_posts": "Published",
        "post_list_tab_drafts": "Drafts",
        "post_list_tab_scheduled_posts": "Scheduled",
        "post_list_tab_trashed_posts": "Trashed",
        "pages_published": "Published",
        "pages_drafts": "Drafts",
        "pages_scheduled": "Scheduled",
        "pages_trashed": "Trashed",
        "media_all": "All",
        "media_images": "Images",
        "media_documents": "Documents",
        "media_videos": "Videos",
        "search": "Search",
    }
    effect_paths = {
        "paths": [
            {"path_parts": ["WP Launch", "My Site", "Content", "Posts"]},
            {"path_parts": ["WP Launch", "My Site", "Pages"]},
            {"path_parts": ["WP Launch", "My Site", "Content", "Media"]},
        ]
    }

    completions = set(_build_multilevel_completions(strings, effect_paths, []))

    assert "My Site > Posts > Published" in completions
    assert "My Site > Posts > Drafts" in completions
    assert "My Site > Posts > Scheduled" in completions
    assert "My Site > Posts > Trashed" in completions
    assert "My Site > Posts > Search" in completions
    assert "My Site > Pages > Published" in completions
    assert "My Site > Pages > Drafts" in completions
    assert "My Site > Pages > Scheduled" in completions
    assert "My Site > Pages > Trashed" in completions
    assert "My Site > Pages > Search" in completions
    assert "My Site > Media > All" in completions
    assert "My Site > Media > Images" in completions
    assert "My Site > Media > Documents" in completions
    assert "My Site > Media > Videos" in completions
    assert "My Site > Media > Search" in completions
    assert not any("Content > Posts >" in p for p in completions)


def test_multilevel_legacy_completions_require_discovered_roots():
    _ensure_bundled_import_path()
    from main import _build_multilevel_completions

    completions = _build_multilevel_completions(
        {"post_list_tab_drafts": "Drafts"},
        {"paths": [{"path_parts": ["WP Launch", "Reader", "Posts"]}]},
        [],
    )

    assert completions == []


def test_provider_backed_checkbox_options_are_report_only(tmp_path: Path):
    _ensure_bundled_import_path()
    from extractors import nav_pipeline

    src = tmp_path / "app" / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)
    (src / "JetpackAvailableItemsProvider.kt").write_text(
        textwrap.dedent(
            """
            package com.example

            object R {
                object string {
                    const val backup_item_themes = 1
                    const val backup_item_plugins = 2
                    const val backup_item_media_uploads = 3
                    const val backup_item_sqls = 4
                    const val backup_item_roots = 5
                }
            }

            class JetpackAvailableItemsProvider {
                fun getAvailableItems(): List<JetpackAvailableItem> {
                    return listOf(
                        JetpackAvailableItem(THEMES, R.string.backup_item_themes),
                        JetpackAvailableItem(PLUGINS, R.string.backup_item_plugins),
                        JetpackAvailableItem(MEDIA_UPLOADS, R.string.backup_item_media_uploads),
                        JetpackAvailableItem(SQLS, R.string.backup_item_sqls),
                        JetpackAvailableItem(ROOTS, R.string.backup_item_roots)
                    )
                }

                data class JetpackAvailableItem(
                    val availableItemType: JetpackAvailableItemType,
                    val labelResId: Int
                )

                enum class JetpackAvailableItemType { THEMES, PLUGINS, MEDIA_UPLOADS, SQLS, ROOTS }
            }
            """
        ),
        encoding="utf-8",
    )
    (src / "BackupDownloadStateListItemBuilder.kt").write_text(
        textwrap.dedent(
            """
            package com.example

            class BackupDownloadStateListItemBuilder {
                fun buildDetailsListStateItems(
                    availableItems: List<JetpackAvailableItemsProvider.JetpackAvailableItem>,
                    onCheckboxItemClicked: (JetpackAvailableItemsProvider.JetpackAvailableItemType) -> Unit
                ): List<Any> {
                    return availableItems.map {
                        CheckboxState(
                            availableItemType = it.availableItemType,
                            label = UiStringRes(it.labelResId),
                            checked = true,
                            onClick = { onCheckboxItemClicked(it.availableItemType) }
                        )
                    }
                }
            }
            """
        ),
        encoding="utf-8",
    )
    strings = {
        "backup_item_themes": "WordPress Themes",
        "backup_item_plugins": "WordPress Plugins",
        "backup_item_media_uploads": "Media Uploads",
        "backup_item_sqls": "Site database",
        "backup_item_roots": "WordPress root",
    }

    groups = nav_pipeline.collect_dynamic_option_groups(str(tmp_path), None, strings)
    provider_groups = [g for g in groups if g.get("items_source") == "provider_return_list"]
    assert len(provider_groups) == 1
    group = provider_groups[0]
    assert group["option_effect_kind"] == "state_toggle"
    assert group["checked_default"] == "true"
    assert group["provider_function"] == "getAvailableItems"
    assert group["model_class"] == "JetpackAvailableItem"
    assert group["options"] == [
        "WordPress Themes",
        "WordPress Plugins",
        "Media Uploads",
        "Site database",
        "WordPress root",
    ]

    effect_paths = nav_pipeline.build_ui_effect_paths(
        str(tmp_path),
        None,
        strings,
        launcher_class="WPLaunchActivity",
    )
    provider_rows = [
        p for p in effect_paths["paths"]
        if p.get("items_source") == "provider_return_list"
    ]
    assert {p["label"] for p in provider_rows} == set(group["options"])
    assert all(p["report_only"] is True for p in provider_rows)
    assert all(p["display_channel"] == "evidence" for p in provider_rows)
    assert all(p["user_visible"] is False for p in provider_rows)
    assert effect_paths["stats"]["provider_option_group_count"] == 1
    assert effect_paths["stats"]["provider_option_item_count"] == 5

    candidates = nav_pipeline.build_candidates_payload(str(tmp_path), None)
    assert candidates["stats"]["provider_option_catalog_count"] == 1
    assert candidates["stats"]["provider_option_group_count"] == 1


def test_generic_list_item_action_resolves_to_activity(tmp_path: Path):
    _ensure_bundled_import_path()
    from extractors import nav_pipeline

    src = tmp_path / "app" / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)
    (src / "ScreenItemBuilder.kt").write_text(
        textwrap.dedent(
            """
            package com.example

            class ScreenItemBuilder {
                fun build(onClick: (Action) -> Unit) = listOf(
                    CategoryHeaderItem(UiStringRes(R.string.section_content)),
                    ListItem(
                        label = UiStringRes(R.string.open_details),
                        onClick = ListItemInteraction.create(DETAILS, onClick)
                    )
                )
            }

            enum class Action { DETAILS }
            """
        ),
        encoding="utf-8",
    )
    (src / "ActionHandler.kt").write_text(
        textwrap.dedent(
            """
            package com.example

            class ActionHandler {
                fun handle(action: Action): NavAction {
                    return when (action) {
                        Action.DETAILS -> NavAction.OpenDetails
                    }
                }
            }

            sealed class NavAction {
                object OpenDetails : NavAction()
            }
            """
        ),
        encoding="utf-8",
    )
    (src / "HostFragment.kt").write_text(
        textwrap.dedent(
            """
            package com.example

            class HostFragment {
                fun navigate(action: NavAction) {
                    when (action) {
                        is NavAction.OpenDetails -> Launcher.openDetails(context)
                    }
                }
            }
            """
        ),
        encoding="utf-8",
    )
    (src / "Launcher.java").write_text(
        textwrap.dedent(
            """
            package com.example;

            public class Launcher {
                public static void openDetails(Context context) {
                    Intent intent = new Intent(context, DetailActivity.class);
                    context.startActivity(intent);
                }
            }
            """
        ),
        encoding="utf-8",
    )

    payload = nav_pipeline.build_ui_effect_paths(
        str(tmp_path),
        [],
        {
            "open_details": "Open Details",
            "example_section_screen_title": "Example",
            "section_content": "Content",
        },
        launcher_class="MainActivity",
        nav_graph={"class_layouts": {"DetailActivity": "detail_activity"}},
    )
    row = next(r for r in payload["paths"] if r.get("action_token") == "DETAILS")
    assert row["effect_kind"] == "activity"
    assert row["target_class"] == "DetailActivity"
    assert row["display_channel"] == "ui"
    assert row["user_visible"] is True
    assert row["path_parts"] == ["Main", "Example", "Content", "Open Details", "Detail"]


def test_pipeline_stage4_emit_scaffold_files(tmp_path: Path):
    out = tmp_path / "scaffold"
    run_pipeline(
        [
            "--android-root",
            str(ROOT / "fixtures" / "minimal_android"),
            "--facts-source",
            str(ROOT / "fixtures" / "minimal_facts"),
            "--out",
            str(out),
            "--stages",
            "0,1,2,3,4",
            "--emit-scaffold-files",
        ]
    )

    assert (out / "intermediate" / "4_scaffold" / "SCAFFOLD_PLAN.txt").is_file()
    assert (out / "intermediate" / "4_scaffold" / "README.md").is_file()
    assert (out / "intermediate" / "4_scaffold" / "harmony_arch.snapshot.json").is_file()


def test_pipeline_stage6_optional_viewer_export(tmp_path: Path):
    if not (ROOT / "viewer" / "feature_tree.html").is_file():
        pytest.skip("viewer/feature_tree.html not present in workspace")

    out = tmp_path / "viewer"
    run_pipeline(
        [
            "--android-root",
            str(ROOT / "fixtures" / "minimal_android"),
            "--facts-source",
            str(ROOT / "fixtures" / "minimal_facts"),
            "--out",
            str(out),
            "--stages",
            "0,1,2,3,5,6",
        ]
    )

    assert (out / "viewer" / "feature_tree.html").is_file()
    assert (out / "viewer" / "feature_tree.v1.json").is_file()
    assert (out / "viewer" / "taxonomy_report.json").is_file()
    assert (out / "viewer" / "vendor" / "vis-network.min.js").is_file()
    assert not (out / "agent_bundle.v1.json").exists()


def test_pipeline_stage_dependency_errors(tmp_path: Path):
    out = tmp_path / "missing"
    result = run_pipeline(
        [
            "--android-root",
            str(ROOT / "fixtures" / "minimal_android"),
            "--out",
            str(out),
            "--stages",
            "6",
        ],
        check=False,
    )

    assert result.returncode == 1
    assert "Stage 6 requires" in result.stderr
    assert "run stage 5 first" in result.stderr


def test_pipeline_taxonomy_overlay(tmp_path: Path):
    out = tmp_path / "overlay"
    overlay = tmp_path / "taxonomy_overlay.yaml"
    overlay.write_text(
        textwrap.dedent(
            """
            version: "1.0"
            features:
              - id: main.entry
                label: Main entry
                match:
                  class_name_regex: "(?i)MainActivity"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    run_pipeline(
        [
            "--android-root",
            str(ROOT / "fixtures" / "minimal_android"),
            "--facts-source",
            str(ROOT / "fixtures" / "minimal_facts"),
            "--out",
            str(out),
            "--stages",
            "0,1,5",
            "--taxonomy-overlay",
            str(overlay),
        ]
    )

    taxonomy = json.loads((out / "intermediate" / "5_feature_tree" / "taxonomy_report.json").read_text(encoding="utf-8"))
    assert any(f["feature_id"] == "main.entry" for f in taxonomy["features"])
    assert taxonomy["summary"]["matched_screen_count"] >= 2


# ── New unit tests for flavor detection, BottomNav, and RecyclerView scan ─────

def test_detect_default_flavor_isdefault(tmp_path):
    """detect_default_flavor returns the flavor marked isDefault true."""
    _ensure_bundled_import_path()
    from extractors.android_project import detect_default_flavor, _FLAVOR_CACHE
    _FLAVOR_CACHE.clear()

    gradle = tmp_path / "app" / "build.gradle"
    gradle.parent.mkdir(parents=True)
    gradle.write_text(
        textwrap.dedent("""\
            android {
                flavorDimensions 'app'
                productFlavors {
                    wordpress { }
                    jetpack { isDefault true }
                }
            }
        """),
        encoding="utf-8",
    )
    result = detect_default_flavor(tmp_path)
    assert result == "jetpack", f"Expected 'jetpack', got {result!r}"


def test_detect_default_flavor_first_defined(tmp_path):
    """detect_default_flavor falls back to first flavor when none is marked default."""
    _ensure_bundled_import_path()
    from extractors.android_project import detect_default_flavor, _FLAVOR_CACHE
    _FLAVOR_CACHE.clear()

    gradle = tmp_path / "app" / "build.gradle"
    gradle.parent.mkdir(parents=True)
    gradle.write_text(
        textwrap.dedent("""\
            android {
                productFlavors {
                    free { }
                    paid { }
                }
            }
        """),
        encoding="utf-8",
    )
    result = detect_default_flavor(tmp_path)
    assert result == "free", f"Expected 'free', got {result!r}"


def test_detect_default_flavor_none(tmp_path):
    """detect_default_flavor returns None for projects without productFlavors."""
    _ensure_bundled_import_path()
    from extractors.android_project import detect_default_flavor, _FLAVOR_CACHE
    _FLAVOR_CACHE.clear()

    gradle = tmp_path / "app" / "build.gradle"
    gradle.parent.mkdir(parents=True)
    gradle.write_text("android { compileSdkVersion 33 }\n", encoding="utf-8")
    assert detect_default_flavor(tmp_path) is None


def test_source_dirs_for_variant_no_flavor(tmp_path):
    """source_dirs_for_variant with flavor=None returns only src/main."""
    _ensure_bundled_import_path()
    from extractors.android_project import source_dirs_for_variant

    src_dir = tmp_path / "src"
    (src_dir / "main").mkdir(parents=True)
    result = source_dirs_for_variant(src_dir, None)
    assert len(result) == 1
    assert result[0].name == "main"


def test_source_dirs_for_variant_with_flavor(tmp_path):
    """source_dirs_for_variant includes flavor dir when it exists."""
    _ensure_bundled_import_path()
    from extractors.android_project import source_dirs_for_variant

    src_dir = tmp_path / "src"
    (src_dir / "main").mkdir(parents=True)
    (src_dir / "jetpack").mkdir(parents=True)
    result = source_dirs_for_variant(src_dir, "jetpack")
    names = [p.name for p in result]
    assert "main" in names
    assert "jetpack" in names


def test_bottomnav_edges_direct_pattern(tmp_path):
    """_extract_bottomnav_edges finds R.id.XXX → XxxFragment() patterns."""
    _ensure_bundled_import_path()
    import sys
    btools = str(ROOT / "bundled_spec_tools")
    if btools not in sys.path:
        sys.path.insert(0, btools)
    from extractors import navigation_extractor, android_project as ap
    from extractors.android_project import _FLAVOR_CACHE
    _FLAVOR_CACHE.clear()

    # Create a minimal source file with BottomNav listener
    src_dir = tmp_path / "src" / "main" / "java" / "com" / "example"
    src_dir.mkdir(parents=True)
    (src_dir / "MainActivity.kt").write_text(
        textwrap.dedent("""\
            package com.example
            class MainActivity {
                fun setup() {
                    bottomNav.setOnItemSelectedListener { item ->
                        when (item.itemId) {
                            R.id.nav_home -> showFragment(HomeFragment())
                            R.id.nav_profile -> showFragment(ProfileFragment())
                        }
                        true
                    }
                }
            }
        """),
        encoding="utf-8",
    )
    # Patch _find_layout_for_class to avoid needing _INFERRED_LAYOUTS
    original = navigation_extractor._find_layout_for_class
    navigation_extractor._find_layout_for_class = lambda c: c.lower()
    try:
        edges = navigation_extractor._extract_bottomnav_edges(tmp_path)
    finally:
        navigation_extractor._find_layout_for_class = original

    targets = {e["to"] for e in edges if e["type"] == "bottom_nav"}
    assert "HomeFragment" in targets, f"Expected HomeFragment in {targets}"
    assert "ProfileFragment" in targets, f"Expected ProfileFragment in {targets}"
    froms = {e["from"] for e in edges if e["type"] == "bottom_nav"}
    assert "MainActivity" in froms


def test_adapter_item_layouts_binding_pattern(tmp_path):
    """_extract_adapter_item_layouts detects XxxBinding::inflate in ViewHolder."""
    _ensure_bundled_import_path()
    from extractors import navigation_extractor
    from extractors.android_project import _FLAVOR_CACHE
    _FLAVOR_CACHE.clear()

    src_dir = tmp_path / "src" / "main" / "java" / "com" / "example"
    src_dir.mkdir(parents=True)

    (src_dir / "PostViewHolder.kt").write_text(
        textwrap.dedent("""\
            package com.example
            import androidx.recyclerview.widget.RecyclerView
            class PostViewHolder(parent: ViewGroup) : RecyclerView.ViewHolder(
                parent.viewBinding(PostCardBinding::inflate).root
            )
        """),
        encoding="utf-8",
    )
    (src_dir / "PostAdapter.kt").write_text(
        textwrap.dedent("""\
            package com.example
            import androidx.recyclerview.widget.ListAdapter
            class PostAdapter : ListAdapter<Post, PostViewHolder>(DiffCallback) {
                override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) =
                    PostViewHolder(parent)
            }
        """),
        encoding="utf-8",
    )

    result = navigation_extractor._extract_adapter_item_layouts(tmp_path)
    assert "PostAdapter" in result, f"PostAdapter not found in {list(result.keys())}"
    assert "post_card" in result["PostAdapter"], f"post_card not in {result['PostAdapter']}"


def test_taskstack_edges_kotlin(tmp_path):
    """_extract_taskstack_edges finds addNextIntent(Intent(ctx, Xxx::class.java))."""
    _ensure_bundled_import_path()
    from extractors import navigation_extractor
    from extractors.android_project import _FLAVOR_CACHE
    _FLAVOR_CACHE.clear()

    src_dir = tmp_path / "src" / "main" / "java" / "com" / "example"
    src_dir.mkdir(parents=True)
    (src_dir / "NotificationHandler.kt").write_text(
        textwrap.dedent("""\
            package com.example
            class NotificationHandler {
                fun open() {
                    TaskStackBuilder.create(context)
                        .addNextIntent(Intent(context, MainActivity::class.java))
                        .addNextIntent(Intent(context, PostDetailActivity::class.java))
                        .startActivities()
                }
            }
        """),
        encoding="utf-8",
    )
    original = navigation_extractor._find_layout_for_class
    navigation_extractor._find_layout_for_class = lambda c: c.lower()
    try:
        edges = navigation_extractor._extract_taskstack_edges(tmp_path)
    finally:
        navigation_extractor._find_layout_for_class = original

    targets = {e["to"] for e in edges if e["type"] == "task_stack"}
    assert "MainActivity" in targets, f"MainActivity not found in {targets}"
    assert "PostDetailActivity" in targets, f"PostDetailActivity not found in {targets}"


def test_resolve_label_sibling_prefix_heuristic():
    _ensure_bundled_import_path()
    from extractors.ui_dag_assembler import _resolve_label, _infer_label_from_sibling

    text_map = {
        "feature_title": "Dark Mode",
        "settings_label": "Enable Notifications",
        "other_title": "Irrelevant",
    }

    assert _infer_label_from_sibling("feature_enabled", text_map) == "Dark Mode"
    assert _infer_label_from_sibling("settings_switch", text_map) == "Enable Notifications"
    assert _infer_label_from_sibling("nomatch_enabled", text_map) == ""

    elem = {"id": "feature_enabled", "tag": "CheckBox"}
    assert _resolve_label(elem, text_map) == "Dark Mode"

    elem_no_sibling = {"id": "unknown_toggle", "tag": "Switch"}
    label = _resolve_label(elem_no_sibling, text_map)
    assert label == "Unknown"


def test_resolve_label_suffix_strip_enabled():
    _ensure_bundled_import_path()
    from extractors.ui_dag_assembler import _resolve_label

    elem = {"id": "feature_enabled", "tag": "CheckBox"}
    assert _resolve_label(elem, {}) == "Feature"

    elem2 = {"id": "auto_backup_enabled", "tag": "Switch"}
    assert _resolve_label(elem2, {}) == "Auto Backup"


def test_resolve_label_runtime_lookup():
    _ensure_bundled_import_path()
    from extractors.ui_dag_assembler import _resolve_label

    rt = {"status_toggle": "Auto Sync", "priority_selector": "High Priority"}
    elem = {"id": "status_toggle", "tag": "Switch"}
    assert _resolve_label(elem, {}, runtime_label_lookup=rt) == "Auto Sync"

    elem2 = {"id": "unknown_checkbox", "tag": "CheckBox"}
    assert _resolve_label(elem2, {}, runtime_label_lookup=rt) == "Unknown"


def test_extract_runtime_label_bindings():
    _ensure_bundled_import_path()
    from extractors.source_extractor import extract_runtime_label_bindings

    source = textwrap.dedent("""\
        fun bind(item: UiItem.FeatureFlag) {
            featureTitle.text = item.title
            statusLabel.text = "Active"
            binding.nameField.setText(getString(R.string.user_name))
            binding.versionText.text = model.version
        }
    """)
    results = extract_runtime_label_bindings(source, "TestAdapter.kt")

    assert len(results) >= 3

    by_view = {r["view_id"]: r for r in results}

    assert "feature_title" in by_view
    assert by_view["feature_title"]["label_source"] == "dynamic"

    assert "status_label" in by_view
    assert by_view["status_label"]["label_source"] == "literal"
    assert by_view["status_label"]["label_text"] == "Active"

    assert "name_field" in by_view
    assert by_view["name_field"]["label_source"] == "resource"
    assert by_view["name_field"]["resource_key"] == "user_name"

    assert "version_text" in by_view
    assert by_view["version_text"]["label_source"] == "dynamic"


def test_build_runtime_label_lookup_filters_dynamic():
    _ensure_bundled_import_path()
    from extractors.ui_dag_assembler import _build_runtime_label_lookup

    findings = {
        "findings": {
            "runtime_label_bindings": [
                {"view_id": "status_label", "label_source": "literal", "label_text": "Active"},
                {"view_id": "feature_title", "label_source": "dynamic", "label_text": "item.title"},
                {"view_id": "name_field", "label_source": "resource", "label_text": "User Name"},
            ]
        }
    }
    lookup = _build_runtime_label_lookup(findings)
    assert lookup == {"status_label": "Active", "name_field": "User Name"}
