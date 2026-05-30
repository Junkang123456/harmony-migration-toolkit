# bundled_spec_tools 架构文档

## 1. 项目定位

`bundled_spec_tools` 是一个**纯静态分析工具**，输入一个 Android 项目的源码目录，输出结构化 JSON，描述该 App 的全部 UI 结构、导航关系、控件行为和生命周期。这些 JSON 文件（称为 **spec**）供下游 stage 消费，最终目标是将 Android App 自动翻译为 HarmonyOS（鸿蒙）应用。

**不做的事**：不编译 Android 代码、不运行 App、不生成 HarmonyOS 代码。它只负责"理解"Android App 的 UI 层。

---

## 2. 运行方式

```bash
python bundled_spec_tools/main.py <android_project_root> [--out <dir>] [--validate]
```

- `<android_project_root>`：Android 工程根目录（含 `src/main/`、`AndroidManifest.xml`）
- `--out`：输出目录，默认 `bundled_spec_tools/output/`
- `--validate`：额外执行字节码/Manifest/Layout 交叉验证

运行时间取决于项目规模，AntennaPod（534 个源文件）约 8-10 分钟，主要耗时在 tree-sitter AST 解析。

---

## 3. 流水线架构

`main.py` 按 7 个阶段依次执行，每个阶段输出独立的 JSON 文件：

```
[1/7] XML 静态提取          → static_xml.json
[2/7] 源码扫描 + 函数图谱    → source_findings.json, function_symbols.json, call_graph.json
[3/7] Ground Truth 合并      → ground_truth.json
[4/7] 导航图提取            → navigation_graph.json, navigation_candidates.json
  [4b] Fragment 检测        → fragments.json
  [4c] 动态 UI 检测         → dynamic_ui.json
  [4d] 行为链提取           → behavior_chains.json
[5/7] Gap 合并（可选）      → gap_analysis.json
[6/7] UI DAG 组装           → ui_dag.json, ui_paths.json, ui_effect_paths.json, ...
[7/7] Spec 生成             → specs/*.json, app_model/
```

### 依赖关系图

```
xml_extractor ─────┐
                   ├─→ ground_truth_builder ─┐
source_extractor ──┤                         │
                   │                         ├─→ generate_specs ─→ specs/*.json
function_graph ────┤                         │
                   ├─→ navigation_extractor ──┤
                   │                         │
                   ├─→ fragment_detector ─────┤
                   │                         │
                   ├─→ dynamic_ui_extractor ──┤
                   │                         │
                   └─→ behavior_chain_extractor ─┘
                                             │
                                  ui_dag_assembler ─→ ui_paths
                                             │
                                  app_model_builder ─→ app_model/
```

---

## 4. 模块详解

### 4.1 XML 静态提取 — `extractors/xml_extractor.py`

**输入**：`res/layout/*.xml`, `res/menu/*.xml`, `res/navigation/*.xml`

**输出**：`static_xml.json`

功能：
- 解析所有 XML 布局文件，提取每个控件的 `id`、`tag`（类型名）、`text`、`hint`、`contentDescription`
- 判断控件是否可交互（Button/EditText/SeekBar 等天然可交互；TextView/ImageView 仅在有 `clickable=true` 或 `onClick` 时才算）
- 检测 `visibility="gone"/"invisible"` 的初始隐藏控件
- 提取 `onClick` XML 属性绑定
- **构建 `layout_trees`**：每个布局文件的完整控件树（保留嵌套层级），供 v2 spec 的 `L0_structure.ui_tree` 使用
- 合并字符串资源 `res/values/strings.xml` 到 `strings` 字段

**关键数据结构**：
```json
{
  "elements": [
    {"id": "btnSave", "tag": "Button", "layout": "activity_main", "text": "Save",
     "is_interactive": true, "source": "xml_layout", ...}
  ],
  "layout_trees": {
    "activity_main": {"tag": "LinearLayout", "id": "", "children": [...]}
  },
  "strings": {"app_name": "MyApp", ...},
  "stats": {"total": 525, "interactive": 182, "hidden_by_default": 30}
}
```

### 4.2 源码扫描 — `extractors/source_extractor.py`

**输入**：`src/main/**/*.java`, `src/main/**/*.kt`

**输出**：`source_findings.json`

