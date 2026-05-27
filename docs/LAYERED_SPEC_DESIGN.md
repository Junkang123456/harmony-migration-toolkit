# 分层 Spec 设计方案

**文档版本**：2026-05-22
**状态**：设计草案，待实现
**范围**：[harmony-migration-toolkit](../)；替代当前扁平 `*_spec.json` 的下一代 spec 体系

---

## 1. 为什么要分层

### 1.1 当前 spec 的问题

拿 `LoginActivity` 的 spec 举例，现在长这样：

```json
{
  "class": "LoginActivity",
  "screen_type": "activity",
  "ui_elements": [],          // ← 空的，180 个元素在 static_xml.json 却没回填
  "behaviors": [],             // ← 空的，点击登录按钮会发生什么？不知道
  "dynamic_ui": [],            // ← 空的，代码里 new 出来的控件看不见
  "navigation": {
    "entry_points": [{"from": "LoginActivity", "trigger": "fn: openLoginPage"}],
    "exit_points": [{"destination": "showAppLoadDialog", "type": "commons_dialog"}]
  }
}
```

这个 spec 能告诉你"这个页面叫 LoginActivity"，但回答不了三个关键问题：

1. **页面上有什么？** → `ui_elements` 是空的（G1 缺陷）
2. **用户操作后会怎样？** → `behaviors` 是空的（G2/G8 缺陷）
3. **到鸿蒙上怎么对应？** → 只有一个全局 placeholder（G7 缺陷）

更深层的问题是：这三类信息混在同一个扁平结构里，"缺了什么"和"该补什么"都看不清楚。

### 1.2 调研的启发

| 论文 | 教会我们什么 |
|------|-------------|
| SSDE（规约驱动工程） | Spec 要分级分层，不同层级服务不同目的 |
| StoryDroid（UI 故事板） | 动态组件可以通过静态分析代码中的 `addView`/`new XxxView` 提取出来，不需要跑 App |
| AMOGA（GUI 模型逆向） | 静态分析器生成事件列表 + 动态爬取探索状态，两者互补 |
| UITrans（A2H 翻译） | Android→HarmonyOS 的组件映射要做到逐组件级别，配合 RAG 和映射表 |

### 1.3 核心思路

把一个页面的 spec 拆成**三层**，每层回答一个问题：

| 层级 | 名称 | 回答的问题 | 一句话描述 |
|------|------|-----------|-----------|
| **L0** | 结构层 | 这个页面**有什么** | 控件树 + 动态组件 + Fragment |
| **L1** | 行为层 | 用户操作后**会怎样** | 事件→处理链→UI 反馈/跳转 |
| **L2** | 映射层 | 到鸿蒙上**怎么对应** | Android 组件→ArkUI 组件逐条映射 |

---

## 2. L0 结构层：这个页面有什么

### 2.1 现在的问题

两个毛病：

**毛病一：ui_elements 大面积为空。** 工具的 `static_xml.json` 里有 180 个 UI 元素（每条带 layout/id/tag/is_interactive），但没有回填到单页 spec。等于"全局表填了，单页视图没填"。

**毛病二：代码创建的控件完全看不见。** 有些控件不写在 XML 里，而是在 Java/Kotlin 代码里动态创建的：

```kotlin
// HomeActivity.onCreate() 里
val tabLayout = TabLayout(this)
tabLayout.addTab(tabLayout.newTab().setText("推荐"))
container.addView(tabLayout)
```

这个 TabLayout 在 XML 里不存在，所以我们的 XML 解析器扫不到。

**毛病三：0 个 Fragment。** 项目里有 13+ 个 Fragment（HomeFragment、RecommendFragment 等），但工具一个都没识别出来。

### 2.2 L0 信息模型

以 `HomeActivity` 为例，L0 结构层长这样：

