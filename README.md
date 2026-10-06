# 数控刀路禁入区审查服务

基于 **FastAPI + PostgreSQL** 的 HTTP 服务，用于登记毫米制二维禁入区并对 G-code 刀路进行连续碰撞审查，出具不可变审查报告。

## 功能概览

- 禁区以**毫米制二维多边形 + 闭合 Z 高度范围 `[z_min, z_max]`** 登记；
- 禁区几何采用**不可变版本**：每次修改生成新版本，旧版本永久保留；
- 审查输入：禁区编号及**明确版本**、毫米制初始刀尖坐标 `(x, y, z)`、初始刀具半径（mm）、G-code 文本；
- **可选多刀半径映射**（请求中 `"tool_radii": {刀号: 半径mm}` 启用）：同一份 G-code 可使用多把刀，
  见下文「多刀审查」；不传映射时 `T` 仍被忽略，单刀请求与旧报告完全兼容；
- G-code 模态解析：`G0/G1/G2/G3`、`G17`、`G20/G21`、`G90/G91`；缺省轴沿用前值；
  - `G20`（英寸）坐标自动换算为毫米（×25.4），`G21` 为默认公制；
  - 圆弧仅支持 **XY 平面恒定 Z**；`I/J` 始终**相对弧起点**；同起终点 + 非零 I/J 表示**整圆**；
- 按程序顺序检查每一段连续运动：
  - 刀尖 Z 位于禁区闭合高度范围内，**且**
  - 水平路径到填充多边形的距离 **不大于刀具半径**
  - 即判定碰撞，**边界接触也算碰撞**；
  - 线段通过二次不等式**精确求解路径-多边形距离区间**，圆弧通过精确弧-边距离 + 二分定位，
    **不会因只采样端点而漏掉途中碰撞**；
- 报告记录：所用禁区版本、输入摘要（行数、运动段数、起止点、刀半径、G-code SHA-256）、
  **首个碰撞的 G-code 原行号与禁区编号**，或无碰撞结论；
- **可选全程最小水平净间隙**（请求中 `"include_min_clearance": true` 启用）：
  对每段运动仅在刀尖 Z 处于该禁区**闭合高度范围**内的部分，求连续 XY 路径到填充多边形的
  最小距离减刀具半径；直线、圆弧、整圆及运动途中进入高度范围均精确覆盖。
  **刀具外沿恰好接触边界（距离 = 刀半径）时净间隙为 0**；刀尖路径落在填充多边形内部/
  边界上（距离 0）时净间隙为 **-刀半径（负）**。返回毫米制最小净间隙、对应原行号、
  刀尖坐标 `[x, y, z]` 与禁区编号；等值时按**运动顺序 → 段内进度 → 禁区编号**选取；
  全程不在任何禁区高度内则结果为 `null`；无运动段按**初始刀位**计算。
  该结果**随报告固化**，且与首碰撞结论相互独立；
- **多刀审查**（可选 `tool_radii` 映射）：首碰撞与最小净间隙均按**事件发生时生效刀具的半径**
  计算，报告对两种命中分别固化原行号、**刀号与半径**（初始刀位的刀号为 `null`）；
  等值先按**执行顺序**，再按既有段内进度与禁区编号裁决。未传映射时 `T` 忽略，旧行为不变；
- **多刀执行事件轨迹**（`tool_radii` 启用时随报告固化，见下文「执行事件轨迹」）：
  按执行顺序记录**初始刀位、每次 T 切换时的静止刀位（含与运动同行）与每段运动**，
  每项含原行号、刀号、半径、起止坐标与命中禁区编号（或空），可逐事件回看；
  首碰撞即轨迹中**首个命中事件**，可选最小净间隙沿同一事件顺序裁决；
- **批量审查**（`POST /reviews/batch`，见下文「批量审查」）：一次提交带唯一
  `batch_id` 的 **1～50 项有序任务**，每项独立给出禁区版本、初始刀尖坐标、刀具半径、
  G-code 及可选最小净间隙/多刀参数；**同一事务**中先校验整批并固定各项禁区版本快照，
  任一项非法返回**逐项错误**且整批不落报告，全部通过才按输入顺序生成不可变报告；
  相同 `batch_id` 重放按内容判定幂等/冲突；
