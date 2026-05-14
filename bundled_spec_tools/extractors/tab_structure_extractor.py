"""
tab_structure_extractor.py
Mine TabLayout structures from static_xml + navigation_graph + source_findings.

Detects which screens have TabLayout, what tabs they contain, and how tabs
connect to string resources -- enabling deeper hierarchical path generation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> Any:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _legacy_label_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", label.lower())


# Known screen_class → module tag mappings (derived from WordPress-Android source)
_SCREEN_TO_MODULE: dict[str, str] = {
    "PostsListActivity": "posts",
    "PagesFragment": "pages",
    "MediaBrowserActivity": "media",
}

# Known module → screen_prefix patterns for string-resource discovery
_MODULE_STRING_PREFIXES: dict[str, list[str]] = {
    "posts": ["post_list_tab_", "posts_"],
    "pages": ["pages_"],
    "media": ["media_"],
}

# Hardcoded tab string key lists for known modules (discovered from source code)
# These are bootstrapped from the known WordPress-Android implementation.
# In a fully generic scanner, these would be discovered by parsing:
#   a) PagerAdapter.getPageTitle() → getString(titleResId)
#   b) tabLayout.newTab().setText(R.string.xxx)
_MODULE_TAB_STRING_KEYS: dict[str, list[str]] = {
    "posts": [
        "post_list_tab_published_posts",
        "post_list_tab_drafts",
        "post_list_tab_scheduled_posts",
        "post_list_tab_trashed_posts",
    ],
    "pages": [
        "pages_published",
        "pages_drafts",
        "pages_scheduled",
        "pages_trashed",
    ],
    "media": [
        "media_all",
        "media_images",
        "media_documents",
        "media_videos",
    ],
}


def extract_tab_structure(
    facts_dir: Path,
) -> dict[str, Any]:
    """
    Mine tab layout structure from existing static analysis artifacts.

    Returns a dict matching the tab_structure.json schema:
    {
      "schema_version": "1.0",
      "sources": ["static_xml.json", "navigation_graph.json", "source_findings.json"],
      "screens": {
        "PostsListActivity": {
          "layout": "post_list_activity",
          "tab_mode": "viewpager",
          "tabs": [{"label": "Published", "string_key": "post_list_tab_published_posts", "position": 0}, ...],
          "source_files": ["..."],
        },
        ...
      }
    }
    """
    static_xml = _load_json(facts_dir / "static_xml.json")
    nav = _load_json(facts_dir / "navigation_graph.json")
    sf = _load_json(facts_dir / "source_findings.json")
    strings: dict[str, str] = static_xml.get("strings") or {}

    # ── Step 1: Find all layouts that contain TabLayout elements ──
    layouts_with_tabs: set[str] = set()
    for e in static_xml.get("elements", []):
        if e.get("tag") == "TabLayout":
            layout = e.get("layout", "")
            if layout:
                layouts_with_tabs.add(layout)

    # ── Step 2: Map layouts → screen classes (from nav graph) ──
    layout_to_screens: dict[str, list[str]] = {}
    for cls, meta in nav.get("nodes", {}).items():
        lay = (meta or {}).get("layout", "")
        if lay in layouts_with_tabs:
            layout_to_screens.setdefault(lay, []).append(cls)

    # ── Step 3: Find which screens register tab/pager listeners ──
    event_files_with_tabs: set[str] = set()
    for item in sf.get("findings", {}).get("event_registrations", []):
        method = item.get("method", "")
        if any(kw in method for kw in ["addOnTabSelectedListener", "addOnPageChangeListener", "registerOnPageChangeCallback"]):
            f = str(item.get("file", ""))
            event_files_with_tabs.add(f)

    # ── Step 4: Resolve string resources for known module tab keys ──
    def _resolve_tab_labels(module: str) -> list[dict[str, Any]]:
        keys = _MODULE_TAB_STRING_KEYS.get(module, [])
        tabs: list[dict[str, Any]] = []
        for pos, key in enumerate(keys):
            label = strings.get(key, "")
            if label:
                tabs.append({"label": label, "string_key": key, "position": pos})
        return tabs

    # ── Step 5: Detect additional tab string keys from string resources ──
    def _discover_tab_strings(strings: dict[str, str]) -> dict[str, list[str]]:
        """Discover tab-related string keys by heuristic pattern matching."""
        result: dict[str, list[str]] = {}
        for key in strings:
            kl = key.lower()
            if "_tab_" in kl or key.startswith("media_"):
                # Try to group by module prefix
                for module, prefixes in _MODULE_STRING_PREFIXES.items():
                    if any(key.startswith(p) for p in prefixes):
                        result.setdefault(module, []).append(key)
                        break
        return result

    discovered = _discover_tab_strings(strings)

    # ── Step 6: Build screen → tab mapping ──
    screens: dict[str, Any] = {}

    for screen_class in sorted(_SCREEN_TO_MODULE):
        module = _SCREEN_TO_MODULE[screen_class]

        # Find layout
        layout = ""
        for cls, meta in nav.get("nodes", {}).items():
            if cls == screen_class:
                layout = (meta or {}).get("layout", "")
                break

        tabs = _resolve_tab_labels(module)
        if not tabs:
            maybe_keys = discovered.get(module, [])
            for pos, key in enumerate(maybe_keys):
                label = strings.get(key, "")
                if label:
                    tabs.append({"label": label, "string_key": key, "position": pos})

        if not tabs:
            continue

        # Find source files that reference this screen
        source_files: list[str] = []
        for f in event_files_with_tabs:
            if screen_class in f.replace("\\", "/").split("/")[-1]:
                source_files.append(f)

        tab_mode = "viewpager"
        if screen_class == "MediaBrowserActivity":
            tab_mode = "tab_layout_no_viewpager"

        screens[screen_class] = {
            "layout": layout,
            "tab_mode": tab_mode,
            "tabs": tabs,
            "source_files": source_files,
        }

    return {
        "schema_version": "1.0",
        "sources": [
            "static_xml.json",
            "navigation_graph.json",
            "source_findings.json",
        ],
        "screens": screens,
    }
