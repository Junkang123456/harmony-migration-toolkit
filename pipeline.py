#!/usr/bin/env python3
"""
Deterministic Android → Harmony migration IR pipeline.

Usage:
  python pipeline.py --android-root PATH [--out DIR] [--stages 0,1,2,3,5,4,7]
  python pipeline.py --android-root PATH --facts-source PATH  # skip Stage 0 scanner (tests)
  python pipeline.py ... --stages 5,7  # refresh feature tree + agent bundle when facts exist
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jsonschema import validators

# Allow `python pipeline.py` from toolkit root without installing as package
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stages.build_android_facts import build_android_facts
from stages.build_feature_tree import build_feature_tree
from stages.build_framework_map import build_framework_map
from stages.build_harmony_arch import build_harmony_arch
from stages.export_agent_bundle import export_agent_bundle
from stages.export_feature_tree_view import export_feature_tree_view
from stages.stage0_run_spec_tools import run_stage0
from stages.stage4_emit_scaffold import emit_scaffold_dry_run
from stages._util import toolkit_root


def _load_schema(name: str) -> dict:
    p = toolkit_root() / "schemas" / name
    return json.loads(p.read_text(encoding="utf-8"))


def _validate(instance: dict, schema_name: str) -> None:
    schema = _load_schema(schema_name)
    cls = validators.validator_for(schema)
    cls.check_schema(schema)
    validator = cls(schema, format_checker=None)
    errors = sorted(validator.iter_errors(instance), key=lambda e: e.path)
    if errors:
        msg = "\n".join(f"  {'/'.join(str(x) for x in e.path)}: {e.message}" for e in errors[:12])
        raise ValueError(f"Schema {schema_name} validation failed:\n{msg}")


def _validate_file(path: Path, schema_name: str) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    _validate(data, schema_name)
    return data


def _require_file(path: Path, stage: int, producer: str) -> bool:
    if path.is_file():
        return True
    print(f"Stage {stage} requires {path} ({producer}).", file=sys.stderr)
    return False


def _require_dir(path: Path, stage: int, producer: str) -> bool:
    if path.is_dir():
        return True
    print(f"Stage {stage} requires {path} ({producer}).", file=sys.stderr)
    return False


def _parse_stages(raw: str) -> set[int]:
    try:
        stages = {int(x.strip()) for x in raw.split(",") if x.strip()}
    except ValueError as exc:
        raise ValueError(f"--stages must be comma-separated integers, got: {raw}") from exc
    unknown = sorted(s for s in stages if s not in {0, 1, 2, 3, 4, 5, 6, 7})
    if unknown:
        raise ValueError(f"Unknown stage(s): {unknown}. Valid stages are 0,1,2,3,4,5,6,7.")
    return stages


def main() -> int:
    root = toolkit_root()
    parser = argparse.ArgumentParser(description="Harmony migration deterministic IR pipeline")
    parser.add_argument("--android-root", type=Path, required=True, help="Android project root")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: ANDROID_ROOT/harmony_migration_out)",
    )
    parser.add_argument(
        "--spec-tools-root",
        type=Path,
        default=None,
        help="Override bundled static analyzer root (default: toolkit bundled_spec_tools/)",
    )
    parser.add_argument(
        "--stages",
        type=str,
        default="0,1,2,3,5,4,7",
        help="Comma-separated stage numbers (default 0,1,2,3,5,4,7)",
    )
    parser.add_argument(
        "--skip-spec-tools",
        action="store_true",
        help="Debug/cache mode: reuse SPEC_TOOLS_ROOT/output without re-running the bundled scanner",
    )
    parser.add_argument(
        "--facts-source",
        type=Path,
        default=None,
        help="Copy pre-built facts tree to 0_android_facts (skips bundled static analyzer run)",
    )
    parser.add_argument(
        "--taxonomy",
        type=Path,
        default=None,
        help="Optional feature taxonomy YAML for stage 5 (no bundled defaults; use with --taxonomy-overlay as needed)",
    )
    parser.add_argument(
        "--taxonomy-overlay",
        type=Path,
        action="append",
        default=[],
        help="Feature taxonomy YAML overlay for stage 5; may be repeated",
    )
    parser.add_argument(
        "--emit-scaffold-files",
        action="store_true",
        help="Stage 4 writes 4_scaffold/ files; default is dry-run text only to stdout",
    )
    args = parser.parse_args()

    android_root = args.android_root.resolve()
    out_dir = args.out.resolve() if args.out else android_root / "harmony_migration_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        stages = _parse_stages(args.stages)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    intermediate_dir = out_dir / "intermediate"
    intermediate_dir.mkdir(parents=True, exist_ok=True)
    facts_dir = intermediate_dir / "0_android_facts"
    android_facts_path = intermediate_dir / "1_android_facts" / "android_facts.v1.json"
    framework_map_path = intermediate_dir / "2_framework_map" / "framework_map.v1.json"
    harmony_arch_path = intermediate_dir / "3_harmony_arch" / "harmony_arch.v1.json"
    feature_tree_path = intermediate_dir / "5_feature_tree" / "feature_tree.v1.json"
    agent_bundle_path = out_dir / "agent_bundle.v1.json"

    if 0 in stages:
        run_stage0(
            android_root,
            intermediate_dir,
            args.spec_tools_root,
            skip_spec_tools=args.skip_spec_tools,
            facts_source=args.facts_source,
        )

    if 1 in stages:
        if not _require_dir(facts_dir, 1, "run stage 0 or provide existing 0_android_facts"):
            return 1
        build_android_facts(
            android_root,
            facts_dir,
            android_facts_path,
        )
        _validate_file(android_facts_path, "android_facts.v1.schema.json")

    if 2 in stages:
        if 1 not in stages and not _require_file(android_facts_path, 2, "run stage 1 first"):
            return 1
        build_framework_map(
            android_facts_path,
            framework_map_path,
        )
        _validate_file(framework_map_path, "framework_map.v1.schema.json")

    if 3 in stages:
        if 1 not in stages and not _require_file(android_facts_path, 3, "run stage 1 first"):
            return 1
        if 2 not in stages and not _require_file(framework_map_path, 3, "run stage 2 first"):
            return 1
        build_harmony_arch(
            android_facts_path,
            framework_map_path,
            harmony_arch_path,
        )
        _validate_file(harmony_arch_path, "harmony_arch.v1.schema.json")

    if 5 in stages:
        if 1 not in stages and not _require_file(android_facts_path, 5, "run stage 1 first"):
            return 1
        if not _require_dir(facts_dir, 5, "run stage 0 first"):
            return 1
        build_feature_tree(
            android_facts_path,
            facts_dir,
            feature_tree_path,
            taxonomy_path=args.taxonomy.resolve() if args.taxonomy else None,
            taxonomy_overlay_paths=[p.resolve() for p in args.taxonomy_overlay],
            harmony_arch_path=harmony_arch_path if harmony_arch_path.is_file() else None,
        )
        _validate_file(feature_tree_path, "feature_tree.v1.schema.json")

    if 4 in stages:
        if 3 not in stages and not _require_file(harmony_arch_path, 4, "run stage 3 first"):
            return 1
        text = emit_scaffold_dry_run(
            harmony_arch_path,
            intermediate_dir,
            write_files=args.emit_scaffold_files,
        )
        if not args.emit_scaffold_files:
            print(text)

    if 6 in stages:
        if 5 not in stages and not _require_file(feature_tree_path, 6, "run stage 5 first"):
            return 1
        export_feature_tree_view(
            feature_tree_path,
            out_dir / "viewer",
            framework_map_path=framework_map_path if framework_map_path.is_file() else None,
            harmony_arch_path=harmony_arch_path if harmony_arch_path.is_file() else None,
        )

    if 7 in stages:
        if 5 not in stages and not _require_file(feature_tree_path, 7, "run stage 5 first"):
            return 1
        export_agent_bundle(
            feature_tree_path=feature_tree_path,
            evidence_path=feature_tree_path.parent / "feature_spec_evidence.json",
            verify_report_path=feature_tree_path.parent / "verify_report.json",
            taxonomy_report_path=feature_tree_path.parent / "taxonomy_report.json",
            framework_map_path=framework_map_path,
            harmony_arch_path=harmony_arch_path,
            android_facts_path=android_facts_path,
            facts_dir=facts_dir,
            intermediate_dir=intermediate_dir,
            out_path=agent_bundle_path,
        )
        _validate_file(agent_bundle_path, "agent_bundle.v1.schema.json")

    print("Pipeline completed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
