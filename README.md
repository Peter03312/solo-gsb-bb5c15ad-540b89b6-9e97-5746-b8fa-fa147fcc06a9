# 临时活动无线电频率申请 API

面向**临时活动**（演出、赛事、应急保障等）的无线电使用申请与审批服务。
使用 **Python + FastAPI + PostgreSQL** 构建，提供可直接调用的 HTTP API。

- 申请包含：平面坐标中的**覆盖圆**、频率**闭区间**、UTC **左闭右开**时段
- 非法申请（半径非正 / 频段下界大于上界 / 起点不早于终点）**整笔拒收**
- 干扰裁决：两项**有效**批准仅在 **时段相交 且 覆盖区域相交**，并且 **频段相交或最短频率间隔小于非负保护带宽** 时构成干扰
- 乐观修订号控制并发：预览返回当前修订号，批准必须携带该号并重新裁决；
  修订号过期或与并发批准相撞，均**不得占用频段**并返回原因
- **整组批准**：多设备可携带同一预期修订号与**非空、无重复**的申请 ID 列表一次性
  申请；锁内同时核对“当前有效许可”与“组内两两”干扰，任一项失败**整组不批准、
  修订号不变**；全部通过时按输入顺序为每项写入批准事件与连续修订号，一次提交
- **可用调频位置查询**：为待审批申请在候选频段下界闭区间内寻找可用位置——覆盖圆、
  UTC 时段与原频段宽度固定（允许单频点），按当前修订快照的有效许可与生效保护带宽
  裁决，返回全部极大的可用连续范围并标明两端是否可取；查询不改任何状态，结果与
  单笔预览在任一可取下界上的干扰结论一致
- **撤销只改变后续裁决所用的**有效集合**；按修订号查询可还原当时全部批准、暂停、
  恢复、撤销记录（整组中的每个修订号都可单独还原截至该项获批时的有效集合）
- **临时停机暂停 / 恢复**：活动设备临时停机时，运营人员可按申请 ID **暂停**已获批且
  未撤销的许可——暂停立即退出有效集合、保留原批准，故障解除后**恢复原许可，无需重提
  申请**。暂停与恢复均携带预期修订号，与批准、撤销、保护带宽变更**共用同一修订号
  裁决**；恢复按**当前保护带宽与全部有效许可**重新裁决干扰。修订过期返回当前修订号、
  恢复互扰返回冲突对象，两者都**保持原状态且不占修订号**；成功各占一个修订号。暂停
  期间仍可用现有撤销操作**永久撤销**，撤销后不能恢复
- **保护带宽在线调整**：`GUARD_BAND` 环境变量仅作全新数据库的初值；之后通过
  `POST /api/config/guard-band` 在线变更——先预览（新带宽下当前全部有效许可之间的
  干扰许可对，不改状态），再携同一拟定值与预期修订号确认。确认与单笔/整组批准、
  撤销**共用同一修订号裁决**；修订号过期或拟定值使任意有效许可对互扰即拒绝
  （带宽、有效集合、修订号均不变）；通过只产生**一个**修订号，后续预览与批准统一
  使用新带宽。历史修订查询可还原当时带宽与有效集合，已有批准记录保存的裁决带宽
  不被追改，重启不覆盖已生效设置
- **候补队列（活动频段暂被占用）**：运营人员可把**待审**申请登记为候补（FIFO），
  并查询或取消候补；同一申请不得重复登记，申请不存在或不是待审状态一律拒绝；
  取消候补**不改变申请状态**。登记与取消**不占修订号**，登记返回排队顺序与当前
  修订号。已生效许可被**暂停或撤销**后，在该操作的**同一事务与同一修订号仲裁**内
  按登记先后、用**当前保护带宽**逐项重裁候补：可通过者即时批准并立即加入有效集合
  （占用后续连续修订号），仍有干扰者保留候补并继续检查后续申请。**手动批准**（单笔
  或整组）会先退队，手动批准者不得再次自动批准。暂停/撤销本身仍只占原有的**一个**
  修订号，响应列出本次自动获批与仍候补的申请 ID、未获批的冲突明细及最终修订号；
  释放失败或事务回滚时队列、许可与修订号均不变。历史按各修订号逐项还原自动审批
  结果（标记 `auto_approved` 与触发它的释放修订号）
- **临时频谱管制窗口**：运营人员以**覆盖圆 + 频率闭区间 + UTC 左闭右开时段 + 原因
  + 预期修订号**先做只读**预演**（列出命中的当前有效许可与候补申请及所据修订号，
  不改状态），再**确认**启用窗口（占一个修订号）。窗口与申请的圆、时段、频段
  **三者都相交才命中**——频段端点相碰算相交、时段端点相接不算、圆相切算相交；
  **保护带宽不扩张窗口**。确认后**既有有效许可继续有效**，但从该修订起，待审预览、
  单笔/整组批准、暂停恢复与释放后的候补自动审批都报告命中窗口，**写操作不得使
  命中申请生效**（未命中者照常处理）。可凭**窗口 ID 与预期修订号解除**：解除再占
  一个修订号，**只影响后续裁决、不主动审批候补**。启用/解除与所有既有写操作
  **共用同一修订号仲裁**；已过期窗口、非法范围与无效窗口 ID 一律拒绝且不改许可、
  队列和修订号。历史修订查询与对比按各修订号**还原当时的管制窗口及其启用/解除
  变动**，原批准事件不追改
- **活动设备换机：许可交接**：换机时运营人员把一张**当前有效许可**交接给一份
  **待审申请**——输入原许可 ID、接替申请 ID 与原因先做**只读预演**（返回当前
  修订号、**按现行保护带宽排除原许可后**接替申请仍面对的干扰对象与命中的生效
  管制窗口，不改状态），再携**预期修订号**确认。确认在**共同修订仲裁**内重算：
  仅当原许可仍有效、接替申请仍待审、修订号未变且排除原许可后**无其他干扰、无
  窗口命中**时才**整笔生效**——原许可**撤销**与接替申请**批准**占用**两个连续
  修订号**；接替申请若在候补则**先退队**（其批准为手动批准，不标记
  `auto_approved`），交接后再按**现有 FIFO 规则重裁其他候补**（自动批准占后续
  连续修订号，`release_revision` 指向交接撤销修订号）。修订过期、状态变化或
  冲突均**不改许可、队列和修订号**。撤销与批准仍是原有事件与语义，另追加不可变
  `license_transfer_events` 关联两个修订号；历史查询与对比能**逐项还原每个修订号**
  的状态，重启后状态不变

---

## 1. 一键启动（Docker Compose）

前置条件：Docker 与 Docker Compose v2。

```bash
docker compose up --build
```

启动后：

- API 地址：<http://localhost:8000>
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- OpenAPI JSON：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/health>
- PostgreSQL：`localhost:5432`（库/用户/密码均为 `radio`）

容器启动时 API 会自动等待数据库就绪，然后**建表并初始化修订号 0**，
无需手工执行迁移。数据保存在命名卷 `radio_pgdata` 中；清空重来：

```bash
docker compose down -v
```

---

## 2. 本地开发运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 方式 A：使用本机 PostgreSQL
export DATABASE_URL="postgresql+psycopg2://radio:radio@localhost:5432/radio"
export GUARD_BAND="2.0"
uvicorn app.main:app --reload

# 方式 B：直接用 SQLite（免装数据库；行锁退化为唯一约束兜底，仅供本地/测试）
export DATABASE_URL="sqlite:///./radio.db"
export GUARD_BAND="2.0"
uvicorn app.main:app --reload
```

运行测试（自动使用临时 SQLite 库，共 170+ 个用例）：

```bash
pip install -r requirements-dev.txt
pytest -q
```

---

## 3. 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg2://radio:radio@localhost:5432/radio` | SQLAlchemy 数据库连接串 |
| `GUARD_BAND` | `0` | **非负有限**保护带宽的**初始值**，仅在建库（或从旧版升级）时写入一次；生效值存于数据库，之后用 `POST /api/config/guard-band` 在线变更，**重启不会覆盖已生效设置**。与频率数字同单位（示例统一用 MHz，即 2.0 = 2 MHz） |
| `APP_HOST` | `0.0.0.0` | 监听地址 |
| `APP_PORT` | `8000` | 监听端口 |
| `SCHEDULED_APPROVAL_POLL_SECONDS` | `0.5` | 预约自动审批后台轮询间隔（秒，最小钳制为 0.05）；到期处理、启动补处理与手动 `process-due` 共用同一仲裁逻辑 |
| `SCHEDULED_APPROVAL_BACKGROUND` | `1` | 是否在本进程内运行到期轮询循环（`0`/`false` 关闭；关闭后仍有启动补处理与 `POST /api/scheduled-approvals/process-due` 可用） |

`docker-compose.yml` 中已为容器设好上述变量；频率单位由调用方自行保持一致，
API 不关心具体单位（坐标与半径同理，如均为米）。

---

## 4. 数据模型与裁决规则

### 申请内容

```json
{
  "name": "城南音乐节",
  "circle":      {"cx": 0.0, "cy": 0.0, "radius": 1000.0},
  "frequency":   {"low": 100.0, "high": 110.0},
  "time_window": {"start": "2026-10-01T08:00:00Z", "end": "2026-10-01T12:00:00Z"}
}
```

- **覆盖圆**：圆心 `(cx, cy)` + 半径 `radius`，平面欧氏距离；
  两圆“圆心距 ≤ 半径之和”即相交（恰好相切算相交）。
- **频率闭区间** `[low, high]`：`low == high`（单一频点）合法；
  端点相碰（`110` 对 `[110,120]`）**算相交**。
- **UTC 左闭右开时段** `[start, end)`：时间须带时区（推荐 `Z`）；
  一个时段的结束恰为另一个的开始（`10:00` 对 `[10:00, 12:00)`）**不算相交**。

### 整笔拒收（HTTP 422，一次返回全部违例）

- `circle.radius <= 0` → `radius_not_positive`
- `frequency.low > frequency.high` → `frequency_low_above_high`（相等允许）
- `time_window.start >= time_window.end` → `time_start_not_before_end`（相等也拒绝）
- 非有限数值 → `value_not_finite`；不带时区的时间 → `naive_datetime`

### 干扰成立条件（三项同时满足）

1. `time_intersect`：左闭右开时段相交；
2. `area_intersect`：覆盖圆相交；
3. 频率满足下列之一：
   - `frequency_intersect`：闭区间频段相交（最短间隔视为 0）；
   - `within_guard_band`：频段不相交，但**最短间隔 < 保护带宽**
     （间隔**恰好等于**保护带宽时允许，即“严格小于”）。

裁决只考虑**有效集合**：已批准、当前未暂停且未被撤销的申请。待审批、已暂停、
已撤销均不参与。

### 裁决前的数据库候选集收窄（不漏判的预过滤）

申请量增长后，单笔、整组、恢复、候补、预约到期及紧急占用的每条裁决链都不再先
把全部有效许可和生效管制窗口逐条装入内存，而是**先在数据库内按时段、覆盖范围
和频段缩小候选集，再沿用上面的精确干扰与窗口命中规则**逐条判定：

- **时段**：用与裁决一致的左闭右开区间重叠条件（`start < other_end` 且
  `other_start < end`）过滤，端点相接的不装入；
- **覆盖范围**：用圆心坐标差不超过“两半径之和”的**外接方框必要条件**过滤
  （含极小的相对容差，防止数据库浮点舍入把相切对滤掉）；精确的圆心距判定仍在
  内存中执行；
- **频段**：许可干扰按 `[freq_low − 保护带宽, freq_high + 保护带宽]` 的闭区间
  重叠过滤——**保护带宽只扩张许可的干扰范围**；管制窗口命中要求闭区间真实相交，
  **保护带宽不扩张管制窗口**；
- 整组裁决取整组成员的**并集包络**；
- 可用频段下界查询中候选下界遍历整个闭区间 `[candidate_low_min,
  candidate_low_max]`，许可包络覆盖 `[candidate_low_min − 保护带宽,
  candidate_low_max + 带宽 + 保护带宽]`，**下界查询覆盖整个候选下界区间**，
  任何在某一可达下界处可能冲突的记录都会保留；
- 收窄只允许**多取**（多取的无关记录由精确规则丢弃，冲突明细顺序仍按申请/窗口
  id 排序，与全量扫描完全一致），**绝不漏掉任何可能冲突的记录**；同事务内刚获批
  的许可（候补 drain、紧急解除恢复、预约到期批量处理）仍在内存中按原追加顺序
  参与后续裁决；
- **保护带宽变更（预演与确认）例外**：仍装入并两两检查**全部当前有效许可**，
  不做候选收窄；
- `applications` 与 `restriction_windows` 上建有状态/时段、频段、覆盖范围索引
  （旧库启动时幂等补建），大量时段互不相交的有效许可不会再被逐条装入裁决，
  服务启动与后台预约扫描保持可用。

### 临时停机：暂停与恢复

活动设备临时停机时，运营人员无需撤销许可或重提申请，可对已获批（`approved`）且
未撤销的申请做**暂停 / 恢复**：

- `POST /api/applications/{id}/suspend`，请求体
  `{"expected_revision": n, "reason": "可选原因"}`：
  许可**立即退出有效集合**（之后预览、批准、整组批准、可用调频查询、带宽预览均不再
  计入它），但原批准事件保持不变；成功占一个修订号，返回状态、暂停修订号与原批准
  修订号。
- 故障解除后 `POST /api/applications/{id}/resume`，请求体同上：服务在修订号锁内按
  **当前生效保护带宽与全部有效许可**对该许可**重新裁决**——
  - 通过 → 占一个修订号，许可重新进入有效集合，响应返回状态、新修订号、
    **原批准修订号**（许可本身不是新申请）与本次恢复所用带宽快照；
  - 修订号过期 → **409 `stale_revision`**（附当前修订号），许可**保持暂停**；
  - 恢复时与有效许可互扰 → **409 `interference`**（响应体附冲突对象及触发条件，
    结构与单笔批准相同），许可同样**保持暂停**。
  - 两种拒绝都**不占用修订号**、不改变有效集合；移除冲突对象（如撤销新许可）后可用
    同一修订号重新恢复。
- 暂停与恢复和单笔/整组批准、撤销、带宽变更共用同一把修订号锁与同一条修订号序列。
- 暂停期间仍可用现有 `POST .../revoke` **永久撤销**（撤销响应附
  `"was_suspended": true`）；撤销后不能恢复。重复暂停、对非暂停许可恢复、对
  待审批申请暂停、对已撤销许可暂停或恢复都被明确拒绝（见错误码表）。
- 暂停 / 恢复各自追加不可变事件（`suspension_events` / `resumption_events`）；
  恢复事件保存本次裁决带宽快照，**原批准事件不追改**。历史修订查询按批准→暂停→
  恢复→撤销的完整时间线还原各修订的有效集合与暂停集合。

### 候补队列：频段暂被占用时的待审申请排队

活动频段暂被占用、待审申请因干扰无法立刻批准时，运营人员可把它**登记为候补**，
由系统在后续释放发生时按登记先后自动重裁。

**登记 / 查询 / 取消（均不占修订号）**

- `POST /api/applications/{id}/waitlist`，请求体可省略或为 `{"reason": "可选"}`：
  申请必须**存在且仍为待审批（pending）**，同一申请不得重复登记。成功返回
  `waitlisted`、`queue_position`（从 1 开始的 FIFO 位置）、`current_revision`、
  `registered_at`；申请状态保持 `pending`。