功能（5 个核心语义模式）：
1. **R.id 分发块**（`id_dispatchers`）：when/switch 中 `R.id.xxx → handler` 的分发映射
2. **事件注册**（`event_registrations`）：`setOnClickListener`、`addTextChangedListener` 等监听器注册，提取 `view_ref`（如 `binding.btnSave`）、`event_type`（click/touch/text_change 等）、`method`、`enclosing_fn`
3. **可见性控制**（`visibility_controls`）：`setVisibility(View.GONE)`、`isVisible = false` 等
4. **布局膨胀**（`inflates`）：`inflate(R.layout.xxx)` 和 `XxxBinding.inflate()`
5. **数据驱动 UI**（`data_driven_ui`）：构建选项列表后传入弹窗/列表的静态文本

误报抑制：
- PascalCase 的 `view_ref` 被视为类型名（非实例变量），自动过滤
- `decorView`、`contentResolver` 等已知系统对象的事件注册被排除
- 第三方库目录不扫描事件注册

**同时生成 `view_ref_id_map`**：per-file 的 `{变量名 → R.id.xxx}` 映射（来自 `findViewById` 调用），供 ground_truth_builder 精确匹配。

### 4.3 函数图谱 — `extractors/function_graph_extractor.py`

**输入**：Kotlin/Java 源码

**输出**：`function_symbols.json`, `call_graph.json`

功能：
- 提取所有函数符号（`symbol_id` 格式：`fn:package.Class.method/arity`）
- 构建调用图（caller → callee 关系），用于 behavior_chain_extractor 追踪调用链
- 优先使用 tree-sitter AST，回退到正则

**数据结构**：
```json
// call_graph.json
{
  "symbols": [{"symbol_id": "fn:...", "function_name": "onClick", "file": "...", ...}],
  "calls": [{"from_symbol_id": "fn:A.onClick/1", "to_symbol_id": "fn:B.navigate/0", ...}],
  "unresolved_calls": [...]
}
```

### 4.4 AST 索引 — `extractors/ast_index.py`

**依赖**：`tree_sitter_language_pack`（可选，缺失时各模块回退到正则）

功能：
- 统一的 tree-sitter Kotlin/Java 解析入口
- `build_class_hierarchy(project_root)`：构建继承链映射（`Class → 父类`）
- `lookup_class(name)`：查找类的完整信息（文件路径、继承链、是否 Fragment/Activity/Dialog）
- `_resolve_android_base(class_name)`：判断一个类是 Activity/Fragment/Dialog/Service/其他
- 提供 `_walk`、`_node_text`、`_line`、`_source_files` 等低级 AST 遍历工具

被 `fragment_detector`、`navigation_extractor`、`source_extractor`、`function_graph_extractor`、`dynamic_ui_extractor` 共同依赖。

### 4.5 Ground Truth 合并 — `extractors/ground_truth_builder.py`

**输入**：`static_xml.json` + `source_findings.json`

**输出**：`ground_truth.json`

功能：将 XML 控件与源码行为绑定在一起：
1. `event_registrations` 的 `view_ref` 对齐 XML `id`（支持 ViewBinding camelCase → snake_case 转换）
2. `id_dispatchers` 直接对齐 XML `id`
3. `visibility_controls` 标记条件可见性
4. 未在 XML 中出现的 `inflates` 生成 `dynamic_gap`（动态布局）
5. `data_driven_ui` 生成数据驱动 gap

**关键统计**（AntennaPod 示例）：
- XML 元素 525 个，其中 239 个绑定了行为，97 个条件可见
- 动态 gap 80 个（含 inflate_layout、inflate_binding、data_driven_ui）
- 未匹配 51 个

### 4.6 导航图提取 — `extractors/navigation_extractor.py`

**输入**：源码 + AndroidManifest.xml + （可选）字节码

**输出**：`navigation_graph.json`, `navigation_candidates.json`

检测的导航模式：
- `startActivity(Intent(this, XxxActivity::class.java))` → Activity 跳转
- `XxxDialog().show(...)` → Dialog 弹出
- `intent-filter` → 外部入口
- `finish()` / `onBackPressed()` → 返回
- 隐式 Intent 解析
- Adapter → Host 绑定
- Fragment 导航（通过 NavController/FragmentTransaction）

