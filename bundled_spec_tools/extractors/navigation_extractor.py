"""
navigation_extractor.py

从 Kotlin/Java 源码中提取屏幕导航图：
  - Activity → Activity 跳转 (startActivity / startActivityForResult)
  - Activity/Fragment → Dialog 弹出
  - Menu → Activity/Dialog 跳转
  - 外部 Intent 入口 (intent-filter, ACTION_*)
  - 返回关系 (finish / onBackPressed)
  - 隐式 Intent 跳转 (AndroidManifest intent-filter 解析)
  - Adapter → Host Activity 绑定 (CAB 菜单、item 点击)

输出 navigation_graph.json，结构为：
{
  "nodes": { "ScreenA": { ... }, ... },
  "edges": [
    { "from": "ScreenA", "to": "ScreenB", "trigger": "click xxx", "type": "activity|dialog|external", "via": "startActivity|Dialog()" },
    ...
  ]
}
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import OrderedDict

try:
    from extractors import ast_index
except Exception:  # pragma: no cover - optional AST layer
    ast_index = None  # type: ignore[assignment]

from extractors import android_project


# ── Commons library launcher functions ───────────────────────────────────────
# These are extension functions defined in external library code (e.g. Simple-Commons).
# They are the only hint we need from "known" knowledge — everything else is derived
# from the project's own source files dynamically.
#
# Format: function_name → destination_class_name
# Use "EXTERNAL_*" for non-Activity destinations (links, store pages, etc.)
_COMMONS_LAUNCHERS = {
    "launchSettings":               "SettingsActivity",
    "launchAbout":                  "AboutActivity",
    "launchFAQ":                    "FAQActivity",
    "launchLicense":                "LicenseActivity",
    "launchCustomizationActivity":  "CustomizationActivity",
    "startCustomizationActivity":   "CustomizationActivity",
    "launchRateUsPrompt":           "EXTERNAL_RATE",
    "launchMoreAppsFromUsIntent":   "EXTERNAL_MORE_APPS",
    "launchApp":                    "EXTERNAL_LAUNCH",
}

# Function names that should NOT be traced as indirect navigation calls.
_SKIP_FN_NAMES = {
    'this', 'super', 'dismiss', 'cancel', 'show', 'hide', 'gone', 'visible',
    'invalidate', 'notifyDataSetChanged', 'update', 'refresh', 'init',
    'true', 'false', 'null', 'Unit', 'it', 'apply', 'also', 'let', 'run',
    'with', 'toast', 'finish', 'recreate', 'invalidateOptionsMenu',
}

# ── Dynamic class→layout mapping ─────────────────────────────────────────────
# Populated by run() from scanning ViewBinding / setContentView declarations.
# No app-specific hardcoding — works for any Android project.
_INFERRED_LAYOUTS: dict = {}
_ALL_KNOWN_LAYOUTS: set = set()


def _scan_all_layouts(project_root: str) -> set:
    """Scan all res/layout* dirs (project + deps) to get every layout name."""
    layouts = set()
    for res_dir in android_project.res_dirs(project_root):
        for layout_dir in res_dir.glob("layout*"):
            for f in layout_dir.glob("*.xml"):
                layouts.add(f.stem)
    return layouts

_BINDING_DECL_RE = re.compile(
    r'by\s+\w*[Bb]inding\w*\s*\(\s*(\w+Binding)\s*::'   # by viewBinding(XxxBinding::inflate)
    r'|(\w+Binding)\.inflate\s*\('                         # XxxBinding.inflate(  [ViewBinding]
    r'|setContentView\s*\(\s*R\.layout\.(\w+)'             # setContentView(R.layout.xxx)
    r'|DataBindingUtil\.setContentView\s*\([^,]+,\s*R\.layout\.(\w+)'   # DataBinding Activity
    r'|DataBindingUtil\.inflate\s*\([^,]+,\s*R\.layout\.(\w+)'          # DataBinding Fragment
)


def _binding_to_layout(binding_class: str) -> str:
    """ActivityManageFoldersBinding  →  activity_manage_folders"""
    name = re.sub(r'Binding$', '', binding_class)
    return re.sub(r'(?<=[a-z])(?=[A-Z])', '_', name).lower()


def _scan_class_layouts(project_root: str | Path) -> dict:
    """Scan source files; infer class→layout from ViewBinding / setContentView.

    Fully generic — no hardcoded class names required.
    """
    result = {}
    for src_file in android_project.source_files(project_root):
        source = src_file.read_text(encoding="utf-8", errors="ignore")
        class_name = src_file.stem
        for m in _BINDING_DECL_RE.finditer(source):
            binding_class = m.group(1) or m.group(2)
            # Groups 3-5: direct layout name (setContentView / DataBindingUtil variants)
            direct_layout = m.group(3) or m.group(4) or m.group(5)
            if direct_layout:
                result[class_name] = direct_layout
                break
            if binding_class:
                result[class_name] = _binding_to_layout(binding_class)
                break
    return result


def _extract_class_name(filepath: Path) -> str:
    return filepath.stem


def _find_layout_for_class(class_name: str) -> str:
    """Return the layout file name for a class.

    Priority:
      1. Dynamically inferred from ViewBinding/setContentView in source (most accurate)
      2. Fallback: camelCase→snake_case conversion of the class name
      3. Fuzzy: search _ALL_KNOWN_LAYOUTS for a dialog layout matching class keywords
    """
    if class_name in _INFERRED_LAYOUTS:
        return _INFERRED_LAYOUTS[class_name]
    fallback = re.sub(r'(?<=[a-z])(?=[A-Z])', '_', class_name).lower()
    if fallback in _ALL_KNOWN_LAYOUTS:
        return fallback
    # Fuzzy: for XxxDialog, try dialog_xxx pattern
    if "Dialog" in class_name:
        keywords = re.findall(r'[A-Z][a-z]+', class_name.replace("Dialog", ""))
        for layout in _ALL_KNOWN_LAYOUTS:
            if not layout.startswith("dialog_"):
                continue
            if all(kw.lower() in layout for kw in keywords):
                return layout
    return fallback


def _is_dialog_class(class_name: str) -> bool:
    return "Dialog" in class_name or "BottomSheet" in class_name


def _is_activity_class(class_name: str) -> bool:
    return "Activity" in class_name


def _extract_edges_from_file(filepath: Path, source: str) -> list[dict]:
    edges = []
    class_name = _extract_class_name(filepath)

    # --- 1. startActivity(Intent(this, XxxActivity::class.java)) ---
    for m in re.finditer(
        r'startActivity(?:ForResult)?\([^)]*?Intent\([^,]*,\s*(\w+)::class\.java',
        source
    ):
        target = m.group(1)
        line = source[:m.start()].count('\n') + 1
        trigger = _find_trigger_context(source, m.start(), class_name)
        edges.append({
            "from": class_name,
            "to": target,
            "to_layout": _find_layout_for_class(target),
            "type": "activity",
            "via": "startActivity",
            "trigger": trigger,
            "line": line,
        })

    # --- 1b. Intent(this, Xxx::class.java).apply { someFn(this) } — indirect launch ---
    for m in re.finditer(
        r'Intent\(\s*this\s*,\s*(\w+)::class\.java\)',
        source
    ):
        target = m.group(1)
        line = source[:m.start()].count('\n') + 1
        if any(e["to"] == target and abs(e["line"] - line) < 5 for e in edges):
            continue
        after = source[m.end():m.end() + 500]
        if re.search(r'startActivity|startActivityForResult', after[:500]):
            continue
        trigger = _find_trigger_context(source, m.start(), class_name)
        edges.append({
            "from": class_name,
            "to": target,
            "to_layout": _find_layout_for_class(target),
            "type": "activity",
            "via": "indirect Intent",
            "trigger": trigger,
            "line": line,
        })

    # --- 1d. Java: startActivity(new Intent(ctx, XxxActivity.class)) ---
    for m in re.finditer(
        r'startActivity(?:ForResult)?\s*\([^)]*?new\s+Intent\s*\([^,)]+,\s*(\w+)\.class\s*\)',
        source
    ):
        target = m.group(1)
        line = source[:m.start()].count('\n') + 1
        if any(e["to"] == target and abs(e["line"] - line) < 5 for e in edges):
            continue
        trigger = _find_trigger_context(source, m.start(), class_name)
        edges.append({
            "from": class_name,
            "to": target,
            "to_layout": _find_layout_for_class(target),
            "type": "activity",
            "via": "startActivity [Java]",
            "trigger": trigger,
            "line": line,
        })

    # --- 1e. Conditional ::class.java assigned to a variable used in Intent(ctx, var) ---
    # Handles: val target = if (...) AActivity::class.java else BActivity::class.java
    #          return Intent(context, target)
    _FN_DECL_RE_1E = re.compile(
        r'(?:fun\s+(\w+)\s*\('
        r'|(?:public|private|protected)\s+(?:static\s+)?[\w<>\[\],\s]+\s+(\w+)\s*\()'
    )
    for fn_m in _FN_DECL_RE_1E.finditer(source):
        fn_name_1e = fn_m.group(1) or fn_m.group(2)
        body_1e = _find_function_body(source, fn_name_1e, max_len=4000)
        if not body_1e:
            continue
        # Only trigger when the body creates Intent with a variable (not a literal class)
        # Pattern: Intent(context, someVariable) — the second arg is NOT followed by ::class
        if not re.search(r'Intent\s*\(\s*\w+[\w.@]*\s*,\s*\w+\s*\)', body_1e):
            continue
        klasses_1e = re.findall(r'\b(\w+)::class\.java', body_1e)
        if not klasses_1e:
            continue
        line_1e = source[:fn_m.start()].count('\n') + 1
        trigger_1e = f"fn: {fn_name_1e}"
        for klass in set(klasses_1e):
            if any(e["to"] == klass and e["from"] == class_name for e in edges):
                continue
            edges.append({
                "from": class_name,
                "to": klass,
                "to_layout": _find_layout_for_class(klass),
                "type": "activity",
                "via": "Intent(ctx, variable)[conditional]",
                "trigger": trigger_1e,
                "line": line_1e,
            })

    # --- 1f. Java: Intent var = new Intent(ctx, Xxx.class); ...; startActivity(var) ---
    _JAVA_FN_DECL_RE = re.compile(
        r'(?:public|private|protected)\s+(?:static\s+)?[\w<>\[\],\s]+\s+(\w+)\s*\('
    )
    for fn_m in _JAVA_FN_DECL_RE.finditer(source):
        fn_name_1f = fn_m.group(1)
        body_1f = _find_function_body(source, fn_name_1f, max_len=5000)
        if not body_1f or 'startActivity' not in body_1f:
            continue
        for im in re.finditer(
            r'(?:Intent|intent)\s+\w+\s*=\s*new\s+Intent\s*\([^,)]+,\s*(\w+)\.class\s*\)',
            body_1f,
        ):
            target = im.group(1)
            line_1f = source[:fn_m.start()].count('\n') + 1
            if any(e["to"] == target and e["from"] == class_name for e in edges):
                continue
            edges.append({
                "from": class_name,
                "to": target,
                "to_layout": _find_layout_for_class(target),
                "type": "activity",
                "via": "Intent(var) [Java]",
                "trigger": f"fn: {fn_name_1f}",
                "line": line_1f,
            })

    # --- 1g. Async lambda: postDelayed / lifecycleScope.launch / runOnUiThread ---
    _ASYNC_WRAPPERS_RE = re.compile(
        r'(?:postDelayed|lifecycleScope\.launch|viewModelScope\.launch|'
        r'runOnUiThread|Handler\s*\([^)]*\)\.post(?:Delayed)?)\s*[\({]'
    )
    for aw in _ASYNC_WRAPPERS_RE.finditer(source):
        block = _get_click_block(source, aw.end() - 1, max_len=1000)
        for im in re.finditer(
            r'Intent\s*\(\s*\w+[\w.@]*\s*,\s*(\w+)::class\.java', block
        ):
            target = im.group(1)
            line_aw = source[:aw.start()].count('\n') + 1
            if any(e["to"] == target and abs(e["line"] - line_aw) < 10 for e in edges):
                continue
            trigger_aw = _find_trigger_context(source, aw.start(), class_name)
            edges.append({
                "from": class_name,
                "to": target,
                "to_layout": _find_layout_for_class(target),
                "type": "activity",
                "via": "async lambda",
                "trigger": trigger_aw,
                "line": line_aw,
            })

    # --- 2. startActivity(Intent(this, XxxActivity::class.java).apply { ... }) ---
    for m in re.finditer(
        r'startActivity(?:ForResult)?\([^)]*?Intent\(\s*this\s*,\s*(\w+)::class',
        source
    ):
        target = m.group(1)
        if not any(e["to"] == target and e["line"] == source[:m.start()].count('\n') + 1 for e in edges):
            line = source[:m.start()].count('\n') + 1
            trigger = _find_trigger_context(source, m.start(), class_name)
            edges.append({
                "from": class_name,
                "to": target,
                "to_layout": _find_layout_for_class(target),
                "type": "activity",
                "via": "startActivity",
                "trigger": trigger,
                "line": line,
            })

    # --- 3. XxxDialog(this) or XxxDialog(activity) — dialog instantiation ---
    for m in re.finditer(
        r'(\w+(?:Dialog|DialogFragment))\s*\([^)]*?(?:this|activity|requireContext)\b',
        source
    ):
        target = m.group(1)
        if _is_dialog_class(target):
            line = source[:m.start()].count('\n') + 1
            trigger = _find_trigger_context(source, m.start(), class_name)
            to_layout = _find_layout_for_class(target)
            dtype = "dialog" if target in _INFERRED_LAYOUTS else "commons_dialog"
            edges.append({
                "from": class_name,
                "to": target,
                "to_layout": to_layout,
                "type": dtype,
                "via": "Dialog()",
                "trigger": trigger,
                "line": line,
            })

    # --- 4. startCustomizationActivity / launchMoreAppsFromUs / specific named navigations ---
    for m in re.finditer(
        r'(?:binding|findViewById)\.(\w+?)\.setOn\w*Listener|\.setOnClickListener\s*\{',
        source
    ):
        line = source[:m.start()].count('\n') + 1
        view_id = m.group(1) if m.group(1) else ""
        block = _get_click_block(source, m.end())

        # check if block contains startActivity or Dialog
        for act_m in re.finditer(r'startActivity\([^)]*?Intent\([^,]*,\s*(\w+)::class', block):
            target = act_m.group(1)
            if not any(e["to"] == target and abs(e["line"] - line) < 5 for e in edges):
                trigger = f"click {view_id}" if view_id else "click"
                edges.append({
                    "from": class_name,
                    "to": target,
                    "to_layout": _find_layout_for_class(target),
                    "type": "activity",
                    "via": "click -> startActivity",
                    "trigger": trigger,
                    "line": line,
                })

        for dlg_m in re.finditer(r'(\w+(?:Dialog|DialogFragment))\s*\(', block):
            target = dlg_m.group(1)
            if _is_dialog_class(target):
                if not any(e["to"] == target and abs(e["line"] - line) < 5 for e in edges):
                    trigger = f"click {view_id}" if view_id else "click"
                    to_layout = _find_layout_for_class(target)
                    dtype = "dialog" if target in _INFERRED_LAYOUTS else "commons_dialog"
                    edges.append({
                        "from": class_name,
                        "to": target,
                        "to_layout": to_layout,
                        "type": dtype,
                        "via": "click -> Dialog()",
                        "trigger": trigger,
                        "line": line,
                    })

        # startCustomizationActivity
        if 'startCustomizationActivity' in block:
            if not any(e["to"] == "CustomizationActivity" and abs(e["line"] - line) < 5 for e in edges):
                trigger = f"click {view_id}" if view_id else "click"
                edges.append({
                    "from": class_name,
                    "to": "CustomizationActivity",
                    "to_layout": "CustomizationActivity",
                    "type": "activity",
                    "via": "click -> startCustomizationActivity()",
                    "trigger": trigger,
                    "line": line,
                })

    # --- 5. Menu item R.id.xxx -> startActivity/Dialog ---
    # Kotlin when blocks: R.id.xxx -> someCall() (single line) or R.id.xxx -> { block }
    for m in re.finditer(r'R\.id\.(\w+)\s*->\s*', source):
        menu_id = m.group(1)
        line = source[:m.start()].count('\n') + 1
        after = source[m.end():]

        # Check if it's a block { ... } or single line expression
        stripped = after.lstrip()
        if stripped.startswith('{'):
            block = _get_click_block(source, m.end() + after.index('{'))
        else:
            # Single line: take until newline or next ->
            nl_pos = after.find('\n')
            block = after[:nl_pos] if nl_pos != -1 else after[:200]

        # Check for commons launchers
        for launch_m in re.finditer(r'(\w+)\s*\(\)', block):
            fn_call = launch_m.group(1)
            if fn_call in _COMMONS_LAUNCHERS:
                target = _COMMONS_LAUNCHERS[fn_call]
                edge_type = "activity" if "Activity" in target else "external"
                if not any(e["to"] == target and abs(e["line"] - line) < 3 for e in edges):
                    edges.append({
                        "from": class_name,
                        "to": target,
                        "to_layout": _find_layout_for_class(target) if "Activity" in target else "",
                        "type": edge_type,
                        "via": f"menu -> {fn_call}()",
                        "trigger": f"menu {menu_id}",
                        "line": line,
                    })

        # Check for startActivity
        for act_m in re.finditer(r'startActivity\([^)]*?Intent\([^,]*,\s*(\w+)::class', block):
            target = act_m.group(1)
            if not any(e["to"] == target and abs(e["line"] - line) < 3 for e in edges):
                edges.append({
                    "from": class_name,
                    "to": target,
                    "to_layout": _find_layout_for_class(target),
                    "type": "activity",
                    "via": "menu -> startActivity",
                    "trigger": f"menu {menu_id}",
                    "line": line,
                })

        # Check for dialog
        for dlg_m in re.finditer(r'(\w+(?:Dialog|DialogFragment))\s*\(', block):
            target = dlg_m.group(1)
            if _is_dialog_class(target):
                if not any(e["to"] == target and abs(e["line"] - line) < 3 for e in edges):
                    to_layout = _find_layout_for_class(target)
                    dtype = "dialog" if target in _INFERRED_LAYOUTS else "commons_dialog"
                    edges.append({
                        "from": class_name,
                        "to": target,
                        "to_layout": to_layout,
                        "type": dtype,
                        "via": "menu -> Dialog()",
                        "trigger": f"menu {menu_id}",
                        "line": line,
                    })

        # Check for indirect function calls that lead to navigation
        for fn_m in re.finditer(r'(\w+)\s*\(\)', block):
            fn_call = fn_m.group(1)
            if fn_call not in _COMMONS_LAUNCHERS and fn_call not in ('this', 'super'):
                # Look up function body in the same source
                fn_body = _find_function_body(source, fn_call)
                if fn_body:
                    for act_m in re.finditer(r'startActivity\([^)]*?Intent\([^,]*,\s*(\w+)::class', fn_body):
                        target = act_m.group(1)
                        if not any(e["to"] == target and abs(e["line"] - line) < 3 for e in edges):
                            edges.append({
                                "from": class_name,
                                "to": target,
                                "to_layout": _find_layout_for_class(target),
                                "type": "activity",
                                "via": f"menu -> {fn_call}() -> startActivity",
                                "trigger": f"menu {menu_id}",
                                "line": line,
                            })
                    for dlg_m in re.finditer(r'(\w+(?:Dialog|DialogFragment))\s*\(', fn_body):
                        target = dlg_m.group(1)
                        if _is_dialog_class(target):
                            if not any(e["to"] == target and abs(e["line"] - line) < 3 for e in edges):
                                to_layout = _find_layout_for_class(target)
                                dtype = "dialog" if target in _INFERRED_LAYOUTS else "commons_dialog"
                                edges.append({
                                    "from": class_name,
                                    "to": target,
                                    "to_layout": to_layout,
                                    "type": dtype,
                                    "via": f"menu -> {fn_call}() -> Dialog()",
                                    "trigger": f"menu {menu_id}",
                                    "line": line,
                                })

    # --- 6. Specific: launchViewVideoIntent, sendViewPagerIntent ---
    for m in re.finditer(r'(?:launchViewVideoIntent|sendViewPagerIntent)\(', source):
        line = source[:m.start()].count('\n') + 1
        target = "ViewPagerActivity"
        trigger = _find_trigger_context(source, m.start(), class_name)
        if not any(e["to"] == target and abs(e["line"] - line) < 5 for e in edges):
            edges.append({
                "from": class_name,
                "to": target,
                "to_layout": _find_layout_for_class(target),
                "type": "activity",
                "via": m.group(0).rstrip('('),
                "trigger": trigger,
                "line": line,
            })

    # --- 7. R.id.xxx holder click patterns (e.g., settings_xxx_holder.setOnClickListener) ---
    for m in re.finditer(
        r'(?:binding)\.(\w+?)\.setOnClickListener',
        source
    ):
        holder_id = m.group(1)
        line = source[:m.start()].count('\n') + 1
        block = _get_click_block(source, m.end())

        for dlg_m in re.finditer(r'(\w+(?:Dialog|DialogFragment))\s*\(', block):
            target = dlg_m.group(1)
            if _is_dialog_class(target):
                if not any(e["to"] == target and abs(e["line"] - line) < 5 for e in edges):
                    to_layout = _find_layout_for_class(target)
                    dtype = "dialog" if target in _INFERRED_LAYOUTS else "commons_dialog"
                    edges.append({
                        "from": class_name,
                        "to": target,
                        "to_layout": to_layout,
                        "type": dtype,
                        "via": f"click {holder_id} -> Dialog()",
                        "trigger": f"click {holder_id}",
                        "line": line,
                    })

        for act_m in re.finditer(r'startActivity\([^)]*?Intent\([^,]*,\s*(\w+)::class', block):
            target = act_m.group(1)
            if not any(e["to"] == target and abs(e["line"] - line) < 5 for e in edges):
                edges.append({
                    "from": class_name,
                    "to": target,
                    "to_layout": _find_layout_for_class(target),
                    "type": "activity",
                    "via": f"click {holder_id} -> startActivity",
                    "trigger": f"click {holder_id}",
                    "line": line,
                })

        # Trace indirect function calls inside the click block.
        # Pattern: binding.settingsXxxHolder.setOnClickListener { changeSomething() }
        # where changeSomething() opens a dialog or starts an activity.
        for fn_m in re.finditer(r'\b(\w+)\s*\(\s*\)', block):
            fn_call = fn_m.group(1)
            if fn_call in _SKIP_FN_NAMES:
                continue
            if fn_call in _COMMONS_LAUNCHERS:
                target = _COMMONS_LAUNCHERS[fn_call]
                edge_type = "activity" if "Activity" in target else "external"
                if not any(e["to"] == target and abs(e["line"] - line) < 5 for e in edges):
                    edges.append({
                        "from": class_name,
                        "to": target,
                        "to_layout": _find_layout_for_class(target) if "Activity" in target else "",
                        "type": edge_type,
                        "via": f"click {holder_id} -> {fn_call}()",
                        "trigger": f"click {holder_id}",
                        "line": line,
                    })
                continue
            fn_body = _find_function_body(source, fn_call)
            if not fn_body:
                continue
            # Check dialogs in fn_body
            for dlg_m2 in re.finditer(r'(\w+(?:Dialog|DialogFragment))\s*\(', fn_body):
                target = dlg_m2.group(1)
                if _is_dialog_class(target):
                    if not any(e["to"] == target and abs(e["line"] - line) < 5 for e in edges):
                        to_layout = _find_layout_for_class(target)
                        dtype = "dialog" if target in _INFERRED_LAYOUTS else "commons_dialog"
                        edges.append({
                            "from": class_name,
                            "to": target,
                            "to_layout": to_layout,
                            "type": dtype,
                            "via": f"click {holder_id} -> {fn_call}() -> Dialog()",
                            "trigger": f"click {holder_id}",
                            "line": line,
                        })
            # Check startActivity in fn_body
            for act_m2 in re.finditer(
                r'startActivity\([^)]*?Intent\([^,]*,\s*(\w+)::class', fn_body
            ):
                target = act_m2.group(1)
                if not any(e["to"] == target and abs(e["line"] - line) < 5 for e in edges):
                    edges.append({
                        "from": class_name,
                        "to": target,
                        "to_layout": _find_layout_for_class(target),
                        "type": "activity",
                        "via": f"click {holder_id} -> {fn_call}() -> startActivity",
                        "trigger": f"click {holder_id}",
                        "line": line,
                    })
            # Check commons launchers called inside fn_body
            for launch_m2 in re.finditer(r'\b(\w+)\s*\(\s*\)', fn_body):
                launch_fn = launch_m2.group(1)
                if launch_fn in _COMMONS_LAUNCHERS:
                    target = _COMMONS_LAUNCHERS[launch_fn]
                    edge_type = "activity" if "Activity" in target else "external"
                    if not any(e["to"] == target and abs(e["line"] - line) < 5 for e in edges):
                        edges.append({
                            "from": class_name,
                            "to": target,
                            "to_layout": _find_layout_for_class(target) if "Activity" in target else "",
                            "type": edge_type,
                            "via": f"click {holder_id} -> {fn_call}() -> {launch_fn}()",
                            "trigger": f"click {holder_id}",
                            "line": line,
                        })

    # --- 8. Commons library launchers: launchSettings(), launchAbout(), etc. ---
    for m in re.finditer(r'(\w+)\s*\(\)', source):
        fn_call = m.group(1)
        if fn_call in _COMMONS_LAUNCHERS:
            target = _COMMONS_LAUNCHERS[fn_call]
            line = source[:m.start()].count('\n') + 1
            trigger = _find_trigger_context(source, m.start(), class_name)
            edge_type = "activity" if "Activity" in target else "external"
            if not any(e["to"] == target and abs(e["line"] - line) < 3 for e in edges):
                edges.append({
                    "from": class_name,
                    "to": target,
                    "to_layout": _find_layout_for_class(target) if "Activity" in target else "",
                    "type": edge_type,
                    "via": fn_call + "()",
                    "trigger": trigger,
                    "line": line,
                })

    # --- 9. External intent / action entries ---
    for m in re.finditer(r'isPickImageIntent|isPickVideoIntent|isGetImageContentIntent|isGetVideoContentIntent|isGetAnyContentIntent|isSetWallpaperIntent|isExternalIntent', source):
        line = source[:m.start()].count('\n') + 1
        method_name = m.group(0)
        if not any(e.get("trigger", "").startswith("external:") and e["line"] == line for e in edges):
            edges.append({
                "from": "EXTERNAL",
                "to": class_name,
                "to_layout": _find_layout_for_class(class_name),
                "type": "external_intent",
                "via": method_name,
                "trigger": f"external: {method_name}",
                "line": line,
            })

    return edges


def _find_trigger_context(source: str, pos: int, class_name: str) -> str:
    start = max(0, pos - 500)
    context = source[start:pos]

    # Use the LAST (nearest to call site) fun declaration, not the first.
    # re.search returns the first match; iterate and keep the last.
    fn_name = ""
    for fn_match in re.finditer(
        r'(?:private|public|protected|internal)?\s*fun\s+(\w+)', context
    ):
        fn_name = fn_match.group(1)
    if fn_name:
        setup_match = re.match(r'setup(\w+)', fn_name)
        if setup_match:
            return f"setup: {setup_match.group(1)}"
        return f"fn: {fn_name}"

    # Java method fallback: public [static] ReturnType methodName(
    for jm in re.finditer(
        r'(?:public|private|protected)\s+(?:static\s+)?[\w<>\[\],\s]+\s+(\w+)\s*\(',
        context,
    ):
        fn_name = jm.group(1)
    if fn_name:
        return f"fn: {fn_name}"

    # Nearest setOnClickListener receiver within the context window
    click_name = ""
    for click_match in re.finditer(r'(\w+)\.setOnClickListener', context):
        click_name = click_match.group(1)
    if click_name:
        return f"click: {click_name}"

    menu_match = re.search(r'R\.id\.(\w+)', context)
    if menu_match:
        return f"menu: {menu_match.group(1)}"

    return ""


def _get_click_block(source: str, open_pos: int, max_len: int = 2000) -> str:
    brace_pos = source.find('{', open_pos)
    if brace_pos == -1:
        return source[open_pos:open_pos + max_len]
    depth = 0
    for i, ch in enumerate(source[brace_pos:brace_pos + max_len]):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
        if depth == 0:
            return source[brace_pos + 1:brace_pos + i]
    return source[brace_pos + 1:brace_pos + max_len]


def _find_function_body(source: str, fn_name: str, max_len: int = 3000) -> str:
    pattern = re.compile(
        r'(?:(?:private|public|protected|internal)?\s*(?:override\s+)?(?:suspend\s+)?fun\s+'
        + re.escape(fn_name) + r'\s*\('
        + r'|(?:public|private|protected)\s+(?:static\s+)?[\w<>\[\],\s]+\s+'
        + re.escape(fn_name) + r'\s*\()'
    )
    m = pattern.search(source)
    if not m:
        return ""
    after = source[m.start():]
    brace_pos = after.find('{')
    if brace_pos == -1:
        return ""
    return _get_click_block(after, brace_pos, max_len)


def _build_node(class_name: str, edges_from: list[dict], edges_to: list[dict]) -> dict:
    layout = _find_layout_for_class(class_name)
    node_type = "dialog" if _is_dialog_class(class_name) else "activity"
    return {
        "name": class_name,
        "layout": layout,
        "type": node_type,
        "navigates_to": list(OrderedDict.fromkeys(e["to"] for e in edges_from)),
        "navigated_from": list(OrderedDict.fromkeys(e["from"] for e in edges_to)),
        "edges_out": len(edges_from),
        "edges_in": len(edges_to),
    }


_ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


def _parse_manifest_intents(project_root: str) -> dict:
    """Parse AndroidManifest.xml to build action → Activity mapping.

    Returns:
        {
            "android.intent.action.EDIT": ["EditActivity"],
            "android.intent.action.VIEW": ["ViewPagerActivity"],
            ...
        }
    """
    return android_project.manifest_action_map(project_root)


def get_launcher_activity_class(project_root: str) -> str:
    """Return the short Activity class name for MAIN + LAUNCHER, or \"\"."""
    return android_project.launcher_activity_class(project_root)


