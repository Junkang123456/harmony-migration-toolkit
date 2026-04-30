# LLM prompt contract: `gap_items` only

Use this template when invoking an LLM for **implementation details** that the deterministic pipeline cannot produce.

## Inputs (read-only for the model)

1. `2_framework_map/framework_map.v1.json` — field `gap_items[]` only.
2. Original Android sources at paths implied by each `source_ref` (e.g. `navigation:ClassName` → search under `--android-root`).
3. Optional: `0_android_facts/specs/*_spec.json` for the same screen.

## Output rules

- Write **only** new files under `llm_out/` (e.g. `llm_out/<gap_id>.md` or `.ets` drafts) **or** a single `llm_out/gap_fill.json` with shape:

```json
{
  "schema_version": "1.0",
  "entries": [
    {
      "gap_id": "SYN_0_...",
      "implementation_notes": "...",
      "proposed_files": [{"path": "relative/path.ets", "content": "..."}]
    }
  ]
}
```

- **Do not** edit `navigation_graph.json`, `android_facts.v1.json`, or `data/framework_map/rules.yaml`.
- **Do not** claim new screens or routes that are absent from `1_android_facts/android_facts.v1.json`.

## Validation

After generation, run project formatters / static checks. Failed outputs must be revised without mutating deterministic IR.