- 报告**一旦生成不可变**，后续禁区更新新版本不影响任何旧报告；
- 非法几何、负/非法刀具半径、非法圆弧、不支持的运动/坐标系指令、非法开关类型，以及多刀模式下
  非整数/负数 `T`、同行多个 `T`、未映射刀号、非法映射半径一律返回 `422`，**不留任何报告**。

## 一键启动（Docker Compose）

要求：Docker 与 Docker Compose v2。

```bash
docker compose up --build
```

启动后：

- API 服务地址：`http://localhost:8080`
- 健康检查：`GET http://localhost:8080/health`
- 交互式 API 文档（Swagger UI）：`http://localhost:8080/docs`
- OpenAPI 描述：`http://localhost:8080/openapi.json`

首次启动时应用会自动连接 PostgreSQL 并完成数据库表初始化（`create_all`），无需手工建表；
对**升级前的旧库**会幂等补齐 `reports` 表的最小净间隙列（`ALTER TABLE ... ADD COLUMN`），
v1.2 的多刀命中刀号/半径列（`collision_tool_no` / `collision_tool_radius`），
以及 v1.3 的执行事件轨迹列（`execution_trace`）；v1.4 新增的 `batches` 批量审查表
由 `create_all` 对旧库幂等建出。
旧报告读取为 `min_clearance_enabled=false`、`min_clearance=null`，多刀字段为 `null`，
`execution_trace=null`，旧请求行为不变。

停止并保留数据：

```bash
docker compose down
```

彻底清空数据库卷：

```bash
docker compose down -v
```

## 配置

通过环境变量配置（见 `.env.example`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://cnc:cnc_password@db:5432/cnc_audit` | PostgreSQL 连接串（SQLAlchemy + psycopg v3） |
| `HOST` | `0.0.0.0` | 服务监听地址 |
| `PORT` | `8080` | 服务监听端口，容器内固定暴露 `8080` |

`docker-compose.yml` 中包含：

- `db`：`postgres:16-alpine`，库名/用户/密码 `cnc_audit` / `cnc` / `cnc_password`，数据卷 `pgdata`，带健康检查；
- `api`：本仓库构建的镜像，等待数据库健康后启动。

本地非容器运行：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="postgresql+psycopg://cnc:cnc_password@localhost:5432/cnc_audit"
uvicorn app.main:app --host 0.0.0.0 --port 8080
# 也可用 SQLite 做本地试验：
# export DATABASE_URL="sqlite:///./cnc_audit.db"
```

## HTTP API

### 1. 登记禁区（版本 1）

`POST /zones`

```json
{
  "zone_no": 1,
  "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
  "z_min": -5.0,
  "z_max": 5.0,
  "description": "机床基座（毫米）"
}
```

`zone_no` 省略时自动分配递增编号。多边形至少 3 个不共线、不重合顶点，不允许自相交/自接触；`z_min <= z_max`。非法几何返回 `422`，编号冲突返回 `409`。

### 2. 禁区列表 / 详情（含全部版本）

- `GET /zones`
- `GET /zones/{zone_no}`

### 3. 登记不可变新版本（修改几何）

`POST /zones/{zone_no}/versions`，请求体同几何部分；版本号自动在上一版本基础上 +1，旧版本不变。

### 4. 提交刀路审查

`POST /reviews`

```json
{
  "zones": [{"zone_no": 1, "version": 1}],
  "start": [-5.0, 5.0, 0.0],
  "tool_radius": 0.5,
  "gcode": "G21 G90 G17\nG1 X15 Y5 Z0\n",
  "include_min_clearance": true,
  "tool_radii": {"1": 0.5, "2": 2.0}
}
```

`include_min_clearance` 可省略（默认 `false`，行为与旧版完全一致），且只接受布尔值
（`true`/`false`，传 `"true"`/`1` 等一律 `422`）。`tool_radii` 可省略（默认不传，单刀模式），
见下文「多刀审查」。

碰撞响应示例：

```json
{
  "report_id": 1,
  "used_zone_versions": {"1": 1},
  "input_summary": {"gcode_lines": 2, "nonblank_lines": 2, "motion_count": 1,
                    "start_position_mm": [-5.0, 5.0, 0.0],
                    "tool_radius_mm": 0.5,
                    "final_position_mm": [15.0, 5.0, 0.0],
                    "gcode_sha256": "..."},
  "has_collision": true,
  "collision_line_no": 2,
  "collision_zone_no": 1,
  "conclusion": "collision",
  "message": "发现碰撞：G-code 第 2 行与禁区编号 1 冲突"
}
```

无碰撞时 `conclusion` 为 `"no_collision"`，两个碰撞字段为 `null`。G-code 解析错误返回 `422`：

```json
{"detail": {"error": "invalid_gcode", "line_no": 1,
            "message": "第 1 行: 不支持的 G 代码 G54（...）"}}