def _resolve_implicit_intents(
    all_edges: list[dict],
    project_root: str | Path,
    action_map: dict[str, list[str]],
) -> list[dict]:
    """Resolve implicit Intent calls (openEditor, openPathIntent, etc.) to Activity edges.

    Pattern: source code calls openEditor/openEditorIntent/launchCamera etc.
    These use ACTION_EDIT/ACTION_VIEW etc. resolved via AndroidManifest.
    """
    if not action_map:
        return []

    _IMPLICIT_FN_TO_ACTION = {
        "openEditor": "android.intent.action.EDIT",
        "openEditorIntent": "android.intent.action.EDIT",
    }

    edges = []
    for src_file in android_project.source_files(project_root):
        source = src_file.read_text(encoding="utf-8", errors="ignore")
        class_name = src_file.stem

        for fn_name, action in _IMPLICIT_FN_TO_ACTION.items():
            targets = action_map.get(action, [])
            if not targets:
                continue
            for m in re.finditer(re.escape(fn_name) + r'\s*\(', source):
                line = source[:m.start()].count('\n') + 1
                trigger = _find_trigger_context(source, m.start(), class_name)
                for target in targets:
                    if not any(
                        e["to"] == target and abs(e["line"] - line) < 3
                        for e in all_edges + edges
                    ):
                        edges.append({
                            "from": class_name,
                            "to": target,
                            "to_layout": _find_layout_for_class(target),
                            "type": "activity",
                            "via": f"{fn_name}() [implicit:{action}]",
                            "trigger": trigger or f"fn: {fn_name}",
                            "line": line,
                        })

        # Generic: any startActivityForResult / startActivity with Intent.ACTION_XXX constant
        for m in re.finditer(
            r'(?:startActivity|startActivityForResult)\s*\([^)]*?Intent\.\s*(\w+)',
            source,
        ):
            action_const = m.group(1)
            action_map_to = {
                "ACTION_EDIT": "android.intent.action.EDIT",
                "ACTION_VIEW": "android.intent.action.VIEW",
                "ACTION_PICK": "android.intent.action.PICK",
                "ACTION_SEND": "android.intent.action.SEND",
                "ACTION_GET_CONTENT": "android.intent.action.GET_CONTENT",
                "ACTION_INSERT": "android.intent.action.INSERT",
            }
            action = action_map_to.get(action_const)
            if not action:
                continue
            targets = action_map.get(action, [])
            line = source[:m.start()].count('\n') + 1
            trigger = _find_trigger_context(source, m.start(), class_name)
            for target in targets:
                if not any(
                    e["to"] == target and abs(e["line"] - line) < 3
                    for e in all_edges + edges
                ):
                    edges.append({
                        "from": class_name,
                        "to": target,
                        "to_layout": _find_layout_for_class(target),
                        "type": "activity",
                        "via": f"startActivity [implicit:{action}]",
                        "trigger": trigger,
                        "line": line,
                    })

    return edges


