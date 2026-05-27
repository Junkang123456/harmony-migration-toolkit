# Toolkit Stage 0 改进 — P0-P2 实现计划

## Context

当前 Toolkit 的 Stage 0 静态分析有三个核心缺陷：
1. **ui_elements 大面积为空** — `static_xml.json` 有 180 个元素但 spec 没回填，且不保留树形结构
2. **0 个 Fragment** — `navigation_extractor.py` 只识别 Activity/Dialog
3. **行为链为空** — 知道"谁注册了 onClick"，但不知道"点击后发生什么"

按 `docs/LAYERED_SPEC_DESIGN.md` 的分期方案，P0-P2 分别解决这三个问题：
- **P0**: ui_tree 回填（树形化） + fragment_detector
- **P1**: dynamic_ui_extractor（代码中动态创建的控件）
- **P2**: behavior_chain_extractor（事件→效果链追踪）

## 依赖关系

```
P0-A (ui_tree 回填)  ─────┐
                           ├──→ P1 (dynamic_ui) ──→ Spec v2 整合
P0-B (fragment_detector) ─┘
                           
P0-A + P0-B ──→ P2 (behavior_chain) ──→ Spec v2 整合
```

---

## P0-A: ui_tree 回填（修改 xml_extractor.py）

### 问题
`xml_extractor.py` 的 `extract_layout()` 用 `tree.iter()` 扁平遍历 XML，丢掉了所有父子嵌套关系。

### 方案
在 `xml_extractor.py` 中新增 `extract_layout_tree()` 函数，递归构建树形结构。现有的 `elements` 扁平列表**保留不动**（向后兼容），新增 `layout_trees` 字典。

### 修改文件
- **`bundled_spec_tools/extractors/xml_extractor.py`**

### 实现细节

**新增函数**：
```python
def extract_layout_tree(xml_path: Path, source_prefix="static_xml_layout",
                        file_prefix="", include_resolver=None) -> dict | None:
    """返回一棵树而不是扁平列表。"""

def _build_tree_node(elem, xml_path, source_prefix, file_prefix, include_resolver) -> dict:
    """递归构建树节点。"""
```

**树节点 schema**：
```python
{
    "tag": "ConstraintLayout",
    "id": "root_container",
    "text": "", "hint": "", "content_desc": "",
    "is_interactive": False,
    "visibility": "visible",
    "source": "static_xml",
    "on_click_attr": "",
    "children": [...]   # 递归
}
```

**`<include>` 处理**：遇到 `<include layout="@layout/xxx">`，用 `include_resolver(layout_name)` 找到 XML 文件，解析后内联为子节点。

**`<merge>` 处理**：被 include 的 layout 根节点如果是 `<merge>`，把它的 children 直接提升到父节点。

**`include_resolver` 构建**：在 `run()` 中，用所有 `res_dirs` 构建一个 `layout_name → Path` 的解析器。

**`run()` 返回值变更**：
```python
return {
    "elements": all_elements,       # 不变
    "layout_trees": layout_trees,   # 新增: {layout_name: tree_node}
    "stats": stats,
    "strings": strings,
}
```

---

## P0-B: fragment_detector.py（新增）

### 问题
`navigation_extractor.py` 只识别 Activity 和 Dialog，不识别 Fragment 子类和 FragmentTransaction。

### 方案
新增 `fragment_detector.py`，检测 4 种 Fragment 挂载模式。

### 新建文件
- **`bundled_spec_tools/extractors/fragment_detector.py`**

### 接口
```python
def run(project_root: str, dep_roots: list[str] | None = None,
        file_prefix: str = "") -> dict:
    """返回 {"fragments": [...], "stats": {...}}"""
```

### 4 种检测模式

**模式 1 — FragmentTransaction.replace/add**：
```python
_FRAGMENT_TX_RE = re.compile(
    r'\.(?:replace|add)\s*\(\s*R\.id\.(\w+)\s*,\s*(\w+(?:Fragment|BottomSheet))\s*[\.(]',
    re.MULTILINE,
)
```
输出：`{class, container_id, attach_method: "FragmentTransaction.replace", line}`

