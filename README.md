# harmony-migration-toolkit

Deterministic **Android → HarmonyOS migration IR** pipeline. It wraps [`spec-tools-for-opencode`](../spec-tools-for-opencode) for static facts (Stage 0), then emits versioned JSON artifacts for framework mapping and Harmony **architecture placeholders**.

Non-deterministic work (**LLM / human**) is restricted to `gap_items` and optional `llm_out/` — see [prompts/gap_prompt.md](prompts/gap_prompt.md).

## Design: Feature Tree and Viewer

End-to-end design for the planned **feature tree IR** (screen / UI / behavior / implementation anchors), **screen–screen edges**, **Harmony projection**, and **interactive HTML viewer** — implementation checklist included:

[docs/FEATURE_TREE_AND_VIEWER_DESIGN.md](docs/FEATURE_TREE_AND_VIEWER_DESIGN.md)

## Requirements

- Python 3.10+
- `pip install -r requirements.txt`
- Stage 0 (default): sibling checkout at `../spec-tools-for-opencode` or pass `--spec-tools-root`.

## Usage

```bash
cd harmony-migration-toolkit
pip install -r requirements.txt

# Full run (executes spec-tools main.py — may take several seconds)
python pipeline.py --android-root /path/to/android/project

# Reuse last spec-tools output without re-scanning
python pipeline.py --android-root /path/to/android/project --skip-spec-tools

# Tests / CI: inject pre-built facts
python pipeline.py --android-root ./fixtures/minimal_android --facts-source ./fixtures/minimal_facts --out ./out_fixture_test

# Refresh only feature tree + viewer from existing facts and android_facts
python pipeline.py --android-root /path/to/android/project --out ./out --stages 5,6

# Use an app-specific feature taxonomy overlay
python pipeline.py --android-root /path/to/android/project --taxonomy-overlay ./taxonomies/my_app.yaml

# Emit scaffold summary files
python pipeline.py ... --emit-scaffold-files
```

### Stages

| Stage | Output |
|-------|--------|
| 0 | `out/0_android_facts/` — copy of spec-tools `output/` + normalized paths + `manifest.json` |
| 1 | `out/1_android_facts/android_facts.v1.json` |
| 2 | `out/2_framework_map/framework_map.v1.json` |
| 3 | `out/3_harmony_arch/harmony_arch.v1.json` |
| 4 | Dry-run plan to stdout, or `out/4_scaffold/` with `--emit-scaffold-files` |
| 5 | `out/5_feature_tree/feature_tree.v1.json` + `feature_spec_evidence.json` + `verify_report.json` |
| 6 | `out/viewer/` — static feature tree viewer and sidecar JSON |

Select stages: `--stages 0,1,2` (comma-separated).

The default stage order is `0,1,2,3,5,4,6`: feature tree generation runs before scaffold emission so the viewer sidecars can be refreshed in the same default run.

### Feature taxonomy

Stage 5 uses [data/feature_taxonomy.yaml](data/feature_taxonomy.yaml) to group screens into logical `feature:*` nodes. The built-in taxonomy contains common app areas, but new apps should usually add an app-specific overlay with `--taxonomy-overlay`. Stage 5 writes `out/5_feature_tree/taxonomy_report.json`, including matched feature counts and `unmatched_screens`, so taxonomy rules can be iterated after the first run.

## LLM boundary (contract)

**Deterministic tools own:** merging XML + source facts (via spec-tools), Gradle/manifest parsing, framework **mapping tables** under [data/framework_map/rules.yaml](data/framework_map/rules.yaml), IR JSON and schema validation.

**LLM may assist:** filling `implementation_notes`, ArkTS/ArkUI drafts, Compose-heavy UI, JNI/NAPI ports — only via structured outputs described in [prompts/gap_prompt.md](prompts/gap_prompt.md).

**LLM must not:** silently change navigation graphs, invent screens, or override `rules_version` mappings without a human-reviewed table change.

## Schemas

JSON Schema Draft 2020-12 under [schemas/](schemas/): `android_facts.v1.schema.json`, `framework_map.v1.schema.json`, `harmony_arch.v1.schema.json`, `feature_tree.v1.schema.json`.

## Tests

```bash
pip install -r requirements.txt
pytest tests/ -q
```
