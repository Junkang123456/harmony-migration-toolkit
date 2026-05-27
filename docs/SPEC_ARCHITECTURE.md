# Spec 架构：现状与改进方案

**文档版本**：2026-05-22
**状态**：设计方案

---

## 第一部分：现有架构

### 整体 Pipeline

```
pipeline.py --android-root <项目路径>
  │
  ├─ Stage 0  bundled_spec_tools/main.py    静态提取（7 步）
  ├─ Stage 1  build_android_facts.py        汇总为 android_facts.v1.json
  ├─ Stage 2  build_framework_map.py        Android↔Harmony 框架映射
  ├─ Stage 3  build_harmony_arch.py         Harmony 架构占位
  ├─ Stage 5  build_feature_tree.py         功能树 IR
  ├─ Stage 4  stage4_emit_scaffold.py       脚手架（可选）
  └─ Stage 7  export_agent_bundle.py        Agent 交付包
```

核心分析都在 Stage 0 里完成，后续 Stage 是汇总、映射、组装。

### Stage 0 内部流程

Stage 0（`bundled_spec_tools/main.py`）是整个工具的分析引擎，分 7 步：

```
Android 项目源码
     │
     ├─ [1] xml_extractor ──────────→ static_xml.json
     │     解析 res/layout/*.xml        180 个 UI 元素（id/tag/layout/is_interactive）
     │
     ├─ [2] source_extractor ───────→ source_findings.json
     │     扫描 .kt/.java 源码          5 类模式：事件注册(70) / R.id分发(52) /
     │     function_graph_extractor      可见性控制(160) / 布局膨胀(3) / 数据驱动UI(0)
     │                                  + function_symbols.json (1886 符号)
     │                                  + call_graph.json (2495 调用边)
     │
     ├─ [3] ground_truth_builder ───→ ground_truth.json
     │     合并 XML + Source              交叉匹配：哪些元素绑定了行为
     │
     ├─ [4] navigation_extractor ───→ navigation_graph.json
     │     提取页面跳转关系               节点(39 screen) + 边(50 跳转)
     │                                  + class→layout 映射(42 条)
     │
     ├─ [5] gap 合并（可选）
     │
     ├─ [6] UI DAG / 路径枚举 ─────→ ui_dag.json / ui_paths*.json / ui_effect_paths.json
     │     从 launcher 出发 BFS          + app_model/ (screens/features/paths)
     │
     └─ [7] generate_specs ────────→ specs/*_spec.json (86 个)
           为每个 screen 生成 spec       每个 spec 含 ui_elements / behaviors / navigation
```

### 提取器清单

| 文件 | 做什么 | 输入 | 输出 |
|------|--------|------|------|
| `xml_extractor.py` | 解析 layout/menu XML，提取所有 UI 元素 | `res/layout/*.xml` | `static_xml.json` |
| `source_extractor.py` | 扫源码 5 类语义模式（事件注册/R.id 分发/可见性/inflate/数据驱动） | `.kt` / `.java` | `source_findings.json` |
| `function_graph_extractor.py` | 提取函数符号 + 调用关系 | `.kt` / `.java` | `function_symbols.json` + `call_graph.json` |
| `ground_truth_builder.py` | 合并 XML 元素和源码行为，交叉匹配 | static_xml + source_findings | `ground_truth.json` |
| `navigation_extractor.py` | 提取页面跳转（startActivity / Dialog / Fragment） | 源码 + Manifest | `navigation_graph.json` |
| `nav_pipeline.py` | 导航候选 + UI 效果路径 | 源码 + nav_graph | `navigation_candidates.json` + `ui_effect_paths.json` |
| `ui_dag_assembler.py` | 从 launcher 构建可达 DAG | nav_graph + ground_truth | `ui_dag.json` |
| `generate_specs.py` | 按 screen 组装单页 spec | nav + ground_truth + paths + dag | `specs/*_spec.json` |
| `app_model_builder.py` | 构建 screens/features/paths 的 app_model 视图 | 全部中间产物 | `app_model/` 目录 |

辅助模块：`android_project.py`（项目路径约定）、`ast_index.py`（AST 索引）、`class_parser.py`（类解析）、`bytecode_navigation.py`（字节码辅助）、`dependency_resolver.py`（includeBuild 依赖检测）。

### 当前单页 Spec 格式（v1）

以 LoginActivity 为例：