**数据结构**：
```json
{
  "nodes": {
    "MainActivity": {"type": "activity", "layout": "main", ...},
    "SettingsDialog": {"type": "dialog", "layout": "settings_dialog", ...}
  },
  "edges": [
    {"from": "MainActivity", "to": "SettingsDialog", "trigger": "menu settings",
     "type": "dialog", "via": "Dialog()"}
  ],
  "class_layouts": {"MainActivity": "main", ...}
}
```

辅助模块 `nav_pipeline.py` 提供三层导航增强：
- L1：navigation_candidates（原始事实 + 非导航效果）
- L2：将通用 Kotlin/Java 模式提升为边（createIntent 工厂、本地 Intent 变量）
- L3：可选 per-repo overlay JSON

### 4.7 Fragment 检测 — `extractors/fragment_detector.py`

**输入**：源码 + XML 布局

**输出**：`fragments.json`

检测 9 种 Fragment 挂载模式：
1. **FragmentTransaction.replace/add**（正则）
2. **ViewPager Adapter**（正则 → getItem 中的 Fragment 实例化）
3. **XML `<fragment>` 标签**
4. **AST 类声明**（tree-sitter 遍历所有 Fragment 子类）
5. **AST FragmentTransaction**（AST 精确解析 .replace/.add 调用参数）
6. **AST loadFragment/showFragment**（自定义 Fragment 加载方法）
7. **AST .show() 调用**（DialogFragment 展示）
8. **AST switch-case 工厂**（工厂方法中 when/switch 返回不同 Fragment）
9. **Fragment 实例化上下文**（new XxxFragment() 所在方法的宿主类）

参数解析通过 `_resolve_fragment_arg()` 追踪数据流：直接类名 → `new Xxx()` → `Xxx.newInstance()` → 变量赋值 → 方法返回值。

**覆盖率**（AntennaPod）：84 个声明的 Fragment 中 79 个找到了宿主（94%）。

### 4.8 动态 UI 检测 — `extractors/dynamic_ui_extractor.py`

**输入**：源码

**输出**：`dynamic_ui.json`

检测 3 种动态 UI 创建模式：
1. **addView**：`container.addView(new XxxView(ctx))` — 程序化创建控件
2. **inflate + addView**：先 inflate 布局再 addView — 动态插入布局片段
3. **setAdapter**：RecyclerView/ListView 的 Adapter 关联，追踪到 item 布局

**输出**：
```json
{
  "dynamic_elements": [...],    // addView 创建的控件（v2 L0_structure.dynamic_elements）
  "adapter_layouts": [          // Adapter 绑定关系（v2 L1_behavior.adapter_bindings）
    {"adapter_class": "QueueRecyclerAdapter", "item_layout": "feeditemlist_item",
     "host_class": "QueueFragment", "host_id": "recyclerView"}
  ]
}
```

> **注意**：v1 spec 的 `dynamic_ui` 字段来自 ground_truth_builder 的 `dynamic_gap`（inflate 模式），与此处的 `dynamic_elements`（addView 模式）是**互补关系**，不是重复。

### 4.9 行为链提取 — `extractors/behavior_chain_extractor.py`

**输入**：`source_findings.json` + `call_graph.json` + XML id 集合

**输出**：`behavior_chains.json`

三层架构：

**Layer 1 — Step Pipeline**（纯函数，无 Android 假设）：
- `classify_call(method_name)` → 分类为 `navigate` / `ui_feedback` / `ui_update` / `async` / `call`
- `build_step(call, call_graph, depth)` → 递归构建调用链步骤
- `follow_calls(body, call_graph, max_depth=3)` → 遍历调用图

**Layer 2 — Body Parser**（文本处理）：
- `extract_handler_body(source_text, method_start_line)` → 提取方法体，支持三种形式：
  - 花括号块 `{ ... }`（匿名类、多行 lambda）
  - 箭头 lambda `v -> expression()`（无花括号单行 lambda）
  - 方法引用 `this::onClick` 回退
- `split_conditions(body)` → 检测 if/when/switch 分支
- `extract_braced_block(text, pos)` → 大括号匹配

**Layer 3 — Specialized Extractors**：
- `extract_event_chains(event_registrations, call_graph, project_root, xml_ids)` → 为每个事件注册构建完整的 event → handler → effect 链
- `extract_lifecycle_hooks(symbols)` → 提取生命周期方法调用（callee 自动去重）