**模式 2 — FragmentPagerAdapter/FragmentStateAdapter 的 getItem/createFragment**：
```python
_FRAGMENT_ADAPTER_CLASS_RE = re.compile(
    r'class\s+(\w+)\s*[^{]*:\s*(?:FragmentPagerAdapter|FragmentStateAdapter|FragmentStatePagerAdapter)',
)
_POSITION_BRANCH_RE = re.compile(r'(\d+)\s*->\s*(\w+Fragment)\s*\(')
```
输出：每个 position 一条记录，`attach_method: "ViewPager2+FragmentStateAdapter"`

**模式 3 — XML 静态 `<fragment>` 标签**：
从 layout XML 中找 `<fragment android:name="com.xxx.YyyFragment">`。

**模式 4 — Fragment 子类声明**：
```python
_FRAGMENT_CLASS_RE = re.compile(
    r'class\s+(\w+)\s*[^{]*:\s*'
    r'(?:Fragment|DialogFragment|BottomSheetDialogFragment|PreferenceFragmentCompat)\s*\(',
)
```
输出：`attach_method: "class_declaration"`，作为兜底。

**去重**：同一个 Fragment class 出现在多个 pattern 中时，保留信息最丰富的（transaction > adapter > xml > declaration）。

---

## P1: dynamic_ui_extractor.py（新增）

### 问题
代码中 `addView()` / `new XxxView()` 创建的控件在 XML 里不存在，扫不到。

### 新建文件
- **`bundled_spec_tools/extractors/dynamic_ui_extractor.py`**

### 接口
```python
def run(project_root: str, dep_roots: list[str] | None = None,
        file_prefix: str = "") -> dict:
    """返回 {"dynamic_elements": [...], "adapter_layouts": [...], "stats": {...}}"""
```

### 3 种检测模式

**模式 1 — addView**：
```python
_ADD_VIEW_RE = re.compile(r'(\w+)\s*\.\s*addView\s*\(\s*(\w+)')
```

**模式 2 — inflate 后 addView**：
```python
_INFLATE_AND_ADD_RE = re.compile(
    r'(?:val|var)\s+(\w+)\s*=\s*\w+\.inflate\s*\(\s*R\.layout\.(\w+)'
)
```

**模式 3 — setAdapter 追踪 item layout**：
```python
_SET_ADAPTER_RE = re.compile(r'(\w+)\s*\.\s*(?:adapter\s*=|setAdapter\s*\()\s*(\w+Adapter)')
```

**作用域限制**：只扫描 `onCreate` / `onCreateView` / `initView` / `initUI` / `setupView` 方法体。

---

## P2: behavior_chain_extractor.py（新增）

### 问题
知道"btn_login 注册了 onClick"（event_registration），不知道"点击后发生什么"（效果链）。

### 新建文件
- **`bundled_spec_tools/extractors/behavior_chain_extractor.py`**

### 接口
```python
def run(source_findings: dict, call_graph: dict,
        project_root: str, file_prefix: str = "") -> dict:
    """返回 {"behavior_chains": [...], "stats": {...}}"""
```

### 核心算法

**第一步：建图索引**
```python
calls_by_from: {symbol_id: [call_edges]}
symbols_by_id: {symbol_id: symbol_record}
```

**第二步：对每个 event_registration**
1. 找到 handler 方法（用 `enclosing_symbol_id`）
2. 读 handler 的源码文本

**第三步：沿 call_graph 向下追踪（最多 3 层）**

| 分类 | 匹配模式 | 示例 |
|------|---------|------|
| `navigate` | `startActivity`, `navigate`, `findNavController` | 页面跳转 |
| `ui_feedback` | `makeText`, `Toast`, `AlertDialog`, `Snackbar`, `showDialog` | 用户提示 |
| `ui_update` | `setVisibility`, `setText`, `setEnabled`, `isVisible`, `beGone` | UI 状态变更 |
| `async` | `launch`, `async`, `withContext`, `enqueue` | 异步操作 |
| `call` | 其他用户方法 | 继续递归追踪 |