```json
{
  "screen_id": "home_activity",
  "class": "HomeActivity",
  "screen_type": "activity",
  "layout": "activity_home",

  "ui_tree": {
    "tag": "ConstraintLayout",
    "id": "",
    "source": "static_xml",
    "children": [
      {
        "tag": "ViewPager2",
        "id": "viewpager_home",
        "source": "static_xml",
        "is_interactive": true,
        "children": []
      },
      {
        "tag": "LinearLayout",
        "id": "ll_bottom_tab",
        "source": "static_xml",
        "is_interactive": true,
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
    {
      "class": "HomeFragment",
      "container_id": "viewpager_home",
      "attach_method": "ViewPager2+FragmentStateAdapter",
      "position": 0,
      "source_file": "app/src/main/java/.../fragment/HomeFragment.kt"
    },
    {
      "class": "RecommendFragment",
      "container_id": "viewpager_home",
      "attach_method": "ViewPager2+FragmentStateAdapter",
      "position": 1,
      "source_file": "app/src/main/java/.../fragment/RecommendFragment.kt"
    },
    {
      "class": "WorkFragment",
      "container_id": "viewpager_home",
      "attach_method": "ViewPager2+FragmentStateAdapter",
      "position": 2
    },
    {
      "class": "MineFragment",
      "container_id": "viewpager_home",
      "attach_method": "ViewPager2+FragmentStateAdapter",
      "position": 3
    }
  ]
}
```

### 2.3 跟当前 spec 的关键区别

| | 当前 spec v1 | L0 结构层 |
|---|---|---|
| UI 元素 | 扁平数组 `ui_elements[]`，大面积为空 | **树形结构** `ui_tree`，保留 ViewGroup 嵌套关系 |
| 数据来源 | 只从 XML 解析 | XML + **代码动态组件**（标记 `source: "dynamic_code"`） |
| Fragment | 无 | 有，标记 `attach_method`（replace/add/ViewPager） |
| 层级关系 | 丢失（只知道有个 Button，不知道它在哪个容器里） | 保留（Button 在 LinearLayout 里，LinearLayout 在 ScrollView 里） |

### 2.4 怎么生成

**回填 static_xml（修复 G1）**：把 `static_xml.json` 里匹配该 layout 的元素回填进 `ui_tree`，按 XML 层级组装成树（`<include>` / `<merge>` 递归展开）。数据已经有了，只是没组装。

**动态组件检测（新增 `dynamic_ui_extractor.py`）**：

扫描策略（参考 StoryDroid Algorithm 2）：
1. 找到每个 Activity/Fragment 的 `onCreate()` / `onCreateView()` 方法体
2. 正则匹配三类调用：
   - `xxx.addView(yyy)` → 提取 yyy 的类型（往上找 `val yyy = XxxView(...)` 或 `new XxxView(...)`）
   - `LayoutInflater.inflate(R.layout.xxx)` → 记录引入了哪个 layout
   - `setAdapter(xxx)` → 回溯 adapter 的 item layout（记录 `⟨Activity, ViewType, ItemLayout⟩` 三元组）
3. 提取组件属性：`setText("xxx")`、`setHint("xxx")` 等 setter 调用
4. 合并进 `ui_tree`，标记 `source: "dynamic_code"` + `source_file` + `source_line`

边界：只扫描 `onCreate` / `onCreateView` / `initView` 等初始化方法（StoryDroid 也只做初始状态），运行时动态变化的不追踪。

**Fragment 识别（新增 `fragment_detector.py`）**：

识别三种模式：
1. `FragmentTransaction.replace(R.id.container, XxxFragment())` / `.add(...)` → 记录 Fragment 类名 + 容器 ID
2. `FragmentPagerAdapter` / `FragmentStateAdapter` 的 `getItem(position)` → 解析 `when(position)` 分支，提取各 tab 对应的 Fragment
3. `<fragment>` XML 标签（静态嵌入的 Fragment）

### 2.5 解决的问题

| 缺陷 | 解决方式 |
|------|---------|
| G1（ui_elements 为空） | `static_xml.json` 回填 + 动态组件检测 |
| G5（0 个 Fragment） | Fragment 识别器 |
| G4（layout↔class 不一致） | L0 同时记录 `class` 和 `layout`，消除歧义 |

---

## 3. L1 行为层：用户操作后会怎样

### 3.1 现在的问题

现在的 `ui_effect_paths.json` 是空的（`path_count: 0`）。`source_findings.json` 里有 70 个 `event_registration`（谁在哪注册了什么监听器），但缺少后续的**行为链**——注册了 `onClick`，然后呢？点击之后调了什么方法、弹了什么提示、跳到了哪个页面？这些全不知道。

具体来说：`static_xml.json` 里 180 个元素，87 个标记为可交互（按钮/输入框等），但只有 33 个匹配到了行为绑定，剩下 82 个（45%）不知道点了会发生什么。

### 3.2 L1 信息模型

还是用 `HomeActivity` 举例：

