# TimeTraceAgent（刻迹电脑端）

给 Claude Code 和 Codex 排任务的无人值守队列，也是刻迹 iOS App 的电脑端 Runner：手机派任务，这台 Mac 用你已经登录的 `claude` / `codex` 执行。撞到限额就睡到重置时间，再用原会话续接；顺手把两个工具每个限额桶的余量采样进 SQLite。

命令是 `timetrace`，短名 `tta`，两者完全等价。零依赖，Python 3.9+ 标准库，目前只支持 macOS。Apache-2.0 许可（见 `LICENSE`）；安全问题请按 `SECURITY.md` 私下报告。

## 快速开始

```sh
brew install underestimatedme/timetrace/timetraceagent
tta setup                          # 检查工具 → 登记仓库 → 扫码绑定手机 → 安装后台 Runner
```

## 手机远程派发（Valley Runner）

Claude/Codex 的登录凭据不会上传：iPhone 只把任务发给 Valley，本机 `timetrace agent` 通过出站 HTTPS 领取任务，再调用当前 macOS 用户已经登录的 CLI。

```sh
timetrace cloud login                    # 终端显示二维码，用 iPhone「你的 AI → 扫码绑定」扫描后确认
timetrace workspace add ~/code/TimeTrace # 只显式开放这个仓库
timetrace agent doctor                   # 检查配对、CLI 和工作区
timetrace agent run --once               # 联调一轮
timetrace agent install                  # 安装并启动登录用户的 LaunchAgent
```

`timetrace cloud login` 打印的二维码内容是 `timetrace://pair?code=<8 位码>&name=<电脑名>&platform=darwin&v=1`：只有授权码和展示用的电脑名，没有任何凭据，手机上仍需登录账号并点「确认绑定」才会生效。也可以用系统相机扫描（会打开刻迹 App）；扫不了码时，二维码下方的 8 位码照旧可以在「你的 AI → 绑定新电脑」里手动输入。二维码按白底黑码输出，终端窗口太窄时把窗口拉宽一些再扫。一个账号可以绑定多台电脑，在手机「设备与授权」里重命名或解绑。

Runner refresh token 存在 macOS 登录 Keychain（service `com.atlaspaces.timetrace.runner`）；access token 15 分钟轮换。每个远程任务仍进入独立 worktree，且 push 被禁用。电脑关机、休眠或未登录时，Valley 只保留排队任务，不会在云端接管本地代码或账号。

## 远程任务的产出在哪里

每个手机派发的任务都在 `~/.timetrace/worktrees/<id>/` 里的独立分支（`timetrace/<id>`）上执行并提交；主仓库的工作区不动，也不会推送。
验收通过后要不要合并回主分支，目前需要你在电脑上自己 `git merge timetrace/<id>`（0.1 不做自动合并）。
Codex 的 workspace-write 沙箱只额外放行提交所需的几处 git 元数据：本 worktree 的管理目录（`.git/worktrees/<id>`）、`.git/objects` 和分支 ref 所在目录（`refs/heads/timetrace`）。主仓库的 `.git/config`、hooks 和其他分支都不可写，沙箱内也没有网络。

### 非 Git 工作区（素材文件夹）

`timetrace workspace add <路径>` 自动判断类型：目录里有 `.git` 就是 Git 仓库，否则登记为文件夹工作区（`--kind git|folder` 可强制）。文件夹工作区没有 worktree、分支和提交：每个任务在 `<文件夹>/timetrace-out/<名称>/` 里执行（名称为任务下发的 `output_name`，否则为计划 id），AI 只能写这个目录，素材只读。运行前后电脑端对 `timetrace-out/` 以外的文件做路径 / 大小 / 修改时间快照比对，任何改动都会让任务失败并列出被改动的文件（最多 20 个）；文件超过 50,000 个时只校验顶层两级。完成后自动登记一个 `folder` 产出物（输出目录的文件清单）。