**element_id 解析**（ViewBinding 支持）：
- `_build_viewref_to_id(source)` → 从 `val x = findViewById(R.id.y)` 和 `x = findViewById(R.id.y)` 构建映射（同时支持 Kotlin 和 Java 风格）
- `resolve_element_id(view_ref, viewref_map, xml_ids)` 支持三种解析路径：
  1. `findViewById` 显式映射（`viewref_map`）— 覆盖混合大小写 id（如 `widget_opacity_seekBar`）
  2. ViewBinding camelCase → snake_case 转换，再与已知 XML id 集合匹配
  3. 直接匹配

覆盖率（AntennaPod）：87% 的 event_bindings 成功解析到 element_id。

**effect_chain 结构**：
```json
{
  "element_id": "butSave",
  "event_type": "click",
  "handler": {"method": "onCreate", "file": "...", "line": 42},
  "effect_chain": [
    {"step": "navigate", "target": "finish", "destination": ""},
    {"step": "ui_update", "action": "setVisibility", "value": "gone"},
    {"step": "condition", "expr": "isValid()", "then": [...], "else": [...]}
  ],
  "chain_depth": 3
}
```

步骤类型：
| step | 含义 | 关键字段 |
|------|------|---------|
| `navigate` | 页面跳转/返回 | `target`（startActivity/finish）, `destination` |
| `ui_feedback` | 用户反馈 | `action`（Toast/Snackbar/dismiss） |
| `ui_update` | UI 状态更新 | `action`（setVisibility/setText）, `value` |
| `async` | 异步操作 | `target`（launch/enqueue） |
| `call` | 普通方法调用 | `target`, `symbol_id`, `nested` |
| `condition` | 条件分支 | `expr`, `then`, `else` |

### 4.10 共享工具 — `extractors/view_ref_utils.py`

被 `ground_truth_builder` 和 `behavior_chain_extractor` 共同使用的 view 引用解析工具：

- `camel_to_snake(name)` — ViewBinding 命名转换：`drawerLayout` → `drawer_layout`
- `clean_view_ref(ref)` — 去除 `binding.`/`viewBinding.` 等前缀
- `resolve_view_id(raw_ref, known_ids, file_ref_map)` — 多策略解析 view 引用到 XML id

### 4.11 Android 项目工具 — `extractors/android_project.py`

提供项目结构探测的基础函数：

- `source_files(project_root)` — 收集 `src/main/**/*.java` 和 `*.kt`
- `source_dirs(project_root)` — 返回源码根目录
- `res_dirs(project_root)` — 返回资源目录列表
- `manifests(project_root)` — 返回 AndroidManifest.xml 路径列表
- `_is_ignored(path)` — 跳过 `.git`、`build`、`.gradle` 等目录

### 4.12 依赖解析 — `extractors/dependency_resolver.py`

自动下载 GitHub 依赖库源码：

- 解析 `libs.versions.toml` 中的 JitPack 依赖（`com.github.Xxx:Yyy:commit`）
- 解析 `settings.gradle.kts` 中的 `includeBuild` 本地路径
- 下载到本地缓存 `~/.cache/harmony-migration/deps/`
- 返回额外的源码根目录列表，供各提取器扫描

### 4.13 字节码导航 — `extractors/bytecode_navigation.py`

从编译后的 `.class` 文件提取导航关系（补充源码分析）：

- 分析 JVM 常量池中的 `new Intent(ctx, XxxActivity.class)` → `startActivity`
- 检测 `new XxxDialog()` 创建
- 识别 Adapter 字段赋值
- 需要项目已编译（`build/intermediates/javac/`）

辅助模块 `extractors/class_parser.py` 提供纯 Python 的 `.class` 文件解析，无外部依赖。

### 4.14 UI DAG 组装 — `extractors/ui_dag_assembler.py`

**输入**：ground_truth + navigation_graph

**输出**：`ui_dag.json`

从 launcher Activity 出发，沿导航边递归展开，构建完整的 UI 导航有向无环图（DAG）。每个节点包含该屏幕的控件列表和导航关系。`max_depth=8` 防止无限递归。

同时生成扁平化路径 `ui_paths.json`：每条路径是从 launcher 到叶子屏幕的一条导航链。

### 4.15 导航路径枚举 — `extractors/ui_paths_nav_enumerator.py`