```json
{
  "screen_id": "home_activity",
  "event_bindings": [
    {
      "element_id": "tv_create_tab",
      "event_type": "click",
      "handler": {
        "method": "switchTab",
        "file": "app/src/main/java/.../HomeActivity.kt",
        "line": 156
      },
      "effect_chain": [
        { "step": "call", "target": "viewPager.setCurrentItem(0)", "line": 158 },
        { "step": "ui_update", "target": "tv_create_tab", "action": "setSelected(true)" },
        { "step": "ui_update", "target": "tv_template_tab", "action": "setSelected(false)" }
      ],
      "confidence": "static_analysis"
    },
    {
      "element_id": null,
      "event_type": "lifecycle",
      "handler": {
        "method": "showCampaignDialog",
        "file": "app/src/main/java/.../HomeActivity.kt",
        "line": 201
      },
      "effect_chain": [
        { "step": "condition", "expr": "campaignInfo != null" },
        { "step": "navigate", "destination": "CampaignDialog", "via": "Dialog()" }
      ],
      "confidence": "static_analysis"
    }
  ],

  "adapter_bindings": [
    {
      "container_id": "viewpager_home",
      "adapter_class": "HomeFragmentAdapter",
      "adapter_type": "FragmentStateAdapter",
      "items": [
        { "position": 0, "fragment": "HomeFragment" },
        { "position": 1, "fragment": "RecommendFragment" },
        { "position": 2, "fragment": "WorkFragment" },
        { "position": 3, "fragment": "MineFragment" }
      ]
    }
  ],

  "lifecycle_hooks": {
    "onCreate": ["initView", "initObserver", "initTabLayout"],
    "onResume": ["checkLoginStatus", "refreshCampaign"]
  },

  "data_observers": [
    {
      "observable": "renewManageViewModel.xxxLiveData",
      "observer_method": "initObserver",
      "effect": "更新 UI 显示或弹窗",
      "file": "app/src/main/java/.../HomeActivity.kt",
      "line": 120
    }
  ]
}
```

### 3.3 关键概念：effect_chain（效果链）

这是 L1 的核心数据结构。一个 effect_chain 描述的是：**用户触发某个事件后，从 handler 入口开始，依次发生了什么。**

每个 step 有这几种类型：

| step 类型 | 含义 | 示例 |
|-----------|------|------|
| `call` | 调用了某个方法 | `validate()`, `loginApi.login()` |
| `navigate` | 页面跳转 | `startActivity(HomeActivity)` |
| `ui_update` | 更新了 UI 状态 | `setVisibility(GONE)`, `setText("xxx")` |
| `ui_feedback` | 给用户的反馈 | `Toast.show("密码不能为空")`, `Dialog.show(...)` |
| `condition` | 条件分支 | `if (password.isBlank())` |
| `async` | 异步操作 | `api.call()`, `coroutine launch` |

一个完整的例子——LoginActivity 的登录按钮：

```json
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
  ]
}
```

Agent 看到这个，就知道：这个按钮点击后先校验、空密码弹 Toast、通过后调接口、成功跳首页、失败弹对话框。翻译成 ArkTS 时，每个 step 都有明确的对应物。

### 3.4 怎么生成

新增 `behavior_chain_extractor.py`，核心逻辑：

**输入**：`source_findings.json` 的 `event_registrations`（70 个）+ `call_graph.json`（2495 条调用边）

**追踪过程**：
1. 对每个 event_registration，找到 handler 方法（`enclosing_fn` 字段已经有了）
2. 从 handler 方法开始，沿 `call_graph` 向下追踪：
   - 遇到 `startActivity` / `findNavController().navigate` → 记录 `navigate` step
   - 遇到 `Toast.makeText` / `AlertDialog.Builder` → 记录 `ui_feedback` step
   - 遇到 `setVisibility` / `setText` / `setImageResource` → 记录 `ui_update` step
   - 遇到 `if` / `when` 包裹的上述调用 → 记录 `condition` step
   - 遇到普通方法调用 → 记录 `call` step 并继续追踪
3. **深度限制**：从 handler 入口最多追踪 3 层调用（防止爆炸）
4. **置信度标记**：
   - `static_analysis`：直接从代码模式匹配到的
   - `inferred`：从 call_graph 间接推断的（Kotlin lambda / 反射可能漏报）