- 查询：`GET /api/waitlist` 返回整条队列（登记先后）与当前修订号；
  `GET /api/applications/{id}/waitlist` 返回单个申请的排队位置（未登记返回 404）。
  `GET /api/revision/current` 也附 `waitlist_application_ids` / `waitlist_entries`。
- 取消：`POST /api/applications/{id}/waitlist/cancel`：从队列移除，**不改变申请
  状态**（仍为 pending）、不占修订号，返回取消时的当前修订号。取消后可重新登记。
- 拒绝情形：未知 ID → 404 `not_found`；重复登记 → 409 `already_waitlisted`；
  对非待审申请登记 → 409 `not_pending`；取消/查询一个未在队列中的申请 →
  409 `not_waitlisted`（取消）或 404（单项查询）。

**释放时自动重裁（暂停 / 撤销同一事务内）**

已生效许可被**暂停或撤销**时，在该操作**同一事务、同一修订号锁**内，操作本身先
占用其原有的**一个**修订号（释放修订号），随后：

1. 按登记先后逐项取出候补，用**当前生效保护带宽**与“截至此刻的全部有效许可”
   （含本次刚自动获批者）重裁——裁决规则与单笔预览完全相同；
2. 无冲突 → **即时批准**，退出候补、加入有效集合，占用**下一个连续修订号**，
   继续处理后续候补；
3. 仍有干扰 → **保留候补**（位置不变），并继续检查队列中的下一项。

暂停/撤销响应在**保持原有字段兼容**（`revision` 仍是该释放操作自己的修订号）的
基础上追加：

- `auto_approved_application_ids` / `auto_approvals`：本次自动获批的申请 ID（登记
  顺序）及各自的修订号、所用保护带宽、`release_revision`（触发它的释放修订号）；
- `remaining_waitlist_application_ids`：仍处候补的申请 ID；
- `waitlist_pending[].conflicts`：每个未获批申请的冲突对象与触发条件（结构同单笔
  预览）；
- `final_revision`：本次事务结束后的最终修订号（= 释放修订号 + 自动获批数）。

**其余规则**

- **手动批准即退队**：单笔 `approve` 或整组 `batch-approve` 成功时，命中的候补项
  先从队列删除，因此手动批准者**不会**在之后的释放中被再次自动批准。
- 自动获批与普通批准写入**同一条不可变批准事件流**，仅额外标记
  `auto_approved: true` 与 `release_revision`；按其中任一修订号查询
  `GET /api/revision/{n}` 即可逐项还原自动审批结果（有效集合、批准记录的
  `status_as_of`），原手动/整组批准字段保持不变。
- **恢复（resume）与保护带宽变更不触发候补重裁**；只有暂停与撤销属于“释放”。
- 释放失败（如暂停时修订号过期 409）或事务回滚时，队列、许可状态与修订号
  **均不变**——不会出现部分获批。
- 候补登记/取消虽不改修订号，但同样在单行修订号锁上排队，与释放事务串行，
  因此登记绝不会与正在进行的自动重裁互相穿插。

### 临时频谱管制窗口（预演 → 确认 → 解除）

运营人员对某片区域做临时频谱管制时，先以与申请相同的三元形状（覆盖圆、频率闭区间、
UTC 左闭右开时段）加原因做**只读预演**，再携预演返回的当前修订号确认。

**命中判定（窗口与申请/许可三者都相交）**

1. 覆盖圆相交（圆心距 ≤ 半径和，**相切算相交**）；
2. UTC 左闭右开时段相交（一端结束恰为另一端开始**不算**）；
3. 频率**闭区间相交**（端点相碰**算相交**）。

**保护带宽不扩张禁用范围**：频段不相交时，即使最短间隔小于保护带宽也**不命中**
（这是窗口与许可干扰裁决的唯一区别）。

**两步操作**

- `POST /api/restriction-windows/preview`，请求体
  `{"circle": ..., "frequency": ..., "time_window": ..., "reason": "..."}`：
  **不改任何状态、不占修订号**。返回当前修订号 `current_revision`（确认时作为
  `expected_revision`）、命 中的**当前有效许可** `hit_effective`
  （及 `hit_effective_application_ids`）与**候补申请** `hit_waitlist`
  （含各自排队位置及 `hit_waitlist_application_ids`），每项附三维触发标志。
- `POST /api/restriction-windows`，请求体在预演字段外追加
  `{"expected_revision": n}`：与所有既有写操作**共用同一修订号锁**。修订号过期 →
  409 `stale_revision`；通过则窗口立即生效、**占一个修订号**，响应给出稳定的
  `window_id` / `id`、窗口生效修订号，并在锁内重列一遍命中对象。窗口生效时
  **命中的既有有效许可继续有效**（不被撤销、不退出有效集合），但自该修订起：
  - 待审 `preview` 追加 `has_restriction_window_conflicts` 与
    `restriction_window_conflicts[]`（原 `has_conflicts` / `conflicts[]` 仍只表示
    许可干扰，字段与错误结构保持兼容）；
  - 单笔与整组批准、暂停许可的**恢复**都会报告命中窗口并返回
    **409 `restriction_window`**，许可不生效、申请仍 pending、**不占修订号**
    （整组中命中窗口的成员在 `details.applications[].restriction_window_conflicts`
    中列出，整组不批准；若同组还存在普通许可干扰则仍返回 `interference`）；
  - 暂停/撤销释放后的**候补自动审批**逐项重裁时同样检查窗口：命中窗口者**保留
    队列、继续检查后项**（`waitlist_pending[].restriction_window_conflicts` 给出
    命中窗口）；
  - **可用调频位置查询**把每个相交窗口的频段作为**额外的闭禁区**
    `[window_low - w, window_high]`（两端都不可取，不被保护带宽加宽），即可用调频
    范围排除会命中窗口的位置。
  - 窗口只影响**会让许可生效的写操作**：暂停、撤销、带宽变更、候补登记/取消不受
    窗口限制。
- `POST /api/restriction-windows/{id}/release`，请求体
  `{"expected_revision": n, "reason": "..."}`：**解除**窗口，再占一个修订号。
  解除**只影响后续裁决**——窗口移走后新的预览/批准/恢复/候补重裁不再受它阻挡，
  但解除本身**不主动审批候补**（候补要等下一次暂停/撤销释放，或手动批准）。
  未知窗口 ID → 404 `restriction_window_not_found`；重复解除 →
  409 `restriction_window_not_active`；修订号过期 → 409 `stale_revision`
  （三种拒绝都不占修订号、不改许可与队列）。

**校验与非法范围**：窗口的圆/频段/时段沿用申请的整笔语义校验（`radius_not_positive`、
`frequency_low_above_high`、`time_start_not_before_end`、`value_not_finite`、
`naive_datetime`，一次返回全部违例，422 `validation_failed`）。此外**确认或预演时
已过期的窗口**（左闭右开时段的 `end <= 当前 UTC 时刻`；已开始但尚未结束的窗口
允许）以明细码 `restriction_window_expired` 拒绝。任何非法/过期/无效请求都**不改变
许可、候补队列与修订号**。

**查询与历史**

- `GET /api/restriction-windows` 仅列**当前生效**窗口（`active_windows`）；
  加 `?include_inactive=true`（或 `?status=released` / `?status=all`）同时从事件流
  还原**已解除**窗口（附 `activated_at_revision` / `released_at_revision`）。
- `GET /api/restriction-windows/{id}` 查单个窗口（生效中或已解除；从未存在 → 404）。
- 每次启用/解除各写一条不可变事件（`restriction_window_events`，占各自的修订号）。
  `GET /api/revision/{n}` 与 `/api/revision/current` 追加
  `active_restriction_window_ids` / `active_restriction_windows` 与
  `restriction_window_events`；`/api/revision/compare` 追加窗口集合增删
  （`active_restriction_window_ids`）与区间内的 `restriction_window_activated` /
  `restriction_window_released` 事件（也并入统一的 `differences` / `events`）。
  **原批准事件不追改**。

### 活动设备换机：许可交接（预演 → 确认）

活动设备换机时，运营人员把一张**当前有效许可**交接给一份**待审申请**：旧设备的
许可整笔撤销，接替申请在同一事务内获批，二者占连续修订号。

**两步操作**

- `POST /api/license-transfers/preview`，请求体
  `{"source_application_id": 原许可, "successor_application_id": 接替申请,
  "reason": "可选原因"}`：**只读预演，不占修订号、不改任何状态**。返回：
  - `current_revision`（确认时作为 `expected_revision`）与当前 `guard_band`；
  - 接替申请针对**排除了原许可之后**的当前有效集合（`effective_application_ids`
    即该集合）按现行保护带宽重算的干扰对象 `conflicts[]`（含三维触发条件，结构同
    单笔预览）——原许可把频段让给接替者，故不列为干扰；
  - 接替申请命中的生效管制窗口 `restriction_window_conflicts[]`（**保护带宽不
    扩张窗口**）；
  - `successor_waitlist_position`（接替申请当前是否在候补及其 FIFO 位置）、
    `planned_revoc_revision` / `planned_successor_revision`（确认将占用的两个
    连续修订号）；
  - `ready` 与 `blockers[]`：原许可不存在/非有效（`source_not_effective`）、
    原许可仍持有**未解除的紧急占频**（`emergency_license_protected`；紧急占用
    只能经专门解除结束，不能作为交接原许可）、接替申请不存在/非待审
    （`successor_not_pending`）、仍有干扰或窗口命中时 `ready=false` 并逐项列出
    （预演始终返回 200，状态问题放在 `blockers` 而非直接报错，便于运营查看
    现状）。
- `POST /api/license-transfers`，请求体在预演字段外追加
  `{"expected_revision": n}`：与所有既有写操作**共用同一修订号锁**，在锁内
  **重新计算**。确认顺序：原许可与接替申请是否存在（未知 → 404 `not_found`）
  → 原许可是否**仍有效**（必须 `approved`；暂停/已撤销 → 409
  `source_not_effective`）→ 原许可是否持有**未解除的紧急占频**（生效紧急许可
  只能由专门解除结束 → 409 `emergency_license_protected`）→ 接替申请是否
  **仍待审**（已撤销 → 409 `already_revoked`；已批准/暂停 → 409
  `successor_not_pending`）→ 修订号是否为当前值（过期 → 409
  `stale_revision`）→ **排除原许可后**按当前带宽重算接替申请的干扰与窗口命中
  （窗口 → 409 `restriction_window`；其他许可干扰 → 409 `interference`）。任一
  不满足都**整笔拒绝**：许可、候补队列、紧急占频记录、修订号**均不变**（不会
  出现只撤销未批准，也不会留下已撤销许可却仍生效的紧急占频）。已解除紧急占频
  的原许可此时是普通已撤销许可，按前述 `source_not_effective` 规则拒绝。

**整笔生效时（一次提交）**

1. 写入一条普通 `revocation_events`（原许可撤销），占修订号 **n+1**，原许可
   状态变 `revoked`——**撤销语义与现有撤销完全一致**（含原批准修订号、原因）；
2. 写入一条普通 `approval_events`（接替申请批准，`auto_approved=false`、
   `release_revision=null`），占**下一个连续修订号 n+2**，接替申请变
   `approved`——**批准语义与现有手动批准完全一致**；
3. 接替申请若在候补，**先退队**（故不会被随后的自动重裁再次批准）；
4. 交接后调用**现有候补 FIFO 重裁**：其余候补按登记先后、用当前保护带宽与
   “截至此刻的全部有效许可”重裁，通过者即时批准、占后续连续修订号
   （`auto_approved=true`，`release_revision` 为交接撤销修订号 n+1），受阻者
   保留位置继续检查后项。

响应给出交接与自动批准结果：`transferred`、`revocation_revision` /
`successor_approval_revision`（即两个连续修订号）、`revision`（= 接替批准
修订号）、`to_revision` / `final_revision`（含自动批准后的最终修订号）、
`successor_was_waitlisted`、原许可与接替申请的最新状态、`revocation` /
`approval` 两段明细，以及与暂停/撤销响应同构的 `auto_approved_application_ids` /
`auto_approvals` / `remaining_waitlist_application_ids` / `waitlist_pending`。

**其余规则**

- 原许可与接替申请必须**不同**（相同 → 422 `same_application`，明细走统一
  校验信封）；ID 必须 ≥ 1、修订号必须 ≥ 0（结构性非法 → 422
  `validation_failed`）。
- **生效紧急许可不得交接**：原许可对应一条**未解除**的紧急占频时，预演在
  `blockers` 中给出 `emergency_license_protected`，确认在共用修订仲裁内返回
  409 `emergency_license_protected`（`details` 附 `application_id` / `status` /
  `current_revision` / `emergency_occupation_id` /
  `emergency_approval_revision`），原许可、接替申请、候补、紧急占频记录与修订号
  **均不变**。紧急占频只能由 `POST
  /api/emergency-occupations/{id}/release` 专门解除；解除后原许可成为普通已撤销
  许可，再交接按 `source_not_effective` 拒绝。
- 另写一条不可变 `license_transfer_events`（以撤销修订号为主键关联）记录
  `source_application_id` / `successor_application_id` /
  `successor_approval_revision` / `reason`，**仅作审计关联**；它不改变、也不
  替代普通撤销/批准事件。
- `GET /api/revision/{n}`、`/api/revision/current` 追加 `license_transfers`；
  `/api/revision/compare` 追加区间内的 `license_transfer`（并入统一的
  `differences` / `events`，按修订号排序；可选 `application_id` 同时匹配原许可
  与接替申请任一方）。交接链接行本身**不**产生额外的许可状态变更——撤销修订
  与批准修订的有效/撤销状态完全由原有撤销、批准事件还原（在 n+1 时原许可已
  撤销、接替尚未批准；在 n+2 时接替已有效），故每个修订号都能精确还原。
- 交接不触碰保护带宽与管制窗口本身；启动时新表由建表流程自动创建，既有库与
  已生效带宽、修订号不受影响，重启状态不变。

### 突发通信紧急占频（预演 → 占用 → 解除）

突发通信需要**优先占频**时，运营人员以待审申请自身的三元形状（覆盖圆、频率
闭区间、UTC 左闭右开时段）代表要抢占的频谱，给出申请 ID 与原因，先做**只读
预演**，再携当前修订号确认占用；占用只能由专门的**解除**结束。

**两步操作**

- `POST /api/emergency-occupations/preview`，请求体
  `{"application_id": 待审申请, "reason": "可选原因"}`：**不改任何状态、不占
  修订号**。返回 `current_revision`（确认时作为 `expected_revision`）、当前
  `guard_band`，并把当前有效许可分成两类：
  - `force_suspend_licenses` / `force_suspend_application_ids`：互扰的**普通
    许可**——确认时会被逐张**强制暂停**（按许可 ID 固定顺序）；
  - `emergency_conflicts` / `emergency_conflict_application_ids`：互扰的
    **其他紧急占用许可**——紧急不能压紧急，存在即整笔不可确认；
  - 另有 `restriction_window_conflicts[]`（命中的生效管制窗口，保护带宽不
    扩张窗口）、`waitlist_position`（紧急申请当前是否在候补及 FIFO 位置）、
    `planned_first_force_suspension_revision` / `planned_approval_revision` /
    `planned_final_revision`（确认将占用的连续修订号）。
  - `ready` 与 `blockers[]`：申请不存在（`not_found`）、非待审（`not_pending`）、
    与紧急许可互扰（`emergency_occupation_conflict`）或命中窗口
    （`restriction_window`）时 `ready=false` 并逐项列出（预演始终 200，状态
    问题放在 `blockers` 而非直接报错）。