```json
{
  "class": "LoginActivity",
  "screen_type": "activity",
  "layout": "login_activity",
  "screen_id": "login_activity",
  "source": "project",
  "ui_elements": [],
  "dynamic_ui": [],
  "behaviors": [],
  "navigation": {
    "entry_points": [
      { "from": "LoginActivity", "trigger": "fn: openLoginPage", "type": "activity" }
    ],
    "exit_points": [
      { "destination": "showAppLoadDialog", "type": "commons_dialog", "via": "Dialog()" }
    ]
  },
  "stats": {
    "total_elements": 0, "interactive": 0, "with_behavior": 0,
    "dynamic_gaps": 0, "conditional_visibility": 0, "nav_in": 1, "nav_out": 2
  }
}
```

### 当前架构的短板

| 短板 | 表现 |
|------|------|
| **ui_elements 大面积为空** | `static_xml.json` 有 180 个元素，但 `generate_specs` 没有按 layout 回填到单页 spec |
| **行为链为空** | `ui_effect_paths.json` path_count=0；只知道"谁注册了 onClick"，不知道"点击后发生什么" |
| **0 个 Fragment** | `navigation_extractor` 只识别 Activity/Dialog，不识别 Fragment 子类 |
| **动态组件盲区** | 只解析 XML layout，代码中 `addView()` / `new XxxView()` 创建的控件看不见 |
| **映射全是占位** | `harmony_arch.v1.json` 的 bundle_name 和路由都是 placeholder |
| **信息没分层** | 结构、行为、映射混在同一个扁平 JSON，缺什么补什么看不清 |

---

## 第二部分：改进方案——三层 Spec

### 核心思路

把单页 spec 从一个扁平 JSON 拆成三层，每层有明确职责：

```
┌─────────────────────────────────────┐
│  L2 映射层  到鸿蒙上怎么对应         │   Android 组件 → ArkUI 组件逐条映射
├─────────────────────────────────────┤
│  L1 行为层  用户操作后会怎样          │   事件 → handler → 效果链（跳转/弹窗/状态变更）
├─────────────────────────────────────┤
│  L0 结构层  这个页面有什么            │   控件树（XML + 动态组件）+ Fragment
└─────────────────────────────────────┘
```

上层依赖下层：L1 的行为绑定到 L0 的控件上，L2 的映射针对 L0 的每个控件。

### L0 结构层

**职责**：完整描述一个页面的 UI 控件树。

**改什么**：
1. `static_xml.json` 的元素按 layout 名回填到对应 spec，组装成树形 `ui_tree`（保留 ViewGroup 嵌套关系）
2. 新增 `dynamic_ui_extractor.py`：扫描 `onCreate` / `onCreateView` 中的 `addView` / `new XxxView` / `LayoutInflater.inflate`，合并进 `ui_tree`，标记 `source: "dynamic_code"`
3. 新增 `fragment_detector.py`：识别 Fragment 子类 + `FragmentTransaction.replace/add` + `FragmentPagerAdapter.getItem`

**示例**（HomeActivity）：

```json
"L0_structure": {
  "ui_tree": {
    "tag": "ConstraintLayout",
    "children": [
      {
        "tag": "ViewPager2", "id": "viewpager_home",
        "source": "static_xml", "is_interactive": true
      },
      {
        "tag": "LinearLayout", "id": "ll_bottom_tab",
        "source": "static_xml", "is_interactive": true,
        "children": [
          { "tag": "ShapeTextView", "id": "tv_create_tab", "label": "PPT创作", "source": "static_xml" },
          { "tag": "ShapeTextView", "id": "tv_template_tab", "label": "模版", "source": "static_xml" },
          { "tag": "ShapeTextView", "id": "tv_works_tab", "label": "作品", "source": "static_xml" },
          { "tag": "ShapeTextView", "id": "tv_mine_tab", "label": "我的", "source": "static_xml" }
        ]
      }
    ]
  },
  "fragments": [
    { "class": "HomeFragment", "container_id": "viewpager_home", "attach_method": "ViewPager2+FragmentStateAdapter", "position": 0 },
    { "class": "RecommendFragment", "container_id": "viewpager_home", "attach_method": "ViewPager2+FragmentStateAdapter", "position": 1 },
    { "class": "WorkFragment", "container_id": "viewpager_home", "attach_method": "ViewPager2+FragmentStateAdapter", "position": 2 },
    { "class": "MineFragment", "container_id": "viewpager_home", "attach_method": "ViewPager2+FragmentStateAdapter", "position": 3 }
  ]
}
```