**边界**：
- 只追踪同步调用链。`launch { }` 协程块里的内容标记 `async` 但不深入展开
- 不追踪跨进程（Broadcast / ContentProvider）
- LiveData / Flow 的 `observe` 回调只记录"观察了谁"，不追踪具体值变化

### 3.5 解决的问题

| 缺陷 | 解决方式 |
|------|---------|
| G2（ui_effect_paths 全空） | behavior_chain_extractor 生成完整的效果链 |
| G8（45% 元素无行为绑定） | 从 event_registration 出发追踪，覆盖率预期从 38% → 60%+ |
| G9（call_graph 无置信度） | 每个 step 标记 `confidence` |

---

## 4. L2 映射层：到鸿蒙上怎么对应

### 4.1 现在的问题

现在的 `harmony_arch.v1.json` 全是 placeholder：`bundle_name: "com.example.placeholder"`，37 条路由全是 `pages/xxxactivity/Index` 的模板。`framework_map.v1.json` 有 10 条映射规则，但粒度是框架级别（RecyclerView → List/Grid/WaterFlow），不是组件实例级别。

Agent 拿到这些信息后，还是得自己从头决定"LoginActivity 里的 EditText 在 ArkUI 里用什么组件"。

### 4.2 L2 信息模型

以 `LoginActivity` 为例：

```json
{
  "screen_id": "login_activity",
  "harmony_target": {
    "page_path": "pages/login/LoginPage",
    "ability": "EntryAbility",
    "module": "entry"
  },

  "component_mappings": [
    {
      "android": { "tag": "EditText", "id": "et_username", "hint": "用户名" },
      "arkui": {
        "component": "TextInput",
        "props": { "placeholder": "用户名" },
        "example": "TextInput({ placeholder: '用户名' }).onChange((value) => { this.username = value })"
      },
      "rule_id": "R-EditText-TextInput",
      "confidence": "rule_match"
    },
    {
      "android": { "tag": "ShapeImageView", "id": "iv_ppt_cover" },
      "arkui": {
        "component": "Image",
        "props": { "src": "$rawfile('placeholder.png')" }
      },
      "rule_id": "R-ImageView-Image",
      "confidence": "rule_match"
    },
    {
      "android": { "tag": "BannerViewPager", "id": "banner" },
      "arkui": null,
      "gap": {
        "status": "no_rule",
        "suggestion": "Swiper 组件",
        "reason": "BannerViewPager 是三方库，需要手动适配 Swiper + 自定义指示器"
      }
    }
  ],

  "navigation_mappings": [
    {
      "android": { "action": "startActivity", "destination": "HomeActivity" },
      "arkui": { "action": "router.pushUrl", "destination": "pages/home/HomePage" }
    },
    {
      "android": { "action": "Dialog()", "destination": "AppLoadDialog" },
      "arkui": { "action": "CustomDialogController.open", "destination": "AppLoadDialog 组件" }
    }
  ],

  "state_mappings": [
    {
      "android": { "pattern": "LiveData<Boolean>", "usage": "loginSuccess" },
      "arkui": { "pattern": "@State + onChange", "usage": "loginSuccess: boolean" }
    }
  ]
}
```

### 4.3 跟当前 framework_map 的关键区别

| | 当前 framework_map | L2 映射层 |
|---|---|---|
| 粒度 | 框架级（RecyclerView → List/Grid） | **组件实例级**（`et_username` EditText → TextInput） |
| 覆盖 | 10 条全局规则 | 每个 screen 的每个组件都有映射或 gap 标记 |
| 导航 | 全是 placeholder 路由 | 真实的路由路径 + 跳转方式映射 |
| gap 处理 | 只说"这个没映射" | 说明**为什么没映射** + 给出 suggestion |

### 4.4 怎么生成

**逐组件映射**（扩展现有 `data/framework_map/rules.yaml`）：

1. 遍历 L0 的 `ui_tree`，对每个控件查 `rules.yaml` 的映射规则
2. 规则匹配到 → 输出 `component_mappings` 条目，标记 `confidence: "rule_match"`
3. 规则没匹配到 → 输出 gap 条目，附 `suggestion`（基于控件类名的模糊推荐）
4. 导航映射：从 L1 的 `effect_chain` 中提取 `navigate` step，结合目标页面类名生成路由

**gap 反馈回流**（UITrans 的持续优化思路）：