```

#### 全程最小水平净间隙（可选）

启用后，提交响应与 `GET /reports/{report_id}` 均额外包含：

- `min_clearance_enabled`：该报告是否启用了计算；
- `min_clearance`：结果对象或 `null`（全程不在任何禁区高度内）：

```json
{
  "min_clearance_enabled": true,
  "min_clearance": {
    "min_clearance_mm": 0.5,
    "line_no": 1,
    "point_mm": [0.0, 11.0, 0.0],
    "zone_no": 1,
    "progress": 0.25
  }
}
```

字段含义：

| 字段 | 说明 |
| --- | --- |
| `min_clearance_mm` | 最小**净间隙**（mm）= 连续 XY 路径到填充多边形最小距离 − 刀具半径。刀具外沿恰好接触边界（距离 = 刀半径）时为 `0`；刀尖路径位于填充多边形内部/边界（距离 0）时为负（`-刀半径`） |
| `line_no` | 取得最小值运动段的 **G-code 原行号**；无运动段（按初始刀位）时为 `null` |
| `point_mm` | 取得最小值时的**刀尖坐标** `[x, y, z]`（mm） |
| `zone_no` | 对应**禁区编号** |
| `progress` | 段内进度：直线为线性参数 `t∈[0,1]`，圆弧为弧长进度 `s∈[0,1]`（整圆自起点计） |

口径要点：

- 每段运动只统计刀尖 Z 位于**该禁区**闭合高度范围 `[z_min, z_max]` 内的部分
  （Z 斜进/斜出时按线性插值精确截取窗口，窗口边界本身计入）；
- 直线段按分段二次函数**精确求极值**，圆弧/整圆按弧点-边候选角解析求值；
  不做端点采样；
- 多个候选净间隙相等时，依次按**运动段顺序**、**段内进度**、**禁区编号**取最小；
- 该结果**不改变**首碰撞的结论、行号与禁区编号（二者独立计算）；
- 结果在报告创建时**固化**：后续 `GET /reports/{id}` 反复查询一致，禁区再出新版本不影响旧报告；
- 未启用的请求与升级前已生成的旧报告：`min_clearance_enabled=false`、`min_clearance=null`，
  旧客户端解析不受影响。

### 5. 多刀审查（可选：`tool_radii` 刀号 → 半径映射）

`POST /reviews` 请求可选传入 `"tool_radii"`：**非负整数刀号**到**非负有限毫米半径**的映射，
JSON 键写为十进制整数字符串（如 `"2"`；`"1.5"`、`"-1"`、`"abc"` 均拒绝）。
传入（含空映射 `{}`）即启用多刀模式；**不传**该字段时保持单刀旧行为，`T` 解析后忽略。

```json
{
  "zones": [{"zone_no": 1, "version": 1}],
  "start": [-5.0, 11.0, 0.0],
  "tool_radius": 0.0,
  "tool_radii": {"1": 0.5, "2": 2.0},
  "gcode": "G1 X-2 Y11 Z0\nT2\nG1 X15 Y11 Z0\n",
  "include_min_clearance": true
}
```

语义：

- **初始刀位**仍使用请求里的 `tool_radius`（初始刀的刀号为空）；首个 `Tn` 之前的运动沿用初始刀；
- `Tn` 在**其所在行先于运动生效**：同行运动按新半径检查；随后运动沿用该刀直至下次切换；
- **每次 T 切换**（含与运动同行）都按**新半径**检查切换时的静止刀位
  （静止点也可能因半径变大而碰撞）；同行 T 与运动**分别检查**：
  先按新半径检查换刀点（本行运动前刀位），再检查本行运动；
- 首碰撞与可选全程最小净间隙均按**事件发生时生效刀具的半径**计算；
- 报告对两种命中分别固化 **G-code 原行号、刀号、半径**：
  - 响应/报告新增首碰撞字段 `collision_tool_no`（初始刀位为 `null`）、`collision_tool_radius_mm`；
  - `min_clearance` 对象在多刀模式下新增 `tool_no`（初始刀位为 `null`）、`tool_radius_mm`；
  - 单刀模式与旧报告：首碰撞两字段为 `null`，`min_clearance` 中**不出现**这两个键；
- 等值裁决：先按**执行顺序**（初始刀位 → 各静止换刀/运动事件），再按既有**段内进度**、
  **禁区编号**；
- `input_summary` 在多刀模式下额外固化 `tool_map_enabled: true` 与 `tool_radii_mm` 快照。

多刀模式下以下输入一律 `422`（错误体为 `invalid_gcode`，携带原行号），且**不生成报告**：

- `T` 非整数或负数（如 `T1.5`、`T-1`）；
- 同一行出现多个 `T`（如 `T1 T2`）；
- `T` 刀号未在 `tool_radii` 中映射（空映射时任何 `T` 都拒绝）；
- 映射本身非法：刀号非非负整数、半径为负/非有限/非数值（如 `"1": -0.5`、`"x": 1`）。

### 6. 执行事件轨迹（多刀模式随报告固化）

多刀模式（传入 `tool_radii`）下，提交响应与 `GET /reports/{report_id}` 均包含
`execution_trace`：**按执行顺序**记录的可回看事件轨迹——

1. **初始刀位**（`kind="initial"`，`seq=0`，行号与刀号为 `null`，半径为 `tool_radius`）；
2. **每次 T 切换时的静止刀位**（`kind="tool_change"`，含与运动同行的 T：
   位置为本行运动前刀位，起止坐标相同，执行顺序**先于**本行运动）；
3. **每段运动**（`kind="motion"`，含起止刀尖坐标）。

```json
[
  {"seq": 0, "kind": "initial", "line_no": null, "tool_no": null,
   "tool_radius_mm": 0.0, "start_mm": [-5.0, 11.0, 0.0], "end_mm": [-5.0, 11.0, 0.0],
   "hit_zone_no": null},
  {"seq": 1, "kind": "motion", "line_no": 1, "tool_no": null,
   "tool_radius_mm": 0.0, "start_mm": [-5.0, 11.0, 0.0], "end_mm": [-2.0, 11.0, 0.0],
   "hit_zone_no": null},
  {"seq": 2, "kind": "tool_change", "line_no": 2, "tool_no": 2,
   "tool_radius_mm": 2.0, "start_mm": [-2.0, 11.0, 0.0], "end_mm": [-2.0, 11.0, 0.0],
   "hit_zone_no": null},
  {"seq": 3, "kind": "motion", "line_no": 3, "tool_no": 2,
   "tool_radius_mm": 2.0, "start_mm": [-2.0, 11.0, 0.0], "end_mm": [15.0, 11.0, 0.0],
   "hit_zone_no": 1}
]
```

每项字段：

| 字段 | 说明 |
| --- | --- |
| `seq` | 执行顺序（初始刀位为 0，其余从 1 起递增；同行 T 的换刀事件先于本行运动） |
| `kind` | `initial` / `tool_change` / `motion` |
| `line_no` | G-code **原行号**；初始刀位为 `null` |
| `tool_no` | 该事件生效**刀号**；初始刀位为 `null` |
| `tool_radius_mm` | 该事件生效刀具**半径**（mm） |
| `start_mm` / `end_mm` | 事件起止刀尖坐标 `[x, y, z]`（mm）；静止事件（初始刀位/换刀）起止相同 |
| `hit_zone_no` | 该事件**命中的禁区编号**（运动段内多禁区取最早命中），未命中为 `null` |

口径要点：

- 同行 T 与运动**分别记录**：先按新半径检查换刀点（`tool_change`），再检查运动（`motion`）；
- **首碰撞对应轨迹中首个 `hit_zone_no` 非空的事件**（其行号/刀号/半径即
  `collision_line_no` / `collision_tool_no` / `collision_tool_radius_mm`）；
- 可选**最小净间隙沿同一事件顺序**裁决（等值先执行顺序，再段内进度、禁区编号）；
- 轨迹在报告创建时**固化**：后续 `GET /reports/{id}` 反复查询一致，
  禁区再出新版本不影响旧报告轨迹；
- **单刀请求**（未传 `tool_radii`）与**升级前的旧报告**：`execution_trace` 为 `null`，
  旧客户端解析不受影响；
- 非法 `T` 仍按原规则 `422` 拒绝且**不留任何报告**（自然也不产生轨迹）。

### 7. 批量审查（一次提交 1～50 项有序任务，`batch_id` 幂等）

`POST /reviews/batch`

```json
{
  "batch_id": "B-20261006-001",
  "items": [
    {"task_id": "pocket-1",
     "zones": [{"zone_no": 1, "version": 1}],
     "start": [-5, 5, 0], "tool_radius": 0.5,
     "gcode": "G21 G90 G17\nG1 X15 Y5 Z0\n",
     "include_min_clearance": true},
    {"task_id": "drill-2",
     "zones": [{"zone_no": 1, "version": 1}],
     "start": [-5, 11, 0], "tool_radius": 0.0,
     "tool_radii": {"2": 2.0},
     "gcode": "G1 X-2 Y11 Z0\nT2\nG1 X15 Y11 Z0\n"}
  ]
}
```

- `batch_id`：批次幂等键，必填；字母数字开头，可含 `_ . : -`，最长 64。
- `items`：**1～50 项**有序任务；每项为完整的单项审查输入（同 `POST /reviews`），
  可另带可选 `task_id`（批内唯一，校验规则同 `batch_id`；省略时按输入顺序号标识）。
  每项独立使用最小净间隙与多刀参数，任务之间互不影响。
- **同一事务**：先逐项校验并固定各项引用的禁区版本快照，全部通过才按**输入顺序**
  生成不可变报告并统一提交；任一项几何、G-code、参数或任务标识非法时，
  返回 `422` 与**逐项错误**（`failed_items` 仅列失败项，含 `index`/`task_id`/`error`），
  **整批不生成任何报告**，且该 `batch_id` 不被占用（修正后可同名重提）：

```json
{
  "error": "invalid_batch",
  "batch_id": "B-20261006-001",
  "message": "批量审查未通过：1 项非法，整批未生成任何报告",
  "failed_items": [
    {"index": 1, "task_id": "drill-2",
     "error": {"error": "invalid_gcode", "line_no": 2, "message": "第 2 行: ..."}}
  ]
}
```

- 全部通过返回 `201`，逐项给出输入顺序号、任务标识与完整单项审查结果
  （含 `report_id`，可继续 `GET /reports/{report_id}` 回看）：

```json
{
  "batch_id": "B-20261006-001",
  "item_count": 2,
  "results": [
    {"index": 0, "task_id": "pocket-1",
     "result": {"report_id": 11, "conclusion": "collision",
                "has_collision": true, "collision_line_no": 2,
                "collision_zone_no": 1, "...": "..."}},
    {"index": 1, "task_id": "drill-2",
     "result": {"report_id": 12, "conclusion": "no_collision", "...": "..."}}
  ]
}
```

- **幂等重放**：相同 `batch_id` + 相同内容再次提交返回 `200` 与**首次逐项结果**
  （`report_id` 不变，不生成新报告）；相同 `batch_id` + 不同内容返回 `409`
  （`batch_conflict`）且不生成新报告。内容按逐项规范化请求计算 SHA-256 判定；
  并发同 `batch_id` 由数据库唯一约束兜底，结果一致。

### 8. 查询报告（不可变）

`GET /reports/{report_id}`

## 命令行调用示例

另见 `examples/curl_examples.sh` 与 `examples/python_client.py`。

```bash
# 登记禁区
curl -s -X POST http://localhost:8080/zones \
  -H 'Content-Type: application/json' \
  -d '{"zone_no":1,"polygon":[[0,0],[10,0],[10,10],[0,10]],"z_min":-5,"z_max":5}'