### L1 行为层

**职责**：描述用户操作后会发生什么——从事件触发到最终效果的完整链路。

**改什么**：
新增 `behavior_chain_extractor.py`，输入 `source_findings.json` 的 event_registrations（70 个）+ `call_graph.json`（2495 条边），对每个事件注册向下追踪调用链，识别：
- `navigate`：`startActivity` / `findNavController().navigate` → 页面跳转
- `ui_feedback`：`Toast.makeText` / `AlertDialog.Builder` → 用户提示
- `ui_update`：`setVisibility` / `setText` → UI 状态变更
- `condition`：`if` / `when` 分支 → 不同条件走不同路径
- `async`：协程/回调 → 异步操作

追踪深度限制 3 层调用，每个 step 标记 `confidence`（`static_analysis` 或 `inferred`）。

**示例**（LoginActivity 的登录按钮）：

```json
"L1_behavior": {
  "event_bindings": [
    {
      "element_id": "btn_login",
      "event_type": "click",
      "handler": { "method": "onLoginClick", "file": "LoginActivity.kt", "line": 85 },
      "effect_chain": [
        { "step": "call", "target": "validate()" },
        {
          "step": "condition", "expr": "password.isBlank()",
          "then": [
            { "step": "ui_feedback", "action": "Toast", "message": "密码不能为空" }
          ],
          "else": [
            { "step": "async", "target": "loginApi.login(username, password)" },
            {
              "step": "condition", "expr": "onSuccess",
              "then": [
                { "step": "navigate", "destination": "HomeActivity", "via": "startActivity" }
              ],
              "else": [
                { "step": "ui_feedback", "action": "Dialog", "message": "网络错误" }
              ]
            }
          ]
        }
      ],
      "confidence": "static_analysis"
    }
  ],
  "adapter_bindings": [
    {
      "container_id": "recycler_templates",
      "adapter_class": "PptTemplateAdapter",
      "item_layout": "item_ppt_template",
      "data_source": "viewModel.templateList"
    }
  ],
  "lifecycle_hooks": {
    "onCreate": ["initView", "initObserver"],
    "onResume": ["checkLoginStatus"]
  }
}
```

### L2 映射层

**职责**：对 L0 里的每个控件，给出对应的 ArkUI 组件和写法。

**改什么**：
扩展 `data/framework_map/rules.yaml`，从框架级（RecyclerView → List）细化到组件实例级。Stage 2 遍历 L0 的 `ui_tree`，逐个控件查规则表：匹配到的输出映射，没匹配到的输出 gap + suggestion。

新增 gap 反馈回流：Agent 填补的成功映射可写入 `candidate_rules.yaml`，经 review 后升级为正式规则。

**示例**（LoginActivity）：

```json
"L2_projection": {
  "harmony_target": {
    "page_path": "pages/login/LoginPage",
    "ability": "EntryAbility",
    "module": "entry"
  },
  "component_mappings": [
    {
      "android": { "tag": "EditText", "id": "et_username" },
      "arkui": { "component": "TextInput", "props": { "placeholder": "用户名" } },
      "rule_id": "R-EditText-TextInput",
      "confidence": "rule_match"
    },
    {
      "android": { "tag": "BannerViewPager", "id": "banner" },
      "arkui": null,
      "gap": { "status": "no_rule", "suggestion": "Swiper 组件" }
    }
  ],
  "navigation_mappings": [
    {
      "android": { "action": "startActivity", "destination": "HomeActivity" },
      "arkui": { "action": "router.pushUrl", "url": "pages/home/HomePage" }
    }
  ]
}
```

### Spec v2 完整格式

三层合在一个文件里：

```json
{
  "spec_version": "2.0",
  "screen_id": "login_activity",
  "class": "LoginActivity",
  "screen_type": "activity",
  "layout": "login_activity",

  "L0_structure": { },
  "L1_behavior": { },
  "L2_projection": { },

  "navigation": {
    "entry_points": [],
    "exit_points": []
  },
  "stats": {
    "total_elements": 12,
    "static_elements": 10,
    "dynamic_elements": 2,
    "interactive": 5,
    "with_behavior_binding": 4,
    "fragments": 0,
    "projection_coverage": 0.83,
    "gaps": 1
  }
}
```

### 改造后的 Pipeline