从 `navigation_graph.json` 枚举短的导航链，生成人类可读的路径字符串：

```
Main > Settings Activity > Download Preferences
Main > Nav Drawer > Subscription
```

输出 `ui_paths_enumerated.json`，上限 800 条路径。

### 4.16 App Model 构建 — `extractors/app_model_builder.py`

**输出**：`app_model/` 目录

构建分层的 App 结构模型：
- `index.json` — 总索引（版本、屏幕列表、Feature 列表、路径总数）
- `screens/<layout>.json` — 每屏幕详情
- `features/ft_<class>.json` — 每 Feature 详情（由类名推导）
- `paths/all_paths.json` — 全部导航路径
- `references/nav_edges.json` — 导航边索引
- `references/ui_point_index.json` — 控件 ID 索引

### 4.17 验证模块 — `verification/`

可选（`--validate` 启用），交叉验证提取结果：

- `manifest_verifier.py` — 检查提取到的 Activity 是否在 AndroidManifest.xml 中声明
- `layout_verifier.py` — 检查 XML 中引用的 Fragment 是否存在对应的类
- `bytecode_verifier.py` — 从字节码继承链验证 Fragment/Activity 分类是否正确
- `report.py` — 汇总验证结果为 `verification_report.json`

---

## 5. Spec 生成 — `generate_specs.py`

### 5.1 核心逻辑

为每个布局文件生成一个 `{layout_name}_spec.json`，逻辑：

1. 从 `ground_truth.static_elements` 按 layout 分组
2. 从 `navigation_graph.class_layouts` 反查 layout → class 映射
3. 查找该 class 的导航出边和入边
4. 按 class_name 过滤 v1 behaviors（防止同名 id 跨文件污染）
5. 合并 fragments、dynamic_elements、behavior_chains、lifecycle_hooks、adapter_layouts
6. 写入 spec 文件

### 5.2 布局去重

`_dedupe_layout_variants()` 处理 snake_case 转换差异（如 `media3_video_player_activity` vs `media3video_player_activity`），按 underscore-collapsed 归一化后保留真实 XML 中存在的那个。

### 5.3 effect_summary

`_summarize_effects()` 将嵌套的 `effect_chain` 树扁平化为翻译相关的标签数组：

```json
["navigate:finish", "ui_feedback:Toast", "ui_update:setVisibility"]
```

只提取 `navigate`、`ui_feedback`、`ui_update`、`async` 四类，跳过通用 `call` 和 `condition`。

### 5.4 Spec Schema（v2.1）

渐进式披露设计：`brief` 供 LLM 快速了解页面，`L0/L1` 供深入翻译时使用。

```json
{
  "spec_version": "2.1",
  "class": "MainActivity",
  "layout": "activity_main",
  "screen_type": "activity|fragment|dialog|adapter_item|unknown",
  "source": "project|library",

  // ── Layer 1 摘要（LLM 优先读取） ──
  "brief": {
    "interactive_controls": [
      {"id": "btnSave", "type": "Button", "label": "Save",
       "actions": ["navigate:finish", "ui_feedback:Toast"]}
    ],
    "nav_in": ["SplashActivity (activity)"],
    "nav_out": ["→ SettingsActivity (menu settings)"],
    "has_fragments": true,
    "has_adapters": false,
    "lifecycle_methods": ["onCreate", "onResume"]
  },

  // ── 控件列表（扁平） ──
  "ui_elements": [{
    "id": "btnSave",
    "type": "Button",
    "label": "Save",
    "visibility": "always|conditional",
    "condition": "if (isLoggedIn) {",
    "is_interactive": true
  }],

  "dynamic_ui": [{
    "source": "inflate_layout|inflate_binding",
    "layout": "activity_main",
    "enclosing_fn": "...",
    "file": "..."
  }],

  "navigation": {
    "entry_points": [{"from": "SplashActivity", "trigger": "fn: onCreate", "type": "activity"}],
    "exit_points": [{"trigger": "menu settings", "destination": "SettingsActivity",
                     "destination_layout": "settings_activity", "type": "activity",
                     "via": "startActivity"}]
  },

  "stats": {
    "conditional_visibility": 2,
    "dynamic_gaps": 1,
    "nav_out": 3,
    "nav_in": 2,
    "fragments": 1,
    "dynamic_elements": 0,
    "event_bindings": 12,
    "event_bindings_with_chain": 5
  },

  // ── Layer 2 详情（按需读取） ──
  "L0_structure": {
    "ui_tree": {"tag": "LinearLayout", "id": "", "children": [...]},
    "fragments": [{"class": "SettingsFragment", "container_id": "fragment_container",
                   "attach_method": "FragmentTransaction.replace"}],
    "dynamic_elements": [{"view_type": "TextView", "creation_method": "addView",
                          "container_id": "dynamicContainer", "properties": {}}]
  },

  "L1_behavior": {
    "event_bindings": [{
      "element_id": "btnSave",
      "event_type": "click",
      "handler_method": "onCreate",
      "effect_chain": [{"step": "navigate", "target": "finish"},
                       {"step": "ui_feedback", "action": "Toast"}],
      "effect_summary": ["navigate:finish", "ui_feedback:Toast"],
      "chain_depth": 2
    }],
    "lifecycle_hooks": {"onCreate": ["initView"], "onResume": ["refreshData"]},
    "adapter_bindings": [{"container_id": "recyclerView", "adapter_class": "MyAdapter",
                          "item_layout": "item_row"}]
  }
}
```