# 提交审查
curl -s -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' \
  -d '{"zones":[{"zone_no":1,"version":1}],"start":[-5,5,0],
       "tool_radius":0.5,"gcode":"G21 G90 G17\nG1 X15 Y5 Z0\n"}'

# 多刀审查：初始刀半径 0，T2 换 r=2 的刀后横穿（碰撞行固化刀号/半径）
curl -s -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' \
  -d '{"zones":[{"zone_no":1,"version":1}],"start":[-5,11,0],
       "tool_radius":0.0,"tool_radii":{"2":2.0},
       "gcode":"G1 X-2 Y11 Z0\nT2\nG1 X15 Y11 Z0\n"}'

# 多刀执行事件轨迹：同行 T 与运动分别记录，先查换刀点再查运动；
# execution_trace 随报告固化，GET /reports/{id} 可反复回看
curl -s -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' \
  -d '{"zones":[{"zone_no":1,"version":1}],"start":[0,11,0],
       "tool_radius":0.0,"tool_radii":{"1":2.0},
       "gcode":"T1 G1 X5 Y11 Z0\n"}'
# 响应 execution_trace 依次为 initial / tool_change（命中）/ motion（命中），
# 首碰撞即首个命中事件（第 1 行换刀点，刀号 1）
```

## G-code 支持范围与拒绝策略

| 代码 | 含义 | 处理 |
| --- | --- | --- |
| `G0` / `G1` | 快速 / 直线插补（X/Y/Z） | 检查连续运动，Z 变化时按高度区间精确求交 |
| `G2` / `G3` | 顺 / 逆圆弧，`I/J` 相对弧起点 | 仅 G17 XY、恒定 Z；半径不一致、I=J=0 等拒绝；同起终点为整圆 |
| `G17` | XY 平面 | 默认；`G18/G19` 拒绝 |
| `G20` / `G21` | 英寸 / 毫米 | 默认 G21；G20 数值自动换算毫米 |
| `G90` / `G91` | 绝对 / 增量 | 默认 G90 |
| 其他 G | `G52`–`G59`、固定循环（如 `G81`）、`G28` 等 | **明确拒绝（422）** |
| `R` 圆弧半径格式 | — | **明确拒绝（422）**，仅接受 I/J |
| `F/S/N/M` 等 | 进给/转速/行号/辅助功能 | 解析后忽略，不参与几何 |
| `T` | 刀号 | **单刀模式**（未传 `tool_radii`）：解析后忽略；**多刀模式**：非负整数、同行至多一个且必须已映射，`Tn` 先于本行运动生效；每次 T 切换（含同行）都按新半径检查切换时静止刀位并记入执行事件轨迹 |

支持分号行注释与圆括号注释（括号不平衡将拒绝）；`%` 程序标记忽略；地址字大小写不敏感；
行号报告使用**原始物理行号（从 1 开始）**。

## 项目结构

```
app/
  main.py        # FastAPI 路由：禁区/版本/审查/批量审查/报告
  schemas.py     # 请求响应模型与输入校验（含执行事件轨迹与批量输出）
  models.py      # zones / zone_versions / reports / batches 表
  database.py    # 引擎与会话
  gcode.py       # G-code 词法/模态解析（含多刀 T 切换与静止换刀事件）
  geometry.py    # 多边形校验、点/线段/圆弧距离、弧-多边形最近点
  collision.py   # 连续运动碰撞引擎（精确区间求解 + 圆弧二分）+ 全程最小净间隙
                 # + 多刀执行事件轨迹（按执行顺序检查）