```
Stage 0 (bundled_spec_tools/main.py)
  ├─ [1] xml_extractor              → static_xml.json           (不变)
  ├─ [2] source_extractor           → source_findings.json      (不变)
  │       function_graph_extractor   → call_graph.json           (不变)
  ├─ [3] ground_truth_builder       → ground_truth.json         (不变)
  ├─ [4] navigation_extractor       → navigation_graph.json     (不变)
  ├─ [新] dynamic_ui_extractor      → dynamic_ui.json
  ├─ [新] fragment_detector         → fragments.json
  ├─ [新] behavior_chain_extractor  → behavior_chains.json
  ├─ [5-6] DAG / 路径 (不变)
  └─ [7] generate_specs_v2          → specs_v2/*_spec.json
              整合 static_xml + dynamic_ui + fragments           → L0
              整合 source_findings + behavior_chains              → L1
              查 rules.yaml 逐组件映射                            → L2

Stage 1-7 (不变，消费 v2 spec)
```

### 新增模块

| 模块 | 做什么 | 怎么做 |
|------|--------|--------|
| `dynamic_ui_extractor.py` | 检测代码中动态创建的 UI 组件 | 扫 `onCreate`/`onCreateView` 方法体，正则匹配 `addView()`/`new XxxView()`/`inflate(R.layout.xxx)`，追踪 `setAdapter()` 的 item layout |
| `fragment_detector.py` | 识别 Fragment 及其挂载方式 | 找 Fragment 子类 + `FragmentTransaction.replace/add` + `FragmentPagerAdapter.getItem` 的 when 分支 |
| `behavior_chain_extractor.py` | 从事件注册追踪完整效果链 | 以 event_registration 为入口，沿 call_graph 向下追 3 层，识别 navigate/ui_feedback/ui_update/condition 模式 |

### 分期

| 期次 | 内容 | 预期效果 |
|------|------|---------|
| **P0** | L0 基础：static_xml 回填 ui_tree + fragment_detector | spec 的 ui_elements 非空率从 ~20% → 90%+；Fragment 从 0 → 13+ |
| **P1** | L0 增强：dynamic_ui_extractor | 代码动态创建的控件纳入 ui_tree |
| **P2** | L1：behavior_chain_extractor | 有效果链的交互元素从 0% → 55%+ |
| **P3** | L2：逐组件映射 + gap 反馈回流 | 每个控件都有映射或明确的 gap 标记 |
| **P4** | Spec v2 整合 + Schema | 三层合一，输出 `specs_v2/*_spec.json` |

---

## 第三部分：双轨融合——Toolkit × LLM

生成 spec 有两条路：Toolkit 做静态分析，LLM Skill 探索代码仓。先分别讲清楚各自产什么、目录长什么样，再说怎么融合。

### 3.1 LLM Skill 产出的 spec（baseline）

LLM Skill 的做法是：让 Claude 直接读 Android 源码 + AndroidManifest，自上而下地理解项目，产出一整套 Markdown + JSON 规约。以 WordPress Android 项目为例，产出在 `spec/baseline/` 下，结构如下：

```
spec/baseline/
├── ui-manifest.md              ← 全局页面清单（126 页）
├── feature-index.md            ← 全局功能清单（21 feature）
├── feature-base.md             ← 共享基础设施（数据模型、网络层、DB、事件系统）
├── phase_a_report.md           ← Phase A 执行报告
├── _phase_a_summary.json       ← Phase A 统计数据
│
├── features/                   ← 每个 feature 一份 spec
│   ├── F001-auth-account.md
│   ├── F002-sites-mysite.md
│   └── ... (21 个)
│
├── ui/                         ← 每个页面一份 spec
│   ├── page_0001_WPLaunchActivity.md
│   ├── page_0014_LoginActivity.md
│   └── ... (126 个)
│
├── ui-snapshots/               ← 每个页面的结构化快照
│   ├── page_0001_WPLaunchActivity/
│   │   ├── meta.json           ← 页面元数据（layout sources、fragment tags、clickable elements）
│   │   └── view.xml            ← 合成的 UI hierarchy（从 layout XML 静态解析生成）
│   └── ... (126 个目录)
│
├── plans/                      ← 执行计划
│   ├── coverage-matrix.md      ← feature × page 覆盖矩阵
│   ├── feature-plan.md         ← feature 执行顺序（21 slice，按依赖拓扑排序）
│   └── ui-plan.md              ← UI 转换批次（35 batch，按 feature 分组）
│
└── _*.py                       ← 编排脚本（驱动 LLM 生成的 Python 工具）
```