v2.1 相比 v2.0 的变化：
- **新增** `brief` — LLM 友好的摘要层
- **删除** `behaviors`（顶层）— 被 `L1_behavior.event_bindings` 替代
- **删除** `ui_elements[].behaviors` — 同上
- **删除** `screen_id` — 与 `layout` 完全重复
- **删除** `stats.total_elements`/`interactive`/`with_behavior` — 可从 `ui_elements` 数组直接推导

### 5.5 screen_index.json — 全局索引

`generate_specs` 完成后自动生成 `screen_index.json`，LLM 读此一个文件即可了解 App 全貌：

```json
{
  "total_screens": 177,
  "screens": [
    {
      "layout": "activity_main",
      "class": "MainActivity",
      "type": "activity",
      "controls": 15,
      "interactive": 8,
      "event_bindings": 12,
      "nav_in": ["SplashActivity (activity)"],
      "nav_out": ["→ SettingsActivity (menu settings)"],
      "tags": ["navigate:startActivity", "ui_feedback:Snackbar"],
      "spec_file": "activity_main_spec.json"
    }
  ]
}
```

### 5.6 渐进式披露工作流

```
LLM 翻译工作流：
  1. 读 screen_index.json    → 了解 App 全貌，规划 feature 分组
  2. 读 spec.brief           → 了解单页面概览：控件 + 行为标签 + 导航
  3. 读 spec.L0/L1           → 翻译时获取完整 effect_chain 和 ui_tree
```

---

## 6. 输出文件清单

| 文件 | 大小（AntennaPod） | 用途 |
|------|-------------------|------|
| `static_xml.json` | 738KB | XML 控件原始数据 |
| `source_findings.json` | 680KB | 源码扫描的语义模式 |
| `function_symbols.json` | 2.5MB | 函数符号表 |
| `call_graph.json` | 12.8MB | 调用图（最大文件） |
| `ground_truth.json` | 546KB | XML + 源码合并后的完整事实 |
| `navigation_graph.json` | 108KB | 屏幕导航图 |
| `navigation_candidates.json` | 19KB | L1 导航候选 |
| `fragments.json` | 78KB | Fragment 检测结果 |
| `dynamic_ui.json` | 3KB | 动态 UI + Adapter |
| `behavior_chains.json` | 661KB | 事件→效果链 |
| `ui_dag.json` | 43KB | 从 launcher 出发的 UI 树 |
| `ui_paths.json` | 601KB | 扁平化导航路径 |
| `ui_paths_legacy.json` | 27KB | 人类可读路径字符串 |
| `ui_paths_report.json` | 103KB | 路径报告（含 display_report） |
| `ui_paths_enumerated.json` | 3KB | 枚举路径（上限 800） |
| `ui_effect_paths.json` | 320B | 效果路径 |
| `specs/*.json` | 1.5MB 共 177 个 | 每屏幕 migration spec |
| `screen_index.json` | ~50KB | 全局屏幕索引（LLM 概览） |
| `app_model/` | ~1MB | 分层 App 模型 |
| `verification_report.json` | — | 交叉验证报告（--validate） |

---

## 7. 关键算法与设计决策