_ADAPTER_RE = re.compile(
    r'(\w+Adapter)\s*\(\s*(?:this|activity|requireActivity)'
)
_ADAPTER_ASSIGN_RE = re.compile(
    r'(?:binding|findViewById)\.\w+\.adapter\s*=\s*(\w+Adapter)'
)


def _resolve_adapter_bindings(
    all_edges: list[dict],
    project_root: str | Path,
) -> list[dict]:
    """Bind Adapter edges to their host Activity/Fragment class.

    Scans for XxxAdapter(this, ...) constructors and binding.adapter = XxxAdapter(...)
    assignments, then copies the Adapter's out-edges to the host class.
    """
    adapter_to_host: dict[str, list[str]] = {}

    for src_file in android_project.source_files(project_root):
        source = src_file.read_text(encoding="utf-8", errors="ignore")
        class_name = src_file.stem

        for m in _ADAPTER_RE.finditer(source):
            adapter_class = m.group(1)
            adapter_to_host.setdefault(adapter_class, []).append(class_name)

        for m in _ADAPTER_ASSIGN_RE.finditer(source):
            adapter_class = m.group(1)
            adapter_to_host.setdefault(adapter_class, []).append(class_name)

    adapter_edges = {e["from"] for e in all_edges}
    edges = []

    for adapter_class, hosts in adapter_to_host.items():
        if adapter_class not in adapter_edges:
            continue
        adapter_out = [e for e in all_edges if e["from"] == adapter_class]
        for host in set(hosts):
            for ae in adapter_out:
                trigger = ae.get("trigger", "")
                if trigger.startswith("adapter:"):
                    continue
                new_trigger = f"adapter:{adapter_class}:{trigger}" if trigger else f"adapter:{adapter_class}"
                via = f"adapter:{adapter_class}"
                line = ae["line"]
                if not any(
                    e["to"] == ae["to"]
                    and e["trigger"] == new_trigger
                    and e["from"] == host
                    for e in all_edges + edges
                ):
                    edges.append({
                        "from": host,
                        "to": ae["to"],
                        "to_layout": ae.get("to_layout", ""),
                        "type": ae.get("type", "dialog"),
                        "via": via,
                        "trigger": new_trigger,
                        "line": line,
                    })

    return edges