并行数按工具计：`timetrace config set max_parallel_per_tool.claude <1-8>`、`max_parallel_per_tool.codex <1-8>`（默认各 2，Claude 与 Codex 的任务同时跑）；`max_parallel <1-8>` 是整台电脑的总上限（默认 0 = 各工具之和；设为 1 即逐个执行），两者都随工具清单上报给 Valley。同一 Git 仓库的多个任务各自在独立 worktree 里并行（只有创建 worktree 那一步串行）；拆分出的子任务使用服务端下发的分支名加上任务后缀 `timetrace/<阶段>/<子任务>-<8 位>`（每个任务独占一个分支；不合法时回落到 `timetrace/<id>`，与已有分支前缀冲突时直接报错）。文件夹工作区同一时刻只跑一个任务。

### 验收：AI 复核与自动检查

步骤选「AI 复核」时，Valley 下发 `review_turn` 作业：电脑端从该步骤的分支拉一个**只读的独立副本**（`~/.timetrace/reviews/<作业>`，detached，用完即删；文件夹工作区直接用该步的 `timetrace-out/<名称>/`），在里面按对话的只读参数运行 AI，提示为服务端给的验收标准和产出清单加上本机算出的 `git diff --stat` 摘要（≤ 8 KB）。AI 在最后回复里给出 `{"verdict":"pass|fail","reasons":[…]}`；解析不出来时回传 `verdict: "invalid"`，绝不当作通过。

步骤选「自动检查」时，只引用检查的**名称**；命令只在这台电脑上登记：

```sh
timetrace workspace check add TimeTrace unit -- python3 -m unittest discover -s tests
timetrace workspace check add TimeTrace build -- sh -c "npm ci && npm test"   # 需要 shell 就显式写 sh -c
timetrace workspace check list [TimeTrace]
timetrace workspace check remove TimeTrace build
```

名称是 `[a-z0-9_-]{1,40}`，命令按参数列表直接执行（不经过 shell），清单只向 Valley 上报名称。`check` 作业在该步骤分支的独立副本（`~/.timetrace/checks/<作业>`，用完即删）或文件夹工作区的输出目录里运行，超时 30 分钟，环境里去掉 AI 工具的凭据 / 端点变量（`ANTHROPIC_*`、`OPENAI_*`、`CLAUDE*`、`CODEX_*` 等）和名字像密钥的变量（`check_env_keep` 可以保留个别需要的，`check_env_drop` 可以再去掉），回传退出码、耗时和脱敏后的输出末尾（≤ 16 KB）。服务端找不到本机登记的名称时判失败「本机没有这个检查」。**注意：** 检查会在你的账号下、不经沙箱地运行分支里 AI 写的代码（测试、构建脚本），只给你本来就会在本机跑它们的仓库登记检查。

`timetrace agent doctor` 会打印每个工具的套餐等级和实时额度，和手机「你的 AI」页应显示的一致，用来现场对照。

## 安装

三种方式，任选其一：

```sh
# 1) Homebrew（推荐）；升级用 brew upgrade timetraceagent
brew install underestimatedme/timetrace/timetraceagent

# 2) pipx：直接从 GitHub 的发布 tag 安装；升级时把 tag 换成新版本再加 --force
pipx install "git+https://github.com/underestimatedme/TimeTraceAgent.git@v0.3.0"

# 3) 从源码运行：把启动脚本软链到 PATH 里（改代码立即生效）
git clone https://github.com/underestimatedme/TimeTraceAgent.git && cd TimeTraceAgent
ln -s "$(pwd)/bin/timetrace" /usr/local/bin/timetrace      # 或任何在 PATH 里的目录
ln -s "$(pwd)/bin/timetrace" /usr/local/bin/tta            # 可选：短名

tta --version
```

后台 Runner（`tta agent install`）记住的是安装时的命令路径：用方式 3 时不要再移动或删除这个目录。