examples/        # curl 与 Python 调用示例
tests/test_audit.py
Dockerfile
docker-compose.yml
```

## 测试

```bash
pip install httpx
python -m unittest tests.test_audit -v
```

测试使用 SQLite 内存文件覆盖数据库，共覆盖几何校验、线段/圆弧碰撞、模态解析、
版本不可变、旧报告不变、非法输入不落报告、可选全程最小净间隙（直线/圆弧/整圆/
Z 窗口截取/边界接触为零/负值/无运动段/等值裁决/固化与兼容），多刀半径映射
（同行/独立 T 生效、模态延续、初始刀位空刀号、静止换刀命中、按事件半径计算首碰撞与净间隙、
等值执行顺序裁决、非法 T/非法映射 422 不落报告、旧库列迁移与单刀兼容），
多刀执行事件轨迹（同行 T 与运动分别记录且换刀点先查、轨迹顺序与字段、
首碰撞对应首个命中事件、最小净间隙沿同一顺序裁决、轨迹随报告固化且不受禁区新版本影响、
单刀请求与旧报告为 null），以及批量审查（有序逐项结果与报告编号、各项独立引用版本快照、
任一项非法 422 逐项错误且整批不落报告、失败批次不占用 batch_id、任务/批次标识校验、
1～50 项边界、相同 batch_id 重放返回首次结果、不同内容 409 冲突且不生成新报告）等场景。