# ── BottomNav / TabLayout edges ───────────────────────────────────────────────

_INFLATE_LAYOUT_RE = re.compile(r'R\.layout\.(\w+)')
_INFLATE_BINDING_RE = re.compile(r'(\w+Binding)(?:::|\.)\s*inflate')

_BOTTOMNAV_LISTENER_RE = re.compile(
    r'setOn(?:Navigation)?ItemSelectedListener|addOnTabSelectedListener'
)
_FRAGMENT_ADAPTER_OVERRIDE_RE = re.compile(
    r'override\s+fun\s+(?:createFragment|getItem)\s*\([^)]*\)[^{]*\{([^}]{0,2000})',
    re.S,
)
_POS_FRAGMENT_RE = re.compile(r'[-=]>\s*([A-Z]\w+Fragment)\s*\(')


def _load_string_resources(project_root: str | Path) -> dict[str, str]:
    """Load all @string values from res/values/strings*.xml files."""
    strings: dict[str, str] = {}
    for res_dir in android_project.res_dirs(project_root):
        values_dir = res_dir / "values"
        if not values_dir.is_dir():
            continue
        for strings_file in values_dir.glob("strings*.xml"):
            try:
                tree = ET.parse(strings_file)
                for elem in tree.iter("string"):
                    name = elem.get("name", "")
                    text = (elem.text or "").strip()
                    if name and text:
                        strings[name] = text
            except ET.ParseError:
                pass
    return strings