### 7.1 ViewBinding camelCase → snake_case 解析

Android ViewBinding 将 XML `id` 转换为 camelCase 属性名：`drawer_layout` → `binding.drawerLayout`。源码中的 `view_ref` 是 camelCase，XML 中是 snake_case。

`view_ref_utils.camel_to_snake()` 做反向转换：
- 在小写字母→大写字母边界插入下划线：`drawerLayout` → `drawer_Layout` → `drawer_layout`
- 在字母→数字边界插入下划线：`media3Video` → `media3_Video` → `media3_video`

匹配优先级：`findViewById` 显式映射 > snake_case 转换 + XML id 验证 > 直接匹配。

### 7.2 Fragment 宿主检测的 AST 数据流分析

`fragment_detector._resolve_fragment_arg()` 从 FragmentTransaction.replace/add 的参数位置出发，追踪数据流以确定实际的 Fragment 类名：

```
transaction.replace(R.id.container, fragment)
                                      ↓
                        val fragment = SomeFragment()     // 直接实例化
                        val fragment = X.newInstance()     // 工厂方法
                        val fragment = getFragment(type)   // 方法返回值
```

支持 switch-case 工厂模式（when 分支返回不同 Fragment）和 .show() 调用中嵌套构造函数参数的解析。

### 7.3 effect_chain 的调用图遍历

`behavior_chain_extractor` 对每个事件注册：
1. 找到 handler 所在函数的符号（通过 function_symbols）
2. 从 call_graph 中找到该函数调用的所有方法
3. 对每个被调用方法，分类为 navigate/ui_feedback/ui_update/async/call
4. 如果是 call，递归展开（最多 3 层），生成嵌套的 `nested` 字段
5. 检测 handler 体中的 if/when 分支，生成 `condition` 步骤

### 7.4 Spec 防膨胀

当 `class_name == ""` 时（布局无法映射到具体类），以下三个过滤条件会退化为全量匹配：
- `"".find("") == 0` 总为 True → 所有 behavior_chains 被塞入 spec
- `"" == ""` 总为 True → 所有空 host 的 fragments 被塞入 spec

修复：所有匹配条件前加 `class_name and` 守卫。修复后产物从 32MB 降至 1.5MB。

---

## 8. 开发约定

- **小步提交**：每个功能点单独 commit
- **复用优先**：共享逻辑提取到 `view_ref_utils.py` 等公共模块
- **v1 兼容**：v1 spec 字段保留，v2 是增量扩展层
- **tree-sitter 可选**：所有使用 AST 的模块在 tree-sitter 不可用时回退到正则
- **误报抑制**：多层过滤（PascalCase 类型名、系统对象、首字母大写检查等）
- **产物可审计**：每个中间 JSON 文件独立可读，便于调试

---

## 9. 修改日志

| 日期 | 变更 | 相关 commit |
|------|------|------------|
| 2026-05-30 | Fragment 宿主检测：6/84 → 79/84 (94%)，新增 9 种检测模式 | a1fe215, 87bf752, f4c8b51, 995193f |
| 2026-05-30 | 修复 spec 膨胀：32MB → 1.5MB，守卫空 class_name | e0ddf6d |
| 2026-05-30 | 过滤函数名误判为 Dialog（`showErrorDialog` 等） | 24eb581 |
| 2026-05-30 | 去重 snake_case 变体 spec（media3 等） | 64c9cc5 |
| 2026-05-30 | ViewBinding element_id 解析：0% → 87% | 8532fea |
| 2026-05-30 | 添加 effect_summary 标签到 event_bindings | e76f51d |
| 2026-05-30 | 提取共享 view_ref_utils 模块 | 655b02f |
| 2026-05-30 | 修复 v1 behaviors 跨文件污染：按 class_name 过滤文件来源 | eb8551a |
| 2026-05-30 | 支持箭头 lambda（`v -> expr()`）的 handler body 提取 | 984a95f |
| 2026-05-30 | 扩展 findViewById 映射正则以支持 Java 字段赋值 | c5ee72e |
| 2026-05-30 | 去重 lifecycle_hooks 中重复的 callee 方法名 | 04d7ecd |
| 2026-05-30 | Spec v2.1：删除冗余 v1 字段，新增 brief 摘要层，生成 screen_index.json | e8ed2ae, 148d121, 3ccd7d8 |