**第四步：条件分支检测**

**约束**：深度限制 3 层、visited set 防循环、不追踪协程内部、不追踪跨进程。

---

## main.py 集成

在现有 7 步流程中插入新步骤：

```python
# Step 4b: Fragment 检测 (新增)
frag_result = fragment_detector.run(project_root, dep_roots=dep_roots)

# Step 4c: 动态 UI 检测 (新增)
dyn_result = dynamic_ui_extractor.run(project_root, dep_roots=dep_roots)

# Step 4d: 行为链提取 (新增)
bc_result = behavior_chain_extractor.run(src_result, call_graph_payload, project_root)

# Step 7: generate_specs — 传入新数据，输出 v2 spec
generate_all_specs(nav, gt, flat, dag, specs_dir,
    layout_trees=xml_result.get("layout_trees"),
    fragments=frag_result.get("fragments"),
    dynamic_ui=dyn_result.get("dynamic_elements"),
    behavior_chains=bc_result.get("behavior_chains"),
    spec_version="2.0")
```

---

## generate_specs.py 改造（Spec v2 输出）

当 `spec_version == "2.0"` 时，在每个 spec 中增加：
- `L0_structure.ui_tree` — 从 `layout_trees[layout_name]` 取
- `L0_structure.fragments` — 从 `fragments` 过滤 container_id 在本 layout 中的
- `L1_behavior.event_bindings` — 从 `behavior_chains` 过滤本 screen 的 element
- 保留所有 v1 字段（向后兼容）

---

## 产物变化详情（按阶段）

### 当前产物基线（v1）

Stage 0 当前输出 14 个文件，核心数据结构：

**`static_xml.json`**：
```json
{
  "elements": [{"source":"...","file":"...","layout":"...","tag":"...","id":"...","text":"...","hint":"...","content_desc":"...","on_click_attr":"...","visibility":"...","is_interactive":true}],
  "stats": {"total":180,"interactive":95,"hidden_by_default":12,"by_source":{...}},
  "strings": {"app_name":"AntennaPod",...}
}
```

**`specs/xxx_spec.json`**（v1 格式）：
```json
{
  "screen_id":"activity_main","class":"MainActivity","layout":"activity_main",
  "screen_type":"activity","source":"project",
  "ui_elements":[{"id":"...","type":"...","label":"...","visibility":"...","condition":"...","is_interactive":true,"behaviors":[...]}],
  "behaviors":[{"trigger":"...","element_id":"...","action":"...","outcome":"...","file":"..."}],
  "dynamic_ui":[...],
  "navigation":{"entry_points":[...],"exit_points":[...]},
  "stats":{"total_elements":12,"interactive":5,"with_behavior":3,"conditional_visibility":1,"dynamic_gaps":2,"nav_out":3,"nav_in":1}
}
```

---

### P0 阶段产物变化

#### 变更文件：`static_xml.json`

**新增字段：`layout_trees`**（与 `elements` 同级）：
```json
{
  "elements": [...],
  "layout_trees": {
    "activity_main": {
      "tag": "ConstraintLayout",
      "id": "",
      "text": "", "hint": "", "content_desc": "",
      "is_interactive": false,
      "visibility": "visible",
      "source": "static_xml",
      "on_click_attr": "",
      "children": [
        {
          "tag": "FrameLayout", "id": "fragment_container",
          "is_interactive": false, "children": []
        },
        {
          "tag": "Button", "id": "btn_settings", "text": "Settings",
          "is_interactive": true, "children": []
        }
      ]
    }
  },
  "stats": {"...", "layout_tree_count": 25},
  "strings": {...}
}
```

**`layout_trees` 特性**：
- key = layout 文件名（不含 `.xml`），value = 递归树节点
- `<include layout="@layout/xxx">` 内联解析
- `<merge>` 根节点 children 提升到 include 点
- 节点字段与 `elements[]` 一致，额外增加 `children[]`

#### 新增文件：`fragments.json`