def _extract_menu_item_labels(project_root: str | Path) -> dict[str, str]:
    """
    Scan layout XMLs for BottomNavigationView ``app:menu`` references, then parse
    those menu XMLs to produce {menu_item_id: resolved_display_label}.
    """
    strings = _load_string_resources(project_root)
    menu_names: set[str] = set()

    # Find all app:menu="@menu/XXX" references across all layout files
    for res_dir in android_project.res_dirs(project_root):
        for layout_dir in res_dir.glob("layout*"):
            for xml_file in layout_dir.glob("*.xml"):
                try:
                    content = xml_file.read_text(encoding="utf-8", errors="ignore")
                    for m in re.finditer(r'app:menu="@menu/(\w+)"', content):
                        menu_names.add(m.group(1))
                except OSError:
                    pass

    labels: dict[str, str] = {}
    for res_dir in android_project.res_dirs(project_root):
        menu_dir = res_dir / "menu"
        if not menu_dir.is_dir():
            continue
        for menu_name in menu_names:
            menu_file = menu_dir / f"{menu_name}.xml"
            if not menu_file.exists():
                continue
            try:
                tree = ET.parse(menu_file)
                for item in tree.iter("item"):
                    raw_id = item.get(f"{android_project.ANDROID_NS}id", "")
                    item_id = raw_id.split("/")[-1]
                    title_raw = item.get(f"{android_project.ANDROID_NS}title", "")
                    if title_raw.startswith("@string/"):
                        title = strings.get(title_raw[8:], "")
                    else:
                        title = title_raw
                    if item_id and title:
                        labels[item_id] = title
            except ET.ParseError:
                pass

    return labels