数据目录默认 `~/.timetrace/`（可用环境变量 `TIMETRACE_HOME` 覆盖，权限固定为 0700）：`timetrace.db`、`config.json`、`logs/`、`inbox/`、`worktrees/`。

## 设置向导 `timetrace setup`

一条命令走完首次配置，每一步都复用对应的子命令：

1. 检查 `claude` / `codex` 是否安装、是否通过零付费核验（同 `timetrace agent doctor`）；
2. 登记允许远程任务使用的 Git 仓库（同 `timetrace workspace add`，可登记多个，回车结束）；
3. 与手机绑定：终端显示二维码，用 App「你的 AI → 扫码绑定」扫描（同 `timetrace cloud login`）；
4. 安装并启动后台 Runner LaunchAgent（同 `timetrace agent install`）。

脚本化安装可以用参数代替提问：

```sh
timetrace setup --repo ~/code/TimeTrace --yes            # 登记仓库、绑定、安装 LaunchAgent，全部取默认
timetrace setup --repo ~/code/a --repo ~/code/b --no-pair --no-agent --yes
```

已绑定的电脑不会被 `--yes` 重新绑定；输入结束（EOF）视为跳过。

## 隐私与数据流向

```
iPhone ──任务──▶ TimeTrace 云（Valley）◀──出站 HTTPS 轮询── timetrace agent（你的 Mac）──▶ claude / codex（本机登录）
```

- **离开本机的**：电脑名、登记仓库的文件夹名和默认分支名（不含绝对路径）、工具名/套餐等级/零付费核验结论、额度百分比与重置时间；
  任务事件的状态、错误信息、模型最后一条回复的前 1000 字，以及（`upload_output_tail` 为 true 时）运行日志末尾最多 8000 字节。
  所有文本在写入本地待发队列之前先做密钥脱敏（`timetrace/redact.py`：AWS/阿里云 AccessKey、GitHub/OpenAI/Anthropic/Slack/Google token、JWT、PEM 私钥、`xxx_TOKEN=…` 之类的赋值、Bearer 头、URL 里的密码）。
- **额度读取**：Claude Code 的 OAuth access token 只发给 `https://api.anthropic.com/api/oauth/usage`（与 Claude Code 自己的 `/usage` 相同的调用）。
- **从不离开本机的**：Claude/Codex 的登录凭据与 API key、仓库文件与 diff、任务分支（从不推送）、完整运行日志。
- 不想上传任何运行输出：`timetrace config set upload_output_tail false`。脱敏是尽力而为，识别常见格式；仓库里有特殊格式的密钥时建议关掉。

完整的威胁模型和审查结论见 `docs/SECURITY_REVIEW.md`，数据清单见 `SECURITY.md`。

## 用法

```sh
timetrace status                      # 各桶余量、重置倒计时、绑定约束（Codex 实时读，Claude 取最近采样）
timetrace add "重构 payment 模块的错误处理" --repo ~/code/paycore --tool claude
timetrace add "给上一步写单测" --repo ~/code/paycore --after 1 --any-tool
timetrace add "..." --repo ~/code/x --on-success "列出还需要补的重构点，每条一个任务"
timetrace ls [--all]
timetrace run [--once]                # 守护循环；--once 只跑一轮
timetrace logs 1 [-f]
timetrace retry 1 [--fresh]           # failed/blocked → runnable；--fresh 丢弃会话重来
timetrace rm 1
timetrace events [--type task_blocked]
```

任务状态：`pending → runnable → running → blocked → done / failed`。`blocked` 会自动恢复，`failed` 需要人看。

## 守护进程

前台：`timetrace run`（本地队列）或 `timetrace agent run`（手机远程派发）。后台用 launchd：

```sh
timetrace agent install              # 写入 ~/Library/LaunchAgents/com.atlaspaces.timetrace.agent.plist 并加载
tail -f ~/.timetrace/daemon.log
launchctl kickstart -k gui/$(id -u)/com.atlaspaces.timetrace.agent   # 改配置后重启
```

