# 本地队列的 `--after` 依赖：等待前置工作

`timetrace add --after N` 加的任务（子任务）**只有在显式父任务 N 已 `done` 时才会启动**，并且这件事在它真正启动的那一刻、
持有执行槽位之后，再从数据库读一次父任务来确认。`timetrace retry`、之前读到的候选列表、旧版本写下的 `runnable` 行，
都绕不过这一步。

## 以前的问题

父任务失败（保留了工作树和会话）后，调度器把所有还没启动的子任务标成 `failed`（`dependency N failed`）：

- `timetrace retry <父任务>` 之后，子任务仍是 `failed`，只能重建。
- `timetrace retry <子任务>` 把它直接设为 `runnable`，它会越过未完成的父任务先跑。

## 现在的规则

| 情形 | 子任务 | 说明 |
| --- | --- | --- |
| 父任务排队、运行中、因额度暂停、自己在等待 | `pending`，继续等 | `timetrace ls` 显示 `等待前置工作 #N（运行中）` 等 |
| 父任务 `failed`，人还可以重试 | `pending`，继续等 | 显示 `等待前置工作 #N（已失败：先运行 timetrace retry N）`；重试父任务后沿用**同一工作树和会话**（`resume`），通过后才按顺序启动 |
| 更深的链：C → B → A，A 失败 | B、C 都 `pending` | C 显示 `等待前置工作 #B（上游 #A 已失败：先运行 timetrace retry A）`，指向真正要重试的那个 |
| 父任务（或链上的祖先）已被 `timetrace rm` 删除 | `failed`，`dependency missing`（祖先缺失时 `dependency N missing`） | 这是永远无法满足的前置，和单纯等待分开。本地队列没有单独的「取消」状态，`timetrace rm` 就是取消 |
| 子任务自己曾经运行过（有运行记录、会话或工作树）后失败 | 保持 `failed` | 父任务通过也**不会**自动重跑它；人决定是否 `timetrace retry` |
| 旧版本写下的 `runnable` 子任务，而父任务没完成 | 未运行过：回到 `pending`；运行过：`failed` 并写明原因 | 不启动、不占槽位、不挡住队列里其它独立任务 |

`timetrace retry`：

- 父任务已 `done`：和以前一样，变 `runnable`。
- 父任务没完成、子任务从未运行：子任务保持/回到 `pending`，输出 `等待前置工作 …`（重复执行也一样，不报错）。
- 父任务没完成、子任务自己运行过：**拒绝**（退出码 1，不改任何东西），先重试父任务。
- 父任务已不存在：拒绝。

没有新增状态、列或表：等待就是原来的 `pending`，等待什么由当时的父任务链实时算出，不存储（所以不会过期）。
`timetrace ls --json` 给每个任务多了一个 `prerequisite` 字段（`pending` 任务有内容，其余为 `null`）。

## 工作树准备之后：启动前再确认一次父任务

槽位内确认父任务 `done` 之后，还要准备工作树（`git worktree add` 等），这段时间另一个进程可以对已完成的父任务执行真正的
`timetrace rm`。所以父任务在**真正启动工具之前**还会被再读两次，且都读实时数据库：

1. **认领事务**：工作树就绪后，在一个很短的 `BEGIN IMMEDIATE` 事务里重读父任务，通过才把子任务写成 `running` 并新建运行记录。
   这是启动的线性化点：`timetrace rm` 要么整个发生在它之前（父任务已不存在 → 子任务不启动），要么整个发生在它之后
   （子任务是在父任务 `done` 时启动的，和「运行中的子任务的父任务被删」一样，不影响它）。事务只包含几条语句，**不在准备工作树期间持有，
   也不在模型运行期间持有**，所以真实的 `rm`、`retry`、`ls` 不会被卡住。
