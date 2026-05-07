# harmony-migration-toolkit

Deterministic **Android → HarmonyOS migration IR** pipeline. Stage 0 runs the **bundled** static analyzer under [`bundled_spec_tools/`](bundled_spec_tools/) (vendored from the former `spec-tools-for-opencode` tree), then emits one agent-consumable migration bundle plus reproducible intermediate artifacts.

Non-deterministic work (**LLM / human**) is restricted to `gap_items` and optional `llm_out/` — see [prompts/gap_prompt.md](prompts/gap_prompt.md).

## Design: Feature Tree and Viewer

End-to-end design for the planned **feature tree IR** (screen / UI / behavior / implementation anchors), **screen–screen edges**, **Harmony projection**, and **interactive HTML viewer** — implementation checklist included:

[docs/FEATURE_TREE_AND_VIEWER_DESIGN.md](docs/FEATURE_TREE_AND_VIEWER_DESIGN.md)

## Requirements

- Python 3.10+
- `pip install -r requirements.txt`
- Stage 0 (default): uses `harmony-migration-toolkit/bundled_spec_tools/`; pass `--spec-tools-root` only if you maintain a fork elsewhere.

## Usage

```bash
cd harmony-migration-toolkit
pip install -r requirements.txt

# Full run
python pipeline.py --android-root /path/to/android/project
```

The default full run executes all deterministic stages needed for agent handoff. The main file to inspect is:

```text
/path/to/android/project/harmony_migration_out/agent_bundle.v1.json
```

Detailed reproducible artifacts live under `/path/to/android/project/harmony_migration_out/intermediate/`.

To write output somewhere else, pass `--out`:

```bash
python pipeline.py --android-root /path/to/android/project --out /path/to/output
```

### Stages

| Stage | Output |
|-------|--------|
| 0 | `<output>/intermediate/0_android_facts/` — copy of `bundled_spec_tools/output/` + normalized paths + `manifest.json` |
| 1 | `<output>/intermediate/1_android_facts/android_facts.v1.json` |
| 2 | `<output>/intermediate/2_framework_map/framework_map.v1.json` |
| 3 | `<output>/intermediate/3_harmony_arch/harmony_arch.v1.json` |
| 4 | Dry-run plan to stdout, or `<output>/intermediate/4_scaffold/` with `--emit-scaffold-files` |
| 5 | `<output>/intermediate/5_feature_tree/feature_tree.v1.json` + `feature_spec_evidence.json` + `verify_report.json` |
| 6 | Optional debug `<output>/viewer/` — static feature tree viewer and sidecar JSON |
| 7 | `<output>/agent_bundle.v1.json` — final agent-consumable migration bundle |

The default output directory is `<android-root>/harmony_migration_out`. The default stage order is `0,1,2,3,5,4,7`: feature tree generation runs before scaffold emission and final bundle export. The root output is intentionally small: `agent_bundle.v1.json` is the deliverable, while deterministic debugging artifacts live under `intermediate/`.

### Advanced / Debug

Most users should use the full run above. These flags are mainly for tests, debugging, or rerunning part of an existing output:

```bash
# Reuse existing bundled_spec_tools/output without rescanning the Android project
python pipeline.py --android-root /path/to/android/project --skip-spec-tools

# Run selected stages only
python pipeline.py --android-root /path/to/android/project --stages 5,7

# Generate the optional HTML debug viewer
python pipeline.py --android-root /path/to/android/project --stages 0,1,2,3,5,6
```

### Feature taxonomy

Stage 5 groups screens into logical `feature:*` nodes with deterministic automatic mining from screen names, layouts, packages, source paths, and navigation affinity. There is **no** bundled product taxonomy; pass `--taxonomy` (base YAML) and/or repeated `--taxonomy-overlay` only when you want explicit rules on top of the generated grouping.

Stage 5 writes `<output>/intermediate/5_feature_tree/taxonomy_report.json`, including generated feature counts, matched screen counts, and any remaining `unmatched_screens`.

## LLM boundary (contract)

**Deterministic tools own:** merging XML + source facts (via `bundled_spec_tools/`), Gradle/manifest parsing, framework **mapping tables** under [data/framework_map/rules.yaml](data/framework_map/rules.yaml), IR JSON and schema validation.

**LLM may assist:** filling `implementation_notes`, ArkTS/ArkUI drafts, Compose-heavy UI, JNI/NAPI ports — only via structured outputs described in [prompts/gap_prompt.md](prompts/gap_prompt.md).

**LLM must not:** silently change navigation graphs, invent screens, or override `rules_version` mappings without a human-reviewed table change.

## Schemas

JSON Schema Draft 2020-12 under [schemas/](schemas/): `android_facts.v1.schema.json`, `framework_map.v1.schema.json`, `harmony_arch.v1.schema.json`, `feature_tree.v1.schema.json`.

## Tests

```bash
pip install -r requirements.txt
pytest tests/ -q
```
