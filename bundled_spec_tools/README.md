# Bundled static analysis (legacy: spec-tools-for-opencode)

This directory is **vendored** into `harmony-migration-toolkit` so Stage 0 does not depend on a sibling checkout.

- **Entry**: `main.py` — same CLI as before: `python main.py <android_project_root>`
- **Output**: writes to `./output/` relative to this directory when run standalone; the migration **`pipeline.py` copies** that tree into `intermediate/0_android_facts/`.

Upstream logic originated from `spec-tools-for-opencode`; prefer editing here so releases stay self-contained. Optional override: `python pipeline.py ... --spec-tools-root /other/copy`.