2. **spawn 关口**：已有的 `spawn_authority` 检查（显式的 `enforce_spawn_authority()`，以及 `run_streaming` / Claude 流式适配器在
   `Popen` 之前各自再调用一次）现在除了适配器自己的授权，还读一次父任务。认领之后、进程创建之前父任务没了，会以
   `dependency_missing`（或 `dependencies_pending`、`dependency_cycle`）拒绝。检查与 `Popen` 之间仍不是原子的（`process.py` 里早已
   注明），之后才发生的删除按「已启动的子任务」处理。

父任务在这两处任何一处缺失时：

- **绝不启动工具**；子任务 `failed`，`dependency missing`（父任务只是暂时没完成则回到 `pending`）。
- **不伪造执行记录**：没有运行记录（认领留下的那一条会被删掉）、不写会话 ID、任务行不写工作树；因此它仍是「从未运行过的任务」，
  不计入熔断器，也没有 `task_resumed` / `task_done` 事件。
- **工作树保留在磁盘上供核对**：路径和分支写在 `task_failed`（或 `task_waiting`）事件的 `worktree` / `branch` 字段里，输出也会提到。
- 其它独立任务不受影响，同一轮继续运行。

这一步只影响因父任务消失/变化而被拒绝的启动；适配器自身授权被撤销（如 `billing_unverified`）的拒绝流程不变。
`task_resumed` 事件现在在显式关口通过之后才写，被拒绝的恢复不会留下「已恢复」的记录。

## 不会被误锁的队列

- 候选任务在选工具**之前**先过一遍前置检查：起不来的任务不占槽位、不产生 `tool_switched` 事件，也不会让队列报告 `runner busy`。
- 某个候选被拦下后，同一轮继续看下一个：父任务失败、依赖缺失时，排在后面的独立任务照常运行（`timetrace run --once` 也一样）。
- 被拦下的任务没有运行记录，不计入熔断器的失败次数。
- 依赖形成环（只有手工改库才会出现）：环上的任务 `failed`，`dependency cycle`，不会永远等下去。
- 某一行的检查本身出错，只记一条 `loop_error` 事件，不影响其它依赖前进、也不让整轮调度报错。

## 混合版本

没有改库结构，进程锁（`scheduler.lock`、`agent.lock`）和槽位文件不变，所以新旧调度器仍然不能同时运行，也不会同时执行同一个任务。

- 新 → 旧：旧调度器看到「父任务失败」仍会把等待中的子任务标成 `failed`（`dependency N failed`），但不会启动它们。
- 旧 → 新：新调度器在下一轮把「恰好是 `dependency N failed`、从未运行过」的任务放回 `pending`（运行过的不动）；于是旧版造成的
  「子任务丢了」会自己恢复，不需要重建。
- 旧版 `timetrace retry <子任务>` 写下的 `runnable` 行，新调度器在槽位内拦下。旧调度器本身仍会放行它（这是旧版的行为，只能靠升级消除）。
- 新增的状态迁移都是「仅当仍处于读到时的状态才写」，即使跳过进程锁让多个新调度器同时跑，也不会把别人已推进的任务改回去、重复执行。

## 验证

- `tests/test_prerequisite_gate.py`：真实 CLI + 真实 SQLite + 无副作用的固定执行器（覆盖上表每一行、槽位内再读父任务、旧 `runnable` 行、缺父、深层链、已完成的兄弟不重跑）。
- `tests/test_prerequisite_compat.py` + `tests/queue_compat_worker.py`：子进程里的新调度器和**升级前（c1b577b）**调度器；复现旧行为、互斥、新旧交接、多进程竞争、老写入者并发改行。旧调度器角色现在连 `_dispatch_locked`（启动本身）也是 c1b577b 的原文副本，不再借用本树会变的函数。
- `tests/test_prerequisite_launch_gate.py`：独立进程里的真实 `timetrace rm`，分别在工作树准备时、在适配器 spawn 边界（经 `run_streaming` 与直接调用共享检查两种）执行；另有恢复中的子任务、认领事务的互斥、「模型运行/工作树准备期间不持有写锁」。