#### 各文件做什么

**`ui-manifest.md`** — 全局页面清单。列出项目所有 Activity（126 个），每个标注：
- 优先级（P0/P1/P2）
- UI 框架（xml / compose / hybrid / none）
- confidence（LLM 对自己判断的信心）
- 全局约定（导航架构用 NavPathStack、底部 Tab 用 Tabs、命名规范 Activity→XxxPage.ets 等）

**`feature-index.md`** — 全局功能清单。把 126 个页面归到 21 个 feature（如 F001 认证与账号、F003 阅读器），每个 feature 标注依赖关系，形成 5 层拓扑图。

**`feature-base.md`** — 共享基础设施。所有 feature 公用的底层：17+ 个数据模型（AccountModel → Account、PostModel → Post 等）、数据库迁移策略（WellSql/Room → RDB）、网络层（OkHttp → @ohos.net.http）、事件系统（EventBus → EventHub）。

**`features/F001-*.md`** — 单 feature spec。以 F001 认证与账号为例：
- 覆盖的 10 个页面
- 数据流：AccountStore → AccountService → @State
- 服务层接口骨架（AuthService、SessionService）
- 状态管理（AppStorage 键、关联 Pref Keys）
- 验收标准（5 条功能验收项）

**`ui/page_NNNN_*.md`** — 单页面 spec。以 LoginActivity 为例：
- 溯源（源码路径、关联 Fragment、layout sources）
- 页面结构（ASCII 树形图，分状态描述）
- 转换决策表（ConstraintLayout → Column、ProgressBar → LoadingProgress，附理由）
- 状态接口（@State 变量、类型、数据来源）
- 导航关系（触发 → 目标 → 类型）
- 资源映射（新增 string/color/media 条目逐条列出）
- Pitfall 记录（已知的转换陷阱及处理方式）

**`ui-snapshots/*/meta.json`** — 页面元数据。脚本从 AndroidManifest + 源码提取的结构化数据：
- `layout_sources`：该 Activity 用了哪些 layout XML
- `fragment_tags`：源码中出现的所有 Fragment tag（100+ 条）
- `clickable_elements`：可点击的 UI 元素
- `source_file`：Activity 源码路径
- `ui_framework`：xml / compose / hybrid / none
- `priority`、`status`、`output_file`

**`ui-snapshots/*/view.xml`** — 合成的 UI 层级树。脚本从 layout XML 解析出来的，格式类似 Android uiautomator dump：每个节点带 class、resource-id、text、clickable 等属性。

**`plans/`** — 执行计划。`feature-plan.md` 按依赖拓扑排出 21 个 slice 的执行顺序 + 每个 slice 的子任务；`ui-plan.md` 把 126 个页面分成 35 个 batch，每 batch 标注可并行度。

#### LLM Skill 产出的特点

**优势**：
- 有业务理解：知道"这个页面是干什么的"，能做 feature 划分、优先级判断
- 有设计决策：每个组件映射到什么 ArkUI 组件、为什么这么选
- 有全局规划：依赖图、执行顺序、batch 编排
- 信息密度高：一个 `page_NNNN_*.md` 包含结构 + 映射 + 状态 + 导航 + 资源 + pitfall

**劣势**：
- confidence 普遍偏低（很多页面标 `low`）
- 不可复现（同样的代码跑两次结果可能不同）
- 可能幻觉（元素 ID 写错、方法名编造、跳转关系推断错误）
- 没有行号级精度（不知道某个 onClick 注册在第几行）

### 3.2 Toolkit 产出（全部阶段）

Toolkit（harmony-migration-toolkit）用 Python 脚本做确定性静态分析，不依赖 LLM。Pipeline 分 7 个 Stage，产出全部在 `toolkit-out/` 下：

```
toolkit-out/
├── intermediate/
│   ├── 0_android_facts/        ← Stage 0: 静态提取（核心分析引擎）
│   ├── 1_android_facts/        ← Stage 1: 事实汇总
│   ├── 2_framework_map/        ← Stage 2: Android↔Harmony 框架映射
│   ├── 3_harmony_arch/         ← Stage 3: Harmony 架构投影
│   └── 5_feature_tree/         ← Stage 5: 功能树 IR
│
└── agent_bundle.v1.json        ← Stage 7: Agent 交付包（整合所有阶段的索引）
```