- `POST /api/emergency-occupations`，请求体
  `{"application_id": ..., "expected_revision": n, "reason": "..."}`：与所有
  既有写操作**共用同一修订号锁**，锁内**重新计算**：申请必须仍**待审**（未知
  → 404 `not_found`；已撤销 → 409 `already_revoked`；已批准/暂停 → 409
  `not_pending`）→ 修订号必须为当前值（过期 → 409 `stale_revision`）→ **不
  命中任何生效管制窗口**（命中 → 409 `restriction_window`）→ **不与任何其他
  紧急占用许可互扰**（互扰 → 409 `emergency_occupation_conflict`）。任一不
  满足都**整笔拒绝**：许可、候补队列、修订号**均不变**。

**整笔生效时（一次提交，各步占连续修订号）**

1. 互扰的**普通许可先逐张强制暂停**，按固定顺序（许可 ID 升序）各占一个
   连续修订号：写的是**普通 `suspension_events`**（`forced=true`、
   `forced_by_emergency_application_id=紧急申请`），许可状态变 `suspended`、
   现场标记 `force_suspended_by=紧急申请`，**原批准记录不改**；强制暂停
   **不触发候补重裁**（不释放给候补）。
2. 紧急申请若在候补，**先退队**。
3. 再批准紧急申请，占下一个连续修订号：写**普通 `approval_events`**
   （`emergency=true`，且 `auto_approved=false`、`release_revision=null`），
   申请变 `approved`。另写一条 `emergency_occupations` 记录关联强制暂停
   起止修订与紧急批准修订（审计与当前状态镜像）。

响应给出 `occupied`、`id`、`emergency_approval_revision` / `revision`（紧急
批准修订）、`first/last_force_suspension_revision`、`to_revision`、
`force_suspended_application_ids` / `force_suspensions[]`（含逐项修订与许可
快照）、`was_waitlisted` 与紧急申请最新状态。

**占用期间普通操作的边界**

- 被强制暂停的普通许可**可照常撤销**（普通撤销语义，同事务照旧 FIFO 重裁
  候补；撤销时清掉 `force_suspended_by`）。
- 被强制暂停的普通许可**普通恢复一律拒绝**：409 `force_suspended`
  （`details.force_suspended_by` 给出紧急申请）；对它再次普通暂停同样 409
  `force_suspended`。
- 紧急许可**不得普通暂停、普通撤销或作为换机交接的原许可**：普通暂停/撤销
  返回 409 `emergency_license_protected`；换机交接预演在 `blockers` 中给出同码、
  确认返回 409 `emergency_license_protected`（许可、接替申请、候补、占频记录与
  修订号均不变）；占用**只能通过解除结束**。

**解除（按暂停顺序重裁，整笔原子提交）**

`POST /api/emergency-occupations/{紧急申请ID}/release`，请求体
`{"expected_revision": n, "reason": "可选"}`。在同一修订锁内按序：

1. **先撤销紧急许可**，占一个修订号（普通 `revocation_events`，
   `emergency_release=true`、`emergency_application_id=紧急申请`）；
2. 再按**原强制暂停顺序**，对**仍因本次占用处于强制暂停**的许可，用**当前
   保护带宽**、**当前窗口**和“截至此刻的全部有效许可”（含本轮已恢复者）
   **逐张重裁**：
   - 无互扰且不命中窗口者**恢复**，各占一个连续修订号（普通
     `resumption_events`，`forced_recovery=true`、
     `emergency_application_id=紧急申请`），清掉 `force_suspended_by`；
   - 受阻者**保持暂停**，清掉紧急标记（占用已结束，它退回为**普通暂停**，
     之后可正常普通恢复），响应在 `blocked[]` 中返回其互扰对象
     （`conflicts[]`）与命中窗口（`restriction_window_conflicts[]`）；
   - 占用期间已被普通撤销的许可不在恢复之列；
3. 最后按 **FIFO 重裁候补**（沿用现有候补重裁，自动批准占后续连续修订号）。

响应给出 `released`、`emergency_revocation_revision` / `revision`、
`restored_application_ids` / `restorations[]`（逐项修订、带宽与许可快照）、
`blocked_application_ids` / `blocked[]`、`final_revision` / `to_revision`，以及
与暂停/撤销响应同构的候补重裁结果（`auto_approved_application_ids` /
`auto_approvals` / `remaining_waitlist_application_ids` / `waitlist_pending`）。

**解除的拒绝与幂等**

- 紧急申请从未有过占用 → 404 `emergency_occupation_not_found`；
- 占用已经解除后再次解除 → 409 `emergency_occupation_not_active`
  （`details.released_revision` 给出解除修订号）；
- 修订号过期 → 409 `stale_revision`。
  三种拒绝（含**过期修订**与**重复解除**）都**不改状态、不占修订号**。

**查询与历史**

- `GET /api/emergency-occupations` 仅列生效中占用（`active_occupations`）；
  `?include_inactive=true`（或 `?status=released` / `?status=all`）同时列出
  已解除占用（`released_occupations`；附 `released_revision` /
  `release_reason` / `released_at`），合并列表 `occupations` 按占用 ID 排序。
- `GET /api/emergency-occupations/{紧急申请ID}` 查单个占用（生效中或已解除；
  从未存在 → 404 `emergency_occupation_not_found`）。
- `GET /api/revision/{n}` 与 `/api/revision/current` 追加
  `active_emergency_application_ids`（当前持有紧急许可的申请，有效集合的
  子集）、`force_suspended_application_ids`（被生效占用强制暂停的许可，暂停
  集合的子集）、`emergency_occupations`（含各占用强制暂停/已恢复清单与
  `status`/`released_revision`）与 `emergency_occupation_events`
  （`activated` 于紧急批准修订、`released` 于紧急撤销修订）；每条
  批准/暂停/恢复/撤销记录追加相应的 `emergency` / `forced` /
  `forced_by_emergency_application_id` / `forced_recovery` /
  `emergency_application_id` / `emergency_release` 标志（普通许可操作既有字段
  全部保持，纯增量）；`/api/revision/compare` 追加
  `active_emergency_application_ids` 与 `force_suspended_application_ids` 的
  端点集合增删，以及区间内的 `emergency_occupation_activated` /
  `emergency_occupation_released` 事件（并入统一的 `differences` / `events`，
  按修订号排序）。强制暂停、恢复、紧急批准与紧急撤销都是**普通事件流**中的
  行，故每个修订号（含“已强制暂停、紧急尚未批准”与“紧急已撤销、许可尚未
  恢复”的中间修订）都能精确还原；**原批准事件不追改**。
- 占用不触碰保护带宽与管制窗口本身；启动时新表由建表流程自动创建，新增列由
  幂等迁移补齐（历史事件默认按普通事件读取），既有库、已生效带宽与修订号
  不受影响，重启状态不变。

### 预约自动审批（约定 UTC 时刻 → 到期在共用修订仲裁内重裁）

运营人员可为**待审批（pending）**申请约定一个自动审批时刻：该 UTC 时刻必须
**严格晚于当前时刻**且**严格早于申请自身的申请时段结束**（左闭右开，等于结束时刻
不允许）。预约本身**不占修订号、不改变申请状态**（候补中的申请保留队位）。

**预约 / 改期 / 取消**

- `POST /api/applications/{id}/scheduled-approvals`，请求体
  `{"scheduled_at": "2026-10-04T08:00:00Z", "reason": "可选"}`：
  - 申请不存在 → 404 `not_found`；非待审 → 409 `not_pending`；时刻无时区、不晚于
    当前或不早于申请时段结束 → 422 `scheduled_time_invalid`（`details` 用
    `scheduled_at_not_in_future` / `scheduled_at_not_before_window_end` /
    `naive_datetime` 逐项说明）。**任一拒绝都不留预约**。
  - 成功返回 `id`/`scheduled_approval_id`、`status: "scheduled"`、约定时刻、
    当前修订号（HTTP 201）。
  - **同一申请同时只能有一个待执行预约**：对已有待执行预约的申请再次调用本接口
    即**改期**（HTTP 200，`action: "rescheduled"`），复用同一预约 ID，不新建行。
- `POST /api/applications/{id}/scheduled-approvals/reschedule`：到期前显式改期，
  新时刻遵守与预约相同的合法性校验；非法时刻 → 422，**原预约保持不变**。
  无待执行预约 → 409 `scheduled_approval_not_active`（`details.status` 给出终态）。
- `POST /api/applications/{id}/scheduled-approvals/cancel`（请求体可省略或带
  `reason`）：到期前取消，预约进入 `cancelled` 终态、**不占修订号**；取消后可
  重新预约。无待执行预约 → 409 `scheduled_approval_not_active`。

**到期处理（共用修订仲裁；启动补处理与后台轮询）**

到期不做预判，而是在**实际处理时刻**进入与手动批准/暂停/撤销/交接/紧急占用相同的
单例修订号锁事务，按**当时的生效保护带宽、全部有效许可与生效管制窗口**重新裁决：

- **可通过** → 批准并占**一个新修订号**：写入一条普通 `approval_events` 行并标记
  `scheduled: true`（它不是候补排空，故 `auto_approved` 仍为 `false`）；若申请
  同时在候补，**先退出候补**；申请变为 approved，预约进入 `approved`，记录
  `processed_at` / `approval_revision` / `processed_guard_band`。同一次扫描内
  后处理的预约还要把刚获批的许可计入有效集合。
- **有许可干扰或命中管制窗口** → 在预约行上**记录阻塞明细**（`blocked_details`
  结构同待审预览：`conflicts[]` 与 `restriction_window_conflicts[]`），预约进入
  `blocked`，**申请仍 pending、不占修订号，且不自动重试**（再次扫描不会处理它）。
  之后可手动批准，或取消约束后**重新预约**。
- **预约自动审批不触发候补重裁**：其他候补保持队位（与暂停/撤销释放的排空相区别）。

到期处理由一个后台轮询循环驱动（间隔由 `SCHEDULED_APPROVAL_POLL_SECONDS` 控制，
默认 0.5 秒；`SCHEDULED_APPROVAL_BACKGROUND=0` 可关闭轮询），并且**服务启动时先做
一次补处理**：停机期间到期的预约在重启时刻按上述规则重裁。运维也可随时调用
`POST /api/scheduled-approvals/process-due` 立即触发同一逻辑（只处理仍为
`scheduled` 的到期行，因此重启/重复调用**绝不会重复批准**）。

**其他路径先决定 → 原预约失效；竞争只有一种结果生效**

申请经下列任一路径获批时，其待执行预约在**同一事务**内置为 `invalidated`
（`outcome_reason` 标明来源），到期后不再触发：手动单笔批准（`manual_approval`）、
整组批准（`batch_approval`）、暂停/撤销/紧急解除排空中的**候补自动批准**
（`waitlist_auto_approval`）、换机交接的接替批准（`license_transfer`）、紧急占用
批准（`emergency_occupation`）。取消、改期、到期处理与这些审批都在同一修订号锁上
串行，因此**只允许一种结果生效**。

**查询与历史（按修订可见，杜绝审计穿越）**

- `GET /api/scheduled-approvals?status=scheduled&application_id=…`：按状态/申请
  过滤列出预约（实时当前状态）；状态取值
  `scheduled`/`approved`/`blocked`/`cancelled`/`invalidated`。
- `GET /api/scheduled-approvals/{id}`：返回预约状态、约定时刻、**实际处理时间**
  `processed_at`、获批时的 `approval_revision`/`processed_guard_band`，或阻塞时的
  `blocked_details`；终态另附 `outcome_reason`。所有预约行（实时与历史）还带
  `booked_revision`（预约首次可见的修订号）与 `outcome_revision`（终态首次可见的
  修订号；仍待执行时为 `null`）。
- `GET /api/applications/{id}/scheduled-approvals`：单个申请的全部预约（含历史）。
- `GET /api/revision/current` 追加 `scheduled_approval_ids`/`scheduled_approvals`
  （仅待执行项）；`GET /api/revision/{n}` 的批准记录追加 `scheduled` 标志，并附
  `scheduled_approvals[]` 预约审计行。**历史修订标明自动审批且保留原批准事件语义**：
  预约产生的批准是普通批准事件流中一行，只是多了 `scheduled` 标记，永不追改。

预约的**登记、改期、取消都不占修订号**，但修订快照按下列规则还原，保证只读历史
不发生审计穿越：

- **后来才创建的预约不出现在较早修订**：每条预约记录 `booked_revision`（预约时的
  当前修订号）；修订 N 的快照只含 `booked_revision <= N` 的预约。
- **终态只从各自的决定修订起可见**：`outcome_revision` 标记终态首次出现的修订号——
  到期获批取它占用的批准修订号；被单笔批准、整组批准、候补自动批准、许可交接接替
  批准或紧急占用批准置为失效时，取该审批事件自身的修订号（整组中每项只在自己的
  修订号失效）；到期阻塞与取消不占修订号，取操作时的当前修订号。查询更早修订时
  该行**回绕为待执行 `scheduled`**，且不返回 `status` 之外的任何终态字段
  （`approval_revision`/`processed_at`/`processed_guard_band`/`blocked_details`/
  `outcome_reason` 均为 `null`），因此**审批修订号不会提前泄漏**。
- **改期按修订还原**：每次改期在只追加的 `scheduled_approval_changes` 流水里记录
  当时的修订号、约定时刻与原因；修订 N 的快照取修订号 `<= N` 的最后一条，后来的
  改期不会挪动旧修订里的预约时刻。
- **同一修订号内的不占号操作按当前状态呈现**：登记、改期、取消（以及到期阻塞）
  若发生在同一修订号窗口内，该修订号的快照直接呈现窗口内的最新状态（例如修订 0
  登记又取消，则修订 0 快照显示 `cancelled`）；跨修订号才做上述回绕。
- 每个申请至多一条待执行预约（局部唯一索引）的规则不变；历史查询全程只读，
  `GET /api/revision/{n}` 的既有字段、修订越界（未来修订 404、负修订 422）错误
  完全兼容，新增的 `booked_revision`/`outcome_revision` 均为追加字段。

### 可用调频位置查询（不改状态）

`POST /api/applications/{id}/available-ranges` 为**待审批**申请寻找可用调频位置：
覆盖圆、UTC 时段与原**频段宽度** `w = high - low` 固定（`w = 0` 的单频点允许），
仅让频段**下界** `x` 在请求体的闭区间 `[candidate_low_min, candidate_low_max]`
内变动（两端相等即查询单个候选位置）。裁决与 `preview` 使用**同一修订快照**的
有效许可与当前生效保护带宽：只有空间与时间均相交的许可才会阻断；对这样的许可
`[L0, L1]`，下界 `x` 的禁区恰为 `(L0 - w - g, L1 + g)`——端点相碰算干扰、分离间隔
**严格小于** `g` 算干扰，故 `g > 0` 时禁区为开区间（间隔恰等于 `g` 可取），
`g = 0` 时为闭区间（相碰的端点本身不可取）。此外，每个在空间与时间上相交的
**生效管制窗口** `[W0, W1]` 增加一个**闭**禁区 `[W0 - w, W1]`（两端都不可取；
保护带宽不扩张窗口）。