当 Agent 或人工在 `llm_out/` 成功填补了某个 gap 后：
1. 将成功的映射写入 `data/framework_map/candidate_rules.yaml`（候选规则池）
2. 候选规则经人工 review 后可提升为正式规则
3. 下次运行时，之前的 gap 自动变成 `rule_match`

这样 `rules.yaml` 会随着项目使用**越来越完善**，而不是永远是那 10 条。

### 4.5 解决的问题

| 缺陷 | 解决方式 |
|------|---------|
| G7（harmony_arch 全 placeholder） | 逐组件级映射替代全局占位 |
| 映射表静态不增长 | gap 反馈回流机制 |

---

## 5. 可选：叙事层（给人看的）

上面三层是给 Agent 和工具消费的结构化数据。如果需要给 PM 或 QA 看的人类可读文档，可以从 L0 + L1 确定性编译出 Gherkin 式场景描述：

```
## LoginActivity — 登录页面

### 场景 1: 登录成功
  Given 用户在登录页
  When 输入用户名和密码，点击「登录」按钮
  Then 调用登录接口
  And 成功后跳转到首页 (HomeActivity)

### 场景 2: 密码为空
  Given 用户在登录页
  When 密码为空时点击「登录」按钮
  Then 显示提示「密码不能为空」
```

编译规则很简单：
- `click` + `navigate` → "When 点击「{label}」Then 跳转到 {destination}"
- `click` + `ui_feedback(Toast)` → "When 点击「{label}」Then 显示提示「{message}」"
- `condition` → 分出不同场景

这一层**不是必需品**。Agent 直接读 L1 的 JSON 就够了。但它可以放进 viewer 给人审查用，也可以作为验收测试用例的基础。标记为 **optional**，不纳入核心 pipeline。

---

## 6. 单页 Spec v2 格式

三层整合到一起，替代当前的 `*_spec.json`：

```json
{
  "spec_version": "2.0",
  "screen_id": "home_activity",
  "class": "HomeActivity",
  "screen_type": "activity",
  "layout": "activity_home",

  "L0_structure": {
    "ui_tree": { "...": "见 §2.2" },
    "fragments": [ "...": "见 §2.2" ]
  },

  "L1_behavior": {
    "event_bindings": [ "...": "见 §3.2" ],
    "adapter_bindings": [ "...": "见 §3.2" ],
    "lifecycle_hooks": { "...": "见 §3.2" },
    "data_observers": [ "...": "见 §3.2" ]
  },

  "L2_projection": {
    "harmony_target": { "...": "见 §4.2" },
    "component_mappings": [ "...": "见 §4.2" ],
    "navigation_mappings": [ "...": "见 §4.2" ]
  },

  "navigation": {
    "entry_points": [],
    "exit_points": []
  },

  "stats": {
    "total_elements": 6,
    "static_elements": 6,
    "dynamic_elements": 0,
    "interactive": 2,
    "with_behavior_binding": 2,
    "fragments": 4,
    "scenarios_compilable": 3,
    "projection_coverage": 0.83,
    "gaps": 1
  }
}
```

**兼容策略**：
- `spec_version: "2.0"` 区分新旧格式
- v1 消费方可忽略 `L0_` / `L1_` / `L2_` 前缀字段，`navigation` 字段保持不变
- `stats` 新增多个指标，让消费方一眼看出这个 spec 的覆盖质量

---

## 7. Pipeline 改造

### 7.1 数据流

```
Stage 0 (bundled_spec_tools/main.py)
  ├── xml_extractor        → static_xml.json          ─┐
  ├── source_extractor     → source_findings.json       │
  ├── function_graph       → call_graph.json            ├─→ L0 结构层
  ├── navigation_extractor → navigation_graph.json      │
  ├── [新增] dynamic_ui_extractor  → dynamic_ui.json   ─┘
  └── [新增] fragment_detector     → fragments.json    ─┘

  ├── [新增] behavior_chain_extractor                  ─→ L1 行为层
  │     (输入: source_findings + call_graph + navigation_graph)

Stage 2 (framework_map)
  └── [增强] 逐组件映射                                ─→ L2 映射层
        (输入: L0 ui_tree + rules.yaml)

[新增] Stage 0.5 (generate_specs_v2)
  └── 整合 L0 + L1 + L2 → specs_v2/*_spec.json
```

### 7.2 新增 / 修改的文件