def _extract_bottomnav_edges(project_root: str | Path) -> list[dict]:
    """
    Extract BottomNavigationView / TabLayout navigation edges (additive, zero hardcoding).

    Handles two patterns:
    • Pattern A – setOnItemSelectedListener with ``when(item.itemId)`` blocks mapping
      ``R.id.XXX`` to ``XxxFragment()`` within the same file.
    • Pattern B – FragmentStateAdapter / FragmentPagerAdapter ``createFragment`` /
      ``getItem`` overrides that return Fragment instances per position.

    Trigger format ``bottomnav:<menu_id>`` is intentionally opaque so the assembler
    treats this as a transparent hop and lets the Fragment's own screen label surface
    in exploration chains (avoids the "My Site > My Site" duplicate).
    """
    edges: list[dict] = []
    menu_labels = _extract_menu_item_labels(project_root)

    for src_file in android_project.source_files(project_root):
        source = src_file.read_text(encoding="utf-8", errors="ignore")
        class_name = src_file.stem

        # Pattern A: BottomNav / TabLayout item listener
        if _BOTTOMNAV_LISTENER_RE.search(source):
            for m in re.finditer(
                r'R\.id\.(\w+)[^\n]{0,500}?([A-Z]\w+Fragment)\s*\(',
                source,
                re.S,
            ):
                menu_id = m.group(1)
                fragment_class = m.group(2)
                if any(
                    e["from"] == class_name
                    and e["to"] == fragment_class
                    and e["type"] == "bottom_nav"
                    for e in edges
                ):
                    continue
                edges.append({
                    "from": class_name,
                    "to": fragment_class,
                    "to_layout": _find_layout_for_class(fragment_class),
                    "type": "bottom_nav",
                    "via": f"BottomNav:{menu_id}",
                    "trigger": f"bottomnav:{menu_id}",
                    "display_label": menu_labels.get(menu_id, ""),
                    "line": source[:m.start()].count("\n") + 1,
                    "user_visible": True,
                })

        # Pattern B: FragmentStateAdapter / FragmentPagerAdapter createFragment/getItem
        if re.search(r'FragmentState(?:Pager)?Adapter|FragmentPagerAdapter', source):
            for m in _FRAGMENT_ADAPTER_OVERRIDE_RE.finditer(source):
                body = m.group(1)
                for frag_class in _POS_FRAGMENT_RE.findall(body):
                    if any(
                        e["from"] == class_name
                        and e["to"] == frag_class
                        and e["type"] == "bottom_nav"
                        for e in edges
                    ):
                        continue
                    edges.append({
                        "from": class_name,
                        "to": frag_class,
                        "to_layout": _find_layout_for_class(frag_class),
                        "type": "bottom_nav",
                        "via": "ViewPager",
                        "trigger": f"bottomnav:{frag_class}",
                        "display_label": re.sub(
                            r'(?<=[a-z])(?=[A-Z])', ' ',
                            frag_class.replace("Fragment", ""),
                        ).strip(),
                        "line": source[:m.start()].count("\n") + 1,
                        "user_visible": True,
                    })

        # Pattern C: inner NavAdapter / fragment factory with enum → Fragment when blocks.
        # Catches e.g. WordPress's inner class NavAdapter.createFragment(PageType) {
        #   when (pageType) { MY_SITE -> MySiteFragment.newInstance() ... }
        # The file also contains BottomNav setup, so we know these fragments are tab content.
        if re.search(r'inner\s+class\s+\w*[Aa]dapter|fun\s+createFragment\s*\(', source):
            for m in re.finditer(
                r'when\s*\([^)]+\)\s*\{([^}]{0,2000})\}',
                source,
                re.S,
            ):
                body = m.group(1)
                for frag_class in re.findall(r'([A-Z]\w+Fragment)\.(?:newInstance|newInstance)\(', body):
                    if any(
                        e["from"] == class_name and e["to"] == frag_class
                        and e["type"] == "bottom_nav"
                        for e in edges
                    ):
                        continue
                    edges.append({
                        "from": class_name,
                        "to": frag_class,
                        "to_layout": _find_layout_for_class(frag_class),
                        "type": "bottom_nav",
                        "via": "NavAdapter",
                        "trigger": f"bottomnav:{frag_class}",
                        "display_label": re.sub(
                            r"(?<=[a-z])(?=[A-Z])", " ",
                            frag_class.replace("Fragment", ""),
                        ).strip(),
                        "line": source[:m.start()].count("\n") + 1,
                        "user_visible": True,
                    })
                # Also catch bare constructor calls: ReaderFragment(), else ReaderFragment(), etc.
                for frag_class in re.findall(r'(?:[-=]>|else)\s*([A-Z]\w+Fragment)\s*[\.(]', body):
                    if any(
                        e["from"] == class_name and e["to"] == frag_class
                        and e["type"] == "bottom_nav"
                        for e in edges
                    ):
                        continue
                    edges.append({
                        "from": class_name,
                        "to": frag_class,
                        "to_layout": _find_layout_for_class(frag_class),
                        "type": "bottom_nav",
                        "via": "NavAdapter",
                        "trigger": f"bottomnav:{frag_class}",
                        "display_label": re.sub(
                            r"(?<=[a-z])(?=[A-Z])", " ",
                            frag_class.replace("Fragment", ""),
                        ).strip(),
                        "line": source[:m.start()].count("\n") + 1,
                        "user_visible": True,
                    })

    return edges


# ── RecyclerView adapter → item layout edges ──────────────────────────────────

def _extract_adapter_item_layouts(project_root: str | Path) -> dict[str, list[str]]:
    """
    Two-stage scan for RecyclerView adapter → item layout mappings.

    Stage 1: Find every source file that is a ViewHolder or Adapter and collects
             layout names it inflates (R.layout.XXX or XxxBinding::inflate patterns).
    Stage 2: For each Adapter class, aggregate layouts from its own file plus all
             ViewHolder classes it instantiates in ``onCreateViewHolder``.

    Returns ``{adapter_class_name: [item_layout_name, ...]}``.
    """
    is_adapter_re = re.compile(
        r'RecyclerView\.Adapter|ListAdapter|PagingDataAdapter'
    )

    # Stage 1: collect layouts for every ViewHolder or Adapter file
    file_layouts: dict[str, list[str]] = {}  # {class_name: [layout_name, ...]}
    for src_file in android_project.source_files(project_root):
        class_name = src_file.stem
        is_vh = "ViewHolder" in class_name
        try:
            source = src_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not is_vh and not is_adapter_re.search(source):
            continue
        layouts: list[str] = []
        for m in _INFLATE_LAYOUT_RE.finditer(source):
            lname = m.group(1)
            if lname not in layouts:
                layouts.append(lname)
        for m in _INFLATE_BINDING_RE.finditer(source):
            lname = _binding_to_layout(m.group(1))
            if lname not in layouts:
                layouts.append(lname)
        if layouts:
            file_layouts[class_name] = layouts

    # Stage 2: for each Adapter, union its own layouts + referenced ViewHolder layouts
    adapter_layouts: dict[str, list[str]] = {}
    for src_file in android_project.source_files(project_root):
        class_name = src_file.stem
        try:
            source = src_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not is_adapter_re.search(source):
            continue
        layouts: list[str] = list(file_layouts.get(class_name, []))
        for vh_class, vh_layouts in file_layouts.items():
            if vh_class == class_name:
                continue
            if vh_class in source:
                for lname in vh_layouts:
                    if lname not in layouts:
                        layouts.append(lname)
        if layouts:
            adapter_layouts[class_name] = layouts

    return adapter_layouts


def _link_fragment_adapters(
    project_root: str | Path,
    adapter_item_map: dict[str, list[str]],
) -> list[dict]:
    """
    Find which Fragment/Activity instantiates each Adapter, then emit
    ``recycler_item`` edges  (host_class → item_layout_name).

    Trigger ``recycler_item:<layout>`` is opaque so the assembler treats the hop
    transparently: item layout elements surface directly under the host screen.
    """
    if not adapter_item_map:
        return []

    edges: list[dict] = []
    for src_file in android_project.source_files(project_root):
        class_name = src_file.stem
        try:
            source = src_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for adapter_class, item_layouts in adapter_item_map.items():
            if adapter_class == class_name:
                continue
            if adapter_class not in source:
                continue
            # Require an actual instantiation or assignment, not just an import
            if not re.search(rf'\b{re.escape(adapter_class)}\s*[=(]', source):
                continue
            for item_layout in item_layouts:
                if any(
                    e["from"] == class_name and e["to"] == item_layout
                    for e in edges
                ):
                    continue
                edges.append({
                    "from": class_name,
                    "to": item_layout,
                    "to_layout": item_layout,
                    "type": "recycler_item",
                    "via": f"adapter:{adapter_class}",
                    "trigger": f"recycler_item:{item_layout}",
                    "line": 0,
                    "user_visible": True,
                })
    return edges