响应返回当前修订号、保护带宽、固定带宽 `bandwidth`，以及候选下界区间内**全部
极大的可用连续范围**（升序），每段用 `min_inclusive` / `max_inclusive` 标明两端
是否可取（可能退化为单个可取点）；无可用位置时 `available_ranges` 为空列表。
把申请频段平移到任一可取下界 `x`（即 `[x, x + bandwidth]`）再做单笔预览，干扰
结论必然为空。查询**不保存或改动**申请、许可与修订号。未知 ID（404）、非待审批
申请（409）、非有限或倒置的候选区间（422，明细码 `value_not_finite` /
`candidate_low_min_above_max`）均明确拒绝。

### 修订号与并发模型

- 初始修订号为 **0**；每次批准、**整组批准中的每项**批准、暂停、恢复、撤销、
  **保护带宽变更**、**管制窗口的启用与解除（各一次）**、**紧急占用的每张强制
  暂停与紧急批准（各一次）**、**解除紧急占用的紧急撤销与每张强制恢复（各一次）**
  或**释放时自动获批的每个
  候补申请**都使修订号 +1，并追加一条不可变事件（暂停/恢复因修订号过期或恢复
  互扰被拒绝时**不占**修订号；紧急占用确认因状态/窗口/紧急互扰、解除因修订过期
  或重复解除而拒绝时也**不占**修订号；候补登记与取消也**不占**修订号；预演同样
  不占修订号）。候补自动批准与其触发释放同处一个事务：释放操作占一个修订号，
  每个自动获批申请占其后连续的一个修订号。紧急占用确认同样一次事务：每张强制
  暂停与紧急批准依次各占一个连续修订号（强制暂停**不**重裁候补）；解除也是一次
  事务：紧急撤销、每张成功恢复与每个候补自动批准依次各占一个连续修订号。
- `POST .../preview` 返回 `current_revision`、冲突对象及每个触发条件。
- `POST .../approve` 必须携带 `expected_revision`。服务在 PostgreSQL
  单行修订号表上 `SELECT ... FOR UPDATE` 串行化所有批准/撤销/带宽变更事务：
  - `expected_revision != 当前修订号` → **409 `stale_revision`**（修订过期/被并发抢先）；
  - 号对但锁内**重新裁决**发现冲突 → **409 `interference`**（不得占用频段，响应体附冲突）；
  - 通过 → 写入批准事件，占用新修订号。
- `POST /api/applications/batch-approve` 为**整组原子批准**，与单笔批准/撤销共用
  同一把修订号锁：
  - 请求体 `{"expected_revision": n, "application_ids": [..]}`，列表必须**非空且无重复**
    （空列表 422；重复返回 `duplicate_application_id`）；
  - 同一事务内按固定顺序核对：**ID 是否存在 → 状态是否仍为待审批 → 修订号是否过期 →
    干扰**；任一步失败**整组不批准、不占用任何修订号**，并按失败类型返回具体申请
    （ID 无效给 `missing_application_ids`；状态不符给每项 `status`/`approval_revision`；
    干扰给每项 `conflicts`，并以 `scope: "effective" | "group"` 标明冲突来自当前有效
    许可还是组内另一申请，组内冲突双方都会出现）；
  - 全部通过时**按输入顺序**为每项写一条批准事件，占用连续修订号
    `n+1, n+2, …`，整组**一次提交**；按其中任一修订号查询 `/api/revision/{k}`，
    即还原截至该项获批时的有效集合。
- 并发单笔批准、整组批准、暂停、恢复、撤销、带宽变更或管制窗口启用/解除在同一锁
  （SQLite 退化为唯一修订号约束兜底）上裁决，胜者提交、其余返回 `stale_revision`，
  **不会产生部分获批或互相干扰的有效许可**。
- `POST .../revoke` 只把申请移出有效集合（产生新修订号），历史不删除；对**暂停中**
  的许可同样可用（永久撤销，响应附 `"was_suspended": true`），撤销后不能恢复。
- 历史快照额外返回 `suspended_application_ids`、`suspensions`、`resumptions`；
  批准记录的 `status_as_of` 取值为 `approved` / `suspended` / `revoked`，暂停时附
  `suspended_at_revision`，撤销时附 `revoked_at_revision`（`GET /api/revision/current`
  还附完整的暂停中申请对象 `suspended_applications`）。候补自动获批的批准记录额外
  带 `auto_approved: true` 与 `release_revision`（触发它的暂停/撤销修订号），按其中
  任一修订号查询即可逐项还原自动审批；当前快照另附候补队列
  `waitlist_application_ids` / `waitlist_entries`。
  批准记录内嵌的 `application` 对象同样是**该修订号的事件回放视图**：其 `status`、
  `approval_revision`、`suspended_revision` 与 `force_suspended_by` 按截至该修订的
  批准/暂停/恢复/撤销/紧急解除事件重放得出（撤销后保留暂停号作历史标记，与实时
  行的写法一致），之后的恢复、撤销或紧急占频解除**不会改写**旧修订号的视图；
  当前状态接口（`GET /api/applications/{id}`、`/api/revision/current` 等）仍反映
  实时状态。
- `GET /api/revision/{n}` 还原修订号 **n 当时**的批准、暂停、恢复、撤销记录、
  **当时生效的保护带宽**、带宽变更记录及**当时生效的管制窗口与窗口启用/解除记录**
  （`n` 超过当前修订号返回 404）。
- `GET /api/revision/compare?base_revision={a}&target_revision={b}`
  对两个已存在修订号做只读复核。返回两侧保护带宽、有效/暂停许可集合、生效管制窗口
  集合及增删集合、区间内按修订号排序的 `approval` / `suspension` / `resumption` /
  `revocation` / `guard_band_change` / `restriction_window_activated` /
  `restriction_window_released` 事件（合并字段为 `differences`；各类事件也分别在
  `approvals`、`suspensions`、`resumptions`、`revocations`、`guard_band_changes`、
  `restriction_window_events` 中返回）、逐次 `status_changes`，其中审批事件保留
  `auto_approved` 与 `release_revision`，其内嵌 `application` 同样是**较新一侧
  修订号**的事件回放视图（不随之后的写入改变）。可选 `application_id` 只筛选该申请的
  许可事件（保护带宽与管制窗口变更仍返回）；未知申请返回 404。该接口只读取事件表，
  不修改申请、许可、候补队列、管制窗口或修订号。

### 保护带宽的在线调整

裁决使用的保护带宽是**数据库中的生效值**（`revision_meta` 单行），`GUARD_BAND`
环境变量只在建库（或从旧版库升级）时写入一次，之后重启不再覆盖。运营人员在线
调整分两步：

1. **预览** `POST /api/config/guard-band/preview`，请求体 `{"guard_band": 拟定值}`。
   返回当前修订号、当前带宽、拟定带宽，以及**拟定带宽下当前全部有效许可之间**
   会互扰的许可对（含每项触发条件）。预览**不改变任何状态**。
2. **确认** `POST /api/config/guard-band`，请求体
   `{"guard_band": 同一拟定值, "expected_revision": 预览拿到的修订号}`。
   与单笔/整组批准、撤销**共用同一把修订号锁**：
   - 修订号过期 → **409 `stale_revision`**；
   - 拟定值使任意有效许可对互扰 → **409 `interference`**（响应体附许可对与触发
     条件）；两种拒绝下**带宽、有效集合、修订号均不变**；
   - 通过 → 修订号 **+1（只产生一个）**，追加一条不可变带宽变更事件，后续申请
     预览及单笔、整组批准统一使用新带宽。

拟定值必须是**有限的非负数**，否则 **422 `validation_failed`**（明细码
`value_not_finite` / `guard_band_negative`）。`GET /api/config/guard-band`
可随时查看当前生效值、初始值与当前修订号。历史修订查询
（`GET /api/revision/{n}`）可还原**当时**的带宽与有效集合；已有批准事件各自
保存裁决时所用带宽的快照，**不被追改**。

---

## 5. HTTP 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/applications` | 提交申请（语义违例整笔 422） |
| `GET` | `/api/applications?status=pending` | 列表（可选状态过滤） |
| `GET` | `/api/applications/{id}` | 查看单笔申请 |
| `POST` | `/api/applications/{id}/preview` | 冲突预览（返回冲突对象、触发条件、当前修订号） |
| `POST` | `/api/applications/{id}/available-ranges` | **可用调频位置查询**（候选下界区间内全部极大可用连续范围；不改状态） |
| `POST` | `/api/applications/{id}/approve` | 携带修订号批准（锁内重新裁决） |
| `POST` | `/api/applications/batch-approve` | **整组原子批准**（预期修订号 + 非空无重复 ID 列表） |
| `POST` | `/api/applications/{id}/suspend` | **临时停机暂停**（预期修订号；立即退出有效集合，保留原批准） |
| `POST` | `/api/applications/{id}/resume` | **故障解除恢复**（预期修订号；按当前带宽与有效许可重新裁决） |
| `POST` | `/api/applications/{id}/revoke` | 撤销（仅影响后续有效集合；暂停中亦可永久撤销；同事务按序重裁候补） |
| `POST` | `/api/applications/{id}/waitlist` | **候补登记**（仅待审申请；不占修订号；返回排队位置与当前修订号） |
| `POST` | `/api/applications/{id}/waitlist/cancel` | **取消候补**（不改申请状态、不占修订号） |
| `GET` | `/api/waitlist` | 查询整条候补队列（FIFO）与当前修订号 |
| `GET` | `/api/applications/{id}/waitlist` | 查询单个申请的候补位置 |
| `POST` | `/api/restriction-windows/preview` | **管制窗口预演**（只读：命中的当前有效许可与候补申请、当前修订号） |
| `POST` | `/api/restriction-windows` | **确认启用管制窗口**（圆/频段/时段/原因 + 预期修订号；占一个修订号） |
| `POST` | `/api/restriction-windows/{id}/release` | **解除管制窗口**（窗口 ID + 预期修订号；只影响后续裁决，不审批候补） |
| `GET` | `/api/restriction-windows` | 列出生效窗口（`include_inactive`/`status` 可含已解除） |
| `GET` | `/api/restriction-windows/{id}` | 查询单个窗口（生效中或已解除） |
| `POST` | `/api/license-transfers/preview` | **换机许可交接预演**（只读：当前修订号、排除原许可后的干扰、命中窗口；原许可持有未解除紧急占频时给 `emergency_license_protected` 阻断；不改状态） |
| `POST` | `/api/license-transfers` | **确认换机交接**（原许可 + 接替申请 + 预期修订号；撤销与批准占两个连续修订号，再 FIFO 重裁候补；原许可持有未解除紧急占频 → 409 `emergency_license_protected`，整笔不变） |
| `POST` | `/api/emergency-occupations/preview` | **紧急占频预演**（只读：当前修订号、将强制暂停的普通许可、互扰的紧急许可、命中窗口；不改状态） |
| `POST` | `/api/emergency-occupations` | **确认紧急占用**（待审申请 + 预期修订号；先强制暂停普通许可各占一修订号，再批准紧急申请） |
| `POST` | `/api/emergency-occupations/{id}/release` | **解除紧急占用**（紧急申请 ID + 预期修订号；撤销紧急许可→按暂停顺序重裁恢复→FIFO 重裁候补，整笔原子） |
| `GET` | `/api/emergency-occupations` | 列出生效紧急占用（`include_inactive`/`status` 可含已解除） |
| `GET` | `/api/emergency-occupations/{id}` | 查询单个紧急占用（生效中或已解除） |
| `POST` | `/api/applications/{id}/scheduled-approvals` | **预约 UTC 自动审批**（时刻须晚于当前、早于申请时段结束；不占修订号；重复调用即改期） |
| `POST` | `/api/applications/{id}/scheduled-approvals/reschedule` | **改期**预约（到期前；新时刻同样校验） |
| `POST` | `/api/applications/{id}/scheduled-approvals/cancel` | **取消**预约（到期前；不占修订号） |
| `GET` | `/api/applications/{id}/scheduled-approvals` | 查询单个申请的全部预约（含历史终态） |
| `GET` | `/api/scheduled-approvals` | 列出/过滤预约（`status`、`application_id`） |
| `GET` | `/api/scheduled-approvals/{id}` | 查询单个预约：状态、实际处理时间、审批修订号或阻塞明细 |
| `POST` | `/api/scheduled-approvals/process-due` | 立即处理全部到期预约（管理/运维触发；后台轮询与启动补处理走同一逻辑） |
| `GET` | `/api/revision/current` | 当前修订号 + 当前带宽 + 有效集合 + 候补队列 + 生效窗口 + 全部事件 |
| `GET` | `/api/revision/compare` | 比较两个已存在修订号的带宽、许可集合、状态与按事件顺序的差异（只读） |
| `GET` | `/api/revision/{revision}` | 按修订号还原当时的批准/撤销记录与当时带宽 |
| `GET` | `/api/config/guard-band` | 当前生效带宽、初始带宽与当前修订号 |
| `POST` | `/api/config/guard-band/preview` | 拟定带宽预览（新带宽下有效许可间的干扰对；不改状态） |
| `POST` | `/api/config/guard-band` | 确认新带宽（拟定值 + 预期修订号；共享修订号裁决） |
| `GET` | `/health` | 存活探针 |

所有错误使用统一信封：

```json
{"error": {"code": "stale_revision", "message": "...", "details": {...}}}
```