| 文件 | 类型 | 作用 |
|------|------|------|
| `bundled_spec_tools/extractors/dynamic_ui_extractor.py` | 新增 | 扫描代码中的 addView/new XxxView，输出动态组件 |
| `bundled_spec_tools/extractors/fragment_detector.py` | 新增 | 识别 Fragment 子类 + 挂载方式 |
| `bundled_spec_tools/extractors/behavior_chain_extractor.py` | 新增 | 从 event_registration 追踪效果链 |
| `bundled_spec_tools/generate_specs.py` | 修改 | 升级为 v2 格式，整合三层数据 |
| `data/framework_map/rules.yaml` | 扩展 | 增加组件实例级映射规则 |
| `schemas/spec.v2.schema.json` | 新增 | v2 spec 的 JSON Schema |

### 7.3 跟 feature_tree 的关系

分层 spec 是 feature_tree 节点的**数据来源**，不是替代品：

- feature_tree 的 `ui_control` 节点 ← 读 L0 的 `ui_tree`
- feature_tree 的 `behavior` 节点 ← 读 L1 的 `event_bindings`（新增 `effect_chain_ref` 指向具体 binding）
- feature_tree 的 `projection.harmony` ← 读 L2 的 `component_mappings`

两者的分工：**spec 是单页视图**（一个页面的完整规约），**feature_tree 是全局视图**（跨页面的功能域拓扑图）。

---

## 8. 分期实施

| 期次 | 做什么 | 解决什么 | 工作量 | 前置依赖 |
|------|--------|---------|--------|---------|
| **P0** | L0 基础：`static_xml` 回填 `ui_tree`（树形化）+ `fragment_detector` | G1, G5 | 中 | 无 |
| **P1** | L0 增强：`dynamic_ui_extractor` 动态组件检测 | G1 动态部分 | 中 | P0 |
| **P2** | L1 行为层：`behavior_chain_extractor` | G2, G8 | 大 | P0（需要 ui_tree 来关联 element_id） |
| **P3** | L2 映射层：逐组件映射 + gap 反馈回流 | G7 | 中 | P0（需要 ui_tree 遍历） |
| **P4** | Spec v2 整合 + Schema + 测试 | 整体产出升级 | 中 | P0-P3 |

**P0 是地基**——不把 `ui_tree` 做好，L1 的 behavior 和 L2 的 mapping 都没有锚点可以挂。

### 8.1 验收标准

| 指标 | 当前值 | P0 后 | P2 后 | P4 后 |
|------|--------|-------|-------|-------|
| 单页 spec ui_elements 非空率 | ~20% | 90%+ | 90%+ | 90%+ |
| Fragment 识别数 | 0 | 13+ | 13+ | 13+ |
| 有 effect_chain 的交互元素占比 | 0% | 0% | 55%+ | 55%+ |
| 有组件级映射的 UI 元素占比 | 0% | 0% | 0% | 70%+ |
| spec_version | 1.0 | 1.0 | 1.0 | **2.0** |

---

## 9. 参考

### 9.1 调研论文

| 论文 | 我们借鉴了什么 |
|------|--------------|
| [SSDE] LLM-Assisted Repository-Level Generation with Structured Spec-Driven Engineering | 分级分层的规约树理念 |
| [StoryDroid] Automated Generation of Storyboard for Android Apps (ICSE 2019) | Algorithm 2：动态组件检测 + 合并回静态 layout |
| [AMOGA] A Hybrid Approach for Reverse Engineering GUI Model from Android Apps | 静态分析器 + 动态爬取互补的思路 |
| [UITrans] Android to HarmonyOS UI Translation | 逐组件映射表 + RAG 增强 + 自底向上语义解释 |

### 9.2 内部文档

| 文档 | 角色 |
|------|------|
| [docs/FEATURE_TREE_AND_VIEWER_DESIGN.md](FEATURE_TREE_AND_VIEWER_DESIGN.md) | feature_tree 设计（本文档的上游消费者） |
| [analsis/02-tool-gaps.md](../../analsis/02-tool-gaps.md) | 工具缺陷清单 G1-G10 |
| [analsis/03-code-level-analysis.md](../../analsis/03-code-level-analysis.md) §7 | 优化路线图 P0-P3 |
| [analsis/04-a2h-spec-phase0-integration.md](../../analsis/04-a2h-spec-phase0-integration.md) | 消费侧当前怎么兜底这些缺陷 |
