from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_pipeline(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(ROOT / "pipeline.py"), *args]
    return subprocess.run(cmd, cwd=str(ROOT), check=check, capture_output=True, text=True)


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
        assert not (facts / "app_model" / "features" / stale.name).exists()
        assert not (out / "intermediate" / "0_android_facts.__scan_tmp").exists()
    finally:
        if stale.exists():
            stale.unlink()


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