| HTTP | `error.code` | 触发场景 |
| --- | --- | --- |
| 422 | `validation_failed` | 提交/参数校验失败（整笔拒收，`details` 为全部违例）；整组列表为空或含重复 ID 时同样 422，明细码为 `duplicate_application_id`；拟定带宽非有限数（`value_not_finite`）或为负（`guard_band_negative`）；候选下界区间非有限（`value_not_finite`）或倒置（`candidate_low_min_above_max`）；管制窗口形状非法（同申请各码）或**预演/确认时已过期**（`restriction_window_expired`） |
| 404 | `not_found` | 申请不存在（整组时 `details.missing_application_ids` 列出全部无效 ID） |
| 404 | `restriction_window_not_found` | 解除或查询一个从未存在的管制窗口 ID |
| 404 | `revision_out_of_range` | 查询了未来修订号 |
| 409 | `not_pending` | 批准已处理申请 / 撤销待审批申请 / 对非待审批申请查询可用位置（整组时 `details.applications` 列出每项状态） |
| 409 | `already_revoked` | 重复撤销（整组中混入已撤销申请时同样返回，明细同 `not_pending` 结构）；对已撤销许可暂停或恢复 |
| 409 | `not_approved` | 对**待审批**申请暂停（只有已获批且未撤销的许可可以暂停） |
| 409 | `already_suspended` | 对已暂停许可重复暂停（`details.suspended_at_revision` 给出暂停修订号） |
| 409 | `not_suspended` | 对未处于暂停状态的申请执行恢复（待审批/已获批均然，`details.status` 给出当前状态） |
| 409 | `already_waitlisted` | 把同一申请**重复登记**为候补（`details.position` 给出当前位置） |
| 409 | `not_waitlisted` | 取消一个**不在候补队列中**的申请（`details.status` 给出当前状态）；单项查询未登记申请时同码返回 **404** |
| 409 | `stale_revision` | 修订号过期或被并发批准/暂停/恢复/撤销/带宽变更/**管制窗口启用或解除**/**换机交接确认**抢占（单笔/整组/暂停/恢复/带宽变更/窗口/交接共享裁决） |
| 409 | `interference` | 重新裁决发现干扰，频段未占用（整组时 `details.applications[].conflicts` 用 `scope` 区分 `effective`/`group`）；或拟定带宽会使当前有效许可对互扰，带宽未变更（`details.interfering_pairs` 附许可对与触发条件）；或**恢复暂停许可时与当前有效许可互扰**，许可保持暂停（`details.conflicts` 附冲突对象，与单笔批准同构）。整组中同时存在普通干扰与窗口命中时也用本码，窗口命中另见各项 `restriction_window_conflicts` |
| 409 | `restriction_window` | 写操作会让一个**命中生效管制窗口**的申请生效：单笔/整组批准或暂停恢复被阻止（`details.restriction_window_conflicts[]` 附命中窗口与三维触发标志，单笔另附 `conflicts[]` 保持结构兼容）；许可不生效、申请不离开原状态、不占修订号 |
| 409 | `restriction_window_not_active` | 解除一个**已经解除**的管制窗口（`details.released_at_revision` 给出解除修订号；不占修订号） |
| 422 | `same_application` | 换机交接时原许可与接替申请**是同一个申请**（统一校验信封，明细附 `application_id`） |
| 409 | `source_not_effective` | 换机交接的原许可**不是当前有效许可**（处于待审/暂停/已撤销；`details.status` 给出当前状态；许可、队列、修订号均不变） |
| 409 | `successor_not_pending` | 换机交接的接替申请**已不是待审**（已批准/暂停；已撤销用 `already_revoked`；`details.status`/`approval_revision` 给出当前状态；不改许可、队列与修订号） |
| 409 | `emergency_occupation_conflict` | 紧急占频申请与**另一张生效紧急许可**互扰（紧急不能压紧急；`details.conflicts[]` 附冲突对象；整笔不占用，许可、队列、修订号均不变） |
| 409 | `emergency_license_protected` | 对**生效紧急许可**普通暂停、普通撤销，或把它作为**换机交接原许可**（紧急占用只能由专门解除结束；暂停/撤销时 `details.status` 给出当前状态；交接确认时 `details` 另附 `application_id`/`current_revision`/`emergency_occupation_id`/`emergency_approval_revision`，交接预演在 `blockers` 中给同码；均不改状态、不占修订号） |
| 409 | `force_suspended` | 对**被生效紧急占用强制暂停的普通许可**执行普通暂停或普通恢复（`details.force_suspended_by` 给出紧急申请；不改状态、不占修订号；此类许可可普通撤销） |
| 404 | `emergency_occupation_not_found` | 解除或查询一个**从未有过紧急占用**的紧急申请 ID |
| 409 | `emergency_occupation_not_active` | **重复解除**一个已经解除的紧急占用（`details.released_revision` 给出解除修订号；不改状态、不占修订号） |
| 422 | `scheduled_time_invalid` | 预约时刻**不是严格晚于当前 UTC 时刻**（`scheduled_at_not_in_future`）、**不早于申请时段结束**（`scheduled_at_not_before_window_end`）或为**无时区时刻**（`naive_datetime`）；整笔拒收且**不留预约** |
| 404 | `scheduled_approval_not_found` | 查询一个从未存在的预约 ID |
| 409 | `scheduled_approval_not_active` | 对一个**不在等待中**的预约改期/取消（已批准/已阻塞/已取消/已失效；`details.status` 给出当前状态） |
| 404 | `not_found` | 为不存在的申请预约/改期/取消/查询自动审批（同申请类接口） |

---

## 6. 调用示例（curl 完整走查）

下例保护带宽为 **2 MHz**（`GUARD_BAND=2.0`），两个活动同地同时段。

### 6.1 非法申请被整笔拒收

```bash
curl -i -X POST http://localhost:8000/api/applications \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "bad",
    "circle":    {"cx": 0, "cy": 0, "radius": -5},
    "frequency": {"low": 200, "high": 100},
    "time_window": {"start": "2026-10-01T10:00:00Z", "end": "2026-10-01T08:00:00Z"}
  }'
```

```json
HTTP/1.1 422 Unprocessable Entity
{"error": {"code": "validation_failed", "message": "the whole application was rejected due to invalid fields",
  "details": [
    {"field": "circle.radius", "code": "radius_not_positive", "message": "radius must be strictly positive"},
    {"field": "frequency.low", "code": "frequency_low_above_high", "message": "frequency low bound must not exceed high bound"},
    {"field": "time_window.start", "code": "time_start_not_before_end", "message": "time_window start must be strictly earlier than end"}
  ]}}
```

### 6.2 提交申请 A 并预览

```bash
curl -s -X POST http://localhost:8000/api/applications \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "音乐节 A",
    "circle":    {"cx": 0, "cy": 0, "radius": 1000},
    "frequency": {"low": 100, "high": 110},
    "time_window": {"start": "2026-10-01T08:00:00Z", "end": "2026-10-01T12:00:00Z"}
  }'
# -> {"id": 1, "status": "pending", ...}

curl -s -X POST http://localhost:8000/api/applications/1/preview
```

```json
{"application_id": 1, "current_revision": 0, "guard_band": 2.0,
 "has_conflicts": false, "conflicts": []}
```

### 6.3 携带修订号批准 A（占用修订号 1）

```bash
curl -s -X POST http://localhost:8000/api/applications/1/approve \
  -H 'Content-Type: application/json' -d '{"expected_revision": 0}'
```

```json
{"approved": true, "application_id": 1, "revision": 1, "guard_band": 2.0,
 "application": {"id": 1, "status": "approved", "approval_revision": 1, ...}}
```

### 6.4 提交 B：频段 111–112 MHz，与 A 最短间隔 1 MHz（< 2 MHz）

```bash
curl -s -X POST http://localhost:8000/api/applications \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "马拉松 B",
    "circle":    {"cx": 100, "cy": 0, "radius": 1000},
    "frequency": {"low": 111, "high": 112},
    "time_window": {"start": "2026-10-01T09:00:00Z", "end": "2026-10-01T11:00:00Z"}
  }'
# -> id 2

curl -s -X POST http://localhost:8000/api/applications/2/preview
```

```json
{"application_id": 2, "current_revision": 1, "has_conflicts": true,
 "conflicts": [{
   "application": {"id": 1, "circle": {"cx": 0.0, "cy": 0.0, "radius": 1000.0},
                   "frequency": {"low": 100.0, "high": 110.0}, ...},
   "trigger": {"time_intersect": true, "area_intersect": true,
               "frequency_intersect": false, "minimum_frequency_gap": 1.0,
               "within_guard_band": true},
   "reason": "within_guard_band"}]}
```

### 6.5 过期修订号 → 拒绝占用

```bash
curl -i -X POST http://localhost:8000/api/applications/2/approve \
  -H 'Content-Type: application/json' -d '{"expected_revision": 0}'
```

```json
HTTP/1.1 409 Conflict
{"error": {"code": "stale_revision",
  "message": "expected revision 0 but current revision is 1; re-preview and re-decide",
  "details": {"expected_revision": 0, "current_revision": 1}}}
```

### 6.6 修订号正确但锁内重新裁决相撞 → interference

```bash
curl -i -X POST http://localhost:8000/api/applications/2/approve \
  -H 'Content-Type: application/json' -d '{"expected_revision": 1}'
# HTTP/1.1 409 Conflict
# {"error": {"code": "interference", ...,
#   "details": {"current_revision": 1, "conflicts": [ ...同预览... ]}}}
```

### 6.7 并发批准相撞的复现（两个请求携带同一修订号）

```bash
# 两个完全重叠的待审批申请 id=1、id=2，当前修订号为 0
curl -s -X POST localhost:8000/api/applications/1/approve \
  -H 'Content-Type: application/json' -d '{"expected_revision":0}' > r1.json &
curl -s -X POST localhost:8000/api/applications/2/approve \
  -H 'Content-Type: application/json' -d '{"expected_revision":0}' > r2.json
wait
# 结果：一个 {"approved": true, "revision": 1}
#       另一个 409 {"error": {"code": "stale_revision",
#                 "details": {"expected_revision": 0, "current_revision": 1}}}
```

### 6.8 撤销 A：只影响后续裁决，产生修订号 2

```bash
curl -s -X POST http://localhost:8000/api/applications/1/revoke \
  -H 'Content-Type: application/json' -d '{"reason": "活动改期"}'
# {"revoked": true, "revision": 2, "previous_approval_revision": 1, ...}
```

### 6.9 B 重新预览（有效集合已空）并在修订号 3 获批

```bash
curl -s -X POST http://localhost:8000/api/applications/2/preview
# {"current_revision": 2, "has_conflicts": false, ...}

curl -s -X POST http://localhost:8000/api/applications/2/approve \
  -H 'Content-Type: application/json' -d '{"expected_revision": 2}'
# {"approved": true, "revision": 3, ...}
```

### 6.10 按修订号还原历史

```bash
curl -s http://localhost:8000/api/revision/1
# {"revision": 1, "effective_application_ids": [1],
#  "approvals": [{"revision": 1, "application_id": 1, ...}], "revocations": []}

curl -s http://localhost:8000/api/revision/2
# {"revision": 2, "effective_application_ids": [],
#  "approvals": [{"revision": 1, ..., "revoked_at_revision": 2}],
#  "revocations": [{"revision": 2, "application_id": 1, "reason": "活动改期"}]}

curl -s http://localhost:8000/api/revision/999
# HTTP 404 {"error": {"code": "revision_out_of_range", ...}}
```

批准记录内嵌的 `application` 同样是**该修订号当时**的视图。设许可 A 在修订号
**1** 获批、修订号 **2** 被暂停（`POST /api/applications/1/suspend`），则修订号 1
的批准记录内嵌申请仍是 `approved`、无暂停号——之后的暂停不会改写旧视图：

```bash
curl -s http://localhost:8000/api/revision/1
# {"revision": 1, "effective_application_ids": [1],
#  "approvals": [{"revision": 1, "application_id": 1, "status_as_of": "approved",
#    "application": {"id": 1, "status": "approved",
#                    "approval_revision": 1, "suspended_revision": null,
#                    "force_suspended_by": null, ...}}], ...}

curl -s http://localhost:8000/api/revision/2
# {"revision": 2, "effective_application_ids": [], "suspended_application_ids": [1],
#  "approvals": [{"revision": 1, "application_id": 1, "status_as_of": "suspended",
#    "suspended_at_revision": 2,
#    "application": {"id": 1, "status": "suspended",
#                    "approval_revision": 1, "suspended_revision": 2,
#                    "force_suspended_by": null, ...}}], ...}
```

之后在修订号 **3** 恢复（`POST /api/applications/1/resume`）再查修订号 1、2，结果
与上面完全一致；被紧急占频强制暂停的许可同理——旧修订号视图中的
`force_suspended_by` 仍是当时强制它的紧急申请，紧急解除不会追改。修订对比
（`/api/revision/compare`）区间内审批事件的内嵌申请同理取**较新一侧修订号**的回放
视图；而 `GET /api/applications/1` 与 `/api/revision/current` 始终反映实时状态。

### 6.11 临界值速查（保护带宽 2 MHz）

| 场景 | 结论 |
| --- | --- |
| 频段 `[100,110]` 对 `[110,120]`（闭区间端点相碰） | 冲突 `frequency_intersection` |
| 频段 `[100,110]` 对 `[111,112]`（间隔 1 < 2） | 冲突 `within_guard_band` |
| 频段 `[100,110]` 对 `[112,120]`（间隔 2 == 2） | **不冲突**（严格小于） |
| 时段 `[08,10)` 对 `[10,12)`（左闭右开端点相接） | 时段**不**相交 |
| 两圆相切（圆心距 == 半径和） | 区域相交 |

### 6.12 整组批准：同一场活动的多台设备一次通过

先提交三台互不干扰的设备（频段间隔均大于保护带宽），当前修订号为 0：

```bash
# 假设返回 id 依次为 10、11、12（各自 POST /api/applications）
curl -s -X POST http://localhost:8000/api/applications/batch-approve \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 0, "application_ids": [12, 10, 11]}'
```

整组通过，**按输入顺序**占用连续修订号 1、2、3（注意顺序就是 `[12,10,11]`）：

```json
{"approved": true, "expected_revision": 0,
 "from_revision": 0, "to_revision": 3, "revision": 3, "guard_band": 2.0,
 "application_ids": [12, 10, 11],
 "results": [
   {"application_id": 12, "revision": 1, "application": {"id": 12, "status": "approved", "approval_revision": 1, ...}},
   {"application_id": 10, "revision": 2, "application": {"id": 10, "status": "approved", "approval_revision": 2, ...}},
   {"application_id": 11, "revision": 3, "application": {"id": 11, "status": "approved", "approval_revision": 3, ...}}
 ]}
```

按其中**任一**修订号查询，即还原截至该项获批时的有效集合：

```bash
curl -s http://localhost:8000/api/revision/2
# {"revision": 2, "effective_application_ids": [12, 10], ...}
```

### 6.13 整组失败：组内互扰 → 整组不批准，修订号不变

`13` 与 `14` 频段相交；即使 `15` 本身干净，整组也全部不批准
（冲突同时挂在组内双方，`scope` 为 `group`；当前有效许可冲突时为 `effective`）：

```bash
curl -i -X POST http://localhost:8000/api/applications/batch-approve \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 3, "application_ids": [13, 14, 15]}'
```

```json
HTTP/1.1 409 Conflict
{"error": {"code": "interference",
  "message": "2 application(s) in the batch interfere with effective grants or with each other; ...",
  "details": {"expected_revision": 3, "current_revision": 3,
    "applications": [
      {"application_id": 13, "conflicts": [{"scope": "group", "application": {"id": 14, ...}, "trigger": {...}, "reason": "frequency_intersection"}]},
      {"application_id": 14, "conflicts": [{"scope": "group", "application": {"id": 13, ...}, "trigger": {...}, "reason": "frequency_intersection"}]}
    ]}}}
```

此后 13/14/15 仍是 `pending`，当前修订号仍为 3；修正分组后可用相同修订号重试。
其余失败类型同样整组不批准、不消耗修订号：

```bash
# 列表为空 / 含重复 ID
curl -i -X POST .../batch-approve -d '{"expected_revision": 3, "application_ids": [13, 13]}'
# HTTP 422 {"error": {"code": "validation_failed",
#   "details": [{"field": "application_ids", "code": "duplicate_application_id",
#                "duplicates": [13], ...}]}}

# ID 无效
curl -i -X POST .../batch-approve -d '{"expected_revision": 3, "application_ids": [13, 999]}'
# HTTP 404 {"error": {"code": "not_found",
#   "details": {"missing_application_ids": [999], "expected_revision": 3, "current_revision": 3}}}

# 状态不符（已批准/已撤销）
curl -i -X POST .../batch-approve -d '{"expected_revision": 3, "application_ids": [12, 13]}'
# HTTP 409 {"error": {"code": "not_pending",
#   "details": {"applications": [{"application_id": 12, "status": "approved",
#                                 "approval_revision": 1}], ...}}}

# 修订过期（并发单笔/整组批准或撤销抢先）
curl -i -X POST .../batch-approve -d '{"expected_revision": 0, "application_ids": [13, 14]}'
# HTTP 409 {"error": {"code": "stale_revision",
#   "details": {"expected_revision": 0, "current_revision": 3}}}
```

### 6.14 保护带宽在线调整（预览 → 确认 → 历史还原）

设当前带宽为 **2 MHz**、修订号为 2，有效集合中有两个许可：A `[100,110]` 与
B `[113,114]`（最短间隔 3 MHz，同地同时段）。

**第 1 步：查看当前配置**

```bash
curl -s http://localhost:8000/api/config/guard-band
# {"current_revision": 2, "guard_band": 2.0, "initial_guard_band": 2.0}
```