```json
{
  "fragments": [
    {
      "class": "SettingsFragment",
      "container_id": "fragment_container",
      "attach_method": "FragmentTransaction.replace",
      "host_class": "MainActivity",
      "host_file": "app/src/.../MainActivity.kt",
      "line": 35,
      "source_file": "app/src/.../MainActivity.kt"
    }
  ],
  "stats": {
    "total": 15,
    "by_attach_method": {
      "FragmentTransaction.replace": 5,
      "ViewPager2+FragmentStateAdapter": 3,
      "xml_fragment_tag": 1,
      "class_declaration": 4
    },
    "source_files_scanned": 120
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `class` | string | Fragment 类名 |
| `container_id` | string | 挂载容器的 `android:id`，class_declaration 模式为空 |
| `attach_method` | string | 检测模式 |
| `host_class` | string | 宿主类名 |
| `host_file` | string | 宿主文件路径 |
| `line` | int | 挂载代码行号 |
| `position` | int? | 仅 ViewPager 模式 |
| `source_file` | string | Fragment 源文件 |

---

### P1 阶段产物变化

#### 新增文件：`dynamic_ui.json`

```json
{
  "dynamic_elements": [
    {
      "view_type": "TextView",
      "variable_name": "errorLabel",
      "container_variable": "rootLayout",
      "container_id": "root_container",
      "creation_method": "addView",
      "host_class": "LoginActivity",
      "host_method": "onCreate",
      "file": "app/src/.../LoginActivity.kt",
      "line": 52,
      "properties": {"text": "Login failed"}
    }
  ],
  "adapter_layouts": [
    {
      "adapter_class": "EpisodeListAdapter",
      "item_layout": "item_episode",
      "host_class": "EpisodeListActivity",
      "host_variable": "recyclerView",
      "host_id": "episode_list",
      "file": "app/src/.../EpisodeListAdapter.kt",
      "line": 15
    }
  ],
  "stats": {
    "total_dynamic_elements": 8,
    "total_adapter_layouts": 5,
    "by_creation_method": {"addView":3,"inflate+addView":2,"new+addView":3},
    "source_files_scanned": 120
  }
}
```

---

### P2 阶段产物变化

#### 新增文件：`behavior_chains.json`

```json
{
  "behavior_chains": [
    {
      "element_id": "btn_login",
      "view_ref": "btnLogin",
      "event_type": "click",
      "handler": {
        "symbol_id": "fn:com.example.LoginActivity.onLoginClick/0",
        "method": "onLoginClick",
        "file": "app/src/.../LoginActivity.kt",
        "line": 85
      },
      "effect_chain": [
        {"step":"call","target":"validate()","line":86,"confidence":"static_analysis"},
        {"step":"condition","expr":"password.isBlank()","line":87,
         "then":[{"step":"ui_feedback","action":"Toast.makeText","args":["密码不能为空"],"line":88}],
         "else":[
           {"step":"async","target":"loginApi.login()","line":91},
           {"step":"navigate","destination":"HomeActivity","via":"startActivity","line":93}
         ]}
      ],
      "chain_depth": 2,
      "confidence": "static_analysis"
    }
  ],
  "stats": {
    "total_bindings":70,"with_effect_chain":42,"without_handler":8,
    "max_chain_depth":3,
    "by_step_type":{"call":85,"navigate":23,"ui_feedback":15,"ui_update":31,"async":18,"condition":12}
  }
}
```

**`effect_chain[]` 步骤类型**：
| step 类型 | 额外字段 | 说明 |
|-----------|---------|------|
| `call` | `target`, `symbol_id`, `line` | 调用用户方法 |
| `navigate` | `destination`, `via`, `line` | 页面跳转 |
| `ui_feedback` | `action`, `args`, `line` | 用户提示 |
| `ui_update` | `action`, `target_view`, `line` | UI 状态变更 |
| `async` | `target`, `line` | 异步操作（不再深入） |
| `condition` | `expr`, `line`, `then[]`, `else[]` | 条件分支 |

#### 变更文件：`specs/xxx_spec.json`（v1 → v2 升级）

v2 新增字段（v1 全部保留）：
```json
{
  "spec_version": "2.0",
  "L0_structure": {
    "ui_tree": {"tag":"...","id":"...","children":[...]},
    "fragments": [{"class":"...","container_id":"...","attach_method":"..."}],
    "dynamic_elements": [{"view_type":"...","creation_method":"...","container_id":"...","properties":{}}]
  },
  "L1_behavior": {
    "event_bindings": [
      {"element_id":"...","event_type":"click","handler_method":"...","effect_chain":[...],"chain_depth":1}
    ]
  },
  "stats": {
    "...v1 fields...",
    "fragments": 2,
    "dynamic_elements": 1,
    "event_bindings": 3,
    "event_bindings_with_chain": 2
  }
}
```

---

### 产物一览表

| 文件 | P0 | P1 | P2 | 变化类型 |
|------|:--:|:--:|:--:|---------|
| `static_xml.json` | **改** | - | - | 新增 `layout_trees` 字段 |
| `fragments.json` | **新** | - | - | 全新文件 |
| `dynamic_ui.json` | - | **新** | - | 全新文件 |
| `behavior_chains.json` | - | - | **新** | 全新文件 |
| `specs/*.json` | - | - | **改** | v1→v2：新增 `spec_version` + `L0_structure` + `L1_behavior` |
| 其他 12 个文件 | - | - | - | 不变 |

---

## 关键文件清单

| 文件 | 操作 | 内容 |
|------|------|------|
| `bundled_spec_tools/extractors/xml_extractor.py` | 修改 | 新增 `extract_layout_tree()` + `run()` 返回 `layout_trees` |
| `bundled_spec_tools/extractors/fragment_detector.py` | 新建 | 4 种 Fragment 检测模式 |
| `bundled_spec_tools/extractors/dynamic_ui_extractor.py` | 新建 | addView / inflate / setAdapter 3 种模式 |
| `bundled_spec_tools/extractors/behavior_chain_extractor.py` | 新建 | 事件→效果链追踪（call_graph 3 层遍历） |
| `bundled_spec_tools/main.py` | 修改 | 插入 Step 4b/4c/4d，传新数据给 generate_specs |
| `bundled_spec_tools/generate_specs.py` | 修改 | 接受新数据，输出 v2 格式 |
| `fixtures/minimal_android/...` | 修改/新增 | 加 Fragment、Button、click listener |
| `tests/test_pipeline.py` | 修改 | 加 P0/P1/P2 的断言 |

## 测试策略

### Fixture 扩展

在 `fixtures/minimal_android/` 中增加：
1. **SettingsFragment.kt**：继承 Fragment 的最小 stub
2. **修改 activity_main.xml**：加 `<FrameLayout android:id="@+id/fragment_container">`、`<Button android:id="@+id/btn_settings">`
3. **修改 MainActivity.kt**：加 Fragment transaction + Button click listener

### 测试断言

**P0**：layout_trees 非空、fragments.json 至少 1 条记录
**P1**：dynamic_ui.json 存在
**P2**：behavior_chains.json 至少 1 条带 effect_chain 的记录
**既有断言**：全部保持通过

## 执行流程

**按阶段推进，每阶段完成后停下等用户验证通过再开始下一阶段。每个阶段内小步 git 提交。**

1. **P0 阶段** → 完成后停下，用户验证
2. **P1 阶段** → 完成后停下，用户验证
3. **P2 阶段** → 完成后停下，用户验证

提交粒度：
- P0：3 次提交（P0-A xml_extractor / P0-B fragment_detector / P0 fixture+test+pipeline）
- P1：2 次提交（dynamic_ui_extractor / fixture+test）
- P2：2 次提交（behavior_chain_extractor / fixture+test + generate_specs v2）

## 验证

1. `pytest tests/test_pipeline.py` 全部通过
2. 在 WordPress Android 项目上运行 pipeline：
   - `fragments.json` 识别 13+ 个 Fragment
   - `dynamic_ui.json` 捕获动态创建的控件
   - `behavior_chains.json` 的 `with_effect_chain / total_bindings > 50%`
   - `specs/` 中 v2 spec 包含非空 `L0_structure.ui_tree` 和 `L1_behavior.event_bindings`