下面逐 Stage 说明。

#### Stage 0 — 静态提取（核心分析引擎）

这是整个 Toolkit 的核心，分 7 步扫描 Android 源码，产出所有原始事实：

```
0_android_facts/
├── static_xml.json             ← 所有 layout XML 的 UI 元素（180 个，每条带 layout/id/tag/is_interactive）
├── source_findings.json        ← 源码 5 类语义模式：事件注册(70) / 可见性控制(160) / R.id分发(52) / inflate(3) / 数据驱动UI(0)
├── call_graph.json             ← 函数调用关系（2495 条边，行号级）
├── function_symbols.json       ← 函数符号表（1886 个）
├── navigation_graph.json       ← 页面跳转（50 条边 + 42 个 class↔layout 映射）
├── ground_truth.json           ← XML 元素与源码行为的交叉匹配
├── manifest.json               ← AndroidManifest 解析结果
├── ui_dag.json                 ← 从 launcher 出发的可达图
├── ui_effect_paths.json        ← UI 效果路径（当前为空）
├── specs/                      ← 单页 spec（86 个，扁平 JSON）
└── app_model/                  ← 应用模型视图（screens/features/paths）
```

#### Stage 1 — 事实汇总

把 Stage 0 的原始数据汇总成一份结构化的项目概况：

```
1_android_facts/
└── android_facts.v1.json       ← 项目概况：Gradle 模块、Manifest 信息、导航摘要（39 节点/50 边）、
                                   全部 screen 列表（每条带 class_name/kind/layout/noise 标记）
```

这份文件是后续所有 Stage 的输入起点。

#### Stage 2 — 框架映射

基于 `data/framework_map/rules.yaml` 中的规则，生成 Android 概念到 Harmony 概念的映射：

```
2_framework_map/
└── framework_map.v1.json       ← 10 条框架级映射规则（Activity→UIAbility、Fragment→NavDestination、
                                   Compose→ArkUI、Service→ServiceExtension、Intent→wantAgent 等）
                                   + gap 列表（Kotlin 合成类等无法映射的项）
```

当前粒度是框架级（"RecyclerView 对应 List/Grid"），不是组件实例级（"这个 EditText 对应 TextInput"）。

#### Stage 3 — Harmony 架构投影

基于事实汇总 + 框架映射，生成 HarmonyOS 侧的项目骨架：

```
3_harmony_arch/
└── harmony_arch.v1.json        ← HarmonyOS 项目结构：bundle_name、模块列表（entry/feature 模块划分）、
                                   Ability 列表、路由表（37 条路由，每条从 Android screen 映射到 pages/xxx/Index）、
                                   资源约定（string.json 位置、media 迁移策略）
```

当前是占位状态：`bundle_name: "com.example.placeholder"`，路由全是 `pages/xxxactivity/Index` 的模板。

#### Stage 4 — 脚手架（可选，dry-run）

读 `harmony_arch.v1.json`，打印出 HarmonyOS 项目的模块/Ability/路由目录结构，可选择是否真正写入文件。当前只做 dry-run 输出。

#### Stage 5 — 功能树 IR

把前面所有阶段的数据组装成一棵功能树（feature_tree），这是 Toolkit 的核心产出物：

```
5_feature_tree/
├── feature_tree.v1.json        ← 功能树：节点（product_root / feature / screen / ui_control / behavior / function_symbol）
│                                  + 边（parent_of / navigates_to / presents_modal / binds_event / calls）
├── feature_spec_evidence.json  ← 每个 feature 节点关联的源码证据（文件路径、行号）
├── taxonomy_report.json        ← Feature 分类报告（17 个自动生成的 feature，全是 generated.* 词根聚类）
└── verify_report.json          ← 验证报告（未解析的调用、覆盖率等）
```

注意：自动生成的 feature 划分（`generated.choice.ppttemplate` 这种）是纯词根聚类，**没有业务语义**，质量不高。

#### Stage 6 — Feature Tree Viewer

把 `feature_tree.v1.json` + 周边数据导出为一个可在浏览器打开的可视化 viewer（HTML + 静态 JSON）。

#### Stage 7 — Agent 交付包

把所有阶段的产物打包成一份 `agent_bundle.v1.json`，供 Agent 消费：