**第 2 步：预览拟定带宽 4 MHz（不改状态）**——新带宽下 A、B 间隔 3 < 4，会互扰：

```bash
curl -s -X POST http://localhost:8000/api/config/guard-band/preview \
  -H 'Content-Type: application/json' -d '{"guard_band": 4.0}'
```

```json
{"current_revision": 2, "current_guard_band": 2.0, "proposed_guard_band": 4.0,
 "effective_application_ids": [1, 2],
 "has_conflicts": true, "interfering_pair_count": 1,
 "interfering_pairs": [{
   "first":  {"id": 1, "frequency": {"low": 100.0, "high": 110.0}, ...},
   "second": {"id": 2, "frequency": {"low": 113.0, "high": 114.0}, ...},
   "trigger": {"time_intersect": true, "area_intersect": true,
               "frequency_intersect": false, "minimum_frequency_gap": 3.0,
               "within_guard_band": true},
   "reason": "within_guard_band"}]}
```

**第 3 步：确认该拟定值 → 拒绝（409），带宽/有效集合/修订号均不变**

```bash
curl -i -X POST http://localhost:8000/api/config/guard-band \
  -H 'Content-Type: application/json' \
  -d '{"guard_band": 4.0, "expected_revision": 2}'
# HTTP/1.1 409 Conflict
# {"error": {"code": "interference",
#   "message": "proposed guard band 4.0 would make 1 effective license pair(s) interfere; ...",
#   "details": {"current_revision": 2, "current_guard_band": 2.0,
#               "proposed_guard_band": 4.0, "interfering_pairs": [ ...同上... ]}}}
```

**第 4 步：改设为收紧的 1 MHz → 通过，恰好产生一个修订号（3）**

```bash
curl -s -X POST http://localhost:8000/api/config/guard-band \
  -H 'Content-Type: application/json' \
  -d '{"guard_band": 1.0, "expected_revision": 2}'
# {"updated": true, "revision": 3, "previous_guard_band": 2.0, "guard_band": 1.0}
```

此后申请预览、单笔与整组批准统一使用 **1 MHz**；携带旧修订号的确认/批准
返回 `stale_revision`：

```bash
curl -i -X POST http://localhost:8000/api/config/guard-band \
  -H 'Content-Type: application/json' \
  -d '{"guard_band": 0.5, "expected_revision": 2}'
# HTTP/1.1 409 Conflict {"error": {"code": "stale_revision",
#   "details": {"expected_revision": 2, "current_revision": 3}}}
```

**第 5 步：历史还原**——按修订号查询可还原当时带宽与有效集合；已有批准
记录保存的裁决带宽不被追改：

```bash
curl -s http://localhost:8000/api/revision/2
# {"revision": 2, "guard_band": 2.0,                 <- 当时的带宽
#  "guard_band_changes": [{"revision": 0, "previous_guard_band": null, "guard_band": 2.0, ...}],
#  "approvals": [{"revision": 1, "guard_band": 2.0, ...}, ...], ...}

curl -s http://localhost:8000/api/revision/3
# {"revision": 3, "guard_band": 1.0,
#  "guard_band_changes": [{"revision": 0, ..., "guard_band": 2.0},
#                         {"revision": 3, "previous_guard_band": 2.0, "guard_band": 1.0}], ...}
```

无效拟定值被明确拒绝（422，修订号不变）：

```bash
curl -i -X POST http://localhost:8000/api/config/guard-band \
  -H 'Content-Type: application/json' \
  -d '{"guard_band": -1, "expected_revision": 3}'
# HTTP/1.1 422 {"error": {"code": "validation_failed",
#   "details": [{"field": "guard_band", "code": "guard_band_negative",
#                "message": "guard_band must be non-negative"}]}}
```

### 6.15 可用调频位置查询（不改状态）

设保护带宽为 **2 MHz**、修订号为 1，有效集合中有许可 A `[110,120]`；申请 B
（待审批）频段为 `[100,105]`（宽度 5 MHz），与 A 同时段、覆盖区域相交。运营人员
想在候选下界区间 `[90, 130]` 内为 B 寻找可用位置：

```bash
curl -s -X POST http://localhost:8000/api/applications/2/available-ranges \
  -H 'Content-Type: application/json' \
  -d '{"candidate_low_min": 90, "candidate_low_max": 130}'
```

```json
{"application_id": 2, "current_revision": 1, "guard_band": 2.0, "bandwidth": 5.0,
 "candidate_low_min": 90.0, "candidate_low_max": 130.0,
 "available_ranges": [
   {"min": 90.0,  "max": 103.0, "min_inclusive": true, "max_inclusive": true},
   {"min": 122.0, "max": 130.0, "min_inclusive": true, "max_inclusive": true}
 ]}
```

A 的频段 `[110,120]` 对下界 `x` 的禁区是 `(110-5-2, 120+2) = (103, 122)`：间隔
**恰好等于**保护带宽 2 允许（端点 `103`、`122` 均可取，`max_inclusive` /
`min_inclusive` 为 `true`）。把 B 的频段平移到任一可取下界（如 `[103,108]`，
与 A 间隔恰为 2）再预览，`has_conflicts` 必为 `false`；平移到禁区内
（如 `[110,115]`）必为 `true`。

若保护带宽为 **0**，同一查询的两段端点变为不可取（端点相碰算干扰）：

```json
"available_ranges": [
  {"min": 90.0,  "max": 105.0, "min_inclusive": true,  "max_inclusive": false},
  {"min": 120.0, "max": 130.0, "min_inclusive": false, "max_inclusive": true}
]
```

无可用位置时返回空列表；查询不产生任何修订号、不改变申请状态：

```bash
curl -s -X POST http://localhost:8000/api/applications/2/available-ranges \
  -H 'Content-Type: application/json' \
  -d '{"candidate_low_min": 111, "candidate_low_max": 119}'
# {"application_id": 2, ..., "available_ranges": []}

# 倒置区间 / 非有限值 -> 422；未知 ID -> 404；非待审批申请 -> 409
curl -i -X POST http://localhost:8000/api/applications/2/available-ranges \
  -H 'Content-Type: application/json' \
  -d '{"candidate_low_min": 130, "candidate_low_max": 90}'
# HTTP/1.1 422 {"error": {"code": "validation_failed",
#   "details": [{"field": "candidate_low_min",
#                "code": "candidate_low_min_above_max", ...}]}}
```

脚本版走查：[`examples/walkthrough.sh`](examples/walkthrough.sh)
（`./examples/walkthrough.sh http://localhost:8000`），其中第 26–31 步为
暂停 / 恢复完整走查、第 32–37 步为候补队列完整走查。

### 6.16 临时停机：暂停 → 期间另批新许可 → 恢复互扰 → 撤销新许可 → 恢复

设保护带宽为 **2 MHz**。许可 A（`[100,110]`）已在修订号 **1** 获批。

**第 1 步：设备临时停机，暂停 A（产生修订号 2，立即退出有效集合）**

```bash
curl -s -X POST http://localhost:8000/api/applications/1/suspend \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1, "reason": "设备临时停机"}'
```

```json
{"suspended": true, "application_id": 1, "revision": 2,
 "previous_approval_revision": 1, "reason": "设备临时停机",
 "application": {"id": 1, "status": "suspended",
                 "approval_revision": 1, "suspended_revision": 2, ...}}
```

暂停后，现有预览、单笔/整组批准、可用调频查询、带宽预览都不再计入 A。

**第 2 步：停机期间，与 A 频段相交的设备 C 正常获批（修订号 3）**

```bash
# C 频段 [105,108]；A 在暂停中，预览无冲突
curl -s -X POST http://localhost:8000/api/applications/3/preview
# {"current_revision": 2, "has_conflicts": false, ...}
curl -s -X POST http://localhost:8000/api/applications/3/approve \
  -H 'Content-Type: application/json' -d '{"expected_revision": 2}'
# {"approved": true, "revision": 3, ...}
```

**第 3 步：故障解除恢复 A——重新裁决与 C 互扰 → 409，A 保持暂停、不占修订号**

```bash
curl -i -X POST http://localhost:8000/api/applications/1/resume \
  -H 'Content-Type: application/json' -d '{"expected_revision": 3}'
```

```json
HTTP/1.1 409 Conflict
{"error": {"code": "interference",
  "message": "application 1 would interfere with 1 effective grant(s); the license stays suspended and no revision was consumed",
  "details": {"current_revision": 3, "conflicts": [
    {"application": {"id": 3, ...}, "trigger": {...},
     "reason": "frequency_intersection"}]}}}
```

携带过期修订号恢复同样被拒，返回当前修订号且不改变任何状态：

```bash
curl -i -X POST http://localhost:8000/api/applications/1/resume \
  -H 'Content-Type: application/json' -d '{"expected_revision": 2}'
# HTTP/1.1 409 {"error": {"code": "stale_revision",
#   "details": {"expected_revision": 2, "current_revision": 3}}}
```

**第 4 步：改用既有撤销操作永久移除 C（修订号 4），再恢复 A（修订号 5）**

```bash
curl -s -X POST http://localhost:8000/api/applications/3/revoke \
  -H 'Content-Type: application/json' -d '{"reason": "冲突清退"}'
# {"revoked": true, "revision": 4, ...}
curl -s -X POST http://localhost:8000/api/applications/1/resume \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 4, "reason": "故障解除"}'
```

```json
{"resumed": true, "application_id": 1, "revision": 5,
 "original_approval_revision": 1, "guard_band": 2.0,
 "reason": "故障解除",
 "application": {"id": 1, "status": "approved",
                 "approval_revision": 1, "suspended_revision": null, ...}}
```

注意 A 的 `approval_revision` 仍是 **1**——恢复不是新申请、原批准事件不追改。

**非法状态与未知 ID 均明确拒绝（不占修订号）：**

```bash
# 待审批申请不能暂停
curl -i -X POST .../applications/2/suspend -d '{"expected_revision": 5}'
# 409 {"error": {"code": "not_approved", "details": {"status": "pending"}}}

# 暂停中的许可不能重复暂停
curl -i -X POST .../applications/1/suspend -d '{"expected_revision": 5}'
# 409 {"error": {"code": "already_suspended",
#   "details": {"suspended_at_revision": 2}}}

# 未暂停的许可不能恢复
curl -i -X POST .../applications/1/resume -d '{"expected_revision": 5}'
# 409 {"error": {"code": "not_suspended", "details": {"status": "approved"}}}

# 暂停期间也可直接永久撤销，撤销后不能恢复（响应附 was_suspended）
curl -s -X POST .../applications/1/revoke -d '{"reason": "活动取消"}'
# {"revoked": true, "was_suspended": true, ...}
curl -i -X POST .../applications/1/resume -d '{"expected_revision": 6}'
# 409 {"error": {"code": "already_revoked"}}

# 未知 ID
curl -i -X POST .../applications/999/suspend -d '{"expected_revision": 5}'
# 404 {"error": {"code": "not_found", ...}}
```

**历史还原**：暂停、恢复记录与各修订的有效集合都可按修订号还原；批准记录的
`status_as_of` 会经历 `approved → suspended → approved`（或最终 `revoked`），
原批准事件本身始终不变：

```bash
curl -s http://localhost:8000/api/revision/2
# {"revision": 2, "effective_application_ids": [], "suspended_application_ids": [1],
#  "approvals": [{"revision": 1, "status_as_of": "suspended",
#                 "suspended_at_revision": 2, ...}],
#  "suspensions": [{"revision": 2, "application_id": 1, "reason": "设备临时停机"}],
#  "resumptions": []}

curl -s http://localhost:8000/api/revision/5
# {"revision": 5, "effective_application_ids": [1], "suspended_application_ids": [],
#  "suspensions": [...], "resumptions": [{"revision": 5, "guard_band": 2.0, ...}],
#  "approvals": [{"revision": 1, "status_as_of": "approved", ...}], ...}
```

### 6.17 候补队列：登记 → 撤销释放 → 自动重裁 → 历史逐项还原

设保护带宽 **2 MHz**、修订号为 1，有效许可 A `[100,110]`（修订号 1 获批）。待审
申请 B `[105,108]`、C `[106,109]`、D `[200,210]` 同地同时段。

**第 1 步：登记候补（不占修订号，返回排队顺序与当前修订号）**

```bash
curl -s -X POST http://localhost:8000/api/applications/2/waitlist \
  -H 'Content-Type: application/json' -d '{"reason": "band busy"}'
# {"waitlisted": true, "application_id": 2, "queue_position": 1,
#  "current_revision": 1, "reason": "band busy", "registered_at": "..."}
curl -s -X POST http://localhost:8000/api/applications/3/waitlist \
  -H 'Content-Type: application/json' -d '{}'
# {"waitlisted": true, "application_id": 3, "queue_position": 2, "current_revision": 1, ...}
curl -s -X POST http://localhost:8000/api/applications/4/waitlist \
  -H 'Content-Type: application/json' -d '{}'
# queue_position 3；当前修订号仍是 1（登记不占修订号）
```

重复登记、对非待审申请登记均被拒绝；可查询整条队列或取消：

```bash
curl -i -X POST http://localhost:8000/api/applications/2/waitlist \
  -H 'Content-Type: application/json' -d '{}'
# HTTP 409 {"error": {"code": "already_waitlisted", "details": {"position": 1}}}
curl -s http://localhost:8000/api/waitlist
# {"current_revision": 1, "queue_length": 3,
#  "waitlist_application_ids": [2, 3, 4], "entries": [{"position": 1, ...}, ...]}
```

**第 2 步：撤销 A（释放）——同一事务内按登记先后重裁候补**

A 撤销占修订号 **2**；B 随即通过 → 自动批准占修订号 **3**；C 与刚获批的 B
频段相交 → 保留候补，继续检查；D 通过 → 自动批准占修订号 **4**：

```bash
curl -s -X POST http://localhost:8000/api/applications/1/revoke \
  -H 'Content-Type: application/json' -d '{"reason": "event cancelled"}'
```

```json
{"revoked": true, "revision": 2, "final_revision": 4,
 "auto_approved_application_ids": [2, 4],
 "auto_approvals": [
   {"application_id": 2, "revision": 3, "guard_band": 2.0, "release_revision": 2},
   {"application_id": 4, "revision": 4, "guard_band": 2.0, "release_revision": 2}],
 "remaining_waitlist_application_ids": [3],
 "waitlist_pending": [
   {"application_id": 3, "conflicts": [
     {"application": {"id": 2}, "reason": "frequency_intersection", "trigger": {}}]}]}
```

暂停释放（`.../suspend`）的响应结构完全相同（`revision` 为暂停自己的修订号，
另附 `suspended: true` 与原批准修订号）。

**第 3 步：未获批者保留候补，下次释放再裁；手动批准者先退队**

```bash
# C 仍在队首；撤销 B（修订号 5）后 C 通过 -> 自动批准修订号 6
curl -s -X POST http://localhost:8000/api/applications/2/revoke \
  -H 'Content-Type: application/json' -d '{}'
# {"revoked": true, "revision": 5, "final_revision": 6,
#  "auto_approved_application_ids": [3], "remaining_waitlist_application_ids": []}

# 对某候补先行手动批准（单笔或整组）时，它立即退队，之后的释放不会再自动批准它；
# GET /api/waitlist 中不再包含该 ID。
```

**第 4 步：取消候补不改申请状态、不占修订号**

```bash
curl -s -X POST http://localhost:8000/api/applications/3/waitlist/cancel
# {"cancelled": true, "application_id": 3, "current_revision": 1,
#  "application": {"id": 3, "status": "pending"}}   <- 仍是 pending，修订号不变
```

