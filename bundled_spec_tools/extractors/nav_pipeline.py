"""
nav_pipeline.py

Three-tier navigation enrichment (cross-app, versioned rules):

- L1: navigation_candidates — facts + non-navigating effects (audit trail)
- L2: promote generic Kotlin/Java patterns to edges (createIntent factory, local Intent var)
- L3: optional per-repo overlay JSON merged into edges

Used from navigation_extractor.run(); does not replace existing regex extraction.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from bisect import bisect_right
from pathlib import Path
from typing import Callable

from extractors import android_project

NAV_PIPELINE_VERSION = "1.0"

_RULES_PATH = Path(__file__).resolve().parent.parent / "data" / "nav_rules.json"
_PRINT_MEDIA_FALLBACK_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "android_print_media_sizes.v1.json"
)
_DOCUMENT_PICKER_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "android_document_picker_roots.v1.json"
)
_STRING_LABEL_INDEX_CACHE: dict[int, list[tuple[set[str], str, int]]] = {}


def load_nav_rules() -> dict:
    try:
        return json.loads(_RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "nav_rules_version": "0",
            "handler_anchor_suffixes": ["Handler", "Delegate"],
            "overlay_relative_paths": [
                ".spec-tools/navigation_overlay.v1.json",
                "tools/navigation_overlay.v1.json",
            ],
        }


def _media_size_label(constant: str) -> str:
    m = re.match(r"ISO_([ABC])(\d+)$", constant)
    if m:
        return f"ISO {m.group(1)}{m.group(2)}"
    return constant.replace("_", " ").title()


def _android_sdk_candidates() -> list[Path]:
    candidates: list[Path] = []
    for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = os.environ.get(env_name)
        if value:
            candidates.append(Path(value))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Android" / "Sdk")
    candidates.append(Path.home() / "AppData" / "Local" / "Android" / "Sdk")
    return candidates


def load_android_print_media_size_catalog() -> dict:
    """Android framework paper-size catalog for system print UI paths."""
    for sdk_root in _android_sdk_candidates():
        sources_dir = sdk_root / "sources"
        if not sources_dir.exists():
            continue
        for java_file in sorted(
            sources_dir.glob("android-*/android/print/PrintAttributes.java"),
            reverse=True,
        ):
            try:
                text = java_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            constants = sorted(set(re.findall(r"public\s+static\s+final\s+MediaSize\s+(ISO_[ABC]\d+)\b", text)))
            if constants:
                return {
                    "catalog_source": str(java_file),
                    "items": [{"id": c, "label": _media_size_label(c)} for c in constants],
                }

    try:
        payload = json.loads(_PRINT_MEDIA_FALLBACK_PATH.read_text(encoding="utf-8"))
        items = payload.get("items", [])
        return {
            "catalog_source": str(payload.get("catalog_source") or _PRINT_MEDIA_FALLBACK_PATH),
            "items": items,
        }
    except (OSError, json.JSONDecodeError):
        return {
            "catalog_source": "builtin:minimal_iso_abc",
            "items": [
                {"id": f"ISO_{series}{n}", "label": f"ISO {series}{n}"}
                for series in ("A", "B", "C")
                for n in range(0, 11)
            ],
        }


def load_android_document_picker_catalog() -> dict:
    """Versioned catalog for Android DocumentsUI roots/folders."""
    try:
        payload = json.loads(_DOCUMENT_PICKER_CATALOG_PATH.read_text(encoding="utf-8"))
        return {
            "catalog_source": str(payload.get("catalog_source") or "documentsui_catalog_v1"),
            "items": list(payload.get("items") or []),
        }
    except (OSError, json.JSONDecodeError):
        return {
            "catalog_source": "builtin:documentsui_catalog_v1",
            "items": [
                {"id": "recent", "label": "Recent", "provider_scope": "framework_root"},
                {"id": "downloads", "label": "Downloads", "provider_scope": "framework_root"},
                {"id": "documents", "label": "Documents", "provider_scope": "common_folder"},
                {"id": "phone_storage", "label": "Phone storage", "provider_scope": "storage_root"},
                {
                    "id": "error_reports",
                    "label": "Provider-dependent folder > Error reports",
                    "provider_scope": "provider_dependent",
                },
            ],
        }


def gather_kt_sources(project_root: str, dep_roots: list[str] | None) -> list[tuple[Path, str, str, str]]:
    """(absolute_path, source, class_name/stem, relative_display_path)."""
    items: list[tuple[Path, str, str, str]] = []
    root = Path(project_root)

    def _is_generated_or_build_file(path: Path) -> bool:
        parts = {p.lower() for p in path.parts}
        return bool(parts & {"build", "generated", "intermediates", ".gradle"})

    for src_file in android_project.source_files(root):
        if _is_generated_or_build_file(src_file):
            continue
        rel = android_project.relative_to_root(src_file, root)
        items.append(
            (
                src_file,
                src_file.read_text(encoding="utf-8", errors="ignore"),
                src_file.stem,
                rel,
            )
        )

    for dep in dep_roots or []:
        dep_root = Path(dep)
        dep_name = dep_root.name
        for src_file in android_project.source_files(dep_root):
            if _is_generated_or_build_file(src_file):
                continue
            rel = f"{dep_name}/{android_project.relative_to_root(src_file, dep_root)}"
            items.append(
                (
                    src_file,
                    src_file.read_text(encoding="utf-8", errors="ignore"),
                    src_file.stem,
                    rel,
                )
            )
    return items


_CREATE_INTENT_START = re.compile(
    r"startActivity(?:ForResult)?\s*\(\s*(\w+)\.createIntent\s*\(",
    re.MULTILINE,
)

_VAR_START_ACTIVITY = re.compile(
    r"startActivity(?:ForResult)?\s*\(\s*(\w+)\s*\)",
    re.MULTILINE,
)

_SKIP_VAR_NAMES = frozenset(
    {
        "Intent",
        "this",
        "it",
        "super",
        "null",
    }
)

# val x = Intent(..., Target::class.java)  or  Intent(ctx, Target::class.java)
_ASSIGN_INTENT_TARGET = re.compile(
    r"(?:val|var)\s+(\w+)\s*=\s*Intent\s*\(\s*[^,]*,\s*(\w+)::class\.java",
    re.MULTILINE,
)

# Java: Intent intent = new Intent(context, TargetActivity.class);
_ASSIGN_INTENT_TARGET_JAVA = re.compile(
    r"(?:Intent\s+)?(\w+)\s*=\s*new\s+Intent\s*\([^,)]+,\s*(\w+)\.class\s*\)",
    re.MULTILINE,
)

# Java/Kotlin helper method that returns an Intent containing a concrete target:
#   private static Intent getMainActivityInNewStack(...) { ... new Intent(ctx, Foo.class) ... }
_HELPER_INTENT_METHOD = re.compile(
    r"(?:private|public|protected|static|\s)+"
    r"(?:static\s+)?Intent\s+(\w+)\s*\([^)]*\)[^{]*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}",
    re.MULTILINE | re.DOTALL,
)

# Kotlin override fun createFragment(pos: Int): Fragment { return FooFragment() }
_ADAPTER_CREATE_FRAGMENT_KT = re.compile(
    r"override\s+fun\s+(?:createFragment|getItem)\s*\([^)]*\)[^{]*\{[^}]*\breturn\s+(\w+)\s*[\(\.]",
    re.MULTILINE,
)

# Java @Override public Fragment createFragment(int pos) { return new FooFragment(); }
_ADAPTER_CREATE_FRAGMENT_JAVA = re.compile(
    r"@Override\s*\n?\s*public\s+(?:Fragment\b[^(]*|androidx\.fragment\.app\.Fragment\s+)"
    r"(?:createFragment|getItem)\s*\([^)]*\)\s*\{[^}]*\breturn\s+new\s+(\w+)\s*\(",
    re.MULTILINE | re.DOTALL,
)

_PRINT_SERVICE = re.compile(
    r"(?:getSystemService\s*\(\s*Context\.PRINT_SERVICE|PrintManager\b)",
    re.MULTILINE,
)

_STRING_RES = r"R\.string\.(\w+)"
_UI_ITEM_CALL = re.compile(r"\b(\w*Item)\s*\([\s\S]{0,300}?" + _STRING_RES, re.MULTILINE)
_SETTING_ITEM_CALL = re.compile(
    r"\b(\w*(?:SettingItem|Preference))\s*\([\s\S]{0,300}?(?:title\s*=\s*)?" + _STRING_RES,
    re.MULTILINE,
)
_ACTION_TOKEN = re.compile(
    r"\b(?:on\w*Clicked|onClick|dispatch)\s*\(\s*(?:\w+\.)?(\w+)(?:\s*\([^)]*\))?",
    re.MULTILINE,
)
_BRANCH_TOKEN = re.compile(
    r"(?m)^\s*(?:[\w.]+\s*,\s*)*(?:is\s+)?(?:\w+\.)?(\w+)(?:\([^)]*\))?\s*->"
)


def _line_for(source: str, pos: int) -> int:
    return source[:pos].count("\n") + 1


def _enclosing_function_before(source: str, pos: int) -> str:
    """Best-effort Kotlin/Java function name before a source position."""
    context = source[max(0, pos - 4000) : pos]
    fn_name = ""
    for match in re.finditer(
        r"(?:private|public|protected|internal)?\s*(?:override\s+)?(?:suspend\s+)?fun\s+(\w+)",
        context,
    ):
        fn_name = match.group(1)
    return fn_name


def _window(source: str, pos: int, max_len: int = 1800) -> str:
    return source[pos : pos + max_len]


def _display_from_class(name: str) -> str:
    """Human-ish display for class/action names without relying on app-specific maps."""
    if not name:
        return ""
    base = name.split("$")[0]
    base = re.sub(r"(Activity|Fragment)$", "", base)
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|\d+", base)
    return " ".join(words) or base


def _resolve_screen_class(from_class: str, nav_graph: dict) -> str:
    """Resolve a builder/provider class to the nearest screen class via camelCase word-prefix matching.

    Uses word-level prefix matching (not character-level) to find the screen class
    that shares the longest common prefix of camelCase words with `from_class`.
    Accepts a match if the common prefix covers at least 2 words OR at least 50%
    of the shorter name's words. Prefers Activity/Fragment/Dialog classes as tiebreaker.
    Returns empty string if no match is found (caller falls back to _display_from_class).
    """
    if not from_class or not nav_graph:
        return ""
    screen_set = set(nav_graph.get("class_layouts", {}).keys())
    if from_class in screen_set:
        return from_class

    from_words = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|\d+", from_class)
    if not from_words:
        return ""

    best = ""
    best_score = 0
    best_proportion = 0.0
    best_is_screen = False

    for cls in screen_set:
        if cls == from_class:
            continue
        cls_words = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|\d+", cls)
        if not cls_words:
            continue
        score = 0
        for a, b in zip(from_words, cls_words):
            if a == b:
                score += 1
            else:
                break
        if score < 1:
            continue
        shorter_len = min(len(from_words), len(cls_words))
        proportion = score / shorter_len if shorter_len else 0
        if score < 2 and proportion < 0.5:
            continue
        is_screen = cls.endswith("Activity") or cls.endswith("Fragment") or cls.endswith("Dialog")
        if (score > best_score or
            (score == best_score and proportion > best_proportion) or
            (score == best_score and proportion == best_proportion and is_screen and not best_is_screen)):
            best_score = score
            best_proportion = proportion
            best = cls
            best_is_screen = is_screen

    return best


def _display_from_function(name: str) -> str:
    base = re.sub(r"^(?:show|open|display)", "", name)
    return _display_from_class(base or name)


def _resource_label(label_key: str, strings: dict[str, str] | None) -> str:
    if strings and label_key in strings:
        val = strings[label_key]
        m = re.match(r"^@string/(\w+)$", val)
        if m and m.group(1) in strings:
            return strings[m.group(1)]
        return val
    return label_key.replace("_", " ").title()


def _words_from_identifier(value: str) -> set[str]:
    parts = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|\d+", value)
    if not parts:
        parts = re.split(r"[^a-zA-Z0-9]+", value)
    stop = {
        "activity", "adapter", "builder", "card", "fragment", "handler", "helper",
        "item", "items", "list", "model", "screen", "state", "use", "view",
        "viewmodel",
    }
    return {p.lower() for p in parts if p and p.lower() not in stop}


def _infer_source_area_label(
    class_name: str,
    rel: str,
    strings: dict[str, str] | None,
) -> str:
    """Infer a user-visible area label from generic source/class words and string keys."""
    if not strings:
        return ""
    source_words = _words_from_identifier(class_name)
    source_words.update(_words_from_identifier(Path(rel).stem))
    rel_parts = Path(rel).parts
    for part in rel_parts[-4:]:
        source_words.update(_words_from_identifier(part))
    if not source_words:
        return ""
    source_compact = re.sub(r"[^a-z0-9]+", "", f"{class_name} {Path(rel).as_posix()}".lower())

    cache_key = id(strings)
    if cache_key not in _STRING_LABEL_INDEX_CACHE:
        index: list[tuple[set[str], str, int]] = []
        for key, value in strings.items():
            if not isinstance(value, str) or not value.strip() or value.startswith("@"):
                continue
            key_words = _words_from_identifier(key)
            if not key_words:
                continue
            bonus = 0
            if any(w in key_words for w in ("section", "screen")):
                bonus += 4
            elif "tab" in key_words:
                bonus += 1
            elif "title" in key_words:
                bonus += 1
            if len(value.split()) <= 4:
                bonus += 1
            index.append((key_words, value, bonus))
        _STRING_LABEL_INDEX_CACHE[cache_key] = index

    best_label = ""
    best_score = 0
    for key_words, value, bonus in _STRING_LABEL_INDEX_CACHE[cache_key]:
        if not (key_words & {"section", "screen", "tab"}):
            continue
        overlap = len(source_words & key_words)
        compact_hits = 0
        ordered_words = [w for w in key_words if w not in {"title", "section", "tab", "screen"}]
        for idx in range(len(ordered_words) - 1):
            if f"{ordered_words[idx]}{ordered_words[idx + 1]}" in source_compact:
                compact_hits += 2
        if overlap <= 0 and compact_hits <= 0:
            continue
        score = overlap + compact_hits + bonus
        if score > best_score:
            best_score = score
            best_label = value
    return best_label if best_score >= 3 else ""


def _infer_user_context_parts(source: str, pos: int, strings: dict[str, str] | None) -> list[str]:
    """Infer nearby user-visible section labels before a dynamic item group."""
    context = source[max(0, pos - 2200) : pos]
    header_keys: list[str] = []
    for match in re.finditer(
        r"\b(?:Category|Section|Header)\w*\s*\([^)]*?R\.string\.(\w+)",
        context,
    ):
        header_keys.append(match.group(1))
    if header_keys:
        label = _resource_label(header_keys[-1], strings).strip()
        return [label] if label else []

    keys: list[str] = []
    patterns = (
        r"\b(?:build\w*(?:Header|Title|SubHeader|ActionButton)\w*|setTitle|title|text|label)\s*\([^)]*?R\.string\.(\w+)",
        r"\b(?:titleRes|textRes|labelRes|contentDescRes)\s*=\s*R\.string\.(\w+)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, context):
            keys.append(match.group(1))
    labels: list[str] = []
    for key in keys[-4:]:
        label = _resource_label(key, strings).strip()
        if label and label not in labels:
            labels.append(label)
    return labels[-2:]


def _render_option_label(template: str, var_name: str, raw_value: str, strings: dict[str, str] | None) -> str:
    value = raw_value
    if value.startswith("@string/"):
        return _resource_label(value[len("@string/"):], strings)
    if template:
        rendered = template.replace("${" + var_name + "}", value).replace("$" + var_name, value)
        string_m = re.match(r"@string/(\w+)$", rendered)
        if string_m:
            return _resource_label(string_m.group(1), strings)
        return rendered
    return _resource_label(value[len("@string/"):], strings) if value.startswith("@string/") else value


def _extract_text_template(block: str, var_name: str) -> str:
    """Return a display template from a clickable option body."""
    m = re.search(r"text\s*=\s*\"([^\"]*)\"", block)
    if m:
        return m.group(1)
    m = re.search(r"\bText\s*\(\s*\"([^\"]*)\"", block)
    if m:
        return m.group(1)
    m = re.search(r"stringResource\s*\(\s*(?:id\s*=\s*)?R\.string\.(\w+)", block)
    if m:
        return f"@string/{m.group(1)}"
    # If no explicit label exists but the loop value is textual, use the value itself.
    return "$" + var_name


def _has_clickable_option_signal(block: str) -> bool:
    if re.search(r"\b(onClick|onCheckedChange|onValueChange)\s*=", block):
        return True
    if re.search(r"\b(CheckboxState|ToggleState|ActionButtonState)\s*\(", block):
        return True
    if ".clickable" in block or "setOnClickListener" in block:
        return True
    # Trailing lambda after a composable call, and the lambda is not just rendering Text.
    if re.search(r"\)\s*\{\s*(?!Text\s*\()", block, re.DOTALL):
        return True
    return False


def _option_group_id(file: str, line: int, label: str) -> str:
    raw = f"{file}|{line}|{label}"
    return "og:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _short_type_name(type_name: str) -> str:
    return (type_name or "").strip().split(".")[-1]


def _function_parameter_model(source: str, pos: int, param_name: str) -> str:
    """Return the model type for a function parameter visible at `pos`."""
    fallback = ""
    for m in re.finditer(r"\bfun\s+\w+\s*\(", source):
        open_pos = m.end() - 1
        close_pos = _matching_close_pos(source, open_pos, max_len=4000)
        if close_pos < 0 or close_pos > pos:
            continue
        params = source[open_pos + 1 : close_pos]
        param_match = re.search(
            rf"\b{re.escape(param_name)}\s*:\s*(?:List|MutableList|Collection|Iterable)\s*<\s*([\w.]+)\s*>",
            params,
        )
        if not param_match:
            param_match = re.search(rf"\b{re.escape(param_name)}\s*:\s*([\w.]+)", params)
        if not param_match:
            continue
        model = _short_type_name(param_match.group(1))
        fallback = model
        brace_pos = source.find("{", close_pos, min(len(source), close_pos + 1000))
        if brace_pos < 0:
            continue
        end_pos = _matching_close_pos(source, brace_pos, open_ch="{", close_ch="}", max_len=30000)
        if end_pos < 0 or brace_pos <= pos <= end_pos:
            return model
    return fallback


def _provider_label_access(block: str, var_name: str) -> bool:
    return bool(
        re.search(rf"\b{re.escape(var_name)}\.(?:labelResId|titleResId|stringResId|textResId)\b", block)
        or "UiStringRes(" in block
    )


def _option_effect_kind_from_block(block: str) -> str:
    if re.search(r"\b(CheckboxState|ToggleState)\s*\(", block) or "onCheckedChange" in block:
        return "state_toggle"
    return "option_select"


def _checked_default_from_block(block: str) -> str:
    m = re.search(r"\bchecked\s*=\s*(true|false)\b", block)
    return m.group(1) if m else ""


def _collect_provider_option_catalog(
    sources: list[tuple[Path, str, str, str]],
    strings: dict[str, str] | None,
) -> dict[str, list[dict]]:
    """
    Cross-file catalog for provider functions returning List<Model> via listOf(Model(... R.string ...)).
    """
    by_model: dict[str, list[dict]] = {}
    for _path, source, class_name, rel in sources:
        for fn in re.finditer(
            r"\bfun\s+(\w+)\s*\([^)]*\)\s*:\s*(?:List|MutableList|Collection|Iterable)\s*<\s*([\w.]+)\s*>\s*\{",
            source,
        ):
            provider_fn = fn.group(1)
            model = _short_type_name(fn.group(2))
            body = _balanced_curly_block(source, fn.end() - 1, max_len=20000)
            if "listOf" not in body:
                continue
            items: list[dict] = []
            for call in re.finditer(rf"\b{re.escape(model)}\s*\(", body):
                args = _balanced_block(body, call.end() - 1, max_len=4000)
                string_keys = re.findall(r"R\.string\.(\w+)", args)
                if not string_keys:
                    continue
                label_key = string_keys[0]
                hint_key = string_keys[1] if len(string_keys) > 1 else ""
                items.append(
                    {
                        "label_key": label_key,
                        "label": _resource_label(label_key, strings),
                        "hint_key": hint_key,
                        "hint": _resource_label(hint_key, strings) if hint_key else "",
                    }
                )
            if not items:
                continue
            line = _line_for(source, fn.start())
            by_model.setdefault(model, []).append(
                {
                    "id": _option_group_id(rel, line, f"{provider_fn}:{model}"),
                    "kind": "provider_option_catalog",
                    "effect": "report_candidate",
                    "from_class": class_name,
                    "provider_function": provider_fn,
                    "model_class": model,
                    "items_source": "provider_return_list",
                    "options": [str(item.get("label") or "") for item in items if item.get("label")],
                    "items": items,
                    "file": rel.replace("\\", "/"),
                    "line": line,
                    "confidence": "source",
                    "evidence": f"{provider_fn}(): List<{model}> -> listOf({model}(... R.string ...))",
                }
            )
    return by_model


def _provider_catalog_for_param(
    provider_catalogs: dict[str, list[dict]],
    source: str,
    pos: int,
    param_name: str,
) -> tuple[dict | None, str, int]:
    model = _function_parameter_model(source, pos, param_name)
    if not model:
        return None, "", 0
    catalogs = provider_catalogs.get(model, [])
    if len(catalogs) == 1:
        return catalogs[0], model, 1
    return None, model, len(catalogs)


def collect_dynamic_option_groups(
    project_root: str,
    dep_roots: list[str] | None,
    strings: dict[str, str] | None = None,
) -> list[dict]:
    """Find code/Compose generated option lists that are actually clickable."""
    groups: list[dict] = []
    sources = gather_kt_sources(project_root, dep_roots)
    enum_models = _extract_enum_property_models(sources)
    provider_catalogs = _collect_provider_option_catalog(sources, strings)
    for _path, source, class_name, rel in sources:
        option_sources = _collect_static_option_sources(source)
        enum_projection_sources = _collect_enum_projection_sources(source, enum_models)
        for src_name, src_info in enum_projection_sources.items():
            option_sources[src_name] = list(src_info.get("options", []))

        for m in re.finditer(
            r"\b(\w+)\s*\.\s*(?:map|forEach)\s*\{\s*(\w+)(?:\s*->)?",
            source,
        ):
            src_name = m.group(1)
            var_name = m.group(2) if "->" in m.group(0) else "it"
            values = option_sources.get(src_name)
            brace_pos = source.find("{", m.start())
            block = _balanced_curly_block(source, brace_pos, max_len=5000)
            items_source = "source_static_collection"
            provider_meta: dict = {}
            if not values:
                provider, _model, _count = _provider_catalog_for_param(
                    provider_catalogs, source, m.start(), src_name
                )
                if provider and _provider_label_access(block, var_name):
                    values = list(provider.get("options") or [])
                    provider_meta = dict(provider)
                    items_source = "provider_return_list"
            if not values:
                continue
            if not _has_clickable_option_signal(block):
                continue
            template = _extract_text_template(block, var_name)
            options = [_render_option_label(template, var_name, v, strings) for v in values]
            line = _line_for(source, m.start())
            row = {
                    "id": _option_group_id(rel, line, src_name),
                    "kind": "dynamic_option_group",
                    "effect": "report_candidate",
                    "from_class": class_name,
                    "source_name": src_name,
                    "items_source": items_source,
                    "options": options,
                    "option_effect_kind": _option_effect_kind_from_block(block),
                    "checked_default": _checked_default_from_block(block),
                    "file": rel.replace("\\", "/"),
                    "line": line,
                    "confidence": "source",
                    "evidence": f"{src_name}.map/forEach -> clickable option group",
            }
            inferred_parts = _infer_user_context_parts(source, m.start(), strings)
            if inferred_parts:
                row["path_parts"] = inferred_parts
            if provider_meta:
                row.update(
                    {
                        "provider_catalog_id": provider_meta.get("id", ""),
                        "provider_function": provider_meta.get("provider_function", ""),
                        "model_class": provider_meta.get("model_class", ""),
                        "provider_file": provider_meta.get("file", ""),
                        "provider_line": provider_meta.get("line", 0),
                        "provider_items": provider_meta.get("items", []),
                    }
                )
                row["evidence"] = (
                    f"{src_name}.map/forEach bound to "
                    f"{provider_meta.get('provider_function', '')}(): List<{provider_meta.get('model_class', '')}>"
                )
            groups.append(row)

        for m in re.finditer(
            r"\bitems\s*\(\s*(\w+)\s*\)\s*\{\s*(\w+)(?:\s*->)?",
            source,
        ):
            src_name = m.group(1)
            var_name = m.group(2) if "->" in m.group(0) else "it"
            values = option_sources.get(src_name)
            brace_pos = source.find("{", m.start())
            block = _balanced_curly_block(source, brace_pos, max_len=5000)
            items_source = "source_static_collection"
            provider_meta = {}
            if not values:
                provider, _model, _count = _provider_catalog_for_param(
                    provider_catalogs, source, m.start(), src_name
                )
                if provider and _provider_label_access(block, var_name):
                    values = list(provider.get("options") or [])
                    provider_meta = dict(provider)
                    items_source = "provider_return_list"
            if not values:
                continue
            if not _has_clickable_option_signal(block):
                continue
            template = _extract_text_template(block, var_name)
            options = [_render_option_label(template, var_name, v, strings) for v in values]
            line = _line_for(source, m.start())
            row = {
                    "id": _option_group_id(rel, line, src_name),
                    "kind": "dynamic_option_group",
                    "effect": "report_candidate",
                    "from_class": class_name,
                    "source_name": src_name,
                    "items_source": items_source,
                    "options": options,
                    "option_effect_kind": _option_effect_kind_from_block(block),
                    "checked_default": _checked_default_from_block(block),
                    "file": rel.replace("\\", "/"),
                    "line": line,
                    "confidence": "source",
                    "evidence": f"items({src_name}) -> clickable option group",
            }
            inferred_parts = _infer_user_context_parts(source, m.start(), strings)
            if inferred_parts:
                row["path_parts"] = inferred_parts
            if provider_meta:
                row.update(
                    {
                        "provider_catalog_id": provider_meta.get("id", ""),
                        "provider_function": provider_meta.get("provider_function", ""),
                        "model_class": provider_meta.get("model_class", ""),
                        "provider_file": provider_meta.get("file", ""),
                        "provider_line": provider_meta.get("line", 0),
                        "provider_items": provider_meta.get("items", []),
                    }
                )
                row["evidence"] = (
                    f"items({src_name}) bound to "
                    f"{provider_meta.get('provider_function', '')}(): List<{provider_meta.get('model_class', '')}>"
                )
            groups.append(row)

        for m in re.finditer(
            r"\b(setSingleChoiceItems|setItems|setMultiChoiceItems)\s*\(\s*(\w+)\s*,[\s\S]{0,600}?\)\s*\{",
            source,
        ):
            api_name, src_name = m.group(1), m.group(2)
            values = option_sources.get(src_name)
            if not values:
                continue
            callback = _balanced_curly_block(source, source.find("{", m.end() - 1), max_len=3000)
            if not callback.strip():
                continue
            src_info = enum_projection_sources.get(src_name, {})
            line = _line_for(source, m.start())
            groups.append(
                {
                    "id": _option_group_id(rel, line, src_name),
                    "kind": "dynamic_option_group",
                    "effect": "report_candidate",
                    "from_class": class_name,
                    "source_name": src_name,
                    "items_source": "app_enum_property_projection"
                    if src_info
                    else "source_static_collection",
                    "options": values,
                    "file": rel.replace("\\", "/"),
                    "line": line,
                    "confidence": "source",
                    "evidence": f"{src_info.get('evidence', src_name)} -> {api_name}({src_name}, ...)",
                }
            )

    return groups


def collect_provider_option_catalogs(
    project_root: str,
    dep_roots: list[str] | None,
    strings: dict[str, str] | None = None,
) -> list[dict]:
    sources = gather_kt_sources(project_root, dep_roots)
    catalogs_by_model = _collect_provider_option_catalog(sources, strings)
    return [catalog for catalogs in catalogs_by_model.values() for catalog in catalogs]


def collect_provider_option_binding_diagnostics(
    project_root: str,
    dep_roots: list[str] | None,
) -> list[dict]:
    sources = gather_kt_sources(project_root, dep_roots)
    provider_catalogs = _collect_provider_option_catalog(sources, None)
    rows: list[dict] = []
    for _path, source, class_name, rel in sources:
        for m in re.finditer(
            r"\b(\w+)\s*\.\s*(?:map|forEach)\s*\{\s*(\w+)(?:\s*->)?",
            source,
        ):
            src_name = m.group(1)
            var_name = m.group(2) if "->" in m.group(0) else "it"
            brace_pos = source.find("{", m.start())
            block = _balanced_curly_block(source, brace_pos, max_len=5000)
            if not _provider_label_access(block, var_name) or not _has_clickable_option_signal(block):
                continue
            _provider, model, count = _provider_catalog_for_param(
                provider_catalogs, source, m.start(), src_name
            )
            if not model or count == 1:
                continue
            line = _line_for(source, m.start())
            rows.append(
                {
                    "id": _candidate_id("dynamic_option_group_unresolved_param", rel, line, src_name + model),
                    "kind": "dynamic_option_group_unresolved_param",
                    "effect": "unknown",
                    "from_class": class_name,
                    "source_name": src_name,
                    "model_class": model,
                    "provider_match_count": count,
                    "file": rel.replace("\\", "/"),
                    "line": line,
                    "evidence": f"{src_name}.map/forEach uses {model} labels but provider catalog match count is {count}",
                }
            )
    return rows


def _extract_action_token(text: str) -> str:
    m = re.search(r"\bonClick\s*=\s*\{[\s\S]{0,500}?\b(\w+)\s*\(", text)
    if m:
        return m.group(1)
    m = _ACTION_TOKEN.search(text)
    if m:
        return m.group(1)
    # onClick = SomeClass.method(TOKEN, ...) — used by ListItemInteraction.create() patterns
    m = re.search(r"\bonClick\s*=\s*(?:\w+\.)*\w+\s*\(\s*(\w+)", text)
    if m:
        return m.group(1)
    # Kotlin trailing lambda shorthand: { onClicked(Foo) } is covered above;
    # this catches direct dispatch-style calls inside the click body.
    m = re.search(r"\{\s*(?:\w+\.)?(\w+)(?:\s*\([^)]*\))?\s*\}", text, re.MULTILINE)
    return m.group(1) if m else ""


def _enclosing_function_name(source: str, pos: int) -> str:
    name = ""
    for m in re.finditer(r"(?:[\w@]+\s+)*fun\s+(\w+)\s*\(", source[:pos]):
        name = m.group(1)
    return name


def collect_ui_action_bindings(
    project_root: str,
    dep_roots: list[str] | None,
    strings: dict[str, str] | None = None,
) -> list[dict]:
    """Generic L1: label resource bound to a code action token."""
    rows: list[dict] = []
    for _path, source, class_name, rel in gather_kt_sources(project_root, dep_roots):
        for m in _UI_ITEM_CALL.finditer(source):
            item_type, label_key = m.group(1), m.group(2)
            if item_type.endswith("SettingItem"):
                continue
            open_pos = source.find("(", m.start(), m.end())
            item_body = _balanced_block(source, open_pos) if open_pos >= 0 else ""
            token = _extract_action_token(item_body)
            if not token:
                continue
            line = _line_for(source, m.start())
            enclosing_fn = _enclosing_function_name(source, m.start())
            rows.append(
                {
                    "id": _candidate_id("ui_action_binding", rel, line, label_key + token),
                    "kind": "ui_action_binding",
                    "effect": "report_candidate",
                    "from_class": class_name,
                    "label_key": label_key,
                    "action_token": token,
                    "item_type": item_type,
                    "enclosing_function": enclosing_fn,
                    "file": rel.replace("\\", "/"),
                    "line": line,
                    "evidence": f"{item_type}(R.string.{label_key}) -> {token}",
                    "context_parts": [
                        p for p in [
                            _infer_source_area_label(class_name, rel, strings),
                            *_infer_user_context_parts(source, m.start(), strings),
                        ]
                        if p
                    ],
                }
            )
    return rows


def collect_setting_action_bindings(
    project_root: str,
    dep_roots: list[str] | None,
) -> list[dict]:
    """Generic L1: settings/preference DSL label bound to a click body."""
    rows: list[dict] = []
    for _path, source, class_name, rel in gather_kt_sources(project_root, dep_roots):
        for m in _SETTING_ITEM_CALL.finditer(source):
            item_type, label_key = m.group(1), m.group(2)
            line = _line_for(source, m.start())
            rows.append(
                {
                    "id": _candidate_id("setting_action_binding", rel, line, label_key),
                    "kind": "setting_action_binding",
                    "effect": "report_candidate",
                    "from_class": class_name,
                    "label_key": label_key,
                    "item_type": item_type,
                    "file": rel.replace("\\", "/"),
                    "line": line,
                    "evidence": f"{item_type}(R.string.{label_key})",
                }
            )
    return rows


def _balanced_curly_block(source: str, open_pos: int, max_len: int = 6000) -> str:
    if open_pos < 0 or open_pos >= len(source) or source[open_pos] != "{":
        return ""
    depth = 0
    end = min(len(source), open_pos + max_len)
    for i in range(open_pos, end):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[open_pos + 1 : i]
    return source[open_pos + 1 : end]


def _balanced_block(
    source: str,
    open_pos: int,
    *,
    open_ch: str = "(",
    close_ch: str = ")",
    max_len: int = 6000,
) -> str:
    if open_pos < 0 or open_pos >= len(source) or source[open_pos] != open_ch:
        return ""
    depth = 0
    end = min(len(source), open_pos + max_len)
    in_string = False
    escaped = False
    for i in range(open_pos, end):
        ch = source[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return source[open_pos + 1 : i]
    return source[open_pos + 1 : end]


def _matching_close_pos(
    source: str,
    open_pos: int,
    *,
    open_ch: str = "(",
    close_ch: str = ")",
    max_len: int = 6000,
) -> int:
    if open_pos < 0 or open_pos >= len(source) or source[open_pos] != open_ch:
        return -1
    depth = 0
    end = min(len(source), open_pos + max_len)
    in_string = False
    escaped = False
    for i in range(open_pos, end):
        ch = source[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return -1


def _call_with_trailing_lambda(source: str, start_pos: int, max_len: int = 4000) -> str:
    open_pos = source.find("(", start_pos, start_pos + 200)
    close_pos = _matching_close_pos(source, open_pos, max_len=max_len)
    if close_pos < 0:
        return _window(source, start_pos, max_len)
    text = source[start_pos : close_pos + 1]
    after = source[close_pos + 1 : min(len(source), close_pos + 1 + max_len)]
    stripped_len = len(after) - len(after.lstrip())
    stripped = after.lstrip()
    if stripped.startswith("{"):
        brace_pos = close_pos + 1 + stripped_len
        text += _balanced_curly_block(source, brace_pos, max_len=max_len)
    return text


def _split_top_level_csv(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    paren = brace = bracket = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            buf.append(ch)
        elif ch == "(":
            paren += 1
            buf.append(ch)
        elif ch == ")":
            paren -= 1
            buf.append(ch)
        elif ch == "{":
            brace += 1
            buf.append(ch)
        elif ch == "}":
            brace -= 1
            buf.append(ch)
        elif ch == "[":
            bracket += 1
            buf.append(ch)
        elif ch == "]":
            bracket -= 1
            buf.append(ch)
        elif ch == "," and paren == 0 and brace == 0 and bracket == 0:
            item = "".join(buf).strip()
            if item:
                parts.append(item)
            buf = []
        else:
            buf.append(ch)
    item = "".join(buf).strip()
    if item:
        parts.append(item)
    return parts


def _clean_option_atom(raw: str) -> str:
    value = raw.strip()
    named = re.match(r"\w+\s*=\s*(.+)$", value, re.DOTALL)
    if named:
        value = named.group(1).strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('\\"', '"')
    m = re.match(r"R\.string\.(\w+)$", value)
    if m:
        return f"@string/{m.group(1)}"
    m = re.match(r"(\w+)\s*\([^)]*\)$", value)
    if m:
        return m.group(1)
    return value


def _extract_enum_property_models(sources: list[tuple[Path, str, str, str]]) -> dict[str, dict]:
    """Cross-file Kotlin enum model: constructor property name -> enum value args."""
    models: dict[str, dict] = {}
    enum_re = re.compile(r"enum\s+class\s+(\w+)\s*\(", re.MULTILINE)
    for _path, source, _class_name, rel in sources:
        for m in enum_re.finditer(source):
            enum_name = m.group(1)
            ctor_args = _balanced_block(source, m.end() - 1, max_len=5000)
            prop_names: list[str] = []
            for part in _split_top_level_csv(ctor_args):
                prop = re.search(r"\b(?:val|var)\s+(\w+)\s*:", part)
                if prop:
                    prop_names.append(prop.group(1))
            if not prop_names:
                continue

            close_pos = _matching_close_pos(source, m.end() - 1, max_len=5000)
            open_curly = source.find("{", close_pos, close_pos + 500) if close_pos >= 0 else -1
            body = _balanced_curly_block(source, open_curly, max_len=20000)
            values: list[dict] = []
            for item in _split_top_level_csv(body):
                vm = re.match(r"\s*(\w+)\s*\(", item)
                if not vm:
                    continue
                args = _balanced_block(item, item.find("("), max_len=4000)
                props: dict[str, str] = {}
                positional: list[str] = []
                for arg in _split_top_level_csv(args):
                    named_arg = re.match(r"\s*(\w+)\s*=\s*(.+)$", arg, re.DOTALL)
                    if named_arg:
                        props[named_arg.group(1)] = _clean_option_atom(named_arg.group(2))
                    else:
                        positional.append(_clean_option_atom(arg))
                pos_index = 0
                for prop_name in prop_names:
                    if prop_name in props:
                        continue
                    if pos_index >= len(positional):
                        continue
                    props[prop_name] = positional[pos_index]
                    pos_index += 1
                values.append({"name": vm.group(1), "properties": props})
            if values:
                models[enum_name] = {
                    "properties": prop_names,
                    "values": values,
                    "file": rel.replace("\\", "/"),
                    "line": _line_for(source, m.start()),
                }
    return models


def _collect_static_option_sources(source: str) -> dict[str, list[str]]:
    """Collect simple static option sources from Kotlin source."""
    sources: dict[str, list[str]] = {}

    for m in re.finditer(r"(?:val|var)\s+(\w+)\s*=\s*(?:listOf|arrayOf)\s*\(", source):
        name = m.group(1)
        body = _balanced_block(source, m.end() - 1, max_len=4000)
        values = [_clean_option_atom(p) for p in _split_top_level_csv(body)]
        if values:
            sources[name] = values

    for m in re.finditer(r"(?:val|var)\s+(\w+)\s*=\s*\((\d+)\s*(?:\.\.|until|..<)\s*(\d+)\)", source):
        name, lo, hi = m.group(1), int(m.group(2)), int(m.group(3))
        exclusive = "until" in m.group(0) or "..<" in m.group(0)
        end = hi if exclusive else hi + 1
        sources[name] = [str(i) for i in range(lo, end)]

    for m in re.finditer(r"enum\s+class\s+(\w+)[^{]*\{", source):
        enum_name = m.group(1)
        body = _balanced_curly_block(source, m.end() - 1, max_len=3000)
        values: list[str] = []
        for part in _split_top_level_csv(body):
            name = re.match(r"\s*(\w+)", part)
            if name and name.group(1) not in {"fun", "val", "var"}:
                values.append(name.group(1))
        if values:
            sources[enum_name] = values

    return sources


def _collect_enum_projection_sources(
    source: str,
    enum_models: dict[str, dict],
) -> dict[str, dict]:
    """Collect val items = Enum.values/entries.map { it.label }.toTypedArray()."""
    sources: dict[str, dict] = {}
    val_re = re.compile(
        r"(?:val|var)\s+(\w+)\s*=\s*(\w+)\s*\.\s*(?:values\s*\(\)|entries)\s*"
        r"\.\s*map\s*\{\s*(?:(\w+)\s*->\s*)?(?:(\3)|it)\.(\w+)\s*\}"
        r"(?:\s*\.\s*toTypedArray\s*\(\s*\))?",
        re.MULTILINE,
    )
    for m in val_re.finditer(source):
        var_name, enum_name, prop_name = m.group(1), m.group(2), m.group(5)
        model = enum_models.get(enum_name)
        if not model:
            continue
        options = [
            str(value["properties"].get(prop_name))
            for value in model.get("values", [])
            if value.get("properties", {}).get(prop_name)
        ]
        if not options:
            continue
        sources[var_name] = {
            "options": options,
            "enum_name": enum_name,
            "property": prop_name,
            "evidence": f"{enum_name}.values/entries.map {{ it.{prop_name} }}",
        }
    return sources


def _label_from_atom(value: str, strings: dict[str, str] | None) -> str:
    atom = _clean_option_atom(value)
    if atom.startswith("@string/"):
        return _resource_label(atom[len("@string/"):], strings)
    return atom.replace("_", " ").title() if re.match(r"^[A-Z0-9_]+$", atom) else atom


def _first_string_key(text: str) -> str:
    m = re.search(r"R\.string\.(\w+)", text)
    return m.group(1) if m else ""


def _named_arg(parts: list[str], name: str) -> str:
    for part in parts:
        m = re.match(rf"\s*{re.escape(name)}\s*=\s*(.+)$", part, re.DOTALL)
        if m:
            return m.group(1).strip()
    return ""


def _options_from_expression(
    expr: str,
    enum_models: dict[str, dict],
    strings: dict[str, str] | None,
) -> list[str]:
    expr = expr.strip().rstrip(",")
    m = re.search(r"\b(?:listOf|arrayOf)\s*\(", expr)
    if m:
        body = _balanced_block(expr, m.end() - 1, max_len=8000)
        return [_label_from_atom(part, strings) for part in _split_top_level_csv(body)]

    m = re.search(
        r"\b(\w+)\s*\.\s*(?:values\s*\(\)|entries)\s*"
        r"(?:\.\s*filter\s*\{[^}]+\})?\s*"
        r"\.\s*map\s*\{\s*(?:(\w+)\s*->\s*)?(?:(?:\2)|it)\.(\w+)\s*\}",
        expr,
        re.DOTALL,
    )
    if m:
        enum_name, prop_name = m.group(1), m.group(3)
        return _enum_property_options(enum_models, enum_name, prop_name, strings)
    return []


def _enum_property_options(
    enum_models: dict[str, dict],
    enum_name: str,
    label_prop: str,
    strings: dict[str, str] | None,
    *,
    filter_prop: str = "",
) -> list[str]:
    model = enum_models.get(enum_name)
    if not model:
        return []
    options: list[str] = []
    for value in model.get("values", []):
        props = value.get("properties", {})
        if filter_prop and str(props.get(filter_prop, "true")).lower() == "false":
            continue
        label = props.get(label_prop)
        if not label:
            continue
        options.append(_label_from_atom(str(label), strings))
    return options


def _browser_action_catalog_options(
    sources: list[tuple[Path, str, str, str]],
    strings: dict[str, str] | None,
) -> list[str]:
    keys: list[str] = []
    for _path, source, _class_name, _rel in sources:
        for m in re.finditer(r"\bBrowserActionEntry\s*\([^,]+,\s*R\.string\.(\w+)", source):
            key = m.group(1)
            if key not in keys:
                keys.append(key)
    return [_resource_label(key, strings) for key in keys]


def _setting_route_labels(
    sources: list[tuple[Path, str, str, str]],
    enum_models: dict[str, dict],
    strings: dict[str, str] | None,
) -> dict[str, str]:
    route_labels: dict[str, str] = {}
    for model in enum_models.values():
        if "titleId" not in model.get("properties", []):
            continue
        for value in model.get("values", []):
            label = value.get("properties", {}).get("titleId")
            if label:
                route_labels[value.get("name", "")] = _label_from_atom(str(label), strings)

    list_labels: dict[str, str] = {"mainSettings": _resource_label("settings", strings)}
    for _path, source, _class_name, _rel in sources:
        for m in re.finditer(r"(\w+)\.titleId\s+to\s+(\w+)", source):
            route_name, list_name = m.group(1), m.group(2)
            if route_name in route_labels:
                list_labels[list_name] = route_labels[route_name]
        for m in re.finditer(r"\bcomposable\s*\(\s*(?:SettingRoute\.)?(\w+)\.name\s*\)\s*\{", source):
            route_name = m.group(1)
            block = _balanced_curly_block(source, m.end() - 1, max_len=3000)
            for list_name in re.findall(r"\b(\w+(?:SettingItems|Settings))\b", block):
                if route_name in route_labels:
                    list_labels[list_name] = route_labels[route_name]
    return list_labels


def _setting_category_from_var(var_name: str, labels: dict[str, str]) -> str:
    if var_name in labels:
        return labels[var_name]
    base = re.sub(r"(?:SettingItems|Settings)$", "", var_name)
    return _display_from_class(base[:1].upper() + base[1:])


def collect_setting_option_groups(
    project_root: str,
    dep_roots: list[str] | None,
    strings: dict[str, str] | None = None,
) -> list[dict]:
    """Generic settings DSL options: booleans, list choices, text inputs, gestures."""
    groups: list[dict] = []
    sources = gather_kt_sources(project_root, dep_roots)
    enum_models = _extract_enum_property_models(sources)
    list_labels = _setting_route_labels(sources, enum_models, strings)
    browser_action_options = _browser_action_catalog_options(sources, strings)

    for _path, source, class_name, rel in sources:
        for m in re.finditer(r"(?:private\s+)?(?:val|var)\s+(\w+)\s*=\s*listOf\s*\(", source):
            list_name = m.group(1)
            body = _balanced_block(source, m.end() - 1, max_len=40000)
            if "SettingItem" not in body:
                continue
            category_label = _setting_category_from_var(list_name, list_labels)
            for item in _split_top_level_csv(body):
                item_m = re.match(r"\s*(\w*Setting\w*Item|VersionSettingItem|LinkSettingItem\.\w+)\s*(?:\(|$)", item)
                if not item_m or item_m.group(1).startswith("Divider"):
                    continue
                item_type = item_m.group(1)
                args = _balanced_block(item, item.find("("), max_len=10000) if "(" in item else ""
                title_key = _first_string_key(args or item)
                if not title_key:
                    continue
                title = _resource_label(title_key, strings)
                parts = [_resource_label("settings", strings), category_label, title]
                options: list[str] = []
                effect_kind = "option_select"
                arg_parts = _split_top_level_csv(args)
                if item_type == "BooleanSettingItem":
                    options = ["On", "Off"]
                    effect_kind = "state_toggle"
                elif item_type.startswith("ListSettingWith"):
                    expr = _named_arg(arg_parts, "options")
                    if not expr:
                        expr = next((part for part in arg_parts if "listOf" in part or ".entries" in part), "")
                    options = _options_from_expression(expr, enum_models, strings)
                elif item_type == "ValueSettingItem":
                    options = ["Text Input > {input}"]
                    effect_kind = "text_input"
                elif item_type == "GestureActionSettingItem":
                    options = browser_action_options or ["Choose action"]
                elif item_type in {"ActionSettingItem", "NavigateSettingItem", "VersionSettingItem"}:
                    options = ["Open"]
                if not options:
                    continue
                line = _line_for(source, m.start() + body.find(item))
                groups.append(
                    {
                        "id": _option_group_id(rel, line, title_key),
                        "kind": "setting_option_group",
                        "effect": "report_candidate",
                        "from_class": class_name,
                        "source_name": list_name,
                        "items_source": "setting_dsl",
                        "path_parts": parts,
                        "options": options,
                        "option_effect_kind": effect_kind,
                        "file": rel.replace("\\", "/"),
                        "line": line,
                        "confidence": "source",
                        "evidence": f"{item_type}(R.string.{title_key}) in {list_name}",
                    }
                )
    return groups


def collect_compose_control_option_groups(
    project_root: str,
    dep_roots: list[str] | None,
    strings: dict[str, str] | None = None,
) -> list[dict]:
    """Generic Compose control options from checkbox/toggle/icon-only selectors."""
    groups: list[dict] = []
    sources = gather_kt_sources(project_root, dep_roots)
    enum_models = _extract_enum_property_models(sources)

    for _path, source, class_name, rel in sources:
        container = _display_from_class(class_name)

        seen_toggle_keys: set[tuple[str, int]] = set()
        for m in re.finditer(r"\bToggleItem\s*\(", source):
            args = _balanced_block(source, m.end() - 1, max_len=5000)
            title_key = _first_string_key(args)
            if not title_key:
                continue
            line = _line_for(source, m.start())
            dedupe_key = (title_key, line)
            if dedupe_key in seen_toggle_keys:
                continue
            seen_toggle_keys.add(dedupe_key)
            label = _resource_label(title_key, strings)
            groups.append(
                {
                    "id": _option_group_id(rel, line, title_key),
                    "kind": "compose_control_option_group",
                    "effect": "report_candidate",
                    "from_class": class_name,
                    "source_name": "ToggleItem",
                    "items_source": "compose_interactive_control",
                    "path_parts": [container, label],
                    "options": ["On", "Off"],
                    "option_effect_kind": "state_toggle",
                    "file": rel.replace("\\", "/"),
                    "line": line,
                    "confidence": "source",
                    "evidence": f"ToggleItem(R.string.{title_key}) with clickable/checkbox state",
                }
            )

        touch_options: list[str] = []
        first_touch_line = 0
        for m in re.finditer(
            r"\bTouchAreaItem\s*\([\s\S]{0,260}?state\s*=\s*\w+\s*==\s*(?:\w+\.)?(\w+)",
            source,
        ):
            option = _display_from_class(m.group(1))
            if option not in touch_options:
                touch_options.append(option)
            first_touch_line = first_touch_line or _line_for(source, m.start())
        if touch_options:
            groups.append(
                {
                    "id": _option_group_id(rel, first_touch_line, "TouchAreaItem"),
                    "kind": "compose_control_option_group",
                    "effect": "report_candidate",
                    "from_class": class_name,
                    "source_name": "TouchAreaItem",
                    "items_source": "compose_interactive_control",
                    "path_parts": [container, "Touch area type"],
                    "options": touch_options,
                    "file": rel.replace("\\", "/"),
                    "line": first_touch_line,
                    "confidence": "source",
                    "evidence": "TouchAreaItem(state = current == EnumValue) clickable selector",
                }
            )

        for m in re.finditer(r"\bSwitch\s*\(", source):
            start = max(0, m.start() - 500)
            prefix = source[start : m.start()]
            keys = re.findall(r"R\.string\.(\w+)", prefix)
            if not keys:
                continue
            title_key = keys[-1]
            line = _line_for(source, m.start())
            label = _resource_label(title_key, strings)
            groups.append(
                {
                    "id": _option_group_id(rel, line, "Switch" + title_key),
                    "kind": "compose_control_option_group",
                    "effect": "report_candidate",
                    "from_class": class_name,
                    "source_name": "Switch",
                    "items_source": "compose_interactive_control",
                    "path_parts": [container, label],
                    "options": ["On", "Off"],
                    "option_effect_kind": "state_toggle",
                    "file": rel.replace("\\", "/"),
                    "line": line,
                    "confidence": "source",
                    "evidence": f"Switch near R.string.{title_key} with onCheckedChange",
                }
            )

        if "ToolbarToggleItem" in source and re.search(r"\bToolbarAction\s*\.\s*(?:entries|values\s*\(\))", source):
            options = _enum_property_options(
                enum_models,
                "ToolbarAction",
                "titleResId",
                strings,
                filter_prop="isAddable",
            )
            if options:
                line = _line_for(source, source.find("ToolbarToggleItem"))
                groups.append(
                    {
                        "id": _option_group_id(rel, line, "ToolbarAction"),
                        "kind": "compose_control_option_group",
                        "effect": "report_candidate",
                        "from_class": class_name,
                        "source_name": "ToolbarAction",
                        "items_source": "enum_property_catalog",
                        "path_parts": [container, "Toolbar buttons"],
                        "options": options,
                        "file": rel.replace("\\", "/"),
                        "line": line,
                        "confidence": "source",
                        "evidence": "ToolbarAction.entries filter/map -> ToolbarToggleItem",
                    }
                )
    return groups


def _extract_functions(source: str) -> dict[str, str]:
    functions: dict[str, str] = {}
    expr_re = re.compile(
        r"(?:[\w@]+\s+)*fun\s+(\w+)\s*\([^)]*\)\s*(?::[^{=\n]+)?=\s*([^\n]+)"
    )
    for m in expr_re.finditer(source):
        functions[m.group(1)] = m.group(2).strip()

    block_re = re.compile(r"(?:[\w@]+\s+)*fun\s+(\w+)\s*\([^)]*\)[^{=]*\{")
    for m in block_re.finditer(source):
        name = m.group(1)
        if name in functions:
            continue
        functions[name] = _balanced_curly_block(source, m.end() - 1)
    java_block_re = re.compile(
        r"(?:public|private|protected|static|final|synchronized|native|\s)+"
        r"[\w<>\[\].?,\s]+\s+(\w+)\s*\([^)]*\)\s*(?:throws\s+[\w.,\s]+)?\{"
    )
    for m in java_block_re.finditer(source):
        name = m.group(1)
        if name in functions or name in {"if", "for", "while", "switch", "catch"}:
            continue
        functions[name] = _balanced_curly_block(source, m.end() - 1)
    return functions


def _extract_functions_near(
    source: str,
    signal_re: re.Pattern,
) -> dict[str, str]:
    """Extract only functions enclosing direct UI/navigation signals."""
    functions: dict[str, str] = {}
    fn_re = re.compile(
        r"(?:[\w@]+\s+)*fun\s+(\w+)\s*\([^)]*\)[^{=]*\{|"
        r"(?:public|private|protected|static|final|synchronized|\s)+"
        r"[\w<>\[\].?,\s]+\s+(\w+)\s*\([^)]*\)\s*(?:throws\s+[\w.,\s]+)?\{"
    )
    fn_matches = list(fn_re.finditer(source))
    if not fn_matches:
        return functions
    starts = [m.start() for m in fn_matches]
    for signal in signal_re.finditer(source):
        idx = bisect_right(starts, signal.start()) - 1
        if idx < 0:
            continue
        last_match = fn_matches[idx]
        if signal.start() - last_match.start() > 12000:
            continue
        name = last_match.group(1) or last_match.group(2)
        if not name or name in functions or name in {"if", "for", "while", "switch", "catch"}:
            continue
        brace_pos = source.find("{", last_match.end() - 1, min(len(source), last_match.end() + 1000))
        if brace_pos < 0:
            continue
        functions[name] = _balanced_curly_block(source, brace_pos, max_len=12000)
    return functions


def _classify_effect_body(
    body: str,
    function_effects: dict[str, dict] | None = None,
) -> dict:
    """Classify by generic Android/Compose/Kotlin signals, not project action names."""
    function_effects = function_effects or {}
    if re.search(r"\bIntent\.(?:ACTION_OPEN_DOCUMENT|ACTION_GET_CONTENT|ACTION_CREATE_DOCUMENT)\b", body):
        action_m = re.search(r"\bIntent\.(ACTION_OPEN_DOCUMENT|ACTION_GET_CONTENT|ACTION_CREATE_DOCUMENT)\b", body)
        mime_m = re.search(r"(?:\btype|\.type)\s*=\s*([^;\n}]+)", body)
        mime = mime_m.group(1).strip() if mime_m else ""
        return {
            "effect_kind": "system_document_picker",
            "target": "SystemDocumentPicker",
            "intent_action": action_m.group(1) if action_m else "",
            "mime_type": mime.strip('"'),
        }
    if re.search(r"\b(?:PrintManager|createPrintDocumentAdapter|PRINT_SERVICE|\.print\s*\()", body):
        return {"effect_kind": "system_print", "target": "AndroidPrintManager"}

    m = re.search(r"\b([A-Z]\w*(?:Dialog|DialogFragment|BottomSheet))\s*\([^;\n{}]*\)\s*\.show\s*\(", body)
    if m:
        return {"effect_kind": "dialog", "target": m.group(1)}
    m = re.search(r"\b([A-Z]\w*(?:Dialog|DialogFragment|BottomSheet))\s*\{[\s\S]{0,600}?\}\s*\.show\s*\(", body)
    if m:
        return {"effect_kind": "dialog", "target": m.group(1)}
    m = re.search(r"\b([A-Z]\w*(?:Dialog|DialogFragment|BottomSheet))\s*\([^)]*\)", body)
    if m and ".show" in body[m.end() : m.end() + 120]:
        return {"effect_kind": "dialog", "target": m.group(1)}

    m = re.search(r"startActivity(?:ForResult)?\s*\([^)]*?Intent\([^,]*,\s*(\w+)::class", body)
    if m:
        return {"effect_kind": "activity", "target": m.group(1)}
    m = re.search(r"new\s+Intent\s*\([^,]*,\s*(\w+)\.class\s*\)", body)
    if m and "startActivity" in body:
        return {"effect_kind": "activity", "target": m.group(1)}
    m = re.search(r"Intent\s*\([^,]*,\s*(\w+)::class\.java\s*\)", body)
    if m and "startActivity" in body:
        return {"effect_kind": "activity", "target": m.group(1)}
    m = re.search(r"startActivity(?:ForResult)?\s*\(\s*(\w+)\.createIntent\s*\(", body)
    if m:
        return {"effect_kind": "activity", "target": m.group(1)}

    m = re.search(r"\b(?:\w+\.)?([A-Z]\w+)\s*\([^)]*\)", body)
    if m and not re.search(r"\b(?:Intent|Bundle|Event|LiveData|MutableLiveData)\b", m.group(1)):
        # Generic sealed-class / action-object construction. A later branch or
        # helper with the same token may resolve this to a concrete UI target.
        return {"effect_kind": "dispatched_action", "target_action": m.group(1)}
    m = re.search(r"\b(?:\w+\.)+([A-Z]\w+)\b", body)
    if m:
        return {"effect_kind": "dispatched_action", "target_action": m.group(1)}
    m = re.search(r"\b\w+\.(\w+)\s*\(", body)
    if m:
        return {"effect_kind": "dispatched_action", "target_action": m.group(1)}

    m = re.search(r"\bdispatch\s*\(\s*(?:\w+\.)?(\w+)(?:\s*\([^)]*\))?", body)
    if m:
        return {"effect_kind": "dispatched_action", "target_action": m.group(1)}

    m = re.search(r"\bshow(\w*Dialog)\s*\(", body)
    if m:
        return {"effect_kind": "dialog", "target": m.group(1)}

    m = re.search(r"\bshow(?:Open|Choose|Select)?(\w*)FilePicker\s*\(", body)
    if m:
        kind = m.group(1)
        return {
            "effect_kind": "system_document_picker",
            "target": "SystemDocumentPicker",
            "intent_action": "ACTION_OPEN_DOCUMENT",
            "mime_type": kind.upper() if kind else "*/*",
        }

    if re.search(r"\b(?:showSecondPane\w*|loadInSecondPane|toggle\w*(?:Screen|Pane|Layout|Read|Mode))\s*\(", body):
        return {"effect_kind": "content_or_layout_mode"}

    if re.search(r"\b(?:\w+\.)?toggle\s*\(", body) or re.search(r"\bconfig\.\w+(?:\.\w+)?\s*=", body):
        return {"effect_kind": "state_toggle"}

    for call in re.finditer(r"\b(\w+)\s*\(", body):
        name = call.group(1)
        if name in function_effects:
            return dict(function_effects[name])

    return {"effect_kind": "unknown"}


def collect_action_effects(
    project_root: str,
    dep_roots: list[str] | None,
) -> dict[str, dict]:
    """Map generic action tokens to their resolved effect."""
    sources = gather_kt_sources(project_root, dep_roots)
    function_effects: dict[str, dict] = {}
    direct_effect_signal = re.compile(
        r"startActivity|Intent\s*\(|new\s+Intent|\.createIntent\s*\(|Dialog|BottomSheet|"
        r"PrintManager|ACTION_OPEN_DOCUMENT|ACTION_GET_CONTENT|ACTION_CREATE_DOCUMENT|"
        r"FilePicker|toggle\w*\s*\("
    )
    effect_sources = [
        item for item in sources
        if direct_effect_signal.search(item[1])
    ]
    for _path, source, _class_name, _rel in effect_sources:
        for fn_name, body in _extract_functions_near(source, direct_effect_signal).items():
            function_effects[fn_name] = _classify_effect_body(body)

    # Second pass lets expression-body functions resolve local helper calls.
    for _path, source, _class_name, _rel in effect_sources:
        for fn_name, body in _extract_functions_near(source, direct_effect_signal).items():
            function_effects[fn_name] = _classify_effect_body(body, function_effects)

    action_effects: dict[str, dict] = dict(function_effects)
    for _path, source, class_name, rel in sources:
        action_effects.update(_collect_named_lambda_effects(source, class_name, rel, function_effects))

    for _path, source, class_name, rel in sources:
        matches = list(_BRANCH_TOKEN.finditer(source))
        for m in matches:
            token = m.group(1).rsplit(".", 1)[-1]
            after = source[m.end() :]
            stripped = after.lstrip()
            if stripped.startswith("{"):
                body = _balanced_curly_block(after, after.index("{"))
            else:
                nl = after.find("\n")
                body = after[: nl if nl >= 0 else 1000]
            effect = _classify_effect_body(body, function_effects)
            if effect.get("effect_kind") == "unknown":
                continue
            effect.setdefault("source_class", class_name)
            effect.setdefault("source_file", rel.replace("\\", "/"))
            effect.setdefault("line", _line_for(source, m.start()))
            action_effects[token] = effect
    return action_effects


def _resolve_action_effect(action_token: str, action_effects: dict[str, dict]) -> dict:
    seen: set[str] = set()
    token = action_token
    effect = action_effects.get(token, {"effect_kind": "unknown"})
    while effect.get("effect_kind") == "dispatched_action":
        target = str(effect.get("target_action") or "")
        if not target or target in seen:
            break
        seen.add(target)
        token = target
        effect = action_effects.get(token, effect)
    return dict(effect)


def _collect_named_lambda_effects(
    source: str,
    class_name: str,
    rel: str,
    function_effects: dict[str, dict],
) -> dict[str, dict]:
    effects: dict[str, dict] = {}
    for m in re.finditer(r"\b(on\w+)\s*=\s*\{", source):
        body = _balanced_curly_block(source, m.end() - 1, max_len=5000)
        effect = _classify_effect_body(body, function_effects)
        if effect.get("effect_kind") == "unknown":
            continue
        effect.setdefault("source_class", class_name)
        effect.setdefault("source_file", rel.replace("\\", "/"))
        effect.setdefault("line", _line_for(source, m.start()))
        effects[m.group(1)] = effect
    return effects


def _effect_path_id(parts: list[str], effect_kind: str, source_file: str, line: int) -> str:
    raw = "|".join(parts + [effect_kind, source_file, str(line)])
    return "ep:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _path_row(
    *,
    root_label: str,
    container_label: str,
    label: str,
    label_key: str,
    effect: dict,
    source_file: str,
    line: int,
    action_token: str = "",
    context_parts: list[str] | None = None,
    expose_user_click: bool = False,
) -> dict:
    parts = [root_label]
    if context_parts:
        for part in context_parts:
            if part and part not in parts:
                parts.append(part)
    elif container_label and container_label != root_label:
        parts.append(container_label)
    parts.append(label)
    target = str(effect.get("target") or "")
    effect_kind = str(effect.get("effect_kind") or "unknown")
    if target and effect_kind in ("dialog", "activity"):
        parts.append(_display_from_class(target))
    resolved_target_ui = bool(target and effect_kind in ("dialog", "activity"))
    resolved_ui = resolved_target_ui or bool(expose_user_click and label and effect_kind == "navigate")
    return {
        "path_id": _effect_path_id(parts, effect_kind, source_file, line),
        "path_display_legacy": " > ".join(parts),
        "path_display_report": " › ".join(parts),
        "path_parts": parts,
        "effect_kind": effect_kind,
        "label_key": label_key,
        "label": label,
        "action_token": action_token,
        "target": target,
        "target_class": target if resolved_target_ui else "",
        "source_file": source_file,
        "line": line,
        "report_only": not resolved_ui,
        "display_channel": "ui" if resolved_ui else "evidence",
        "user_visible": resolved_ui,
    }


def _option_path_row(
    *,
    root_label: str,
    parts: list[str],
    option: str,
    group: dict,
    source: str = "source",
) -> dict:
    all_parts = [root_label] + [p for p in parts if p and p != root_label] + [option]
    raw = "|".join(all_parts + [str(group.get("file", "")), str(group.get("line", 0)), source])
    resolved_ui = bool(group.get("path_parts"))
    return {
        "path_id": "ep:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
        "path_display_legacy": " > ".join(all_parts),
        "path_display_report": " › ".join(all_parts),
        "path_parts": all_parts,
        "effect_kind": group.get("option_effect_kind", "option_select"),
        "label_key": "",
        "label": option,
        "action_token": "",
        "target": "",
        "source_file": group.get("file", ""),
        "line": int(group.get("line") or 0),
        "report_only": not resolved_ui,
        "display_channel": "ui" if resolved_ui else "evidence",
        "user_visible": resolved_ui,
        "option_group_id": group.get("id", ""),
        "items_source": group.get("items_source", ""),
        "catalog_source": group.get("catalog_source", ""),
        "provider_scope": group.get("provider_scope", ""),
        "evidence": group.get("evidence", ""),
        "confidence": group.get("confidence", "source"),
    }


def _option_group_matches_target(group: dict, target_display: str) -> bool:
    group_display = _display_from_class(str(group.get("from_class") or ""))
    return group_display == target_display or target_display.endswith(group_display)


def _option_group_relative_parts(group: dict, target_display: str) -> list[str]:
    parts = [str(p) for p in group.get("path_parts", []) if p]
    if not parts:
        return []
    if target_display in parts:
        return parts[parts.index(target_display) + 1 :]
    return parts[1:] if parts and parts[0].endswith(target_display) else parts


def _document_picker_options(effect: dict) -> list[str]:
    mime = str(effect.get("mime_type") or "")
    if "EPUB" in mime or "epub" in mime:
        return ["EPUB file"]
    if "image" in mime:
        return ["Image file"]
    if "video" in mime:
        return ["Video file"]
    if "audio" in mime:
        return ["Audio file"]
    if mime and mime != "*/*":
        return [mime]
    return ["File"]


def _document_picker_group(effect: dict, source_file: str, line: int) -> dict:
    return {
        "id": _option_group_id(source_file, line, str(effect.get("mime_type") or "document")),
        "items_source": "android_system_document_picker",
        "catalog_source": "Intent.ACTION_OPEN_DOCUMENT/ACTION_GET_CONTENT/ACTION_CREATE_DOCUMENT",
        "file": source_file,
        "line": line,
        "confidence": "source",
        "evidence": f"{effect.get('intent_action', 'document_picker')} -> {effect.get('mime_type', '')}",
    }


def _document_picker_catalog_path_rows(
    *,
    root_label: str,
    base_parts: list[str],
    effect: dict,
    source_file: str,
    line: int,
) -> list[dict]:
    catalog = load_android_document_picker_catalog()
    rows: list[dict] = []
    for item in catalog.get("items", []):
        group = {
            "id": _option_group_id(
                "android_documentsui",
                line,
                str(item.get("id") or item.get("label") or ""),
            ),
            "items_source": "android_system_document_picker",
            "catalog_source": catalog.get("catalog_source", "documentsui_catalog_v1"),
            "provider_scope": item.get("provider_scope", ""),
            "file": source_file,
            "line": line,
            "confidence": "framework_catalog",
            "evidence": f"{effect.get('intent_action', 'document_picker')} -> {effect.get('mime_type', '')}",
        }
        for option in _document_picker_options(effect):
            rows.append(
                _option_path_row(
                    root_label=root_label,
                    parts=base_parts + [str(item.get("label") or item.get("id") or "")],
                    option=option,
                    group=group,
                    source="system_document_picker_catalog",
                )
            )
    return rows


def build_ui_effect_paths(
    project_root: str,
    dep_roots: list[str] | None,
    strings: dict[str, str] | None,
    launcher_class: str = "",
    nav_graph: dict | None = None,
) -> dict:
    """Build report-only effect paths for code/Compose menus and settings DSL."""
    strings = strings or {}
    root_label = _display_from_class(launcher_class) if launcher_class else "App"
    action_effects = collect_action_effects(project_root, dep_roots)
    option_groups = collect_dynamic_option_groups(project_root, dep_roots, strings)
    option_groups.extend(collect_compose_control_option_groups(project_root, dep_roots, strings))
    setting_option_groups = collect_setting_option_groups(project_root, dep_roots, strings)
    print_media_catalog = load_android_print_media_size_catalog()
    ui_bindings = collect_ui_action_bindings(project_root, dep_roots, strings)
    rows: list[dict] = []

    for binding in ui_bindings:
        token = str(binding.get("action_token") or "")
        effect = _resolve_action_effect(token, action_effects)
        item_type = str(binding.get("item_type") or "")
        is_list_item = item_type.endswith("Item")
        if effect.get("effect_kind") in ("unknown", "dispatched_action") and not is_list_item:
            continue
        if effect.get("effect_kind") in ("unknown", "dispatched_action") and is_list_item:
            effect = {"effect_kind": "navigate", "source_class": binding.get("from_class", "")}
        source_class = str(binding.get("from_class") or "")
        if "Dialog" in source_class:
            container = _display_from_class(source_class)
        elif is_list_item:
            resolved = _resolve_screen_class(source_class, nav_graph) if nav_graph else ""
            container = _display_from_class(resolved) if resolved else _display_from_class(source_class)
        else:
            container = ""
        label = _resource_label(str(binding.get("label_key") or ""), strings)
        rows.append(
            _path_row(
                root_label=root_label,
                container_label=container,
                label=label,
                label_key=str(binding.get("label_key") or ""),
                effect=effect,
                source_file=str(binding.get("file") or ""),
                line=int(binding.get("line") or 0),
                action_token=token,
                context_parts=list(binding.get("context_parts") or []),
                expose_user_click=True,
            )
        )
        if effect.get("effect_kind") == "system_print":
            catalog_source = str(print_media_catalog.get("catalog_source") or "")
            for item in print_media_catalog.get("items", []):
                group = {
                    "id": _option_group_id("android_framework", 0, str(item.get("id") or "")),
                    "items_source": "android_framework_catalog",
                    "catalog_source": catalog_source,
                    "file": str(binding.get("file") or ""),
                    "line": int(binding.get("line") or 0),
                    "confidence": "framework_catalog",
                    "evidence": "PrintManager.print -> PrintAttributes.MediaSize framework catalog",
                }
                base_parts = []
                if container:
                    base_parts.append(container)
                base_parts.extend([label, "System Print Panel", "Paper size"])
                rows.append(
                    _option_path_row(
                        root_label=root_label,
                        parts=base_parts,
                        option=str(item.get("label") or item.get("id") or ""),
                        group=group,
                        source="system_print_media_size",
                    )
                )
        if effect.get("effect_kind") == "dialog":
            target_display = _display_from_class(str(effect.get("target") or ""))
            child_bindings = [
                b for b in ui_bindings
                if _display_from_function(str(b.get("enclosing_function") or "")) == target_display
            ]
            for child in child_bindings:
                child_token = str(child.get("action_token") or "")
                child_effect = _resolve_action_effect(child_token, action_effects)
                if child_effect.get("effect_kind") in ("unknown", "dispatched_action"):
                    continue
                child_label = _resource_label(str(child.get("label_key") or ""), strings)
                if child_effect.get("effect_kind") == "system_document_picker":
                    picker_group = _document_picker_group(
                        child_effect,
                        str(child.get("file") or ""),
                        int(child.get("line") or 0),
                    )
                    base_parts = []
                    if container:
                        base_parts.append(container)
                    base_parts.extend([label, target_display, child_label, "System Document Picker"])
                    for option in _document_picker_options(child_effect):
                        rows.append(
                            _option_path_row(
                                root_label=root_label,
                                parts=base_parts,
                                option=option,
                                group=picker_group,
                                source="system_document_picker",
                            )
                        )
                    rows.extend(
                        _document_picker_catalog_path_rows(
                            root_label=root_label,
                            base_parts=base_parts,
                            effect=child_effect,
                            source_file=str(child.get("file") or ""),
                            line=int(child.get("line") or 0),
                        )
                    )
            for group in option_groups:
                if not _option_group_matches_target(group, target_display):
                    continue
                base_parts = []
                if container:
                    base_parts.append(container)
                base_parts.extend([label, target_display])
                group_parts = base_parts + _option_group_relative_parts(group, target_display)
                for option in group.get("options", []):
                    rows.append(
                        _option_path_row(
                            root_label=root_label,
                            parts=group_parts,
                            option=str(option),
                            group=group,
                            source="action_dialog_option",
                        )
                    )

    for _path, source, class_name, rel in gather_kt_sources(project_root, dep_roots):
        for m in _SETTING_ITEM_CALL.finditer(source):
            _item_type, label_key = m.group(1), m.group(2)
            win = _call_with_trailing_lambda(source, m.start(), 3000)
            effect = _classify_effect_body(win)
            if effect.get("effect_kind") == "unknown":
                continue
            label = _resource_label(label_key, strings)
            container = _display_from_class(class_name)
            row = _path_row(
                root_label=root_label,
                container_label=container,
                label=label,
                label_key=label_key,
                effect=effect,
                source_file=rel.replace("\\", "/"),
                line=_line_for(source, m.start()),
            )
            rows.append(row)
            if effect.get("effect_kind") == "dialog":
                target_display = _display_from_class(str(effect.get("target") or ""))
                for group in option_groups:
                    if not _option_group_matches_target(group, target_display):
                        continue
                    group_parts = [container, label, target_display] + _option_group_relative_parts(group, target_display)
                    for option in group.get("options", []):
                        rows.append(
                            _option_path_row(
                                root_label=root_label,
                                parts=group_parts,
                                option=str(option),
                                group=group,
                                source="setting_dialog_option",
                            )
                        )

    for group in setting_option_groups:
        for option in group.get("options", []):
            rows.append(
                _option_path_row(
                    root_label=root_label,
                    parts=list(group.get("path_parts") or []),
                    option=str(option),
                    group=group,
                    source="setting_option_group",
                )
            )

    for group in option_groups:
        from_class = str(group.get("from_class") or "")
        resolved = _resolve_screen_class(from_class, nav_graph) if nav_graph else ""
        container = _display_from_class(resolved) if resolved else _display_from_class(from_class)
        for option in group.get("options", []):
            rows.append(
                _option_path_row(
                    root_label=root_label,
                    parts=list(group.get("path_parts") or [container]),
                    option=str(option),
                    group=group,
                    source="standalone_option_group",
                )
            )

    seen: set[str] = set()
    unique: list[dict] = []
    for row in rows:
        key = row["path_id"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    by_kind: dict[str, int] = {}
    by_items_source: dict[str, int] = {}
    for row in unique:
        kind = row.get("effect_kind", "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if row.get("items_source"):
            src = str(row.get("items_source"))
            by_items_source[src] = by_items_source.get(src, 0) + 1
    provider_rows = [row for row in unique if row.get("items_source") == "provider_return_list"]
    provider_groups = {
        str(row.get("option_group_id") or "")
        for row in provider_rows
        if row.get("option_group_id")
    }

    return {
        "schema_version": "1.0",
        "nav_pipeline_version": NAV_PIPELINE_VERSION,
        "bytecode_fallback": {
            "mode": "evidence_only",
            "note": "Bytecode may confirm constants/callbacks, but report labels require source or string resources.",
        },
        "path_count": len(unique),
        "stats": {
            "by_effect_kind": by_kind,
            "by_items_source": by_items_source,
            "provider_option_group_count": len(provider_groups),
            "provider_option_item_count": len(provider_rows),
            "legacy_excluded_dynamic_option_count": sum(
                1 for row in unique
                if row.get("items_source") == "provider_return_list" and row.get("user_visible") is False
            ),
        },
        "paths": unique,
    }


def _candidate_id(kind: str, rel: str, line: int, extra: str) -> str:
    h = hashlib.sha256(f"{kind}|{rel}|{line}|{extra}".encode("utf-8")).hexdigest()[:16]
    return f"cand_{h}"


def collect_navigation_candidates(
    project_root: str,
    dep_roots: list[str] | None,
) -> list[dict]:
    """L1 — all scans are syntactic; no app-specific class names."""
    candidates: list[dict] = []
    for _path, source, class_name, rel in gather_kt_sources(project_root, dep_roots):
        for m in _CREATE_INTENT_START.finditer(source):
            receiver = m.group(1)
            line = source[: m.start()].count("\n") + 1
            candidates.append(
                {
                    "id": _candidate_id("create_intent_factory", rel, line, receiver),
                    "kind": "create_intent_factory",
                    "effect": "navigating_resolvable",
                    "from_class": class_name,
                    "to_class": receiver,
                    "file": rel.replace("\\", "/"),
                    "line": line,
                    "evidence": "startActivity(CompanionReceiver.createIntent(",
                }
            )

        # Pre-build a map of helper-method-name → target class for this source file.
        # Covers patterns like: private static Intent getXxxActivity(ctx) { … new Intent(ctx, Foo.class) … }
        _helper_target_cache: dict[str, str] = {}
        for hm in _HELPER_INTENT_METHOD.finditer(source):
            method_name = hm.group(1)
            body = hm.group(2)
            java_m = _ASSIGN_INTENT_TARGET_JAVA.search(body)
            if java_m:
                _helper_target_cache[method_name] = java_m.group(2)

        for m in _VAR_START_ACTIVITY.finditer(source):
            var = m.group(1)
            if var in _SKIP_VAR_NAMES or not var:
                continue
            line = source[: m.start()].count("\n") + 1
            window = source[max(0, m.start() - 4000) : m.start()]

            # ── Kotlin: val x = Intent(..., Target::class.java) ──────────────
            assign_pat = re.compile(
                rf"(?:val|var)\s+{re.escape(var)}\s*=\s*Intent\s*\(\s*[^,]*,\s*(\w+)::class\.java",
                re.MULTILINE,
            )
            matches = list(assign_pat.finditer(window))
            if matches:
                target = matches[-1].group(1)
                candidates.append(
                    {
                        "id": _candidate_id("local_intent_var", rel, line, var + target),
                        "kind": "local_intent_var",
                        "effect": "navigating_resolvable",
                        "from_class": class_name,
                        "to_class": target,
                        "file": rel.replace("\\", "/"),
                        "line": line,
                        "evidence": f"startActivity({var}) with Intent(..., {target}::class.java)",
                    }
                )
                continue

            # ── Java: Intent intent = new Intent(context, Target.class) ──────
            java_assign_pat = re.compile(
                rf"(?:Intent\s+)?{re.escape(var)}\s*=\s*new\s+Intent\s*\([^,)]+,\s*(\w+)\.class\s*\)",
                re.MULTILINE,
            )
            java_matches = list(java_assign_pat.finditer(window))
            if java_matches:
                target = java_matches[-1].group(1)
                candidates.append(
                    {
                        "id": _candidate_id("local_intent_var_java", rel, line, var + target),
                        "kind": "local_intent_var",
                        "effect": "navigating_resolvable",
                        "from_class": class_name,
                        "to_class": target,
                        "file": rel.replace("\\", "/"),
                        "line": line,
                        "evidence": f"startActivity({var}) with new Intent(..., {target}.class)",
                    }
                )
                continue

            # ── Helper method: var = getXxxActivity(ctx); startActivity(var) ─
            if var in _helper_target_cache:
                target = _helper_target_cache[var]
                candidates.append(
                    {
                        "id": _candidate_id("helper_intent_call", rel, line, var + target),
                        "kind": "helper_intent_call",
                        "effect": "navigating_resolvable",
                        "from_class": class_name,
                        "to_class": target,
                        "file": rel.replace("\\", "/"),
                        "line": line,
                        "evidence": f"startActivity({var}) via helper → new Intent(..., {target}.class)",
                    }
                )
                continue

            candidates.append(
                {
                    "id": _candidate_id("unresolved_start_activity", rel, line, var),
                    "kind": "unresolved_start_activity",
                    "effect": "unknown",
                    "from_class": class_name,
                    "to_class": None,
                    "file": rel.replace("\\", "/"),
                    "line": line,
                    "evidence": f"startActivity({var})",
                }
            )

        # ── FragmentStateAdapter / FragmentPagerAdapter → child Fragments ─────
        for pat in (_ADAPTER_CREATE_FRAGMENT_KT, _ADAPTER_CREATE_FRAGMENT_JAVA):
            for m in pat.finditer(source):
                frag_class = m.group(1)
                line = source[: m.start()].count("\n") + 1
                candidates.append(
                    {
                        "id": _candidate_id("adapter_fragment_child", rel, line, class_name + frag_class),
                        "kind": "adapter_fragment_child",
                        "effect": "navigating_resolvable",
                        "from_class": class_name,
                        "to_class": frag_class,
                        "file": rel.replace("\\", "/"),
                        "line": line,
                        "evidence": f"createFragment/getItem returns {frag_class}",
                    }
                )

        for m in _PRINT_SERVICE.finditer(source):
            line = source[: m.start()].count("\n") + 1
            candidates.append(
                {
                    "id": _candidate_id("system_print", rel, line, m.group(0)),
                    "kind": "system_print",
                    "effect": "non_navigating",
                    "from_class": class_name,
                    "to_class": None,
                    "file": rel.replace("\\", "/"),
                    "line": line,
                    "evidence": m.group(0)[:80],
                }
            )

    candidates.extend(collect_ui_action_bindings(project_root, dep_roots))
    candidates.extend(collect_setting_action_bindings(project_root, dep_roots))
    candidates.extend(collect_provider_option_catalogs(project_root, dep_roots))
    candidates.extend(collect_dynamic_option_groups(project_root, dep_roots))
    candidates.extend(collect_provider_option_binding_diagnostics(project_root, dep_roots))
    candidates.extend(collect_compose_control_option_groups(project_root, dep_roots))
    candidates.extend(collect_setting_option_groups(project_root, dep_roots))
    if any(c.get("kind") == "system_print" for c in candidates):
        catalog = load_android_print_media_size_catalog()
        candidates.append(
            {
                "id": _candidate_id("system_print_media_size_catalog", "android_framework", 0, ""),
                "kind": "system_print_media_size_catalog",
                "effect": "report_candidate",
                "from_class": "AndroidPrintManager",
                "to_class": None,
                "file": str(catalog.get("catalog_source") or ""),
                "line": 0,
                "items_source": "android_framework_catalog",
                "catalog_source": catalog.get("catalog_source", ""),
                "option_count": len(catalog.get("items", [])),
                "evidence": "PrintManager.print -> PrintAttributes.MediaSize framework catalog",
            }
        )
    effects = collect_action_effects(project_root, dep_roots)
    if any(e.get("effect_kind") == "system_document_picker" for e in effects.values()):
        catalog = load_android_document_picker_catalog()
        candidates.append(
            {
                "id": _candidate_id("system_document_picker_catalog", "android_documentsui", 0, ""),
                "kind": "system_document_picker_catalog",
                "effect": "report_candidate",
                "from_class": "SystemDocumentPicker",
                "to_class": None,
                "file": str(catalog.get("catalog_source") or ""),
                "line": 0,
                "items_source": "android_system_document_picker",
                "catalog_source": catalog.get("catalog_source", ""),
                "option_count": len(catalog.get("items", [])),
                "provider_scopes": sorted(
                    {
                        str(item.get("provider_scope") or "")
                        for item in catalog.get("items", [])
                        if item.get("provider_scope")
                    }
                ),
                "evidence": "ACTION_OPEN_DOCUMENT/ACTION_GET_CONTENT/ACTION_CREATE_DOCUMENT -> DocumentsUI catalog",
            }
        )

    # ── Filter out candidates whose to_class is clearly a non-screen class ──
    # These suffixes identify infrastructure classes that are never navigable
    # screens: ViewModel, Helper, Repository, Manager, UseCase, Provider,
    # Adapter, Binding, Factory, Dao.
    _NON_SCREEN_SUFFIXES = (
        "ViewModel", "Helper", "Repository", "Manager", "UseCase",
        "Provider", "Adapter", "Binding", "Factory", "Dao",
    )
    candidates = [
        c for c in candidates
        if not (
            c.get("to_class")
            and any(c["to_class"].endswith(s) for s in _NON_SCREEN_SUFFIXES)
        )
    ]

    return candidates


def extract_l2_create_intent_edges(
    source: str,
    class_name: str,
    rel_file: str,
    layout_for: Callable[[str], str],
) -> list[dict]:
    edges: list[dict] = []
    for m in _CREATE_INTENT_START.finditer(source):
        target = m.group(1)
        line = source[: m.start()].count("\n") + 1
        enclosing_fn = _enclosing_function_before(source, m.start())
        edges.append(
            {
                "from": class_name,
                "to": target,
                "to_layout": layout_for(target),
                "type": "activity",
                "via": "startActivity(createIntent)",
                "trigger": f"l2:createIntent:L{line}",
                "trigger_kind": "l2_create_intent",
                "display_source": "synthetic_navigation",
                "user_visible": False,
                "line": line,
                "source_file": rel_file.replace("\\", "/"),
                "enclosing_fn": enclosing_fn,
            }
        )
    return edges


def extract_l2_variable_intent_edges(
    source: str,
    class_name: str,
    rel_file: str,
    layout_for: Callable[[str], str],
) -> list[dict]:
    edges: list[dict] = []
    for m in _VAR_START_ACTIVITY.finditer(source):
        var = m.group(1)
        if var in _SKIP_VAR_NAMES:
            continue
        line = source[: m.start()].count("\n") + 1
        window = source[max(0, m.start() - 4000) : m.start()]
        assign_pat = re.compile(
            rf"(?:val|var)\s+{re.escape(var)}\s*=\s*Intent\s*\(\s*[^,]*,\s*(\w+)::class\.java",
            re.MULTILINE,
        )
        matches = list(assign_pat.finditer(window))
        if not matches:
            continue
        target = matches[-1].group(1)
        enclosing_fn = _enclosing_function_before(source, m.start())
        edges.append(
            {
                "from": class_name,
                "to": target,
                "to_layout": layout_for(target),
                "type": "activity",
                "via": "startActivity(local Intent)",
                "trigger": f"l2:localIntent:L{line}",
                "trigger_kind": "l2_local_intent",
                "display_source": "synthetic_navigation",
                "user_visible": False,
                "line": line,
                "source_file": rel_file.replace("\\", "/"),
                "enclosing_fn": enclosing_fn,
            }
        )
    return edges


def load_navigation_overlay(project_root: str, rules: dict | None = None) -> list[dict]:
    rules = rules or load_nav_rules()
    root = Path(project_root)
    for rel in rules.get("overlay_relative_paths", []):
        p = root / rel
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        edges = data.get("edges")
        if isinstance(edges, list):
            return [e for e in edges if isinstance(e, dict)]
    return []


def merge_overlay_edges(
    edges: list[dict],
    overlay: list[dict],
    layout_for: Callable[[str], str],
) -> list[dict]:
    """Append overlay edges; fill to_layout when missing."""
    out = list(edges)
    for e in overlay:
        to = e.get("to", "")
        row = {
            "from": e.get("from", ""),
            "to": to,
            "to_layout": e.get("to_layout") or layout_for(to),
            "type": e.get("type", "activity"),
            "via": e.get("via", "overlay"),
            "trigger": e.get("trigger", "overlay"),
            "line": int(e.get("line", 0)),
        }
        sf = e.get("source_file") or e.get("file")
        if sf:
            row["source_file"] = str(sf).replace("\\", "/")
        for key in ("trigger_kind", "display_source", "user_visible", "display_label", "enclosing_fn"):
            if key in e:
                row[key] = e[key]
        out.append(row)
    return out


def _edge_key(e: dict) -> tuple:
    return (
        e.get("from", ""),
        e.get("to", ""),
        e.get("trigger", ""),
        e.get("line", 0),
    )


def dedupe_edges(edges: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    unique: list[dict] = []
    for e in edges:
        k = _edge_key(e)
        if k in seen:
            continue
        seen.add(k)
        unique.append(e)
    return unique


def apply_launcher_anchor_edges(
    edges: list[dict],
    launcher_class: str,
    suffixes: list[str] | None = None,
) -> list[dict]:
    """
    For edges whose `from` ends with configured suffixes (e.g. *Handler), add
    parallel edges from launcher so graph reachability from root matches product flows.
    """
    if not launcher_class:
        return edges
    suf = tuple(suffixes or ("Handler", "Delegate"))
    extras: list[dict] = []
    existing = {_edge_key(e) for e in edges}
    for e in edges:
        src = e.get("from", "")
        if not src or src in ("EXTERNAL", launcher_class):
            continue
        if not any(src.endswith(s) for s in suf):
            continue
        neo = {
            "from": launcher_class,
            "to": e.get("to", ""),
            "to_layout": e.get("to_layout", ""),
            "type": e.get("type", "activity"),
            "via": f"synthetic_anchor({src}):{e.get('via', '')}",
            "trigger": e.get("trigger", ""),
            "line": e.get("line", 0),
        }
        if sf := e.get("source_file"):
            neo["source_file"] = sf
        for key in ("trigger_kind", "display_source", "user_visible", "display_label", "enclosing_fn"):
            if key in e:
                neo[key] = e[key]
        k = _edge_key(neo)
        if k not in existing:
            existing.add(k)
            extras.append(neo)
    return edges + extras


def build_candidates_payload(
    project_root: str,
    dep_roots: list[str] | None,
    rules: dict | None = None,
) -> dict:
    rules = rules or load_nav_rules()
    cands = collect_navigation_candidates(project_root, dep_roots)
    by_kind: dict[str, int] = {}
    by_items_source: dict[str, int] = {}
    for c in cands:
        by_kind[c.get("kind", "?")] = by_kind.get(c.get("kind", "?"), 0) + 1
        if c.get("items_source"):
            src = str(c.get("items_source"))
            by_items_source[src] = by_items_source.get(src, 0) + 1
    provider_catalogs = [c for c in cands if c.get("kind") == "provider_option_catalog"]
    provider_option_groups = [
        c for c in cands
        if c.get("kind") == "dynamic_option_group" and c.get("items_source") == "provider_return_list"
    ]
    return {
        "schema_version": "1.0",
        "nav_pipeline_version": NAV_PIPELINE_VERSION,
        "nav_rules_version": str(rules.get("nav_rules_version", "0")),
        "stats": {
            "total": len(cands),
            "by_kind": by_kind,
            "by_items_source": by_items_source,
            "provider_option_catalog_count": len(provider_catalogs),
            "provider_option_group_count": len(provider_option_groups),
            "provider_option_item_count": sum(len(c.get("options") or []) for c in provider_option_groups),
            "dynamic_option_group_unresolved_params": by_kind.get("dynamic_option_group_unresolved_param", 0),
        },
        "candidates": cands,
    }