```
agent_bundle.v1.json            ← 包含：项目概况（outline）、功能树摘要、框架映射摘要、
                                   Harmony 架构摘要、中间产物索引（208 个文件的路径 + 大小）、
                                   验证状态、使用提示（gaps/harmony_projection/source_evidence 等）
```

#### Toolkit 产出的特点

**优势**：
- 100% 可复现（相同输入永远相同输出）
- 行号级精度（事件注册在哪个文件第几行）
- 调用图完整（2495 条边，覆盖全源码）
- 全 Pipeline 串联（从源码扫描 → 事实汇总 → 框架映射 → 架构投影 → 功能树 → 交付包）
- 不会幻觉

**当前劣势**（第二部分的改进方案正是为了解决这些）：
- 很多 spec 的 `ui_elements` 为空 → **P0 改进：ui_tree 回填**
- 行为链空白 → **P2 改进：behavior_chain_extractor**
- 0 个 Fragment → **P0 改进：fragment_detector**
- 框架映射只有 10 条框架级规则 → **P3 改进：逐组件映射 + gap 回流**
- Feature 划分是词根聚类，没有业务语义 → **这个 Toolkit 做不了，需要 LLM**
- Harmony 架构投影全是占位 → **部分可改进（路由生成），部分需要 LLM（命名约定、模块设计）**
- 没有优先级、批次规划、执行计划 → **Toolkit 做不了，需要 LLM**

### 3.3 对比（基于改进后的 Toolkit）

以下对比假设第二部分的改进（P0-P4）全部完成后，Toolkit 产出三层 Spec v2：

| 维度 | 改进后的 Toolkit | LLM Skill |
|------|-----------------|-----------|
| **L0 结构层** | | |
| ui_tree | 精确、树形、XML 回填（90%+ 非空率） | 有（view.xml 合成），但不保留嵌套层级 |
| Fragment | 有（fragment_detector，含 container_id + attach_method） | 有（meta.json，100+ fragment_tags，但只有 tag 名没有挂载细节） |
| 动态组件 | 有（dynamic_ui_extractor，扫 onCreate） | 有（LLM 读源码识别），但无行号 |
| **L1 行为层** | | |
| 事件绑定 | 精确（70 event_registration，行号级） | 做不到行号级 |
| 效果链 | 有（behavior_chain_extractor，沿 call_graph 追 3 层） | 有（自然语言描述，更易读但非结构化） |
| 效果链覆盖率 | 55%+ 交互元素有链 | 接近 100%（LLM 可以推断） |
| lifecycle / adapter | 有（从 source_findings 提取） | 有 |
| **L2 映射层** | | |
| 组件映射 | 逐组件（遍历 ui_tree 查 rules.yaml，有匹配或有 gap 标记） | 逐组件（57+ 条 ARKUI_MAP + 每页转换决策表 + 设计理由） |
| 导航映射 | 精确（50 边，含 trigger + line） | 有（含目标路由路径，但可能有幻觉） |
| gap 处理 | 标记 `no_rule` + suggestion | 直接给出建议方案 + 理由 |
| **Toolkit 做不了的** | | |
| Feature 划分 | 词根聚类（无业务语义） | **21 feature + 依赖图** |
| 优先级 / 批次 | 无 | **P0/P1/P2 + batch 规划** |
| 状态管理设计 | 无 | **每页 @State 变量 + 数据来源** |
| 资源映射 | 无 | **string/color/media 逐条** |
| 共享基础设施 | 无 | **数据模型 / DB / 网络层 / 事件系统** |
| 执行计划 | 无 | **feature-plan + ui-plan** |
| 验收标准 | 无 | **每 feature 验收项** |
| 可复现 | 是 | 否 |

改进后的 Toolkit 解决了"浅"的问题（L0 有树、L1 有链、L2 有映射），但**"窄"的问题解决不了**——feature 划分、优先级、状态设计、执行计划这些需要业务理解的事情，只有 LLM 能做。

### 3.4 融合方案

#### 核心思路

改进后的 Toolkit 在三层 Spec 上已经能产出结构化的、高精度的数据。LLM 不需要再"补 Toolkit 的空"，而是在 Toolkit 的事实基础上**叠加业务语义层**。