**第 5 步：释放失败时一切不变；恢复 / 带宽变更不触发重裁**

```bash
# 暂停时携带过期修订号 -> 409，队列、许可、修订号均不变（同事务回滚）
curl -i -X POST http://localhost:8000/api/applications/1/suspend \
  -H 'Content-Type: application/json' -d '{"expected_revision": 0}'
# HTTP 409 {"error": {"code": "stale_revision",
#   "details": {"expected_revision": 0, "current_revision": 1}}}

# resume 与 guard-band 确认即便成功也不触发候补重裁，队列原样保留。
```

**第 6 步：历史按修订号逐项还原自动审批**

```bash
curl -s http://localhost:8000/api/revision/2
# 释放修订号当时：effective_application_ids 为 []，revocations 含 A；尚无自动批准
curl -s http://localhost:8000/api/revision/3
# {"revision": 3, "effective_application_ids": [2],
#  "approvals": [{"revision": 3, "application_id": 2,
#                 "auto_approved": true, "release_revision": 2,
#                 "status_as_of": "approved", ...}, ...]}
curl -s http://localhost:8000/api/revision/4
# effective_application_ids 为 [2, 4]；3 仍 pending 且在候补队列中
```

脚本版走查：[`examples/walkthrough.sh`](examples/walkthrough.sh)
（`./examples/walkthrough.sh http://localhost:8000`），其中第 26–31 步为
暂停 / 恢复走查、第 32–37 步为候补队列完整走查。

### 6.18 临时频谱管制窗口：预演 → 确认 → 阻断写操作 → 解除

设保护带宽 **2 MHz**，许可 A `[100,110]` 已在修订号 **1** 获批；待审申请
B `[106,107]`、D `[200,210]`，B 已登记候补。运营人员要在 09:00–11:00 对同地
`[105,108]` 频段做临时管制。

**第 1 步：预演（不改状态、不占修订号）**——列出命中的当前有效许可与候补：

```bash
curl -s -X POST http://localhost:8000/api/restriction-windows/preview \
  -H 'Content-Type: application/json' \
  -d '{
    "circle":    {"cx": 0, "cy": 0, "radius": 1000},
    "frequency": {"low": 105, "high": 108},
    "time_window": {"start": "2026-10-05T09:00:00Z", "end": "2026-10-05T11:00:00Z"},
    "reason": "重要活动临时管制"
  }'
```

```json
{"window": {"circle": {...}, "frequency": {"low": 105.0, "high": 108.0}, ...},
 "current_revision": 1, "hit_count": 2,
 "hit_effective_application_ids": [1],
 "hit_effective": [{"application_id": 1,
   "trigger": {"time_intersect": true, "area_intersect": true,
               "frequency_intersect": true}}],
 "hit_waitlist_application_ids": [2],
 "hit_waitlist": [{"application_id": 2, "queue_position": 1,
   "trigger": {"time_intersect": true, "area_intersect": true,
               "frequency_intersect": true}}]}
```

预演后当前修订号仍是 1。频段端点相碰也算命中：把窗口频段改成 `[110,120]`
预演仍会命中 A（闭区间相碰）；但 `[111,112]` 不命中（间隔 1 MHz 虽小于保护带宽
2 MHz，**保护带宽不扩张窗口**）；窗口时段从 A 的结束时刻 `12:00` 开始也不命中
（左闭右开）。

**第 2 步：携预期修订号确认（占修订号 2）**——既有许可 A 继续有效：

```bash
curl -s -X POST http://localhost:8000/api/restriction-windows \
  -H 'Content-Type: application/json' \
  -d '{
    "circle":    {"cx": 0, "cy": 0, "radius": 1000},
    "frequency": {"low": 105, "high": 108},
    "time_window": {"start": "2026-10-05T09:00:00Z", "end": "2026-10-05T11:00:00Z"},
    "reason": "重要活动临时管制", "expected_revision": 1
  }'
# {"activated": true, "window_id": 1, "id": 1, "revision": 2,
#  "window": {"id": 1, "status": "active", "revision": 2, ...},
#  "hit_effective_application_ids": [1], ...}

curl -s http://localhost:8000/api/revision/current | \
  python3 -c 'import json,sys; d=json.load(sys.stdin); print("effective =", d["effective_application_ids"], "windows =", d["active_restriction_window_ids"])'
# effective = [1] windows = [1]     <- A 未被撤销，继续有效
```

过期修订号确认返回 409 `stale_revision`，窗口、许可、修订号均不变；预演/确认
一个 `end` 已过的窗口返回 422，明细码 `restriction_window_expired`。

**第 3 步：窗口生效后，待审预览报告命中，但原字段含义不变**

```bash
curl -s -X POST http://localhost:8000/api/applications/2/preview
# {"application_id": 2, "current_revision": 2,
#  "has_conflicts": false, "conflicts": [],                         <- 许可干扰（A 频段 [100,110] 与 B 相交时这里可能同时为 true）
#  "has_restriction_window_conflicts": true,
#  "restriction_window_conflicts": [{"window": {"id": 1, ...},
#    "trigger": {"time_intersect": true, "area_intersect": true,
#                "frequency_intersect": true},
#    "reason": "restriction_window"}],
#  "active_restriction_window_ids": [1]}
```

**第 4 步：写操作不得使命中申请生效（409，不占修订号）**

```bash
curl -i -X POST http://localhost:8000/api/applications/2/approve \
  -H 'Content-Type: application/json' -d '{"expected_revision": 2}'
# HTTP/1.1 409 Conflict
# {"error": {"code": "restriction_window",
#   "message": "application 2 hits 1 active restriction window(s); ...",
#   "details": {"current_revision": 2,
#     "restriction_window_conflicts": [{"window": {"id": 1, ...}, "trigger": {...}}],
#     "conflicts": []}}}
# B 仍是 pending；修订号仍是 2。
```

整组批准中若有成员命中窗口，整组不批准（命中成员在
`details.applications[].restriction_window_conflicts` 列出；若组内另有普通许可
干扰则错误码仍是 `interference`）；暂停许可的恢复同样被窗口阻止
（409 `restriction_window`，许可保持暂停）。

**第 5 步：候补释放重裁时命中窗口者保留队列并继续检查后项**

暂停 A（释放，修订号 3）触发同一事务内的候补重裁：B 已无许可冲突但命中窗口 →
保留候补；D `[200,210]` 不命中任何窗口 → 自动批准（修订号 4）：

```bash
curl -s -X POST http://localhost:8000/api/applications/1/suspend \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 2, "reason": "为后项让路"}'
# {"suspended": true, "revision": 3, "final_revision": 4,
#  "auto_approved_application_ids": [4],
#  "remaining_waitlist_application_ids": [2],
#  "waitlist_pending": [{"application_id": 2, "conflicts": [],
#    "restriction_window_conflicts": [{"window": {"id": 1, ...}}]}]}
```

**第 6 步：可用调频范围排除会命中窗口的位置**

B 频段宽度 1 MHz；窗口 `[105,108]` 对下界 `x` 的禁区为**闭**区间 `[104,108]`
（两端都不可取，且不被保护带宽加宽）：

```bash
curl -s -X POST http://localhost:8000/api/applications/2/available-ranges \
  -H 'Content-Type: application/json' -d '{"candidate_low_min": 100, "candidate_low_max": 115}'
# {"available_ranges": [
#   {"min": 100.0, "max": 104.0, "min_inclusive": true,  "max_inclusive": false},
#   {"min": 108.0, "max": 115.0, "min_inclusive": false, "max_inclusive": true}], ...}
```

**第 7 步：凭窗口 ID 与预期修订号解除（占修订号 5；不主动审批候补）**

```bash
curl -s -X POST http://localhost:8000/api/restriction-windows/1/release \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 4, "reason": "管制结束"}'
# {"released": true, "window_id": 1, "revision": 5,
#  "activated_at_revision": 2, "reason": "管制结束", "window": {...}}

curl -s http://localhost:8000/api/waitlist |
  python3 -c 'import json,sys; print("queue =", json.load(sys.stdin)["waitlist_application_ids"])'
# queue = [2]      <- 解除不触发自动审批，B 仍在候补
```

重复解除 → 409 `restriction_window_not_active`；未知 ID → 404
`restriction_window_not_found`；过期修订号 → 409 `stale_revision`（均不占修订号）。
解除后 B 的裁决不再受阻：之后手动批准，或等待下一次暂停/撤销释放自动批准。

**第 8 步：历史查询与对比还原当时的窗口与变动，原批准事件不追改**

```bash
curl -s http://localhost:8000/api/revision/2
# {"revision": 2, "active_restriction_window_ids": [1],
#  "active_restriction_windows": [{"id": 1, "status": "active", "revision": 2, ...}],
#  "restriction_window_events": [{"revision": 2, "event_type": "activated",
#                                "window_id": 1, "window": {...}}],
#  "approvals": [{"revision": 1, "status_as_of": "approved", ...}]}

curl -s "http://localhost:8000/api/revision/compare?base_revision=1&target_revision=5"
# {"active_restriction_window_ids": {"base": [], "target": [], "added": [], "removed": [1]},
#  "restriction_window_events": [
#    {"type": "restriction_window_activated", "revision": 2, ...},
#    {"type": "restriction_window_released",  "revision": 5, ...}], ...}

curl -s "http://localhost:8000/api/restriction-windows?include_inactive=true"
# {"active_windows": [],
#  "released_windows": [{"id": 1, "status": "released",
#    "activated_at_revision": 2, "released_at_revision": 5, ...}]}
```

### 6.19 活动设备换机：许可交接（预演 → 确认 → 连续修订号 → 候补重裁 → 历史还原）

保护带宽 **2 MHz**、同地同时段。许可 A（`[100,110]`）当前有效；接替申请 B
（`[100,102]`，与 A 频段相交，平时不能批准）已登记为候补；候补 C（`[130,132]`）
在 A 让出频谱后可获批；候补 D（`[131,133]`）与 C 相交，将继续排队。

**第 1 步：只读预演（排除原许可 A 后重算，不改状态、不占修订号）**

```bash
curl -s -X POST http://localhost:8000/api/license-transfers/preview \
  -H 'Content-Type: application/json' \
  -d '{"source_application_id":1,"successor_application_id":2,"reason":"设备换机"}'
# {"current_revision":1,"guard_band":2.0,"ready":true,"blockers":[],
#  "source_application_id":1,"successor_application_id":2,
#  "effective_application_ids":[],                         <- 原许可 A 已被排除
#  "conflicts":[],"has_restriction_window_conflicts":false,
#  "successor_waitlist_position":1,
#  "planned_revoc_revision":2,"planned_successor_revision":3}
# GET /api/revision/current 的 current_revision 仍是 1，A 仍 approved、B 仍 pending
```

若排除 A 后 B 仍与另一张有效许可 C 冲突（如 B `[128,129]`、C `[130,140]`，
间隔 1 < 2），预演返回 `"ready":false`，`conflicts` 列出 C 且 `blockers` 含
`{"code":"interference",...}`；原许可已暂停/撤销或接替申请已非待审时，
`blockers` 分别给出 `source_not_effective` / `successor_not_pending`。

**第 2 步：确认（共同修订仲裁内重算，整笔生效或整笔不变）**

```bash
curl -s -X POST http://localhost:8000/api/license-transfers \
  -H 'Content-Type: application/json' \
  -d '{"source_application_id":1,"successor_application_id":2,
       "expected_revision":1,"reason":"设备换机"}'
# {"transferred":true,
#  "revocation_revision":2,"successor_approval_revision":3,
#  "revision":3,"to_revision":4,"final_revision":4,
#  "successor_was_waitlisted":true,                        <- B 已先退队
#  "revocation":{"revision":2,"application_id":1,"previous_approval_revision":1,...},
#  "approval":{"revision":3,"application_id":2,"guard_band":2.0,"auto_approved":false},
#  "auto_approved_application_ids":[3],                   <- 现有 FIFO 重裁：C 通过
#  "auto_approvals":[{"application_id":3,"revision":4,
#                     "auto_approved":true,"release_revision":2,...}],
#  "remaining_waitlist_application_ids":[4]}              <- D 与 C 相交，保留候补
```

修订号过期 → 409 `stale_revision`；确认时原许可已暂停/撤销 → 409
`source_not_effective`；接替申请已被处理 → 409 `successor_not_pending`（已撤销为
`already_revoked`）；接替申请命中生效窗口 → 409 `restriction_window`；与其他
有效许可冲突 → 409 `interference`。任一拒绝下 **A 不撤销、B 不批准、候补不动、
修订号不变**。原许可与接替申请相同 → 422 `same_application`。

**第 3 步：按修订号逐项还原（撤销与批准语义保持，交接可还原）**

```bash
curl -s http://localhost:8000/api/revision/2
# {"revision":2,"effective_application_ids":[],          <- A 已撤销、B 尚未批准的中间修订
#  "revocations":[{"revision":2,"application_id":1,...}],
#  "approvals":[{"revision":1,"application_id":1,"status_as_of":"revoked",
#                "revoked_at_revision":2}],
#  "license_transfers":[{"revision":2,"source_application_id":1,
#                        "successor_application_id":2,
#                        "successor_approval_revision":3,"reason":"设备换机",...}]}

curl -s http://localhost:8000/api/revision/3
# {"revision":3,"effective_application_ids":[2],
#  "approvals":[{... A, "status_as_of":"revoked"},
#               {"revision":3,"application_id":2,"status_as_of":"approved",
#                "auto_approved":false,"release_revision":null}]}

curl -s http://localhost:8000/api/revision/4
# {"revision":4,"effective_application_ids":[2,3],       <- FIFO 自动批准 C（release_revision=2）
#  "suspended_application_ids":[]}

curl -s "http://localhost:8000/api/revision/compare?base_revision=1&target_revision=4"
# {"events":[
#    {"type":"revocation","revision":2,"application_id":1,...},
#    {"type":"license_transfer","revision":2,"source_application_id":1,
#     "successor_application_id":2,"successor_approval_revision":3,...},
#    {"type":"approval","revision":3,"application_id":2,...},
#    {"type":"approval","revision":4,"application_id":3,"auto_approved":true,
#     "release_revision":2,...}],
#  "status_changes":[{"application_id":1,"from_status":"approved","to_status":"revoked",...},
#                    {"application_id":2,"from_status":"pending","to_status":"approved",...},
#                    {"application_id":3,"from_status":"pending","to_status":"approved",...}],
#  "effective_application_ids":{"base":[1],"target":[2,3],...}}
# 加 &application_id=1 或 =2 都会带出这条交接（匹配原许可或接替申请任一方）
```

重启服务后 `current_revision`、有效集合、候补队列、保护带宽与交接历史均保持
不变（`GUARD_BAND` 不覆盖已生效值）。

**附：生效紧急许可不能作为交接原许可（预演阻断、确认 409、整笔不变）**

紧急占频只能经专门解除结束。下例中申请 9 以紧急占用获批（修订号 7），申请 10
是待审接替申请；沿用相同的原许可 ID、接替申请 ID 与确认时的预期修订号调用：