`launchd/com.atlaspaces.timetrace.agent.plist` 是同样内容的模板，手工安装时把 `__TIMETRACE_BIN__`、`__HOME__` 换成实际路径。

## 配置 `~/.timetrace/config.json`（都是可选项，下面是缺省值）

```json
{
  "cloud_base_url": "https://apis.atlaspaces.com/timetrace/api/v1",
  "interval_sec": 30,
  "jitter_sec": 300,
  "default_block_sleep_sec": 3600,
  "circuit_breaker_failures": 3,
  "circuit_window_mins": 300,
  "allowed_repos": [],
  "hook_max_tasks": 5,
  "upload_output_tail": true,
  "max_parallel_per_tool": {"claude": 2, "codex": 2},
  "max_parallel": 0,
  "check_env_drop": [],
  "check_env_keep": [],
  "claude": {"bin": "claude", "permission_mode": "acceptEdits",
             "allowed_tools": ["Bash(git add:*)", "Bash(git commit:*)", "Bash(git status:*)",
                               "Bash(git diff:*)", "Bash(git log:*)"],
             "setting_sources": "user",
             "model": null, "extra_args": []},
  "codex":  {"bin": "codex",  "sandbox": "workspace-write", "model": null, "extra_args": []}
}
```

顶层的标量项可以直接用命令改（带类型校验，原子写入，文件权限 0600）：

```sh
timetrace config list                               # 所有可设置的项及当前生效值
timetrace config get upload_output_tail
timetrace config set upload_output_tail false       # 布尔：true/false、on/off、1/0、yes/no
timetrace config set interval_sec 60
```

嵌套项（`claude`、`codex`）和列表（`allowed_repos`）请直接编辑 `config.json`。改完后重启 Runner 才会生效。

| 项 | 缺省 | 说明 |
| --- | --- | --- |
| `cloud_base_url` | TimeTrace 云 | Valley API 地址；必须是 https（仅 localhost 允许 http） |
| `upload_output_tail` | `true` | 任务结束时是否上传（脱敏后的）日志末尾 8000 字节（检查的输出末尾同样受它控制） |
| `max_parallel_per_tool` | `{"claude": 2, "codex": 2}` | 每个工具同时运行的 AI 作业数（1–8）；`timetrace config set max_parallel_per_tool.codex 3` |
| `max_parallel` | `0` | 整台电脑同时运行的作业总数上限（1–8）；0 = 各工具之和 |
| `check_env_drop` / `check_env_keep` | `[]` / `[]` | 检查命令环境里额外去掉 / 保留（名字像密钥的）变量 |
| `interval_sec` | `30` | 本地队列守护进程的空闲轮询间隔 |
| `jitter_sec` / `default_block_sleep_sec` | `300` / `3600` | 撞限额后的唤醒抖动 / 拿不到重置时间时的默认睡眠 |
| `circuit_breaker_failures` / `circuit_window_mins` | `3` / `300` | 熔断：窗口内失败次数上限 |
| `hook_max_tasks` | `5` | on_success 钩子一次最多生成的任务数 |
| `claude.setting_sources` | `"user"` | 无人值守运行加载哪些 Claude 设置；缺省不加载 worktree 里的项目设置 |

- `allowed_repos` 非空时，`timetrace add --repo` 必须落在其中某个目录之下。
- `permission_mode` 决定 Claude 无头运行时能做什么。`acceptEdits` 允许改文件，命令只放行 `allowed_tools` 里的（缺省是本地 git 提交相关的几条）；要让它跑构建和测试，往 `allowed_tools` 加规则（如 `"Bash(npm test:*)"`）或把模式改成 `bypassPermissions`。这是你的决定，工具不替你做。
- Codex 的 `sandbox` 对应 `codex exec -s`（续接时以 `-c sandbox_mode=…` 传入，续接不会退回到 `~/.codex/config.toml` 里更宽的模式）；`workspace-write` 只允许写 worktree 和提交所需的 git 元数据，且关闭网络。