```
改进后的 Toolkit 产出                       LLM 叠加的语义层
─────────────────────────                   ─────────────────
L0 ui_tree (精确)                           + Feature 归属（这个页面属于哪个 feature）
L0 fragments (精确)                         + 优先级（P0/P1/P2）
L1 event_bindings (行号级)                  + 自然语言描述（给人看的场景说明）
L1 effect_chain (结构化)                    + 补全 Toolkit 追不到的链（反射、跨进程、协程深层）
L2 component_mappings (规则匹配)            + gap 填补（Toolkit 标 no_rule 的，LLM 给方案）
L2 navigation_mappings (精确边)             + 目标路由路径设计
                                            + 状态管理（@State 变量表）
                                            + 资源映射（string/color/media）
                                            + 共享基础设施（数据模型 / DB / 网络层）
                                            + 执行计划（batch / slice 编排）
                                            + 验收标准
```

#### 逐 Stage 融合

**Stage 0（静态提取）→ L0 + L1**：
- Toolkit 是唯一数据源。改进后的 Stage 0 产出精确的 ui_tree、fragments、dynamic_ui、behavior_chains。
- LLM 不需要在这个层面重复工作，但可以校验（比如 Toolkit 的 fragment_detector 漏掉的，LLM 的 meta.json 里有）。

**Stage 1（事实汇总）**：
- Toolkit 产出 `android_facts.v1.json`（screen 列表 + 导航摘要）。
- LLM 在此基础上补充 UI 框架标注（xml/compose/hybrid）和 confidence 评估。

**Stage 2（框架映射）→ L2**：
- 改进后的 Toolkit 遍历 L0 的 ui_tree 逐组件查 rules.yaml，能命中的输出映射，不能命中的标 gap。
- LLM 负责填补 gap：Toolkit 标了 `no_rule` 的控件，LLM 给出映射方案 + 理由。
- 成功的 LLM 映射写入 `candidate_rules.yaml`，review 后升级为正式规则，下次 Toolkit 就能自动命中。

**Stage 3（架构投影）**：
- Toolkit 从 screen 列表 + 映射规则生成路由表骨架。
- LLM 替换占位：真实的 bundle_name、合理的路由路径命名（`pages/login/LoginPage` 而非 `pages/loginactivity/Index`）、模块划分决策。

**Stage 5（功能树）**：
- Toolkit 的图结构保留（节点类型 + 边关系）。
- LLM 的 feature 划分替换 `generated.*` 词根聚类：21 个业务 feature + 依赖图 + 优先级。
- LLM 的验收标准挂到 feature 节点上。

**Stage 7（Agent 交付包）**：
- Toolkit 的 `agent_bundle.v1.json` 结构保留（索引 + 摘要 + 验证）。
- 新增 LLM 产物引用：`feature-index.md`、`ui-manifest.md`、`plans/`。
- Agent 读交付包时，同时获得 Toolkit 的精确事实和 LLM 的业务语义。

#### LLM Skill 应该注入的 Toolkit 产物

改进后的 Toolkit 产出更丰富了，LLM 可以读到更多有用的上下文：

| 注入什么 | 来自 Stage | 用途 |
|---------|-----------|------|
| `specs_v2/*_spec.json` 的 L0 | Stage 0 | 精确的 ui_tree + fragments，LLM 不用自己解析 XML |
| `specs_v2/*_spec.json` 的 L1 | Stage 0 | 精确的 event_bindings + effect_chain，LLM 只需补充语义标注 |
| L2 的 gap 列表 | Stage 2 | LLM 精准知道哪些控件需要自己给映射方案 |
| `android_facts.v1.json` | Stage 1 | 项目全貌：screen 列表、导航摘要、模块信息 |
| `feature_tree.v1.json` | Stage 5 | 图结构，LLM 在此基础上注入业务 feature 语义 |

#### 冲突怎么解

改进后冲突面缩小了——Toolkit 的三层 Spec 和 LLM 的产出大部分是互补而非重叠。剩余的冲突：

| 冲突类型 | 谁优先 | 理由 |
|---------|--------|------|
| UI 元素 / Fragment / 行号 | Toolkit | 静态分析不会编造 |
| 效果链的调用路径 | Toolkit（3 层内） | 精确的 call_graph 追踪 |
| 效果链超出 3 层的部分 | LLM | Toolkit 深度限制外的链路只能靠 LLM 推断 |
| 组件映射（rules.yaml 命中的） | Toolkit | 确定性规则 |
| 组件映射（gap / no_rule 的） | LLM | Toolkit 明确标了"我不知道"，LLM 来填 |
| Feature 归属 / 优先级 / 执行计划 | LLM | Toolkit 做不了业务理解 |