```bash
# 预演：ready=false，blockers 说明不可交接（只读，不占修订号）
curl -s -X POST http://localhost:8000/api/license-transfers/preview \
  -H 'Content-Type: application/json' \
  -d '{"source_application_id":9,"successor_application_id":10,"reason":"设备换机"}'
# {"current_revision":7,"ready":false,
#  "blockers":[{"code":"emergency_license_protected",
#               "field":"source_application_id","status":"approved",
#               "message":"source application 9 holds an active emergency occupation; ..."}],
#  "successor_application":{"id":10,"status":"pending",...}, ...}

# 确认：共用修订仲裁内返回 409，原许可/接替申请/候补/占频记录/修订号均不变
curl -s -i -X POST http://localhost:8000/api/license-transfers \
  -H 'Content-Type: application/json' \
  -d '{"source_application_id":9,"successor_application_id":10,
       "expected_revision":7,"reason":"设备换机"}'
# HTTP/1.1 409 Conflict
# {"error":{"code":"emergency_license_protected",
#   "message":"source application 9 holds an active emergency occupation; release ...",
#   "details":{"application_id":9,"status":"approved","current_revision":7,
#              "emergency_occupation_id":3,"emergency_approval_revision":7}}}
```

须先走专门解除（`POST /api/emergency-occupations/9/release` 携当前
`expected_revision`）；解除后申请 9 为普通**已撤销**许可，再交接按既有规则返回
409 `source_not_effective`。普通（非紧急）有效许可的交接结果与历史事件语义不变。

### 6.20 预约自动审批：预约 → 改期/取消 → 到期重裁（通过/阻塞）→ 失效与历史

约定时刻必须是 **UTC**（示例用 `date -u` 动态生成，避免示例过期）；申请自身的
申请时段结束必须晚于约定时刻。

```bash
# 1) 提交待审申请 S（申请时段为未来 2~3 天，覆盖圆/频段自定）
curl -s -X POST http://localhost:8000/api/applications \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"S\",\"circle\":{\"cx\":0,\"cy\":0,\"radius\":10},
       \"frequency\":{\"low\":300,\"high\":310},
       \"time_window\":{\"start\":\"$(date -u -d '+2 days' +%FT%TZ)\",
                        \"end\":\"$(date -u -d '+3 days' +%FT%TZ)\"}}"
# -> {"id": 1, "status": "pending", ...}

# 2) 预约 1 小时后自动审批（必须严格晚于现在、严格早于申请时段结束）
curl -s -X POST http://localhost:8000/api/applications/1/scheduled-approvals \
  -H 'Content-Type: application/json' \
  -d "{\"scheduled_at\":\"$(date -u -d '+1 hour' +%FT%TZ)\", \"reason\":\"等运营确认窗口\"}"
# HTTP 201 ->
# {"booked": true, "action": "booked", "id": 7, "scheduled_approval_id": 7,
#  "application_id": 1, "scheduled_at": "2026-10-03T19:30:00Z",
#  "status": "scheduled", "current_revision": 0, ...}

# 再次调用同一接口 = 改期（同一预约 ID，HTTP 200）
curl -s -X POST http://localhost:8000/api/applications/1/scheduled-approvals \
  -H 'Content-Type: application/json' \
  -d "{\"scheduled_at\":\"$(date -u -d '+2 hours' +%FT%TZ)\"}"
# {"action": "rescheduled", "scheduled_approval_id": 7, ...}

# 显式改期 / 取消（到期前；不占修订号）
curl -s -X POST http://localhost:8000/api/applications/1/scheduled-approvals/reschedule \
  -H 'Content-Type: application/json' \
  -d "{\"scheduled_at\":\"$(date -u -d '+90 minutes' +%FT%TZ)\"}"
curl -s -X POST http://localhost:8000/api/applications/1/scheduled-approvals/cancel \
  -H 'Content-Type: application/json' -d '{"reason":"活动取消"}'
# {"cancelled": true, "status": "cancelled", ...}

# 非法时刻：不晚于当前 / 不早于申请时段结束 / 无时区 -> 422 且不留预约
curl -s -X POST http://localhost:8000/api/applications/1/scheduled-approvals \
  -H 'Content-Type: application/json' -d '{"scheduled_at":"2020-01-01T00:00:00Z"}'
# HTTP 422 {"error":{"code":"scheduled_time_invalid",
#   "details":[{"field":"scheduled_at","code":"scheduled_at_not_in_future",...}]}}
```

到期自动重裁（把预约改到 2 秒后，后台轮询默认每 0.5 秒处理；也可立即手动触发）：

```bash
# 场景 A：到期时无干扰、未命中窗口 -> 批准，占一个修订号
curl -s -X POST http://localhost:8000/api/applications/1/scheduled-approvals \
  -H 'Content-Type: application/json' \
  -d "{\"scheduled_at\":\"$(date -u -d '+2 seconds' +%FT%TZ)\",\"reason\":\"\"}"
curl -s -X POST http://localhost:8000/api/scheduled-approvals/process-due
# {"processed_count": 1, "approved_application_ids": [1], "blocked_application_ids": [],
#  "from_revision": 0, "final_revision": 1,
#  "results":[{"outcome":"approved","application_id":1,"revision":1,
#              "processed_at":"2026-10-03T...Z","guard_band":2.0}]}

# 查询预约：状态 / 实际处理时间 / 审批修订号
curl -s http://localhost:8000/api/scheduled-approvals/7
# {"id":7,"status":"approved","scheduled_at":"...","processed_at":"...",
#  "approval_revision":1,"processed_guard_band":2.0,...}

# 历史修订标明“自动审批”，但仍是普通批准事件（scheduled=true, auto_approved=false）
curl -s http://localhost:8000/api/revision/1
# {"revision":1,
#  "approvals":[{"revision":1,"application_id":1,"scheduled":true,
#                "auto_approved":false,"release_revision":null,...}],
#  "scheduled_approvals":[{"id":7,"status":"approved",
#                          "approval_revision":1,...}], ...}
```

```bash
# 场景 B：到期时有干扰（或命中管制窗口）-> 记录阻塞明细，仍 pending，不占号，不重试
# 先让一张有效许可占住频段（申请 2：305–308 MHz，与 300–310 相交），再为其
# 待审申请 3（305–308 MHz 会自扰的场景请改用邻近频段，例如 309–311）预约 2 秒后
curl -s -X POST http://localhost:8000/api/applications/3/scheduled-approvals \
  -H 'Content-Type: application/json' \
  -d "{\"scheduled_at\":\"$(date -u -d '+2 seconds' +%FT%TZ)\"}"
curl -s -X POST http://localhost:8000/api/scheduled-approvals/process-due
# {"processed_count":1,"approved_application_ids":[],"blocked_application_ids":[3],
#  "from_revision":1,"final_revision":1,
#  "results":[{"outcome":"blocked","application_id":3,
#    "blocked_details":{"conflicts":[{"application":{"id":2},"reason":"frequency_intersection",...}],
#                       "restriction_window_conflicts":[]}}]}
curl -s http://localhost:8000/api/scheduled-approvals/{预约ID}
# -> status="blocked"，含 processed_at / processed_guard_band / blocked_details，
#    无 approval_revision；申请 3 仍 pending，再次 process-due 处理数为 0（不自动重试）

# 场景 C：申请先被手动批准（或交接/紧急/候补自动批准）-> 预约同事务失效
curl -s -X POST http://localhost:8000/api/applications/4/approve \
  -H 'Content-Type: application/json' -d '{"expected_revision":3}'
curl -s http://localhost:8000/api/scheduled-approvals/{预约ID}
# -> status="invalidated", outcome_reason="manual_approval"；到期扫描不再处理它
```

修订可见性（防止审计穿越；历史查询只读）：

```bash
# 申请在修订 0 预约、修订 1 被手动批准并令预约失效后：
curl -s http://localhost:8000/api/revision/0 | jq '.scheduled_approvals[0]
  | {status, scheduled_at, approval_revision, processed_at,
     outcome_reason, booked_revision, outcome_revision}'
# {"status":"scheduled","approval_revision":null,"processed_at":null,
#  "outcome_reason":null,"booked_revision":0,"outcome_revision":null}
#   ^ 旧修订仍是“待执行”，审批修订号/失效原因都不泄漏
curl -s http://localhost:8000/api/revision/1 | jq '.scheduled_approvals[0]
  | {status, outcome_reason, booked_revision, outcome_revision}'
# {"status":"invalidated","outcome_reason":"manual_approval",
#  "booked_revision":0,"outcome_revision":1}
#   ^ 失效只从决定修订（1）起显示

# 修订 1 之后才登记的预约不会出现在修订 0/1：
curl -s http://localhost:8000/api/revision/0 | jq '.scheduled_approvals
  | map(select(.application_id == 9))'   # -> []
# 改期同样按修订还原：修订 1 改到新时刻后，修订 0 快照仍是旧时刻与旧原因。
# 同一修订号内的登记/改期/取消不占号，该修订快照直接呈现最新当前状态。
```

启动可用性：服务重启时先对停机期间到期的预约做一次**补处理**（逻辑与
`process-due` 完全相同，按重启时刻的带宽/有效许可/窗口重裁），随后才开始接受
流量；轮询间隔可用 `SCHEDULED_APPROVAL_POLL_SECONDS` 调整，
`SCHEDULED_APPROVAL_BACKGROUND=0` 可仅保留手动/启动处理。

### 6.21 候选集收窄：大量时段不相交许可下的裁决（不漏判）

下例可直接运行 `./examples/candidate-narrowing.sh`（默认 100 条无关许可，
`N=300 ./examples/candidate-narrowing.sh` 可加压）。它先装入大量时段互不相交、
频段与覆盖范围都很远的有效许可，再演示各裁决链只装入真正可能冲突的候选：

```bash
# 1) N 个无关有效许可（各自独占一天，频段/圆也很远）——只演示其中一个请求体
curl -s -X POST http://localhost:8000/api/applications -H 'Content-Type: application/json' -d '{
  "name":"unrelated-1",
  "circle":{"cx":9000,"cy":9000,"radius":1},
  "frequency":{"low":9000,"high":9001},
  "time_window":{"start":"2031-01-01T00:00:00Z","end":"2031-01-01T01:00:00Z"}}'
# -> {"id":1,...}；随后 POST /api/applications/1/approve {"expected_revision":0}

# 2) 近处许可 [111,112]：与候选 [100,110] 间隔 1 MHz（< 2 MHz 保护带宽）
# 3) 预演候选：冲突明细只有近处许可，N 个无关许可不进入数据库候选集
curl -s -X POST http://localhost:8000/api/applications/{候选}/preview
# -> {"current_revision":N+1,
#     "conflicts":[{"application":{"id":N+1},"reason":"within_guard_band",...}],
#     "restriction_window_conflicts":[],"active_restriction_window_ids":[...]}

# 4) 可用频段下界查询覆盖整个候选下界区间 [90,140]（许可包络扩张到
#    [90-保护带宽, 140+带宽+保护带宽]，窗口包络不带保护带宽）
curl -s -X POST http://localhost:8000/api/applications/{候选}/available-ranges \
  -H 'Content-Type: application/json' -d '{"candidate_low_min":90,"candidate_low_max":140}'
# -> 近处许可 [111,112]（保护带宽 2）禁出开区间 (99,114)：
#    {"available_ranges":[{"min":90,"max":99,...},{"min":114,"max":140,...}]}

# 5) 锁内批准失败：409 interference，申请保持 pending，修订号不变
curl -s -X POST http://localhost:8000/api/applications/{候选}/approve \
  -H 'Content-Type: application/json' -d '{"expected_revision":N+1}'
# HTTP/1.1 409 {"error":{"code":"interference",
#   "details":{"current_revision":N+1,"conflicts":[{"application":{"id":N+1},...}]}}}

# 6) 管制窗口：保护带宽不扩张窗口——间隔 1 MHz 不命中；闭区间端点相碰仍命中
curl -s -X POST http://localhost:8000/api/restriction-windows/preview \
  -H 'Content-Type: application/json' -d '{"circle":{"cx":0,"cy":0,"radius":10},
    "frequency":{"low":113,"high":120},
    "time_window":{"start":"2031-12-20T08:00:00Z","end":"2031-12-20T11:00:00Z"}}'
# -> {"hit_count":0, ...}                          <- 1 MHz 间隔不算窗口命中

# 7) 保护带宽变更仍检查全部当前有效许可对（不做候选收窄）
curl -s -X POST http://localhost:8000/api/config/guard-band/preview \
  -H 'Content-Type: application/json' -d '{"guard_band":3.0}'
# -> {"effective_application_ids":[...全部 N+3 个...],
#     "interfering_pairs":[...间隔恰为 2 MHz 的那一对在 3 MHz 下出现...]}

# 8) 候补链路：候选先登记候补，撤销近处许可后在同一事务内 FIFO 自动重裁获批
curl -s -X POST http://localhost:8000/api/applications/{候选}/waitlist \
  -H 'Content-Type: application/json' -d '{"reason":"band busy"}'
curl -s -X POST http://localhost:8000/api/applications/{近处许可}/revoke \
  -H 'Content-Type: application/json' -d '{"expected_revision":N+3,"reason":"release"}'
# -> {"revoked":true,"auto_approved_application_ids":[{候选}],
#     "remaining_waitlist_application_ids":[],"final_revision":N+5}
```

收窄是**只多不漏**的预过滤：相切圆、频段端点相碰、保护带宽边界（间隔恰好等于
带宽时仍装入但精确规则放行）等边界记录全部保留；冲突明细顺序、修订仲裁、失败
时的状态（保持 pending/suspended、不占修订号）及全部 HTTP 调用与响应约定均与
收窄前一致。整组、恢复、预约到期（`process-due` 与后台/启动扫描）和紧急占用/
解除走同一套包络收窄 + 精确裁决；服务在大量时段不相交许可下启动与预演/写入
均保持可用。

---

## 7. 项目结构

```
.
├── app/
│   ├── main.py                 # FastAPI 路由、序列化、并发/快照逻辑（含整组批准、暂停/恢复、带宽在线配置、可用位置查询、候补队列/释放自动重裁、临时管制窗口与换机许可交接预演/确认；各裁决链先按时段/覆盖/频段在数据库内收窄候选集）
│   ├── adjudication.py         # 纯函数：圆/时段/频段/保护带宽裁决、管制窗口命中判定（含组内两两、有效集成对检查、含窗口闭禁区的可用下界区间求解）
│   ├── semantic_validation.py  # 整笔拒收的跨字段语义校验（含拟定带宽、候选下界区间与管制窗口形状/过期校验）
│   ├── schemas.py              # Pydantic 请求模型（含 BatchApprovalRequest、Suspension/ResumptionRequest、GuardBand*Request、AvailableRangesRequest、WaitlistRegistrationRequest、RestrictionWindow*Request、LicenseTransfer*Request）
│   ├── models.py               # SQLAlchemy 表（申请 + 批准/暂停/恢复/撤销/带宽/管制窗口/许可交接事件 + 生效管制窗口 + 候补队列 + 修订号/当前带宽；含候选收窄索引）
│   ├── db.py                   # 引擎、等待数据库、建表与旧库列/收窄索引升级、修订号与带宽初值初始化（重启不覆盖）
│   ├── config.py               # 环境变量配置（GUARD_BAND 仅作建库初值）
│   └── errors.py               # 统一错误信封
├── tests/                      # 裁决单元测试 + API 端到端测试（SQLite，含 test_waitlist.py 候补用例、test_restriction_windows*.py 管制窗口用例、test_candidate_narrowing.py 候选收窄边界用例）
├── examples/walkthrough.sh     # curl 走查脚本
├── examples/candidate-narrowing.sh # 大量时段不相交许可下的候选集收窄走查（本轮）
├── Dockerfile
├── docker-compose.yml          # db + api 一键启动
├── requirements.txt / requirements-dev.txt
└── README.md
```