## 自托管

`cloud_base_url` 可配置，缺省指向 TimeTrace 云（`https://apis.atlaspaces.com/timetrace/api/v1`）。服务端（Valley 的 `timetrace` 模块）不在本仓库中；如果你运行自己的兼容服务端：

```sh
timetrace config set cloud_base_url https://valley.example.com/timetrace/api/v1
timetrace cloud login     # 重新绑定到新的服务端
```

客户端只接受 https（本机测试服务器可用 `http://127.0.0.1` / `http://localhost`），不跟随重定向，响应体上限 1 MiB。iOS App 需要指向同一个服务端。

## 安全约束

- 每个任务在独立的 git worktree（`~/.timetrace/worktrees/<id>`，分支 `timetrace/<id>`）里运行，从不碰主工作区。
- worktree 内所有远端的 pushurl 被改成 `no_push://blocked`，`git push` 立即失败；Claude 另加 `--disallowedTools "Bash(git push*)"`，Codex 沙箱内无网络；两个工具的提示词都写明禁止 push、禁止改远端分支和 CI 配置（提示词只是提醒，真正的约束是权限模式和沙箱）。
- Runner 在 worktree 里执行自己的 git 命令之前，会核对 `.git` 指针、`commondir` 和 `config.worktree` 仍是它创建时的样子，被改动就停下等人处理；这些 git 调用还会关闭 fsmonitor、hooks 和外部 diff。
- 手机发来的提示词始终作为位置参数传给 Claude（以 `-` 开头也不会被当成选项）；Claude 只加载用户级设置，不加载 worktree 里的 `.claude/settings.json`。
- 启动工具进程时，名字里含 TOKEN / SECRET / PASSWORD / API_KEY / ACCESS_KEY / CREDENTIAL 的环境变量一律剔除。
- 熔断：默认 5 小时内 3 次失败就停止派工，直到窗口过去或你 `timetrace retry`。
- v0.3 的 on_success 钩子只产出一份声明式 JSON 到 `~/.timetrace/inbox/`，由守护进程下一轮校验后入库；钩子生成的任务不能再生成任务。

## 零付费核验（派发门禁的计费项）

Runner 只在「运行任务不可能产生新增费用」时才派发。这不是配置项，而是每次派发前的实测：

| 工具 | 通过条件 | 实现 |
| --- | --- | --- |
| Claude Code | `claude auth status` 报告 `loggedIn=true`、`authMethod=claude.ai`、`apiProvider=firstParty` 且有 `subscriptionType` | `timetrace/billing.py: verify_claude` |
| Codex | `codex login status` 报告 `Logged in using ChatGPT`，且 `~/.codex/auth.json` 的 `auth_mode=chatgpt`、无 `OPENAI_API_KEY` | `timetrace/billing.py: verify_codex` |

另外两条对两个工具都生效：

- 环境里不能有 API key 或替代端点（`ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL`、`CLAUDE_CODE_USE_BEDROCK/VERTEX/FOUNDRY`、`OPENAI_API_KEY`、`CODEX_API_KEY`、`OPENAI_BASE_URL`），
  Claude 的 `settings.json` 里不能有 `apiKeyHelper` 或上述 `env`。
- 无论核验结果如何，启动工具进程时这些变量都会从子进程环境中剔除，订阅工具不可能悄悄切到按量计费。

核验通过时 adapter 的 `can_enforce_zero_spend` 为 true；否则为 false 并带机器可读原因
（`not_logged_in`、`auth_method_not_subscription:<x>`、`api_key_fallback_in_env:<VAR>` 等），
统一门禁返回 `billing_unverified`，该工具不会收到任务。结论缓存 5 分钟，但真正 spawn 前会强制重新核验。
`timetrace agent doctor` 会逐工具打印核验结论。核验只读登录状态，不会启动模型、不消耗额度。

## 限额消耗的两部分覆盖：终端 + 软件