# ── TaskStackBuilder edges ────────────────────────────────────────────────────

def _extract_taskstack_edges(project_root: str | Path) -> list[dict]:
    """
    Extract TaskStackBuilder.addNextIntent chains.

    Handles both Kotlin (``XxxActivity::class.java``) and Java
    (``new Intent(ctx, XxxActivity.class)``) syntax.
    """
    edges: list[dict] = []
    for src_file in android_project.source_files(project_root):
        try:
            source = src_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "TaskStackBuilder" not in source:
            continue
        class_name = src_file.stem
        # Kotlin: addNextIntent(Intent(ctx, XxxActivity::class.java))
        for m in re.finditer(
            r'addNextIntent\s*\([^)]*?Intent\([^,)]*,\s*(\w+)::class\.java',
            source,
        ):
            target = m.group(1)
            line = source[:m.start()].count("\n") + 1
            trigger = _find_trigger_context(source, m.start(), class_name)
            edges.append({
                "from": class_name,
                "to": target,
                "to_layout": _find_layout_for_class(target),
                "type": "task_stack",
                "via": "TaskStackBuilder.addNextIntent",
                "trigger": trigger,
                "line": line,
            })
        # Java: addNextIntent(new Intent(context, XxxActivity.class))
        for m in re.finditer(
            r'addNextIntent\s*\(\s*new\s+Intent\([^,)]*,\s*(\w+)\.class\)',
            source,
        ):
            target = m.group(1)
            line = source[:m.start()].count("\n") + 1
            trigger = _find_trigger_context(source, m.start(), class_name)
            edges.append({
                "from": class_name,
                "to": target,
                "to_layout": _find_layout_for_class(target),
                "type": "task_stack",
                "via": "TaskStackBuilder.addNextIntent",
                "trigger": trigger,
                "line": line,
            })
    return edges


def _extract_proxy_call_edges(
    all_edges: list[dict],
    class_files: dict,  # class_name → Path
) -> list[dict]:
    """
    Detect proxy launcher/navigator classes and emit caller→target edges.

    A *proxy* class is one that:
      • appears in all_edges as "from" (has outgoing navigation edges)
      • has 0 incoming edges in all_edges
      • does not end with Activity / Fragment / Dialog (is not itself a screen)

    Examples that are auto-detected without hardcoding:
      ActivityLauncher, ActivityNavigator, ReaderActivityLauncher, EditorLauncher …

    Algorithm:
      Phase A — collect proxy classes from current edge set
      Phase B — build {proxy_class: {method_name: [target_class]}} from their edges
                 (trigger field already encodes "fn: <method_name>")
      Phase C — scan every source file; for each call site
                   ProxyClass.method(...)      (static Java/Kotlin call)
                   proxyVar.method(...)        (injected instance; type resolved via field decl)
                 emit edge  caller → target
    """
    if not all_edges or not class_files:
        return []

    # Phase A: identify proxy classes
    incoming_classes: set[str] = {e.get("to", "") for e in all_edges}
    _SCREEN_SUFFIXES = ("Activity", "Fragment", "Dialog", "BottomSheet")

    proxy_out_edges: dict[str, list[dict]] = {}
    for e in all_edges:
        src = e.get("from", "")
        if not src:
            continue
        if src in incoming_classes:
            continue
        if any(src.endswith(s) for s in _SCREEN_SUFFIXES):
            continue
        if src == "EXTERNAL":
            continue
        proxy_out_edges.setdefault(src, []).append(e)

    # Only keep proxies that navigate to at least one real class name (starts uppercase).
    # This filters out utility helpers (WPPermissionUtils, ZendeskHelper) whose edge
    # targets are method names like "showPermissionAlwaysDeniedDialog" — not classes.
    proxy_out_edges = {
        src: edges
        for src, edges in proxy_out_edges.items()
        if any(e.get("to", "")[:1].isupper() for e in edges)
    }

    if not proxy_out_edges:
        return []

    # Phase B: build {proxy_class: {method_name: [target]}} index
    # Extract method name from trigger "fn: someMethod" or "setup: SomeMethod"
    _FN_TRIGGER_RE = re.compile(r'(?:fn:|setup:)\s*(\w+)', re.IGNORECASE)

    proxy_method_targets: dict[str, dict[str, list[str]]] = {}
    for proxy_cls, edges in proxy_out_edges.items():
        method_map: dict[str, list[str]] = {}
        for e in edges:
            trigger = e.get("trigger", "")
            target = e.get("to", "")
            if not target:
                continue
            fn_m = _FN_TRIGGER_RE.match(trigger)
            if fn_m:
                method_name = fn_m.group(1)
                if target not in method_map.setdefault(method_name, []):
                    method_map[method_name].append(target)

        # Fallback: scan the proxy's own source file for method bodies
        # that call startActivity with Kotlin or Java Intent syntax.
        src_file = class_files.get(proxy_cls)
        if src_file:
            try:
                proxy_src = src_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                proxy_src = ""
            if proxy_src:
                _METHOD_DECL_RE = re.compile(
                    r'(?:public\s+static\s+\w+|fun)\s+(\w+)\s*\('
                )
                # Find every method in the proxy file and collect its Intent targets
                for mdecl in _METHOD_DECL_RE.finditer(proxy_src):
                    mname = mdecl.group(1)
                    body = _find_function_body(proxy_src, mname, max_len=4000)
                    if not body:
                        continue
                    # Kotlin ::class.java
                    for tm in re.finditer(
                        r'Intent\s*\([^,)]+,\s*(\w+)::class\.java', body
                    ):
                        tgt = tm.group(1)
                        if tgt not in method_map.setdefault(mname, []):
                            method_map[mname].append(tgt)
                    # Java .class
                    for tm in re.finditer(
                        r'new\s+Intent\s*\([^,)]+,\s*(\w+)\.class\s*\)', body
                    ):
                        tgt = tm.group(1)
                        if tgt not in method_map.setdefault(mname, []):
                            method_map[mname].append(tgt)
                    # createIntent factory pattern
                    for tm in re.finditer(
                        r'startActivity\s*\([^)]*?(\w+)\.createIntent\s*\(', body
                    ):
                        tgt = tm.group(1)
                        if tgt not in method_map.setdefault(mname, []):
                            method_map[mname].append(tgt)

        if method_map:
            proxy_method_targets[proxy_cls] = method_map

    if not proxy_method_targets:
        return []

    # Phase C: scan all source files for call sites
    new_edges: list[dict] = []
    existing_key: set[tuple] = {
        (e.get("from", ""), e.get("to", ""), e.get("trigger", ""), e.get("line", 0))
        for e in all_edges
    }

    # Regex to find instance-field declarations typed as a proxy class:
    #   lateinit var navigator: ActivityNavigator
    #   val activityNavigator: ActivityNavigator
    #   @Inject lateinit var launcher: ActivityLauncher
    _FIELD_TYPE_RE = re.compile(
        r'(?:@\w+\s+)*(?:lateinit\s+)?(?:val|var)\s+(\w+)\s*:\s*(\w+)'
    )

    for caller_class, src_file in class_files.items():
        # Skip proxy classes themselves to avoid self-loop noise
        if caller_class in proxy_method_targets:
            continue
        try:
            source = src_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        # Build var_name → proxy_class map for this file
        var_to_proxy: dict[str, str] = {}
        for fm in _FIELD_TYPE_RE.finditer(source):
            var_name = fm.group(1)
            type_name = fm.group(2)
            if type_name in proxy_method_targets:
                var_to_proxy[var_name] = type_name

        # Scan for call sites: Receiver.method(  where Receiver is proxy or var of proxy type
        for cm in re.finditer(r'\b(\w+)\s*\.\s*(\w+)\s*\(', source):
            receiver = cm.group(1)
            method = cm.group(2)

            # Determine proxy class
            if receiver in proxy_method_targets:
                proxy_cls = receiver          # static: ActivityLauncher.foo(...)
            elif receiver in var_to_proxy:
                proxy_cls = var_to_proxy[receiver]  # instance: navigator.foo(...)
            else:
                continue

            targets = proxy_method_targets[proxy_cls].get(method, [])
            if not targets:
                continue

            line = source[:cm.start()].count('\n') + 1
            trigger = _find_trigger_context(source, cm.start(), caller_class)

            for target in targets:
                key = (caller_class, target, trigger, line)
                if key in existing_key:
                    continue
                existing_key.add(key)
                new_edges.append({
                    "from": caller_class,
                    "to": target,
                    "to_layout": _find_layout_for_class(target),
                    "type": "activity",
                    "via": f"proxy:{proxy_cls}.{method}()",
                    "trigger": trigger,
                    "line": line,
                })

    return new_edges