限额是账号级的，不管你在终端里跑还是在软件里点，烧的都是同一个窗口。timetrace 的到期判断和续接时机必须两边都看得到：

| 消耗来源 | Codex | Claude Code |
|---|---|---|
| 终端（timetrace 无头运行） | `codex exec` 结束后立刻 `account/rateLimits/read` | 每次 `claude -p` 的 `rate_limit_event` |
| 软件（你自己在 Codex 应用 / Claude Code 里用） | 同一接口，服务端真值，天然包含应用内消耗 | **`timetrace statusline`**：挂进 Claude Code 状态栏，每次刷新把 `rate_limits` 写进库 |

装 Claude Code 状态栏钩子（会往 `~/.claude/settings.json` 写 `statusLine`，已有别的状态栏脚本时不覆盖）：

```sh
timetrace statusline --install
```

之后 Claude Code 的状态栏会显示 `timetrace · 5h 86% · 7d 97% · codex 35% · ⏳ 3h12m`，同时你交互会话里撞到的限流（100%）会让守护进程停止往 Claude 派工，直到 `resets_at` 过去或有新样本。

判断规则：任一桶最新样本 `used == 100%` 且 `resets_at` 还没到 → 该工具耗尽；`resets_at` 已过而没有新样本 → 视为已重置，照常派工，由真实运行结果说话。

## 两个工具的差别

| | Claude Code | Codex |
|---|---|---|
| 限额读取 | 无按需接口；每次运行的 `rate_limit_event` 顺带给出 | `codex app-server` 的 `account/rateLimits/read`，随时读、不耗额度 |
| 执行 | `claude -p --output-format stream-json --session-id <uuid>` | `codex exec --json -s workspace-write -C <worktree>` |
| 续接 | `claude -p --resume <session_id>` | `codex exec resume <thread_id>` |
| 撞限流 | `rate_limit_info.status == rejected` 或 HTTP 429 | JSONL `error` 事件含 "usage limit"，退出码 1；重置时间取自 app-server |

## 开发

```sh
cd cli && python3 -m unittest discover -s tests -v
```

## Report occurrence timestamps

Runner phase events include an `observed_at` UTC RFC 3339 timestamp captured
at the actual adapter start/resume call and its return/exception boundary.
Preflight renewal, running-event transport, quota upload and checkpoint/git IO
do not define the execution interval. The worker captures times; the owning
thread durably enqueues running without a blocking flush, then flushes events
in sequence after terminal handling. Claim/Plan identity is durable before any
execution; event payloads are durable before any send. Lease heartbeats and
spawn/cancellation fences remain active while the adapter runs. A killed or
lease-fenced attempt without a trustworthy terminal remains unknown.
Outbox retries, including after restart, retain the
original timestamp and event sequence. The server separately records receipt
time, validates observation order and lease/clock bounds, and uses occurrence
time for report day slicing. Keep the runner clock synchronized; deploy the
Valley nullable `observed_at` migration before this runner update. Legacy outbox
events without this field remain unknown report measurements, not zero-duration
execution. Existing expired-lease reporting restrictions remain in force.

Pending events for each `(job_id, attempt_id, lease_epoch)` are sent together,
ordered by sequence, then acknowledged in one atomic SQLite update. Attempt
groups follow durable enqueue order, not UUID lexical order. A lost response or
failed local acknowledgment retries the whole unchanged batch; a 409 is never
silently discarded. No schema migration is needed for existing outboxes.
See `tests/integration/README.md` for the real Valley/PostgreSQL cancellation
regression, including lost-response recovery and the next job claim.

### Project background inventory

The Runner reports whether each registered workspace is empty, populated, or
unreadable, plus a root README snapshot (README.md, README.markdown, README,
README.txt in that order, ignoring case). Snapshots are limited to 32 KiB of
UTF-8 and descriptions to 500 characters. Local paths and linked files are not
uploaded. Inventory refreshes update the collection timestamp; no AI is invoked.