def run(project_root: str, dep_roots: list[str] | None = None) -> dict:
    global _INFERRED_LAYOUTS, _ALL_KNOWN_LAYOUTS

    root = Path(project_root)
    src_files = android_project.source_files(root)
    if not src_files:
        return {"nodes": {}, "edges": [], "stats": {"total_nodes": 0, "total_edges": 0}}

    _INFERRED_LAYOUTS = _scan_class_layouts(root)

    _ALL_KNOWN_LAYOUTS = _scan_all_layouts(project_root)
    for dep in (dep_roots or []):
        _ALL_KNOWN_LAYOUTS.update(_scan_all_layouts(dep))

    for dep in (dep_roots or []):
        _INFERRED_LAYOUTS.update(_scan_class_layouts(dep))

    all_edges = []
    class_files = {}

    for src_file in src_files:
        source = src_file.read_text(encoding="utf-8", errors="ignore")
        class_name = _extract_class_name(src_file)
        # Skip Kotlin/Java synthetic anonymous-inner-class files (contain $).
        # These are compiler-generated lambda/inner-class artefacts, not real screens.
        if "$" in class_name:
            continue
        class_files[class_name] = src_file
        edges = _extract_edges_from_file(src_file, source)
        all_edges.extend(edges)

    if ast_index is not None:
        try:
            ast_nav = ast_index.build_project_index(
                project_root,
                layout_resolver=_find_layout_for_class,
            )
            all_edges.extend(ast_nav.navigation_edges)
            for dep in dep_roots or []:
                dep_name = Path(dep).name
                dep_nav = ast_index.build_project_index(
                    dep,
                    file_prefix=dep_name,
                    layout_resolver=_find_layout_for_class,
                )
                all_edges.extend(dep_nav.navigation_edges)
        except Exception:
            pass

    # --- Fix Point 6: Proxy launcher / navigator call-site tracing ---
    all_edges.extend(_extract_proxy_call_edges(all_edges, class_files))

    # --- L2: generic createIntent / local Intent variable (nav_pipeline) ---
    from extractors import nav_pipeline

    _rules = nav_pipeline.load_nav_rules()
    for _kt_path, source, class_name, rel in nav_pipeline.gather_kt_sources(
        project_root, dep_roots
    ):
        all_edges.extend(
            nav_pipeline.extract_l2_create_intent_edges(
                source, class_name, rel, _find_layout_for_class
            )
        )
        all_edges.extend(
            nav_pipeline.extract_l2_variable_intent_edges(
                source, class_name, rel, _find_layout_for_class
            )
        )

    # --- Fix Point 2: AndroidManifest implicit Intent resolution ---
    action_map = _parse_manifest_intents(project_root)
    implicit_edges = _resolve_implicit_intents(all_edges, root, action_map)
    all_edges.extend(implicit_edges)

    # --- Fix Point 1: Adapter-Host Activity binding (regex fallback) ---
    adapter_edges = _resolve_adapter_bindings(all_edges, root)
    all_edges.extend(adapter_edges)

    # --- Fix Point 3: BottomNav / TabLayout edges (additive) ---
    all_edges.extend(_extract_bottomnav_edges(root))

    # --- Fix Point 4: RecyclerView adapter → item layout edges (additive) ---
    _adapter_item_map = _extract_adapter_item_layouts(root)
    all_edges.extend(_link_fragment_adapters(root, _adapter_item_map))

    # --- Fix Point 5: TaskStackBuilder edges (additive) ---
    all_edges.extend(_extract_taskstack_edges(root))

    # --- Bytecode analysis (if available) ---
    try:
        from extractors.bytecode_navigation import extract_edges_from_classes, find_class_dir
        bc_dir = find_class_dir(project_root)
        if bc_dir:
            bc_edges = extract_edges_from_classes(bc_dir)
            for be in bc_edges:
                key = (be["from"], be["to"], be.get("trigger", ""))
                if not any((e["from"], e["to"], e.get("trigger", "")) == key for e in all_edges):
                    all_edges.append(be)
    except Exception:
        pass

    # --- L3: per-repo overlay ---
    all_edges = nav_pipeline.merge_overlay_edges(
        all_edges,
        nav_pipeline.load_navigation_overlay(project_root, _rules),
        _find_layout_for_class,
    )

    unique_edges = nav_pipeline.dedupe_edges(all_edges)

    # --- Launcher anchor: *Handler / *Delegate → parallel edges from MAIN/LAUNCHER ---
    _launcher = get_launcher_activity_class(project_root)
    _suffixes = _rules.get("handler_anchor_suffixes")
    unique_edges = nav_pipeline.apply_launcher_anchor_edges(
        unique_edges, _launcher, _suffixes
    )
    unique_edges = nav_pipeline.dedupe_edges(unique_edges)

    # Build nodes
    all_class_names = set()
    for e in unique_edges:
        all_class_names.add(e["from"])
        all_class_names.add(e["to"])

    nodes = {}
    for cn in sorted(all_class_names):
        if cn == "EXTERNAL":
            nodes[cn] = {
                "name": "EXTERNAL",
                "layout": "",
                "type": "external",
                "navigates_to": list(OrderedDict.fromkeys(e["to"] for e in unique_edges if e["from"] == cn)),
                "navigated_from": [],
                "edges_out": sum(1 for e in unique_edges if e["from"] == cn),
                "edges_in": 0,
            }
            continue
        edges_from = [e for e in unique_edges if e["from"] == cn]
        edges_to = [e for e in unique_edges if e["to"] == cn]
        nodes[cn] = _build_node(cn, edges_from, edges_to)

    # Backfill to_layout for edges missing it (e.g. bytecode edges)
    for e in unique_edges:
        if not e.get("to_layout"):
            target = e["to"]
            if target in nodes:
                e["to_layout"] = nodes[target].get("layout", "")
            else:
                e["to_layout"] = _find_layout_for_class(target)

    # Build class_layouts from nodes (covers Compose screens without XML)
    class_layouts = dict(_INFERRED_LAYOUTS)
    for cn, node in nodes.items():
        layout = node.get("layout", "")
        if layout and cn not in class_layouts:
            class_layouts[cn] = layout

    stats = {
        "total_nodes": len(nodes),
        "total_edges": len(unique_edges),
        "activity_nodes": sum(1 for n in nodes.values() if n["type"] == "activity"),
        "dialog_nodes": sum(1 for n in nodes.values() if n["type"] == "dialog"),
        "external_nodes": sum(1 for n in nodes.values() if n["type"] == "external"),
        "by_type": {},
    }
    for e in unique_edges:
        t = e["type"]
        stats["by_type"][t] = stats["by_type"].get(t, 0) + 1

    return {
        "nodes": nodes,
        "edges": unique_edges,
        "stats": stats,
        "class_layouts": class_layouts,
        "meta": {
            "nav_pipeline_version": nav_pipeline.NAV_PIPELINE_VERSION,
            "nav_rules_version": str(_rules.get("nav_rules_version", "0")),
            "launcher_activity": _launcher,
            "navigation_candidates_output": "navigation_candidates.json",
        },
    }
