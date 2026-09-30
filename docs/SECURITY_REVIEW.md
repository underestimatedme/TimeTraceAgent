# Security review: the timetrace runner

Date: 2026-09-25 · Scope: the whole runner as of 2026-09-25, with the
fixes that landed before the first public release (0.3.0). Reviewed against Claude Code
2.1.281 and Codex CLI 0.155.1 on macOS.

## Threat model

| Actor | Trust | What they can do by design |
| --- | --- | --- |
| The Mac user who installed timetrace and owns the phone account | trusted | everything |
| Valley (the TimeTrace cloud) or anyone who takes over the phone account | semi-trusted | send prompts that an AI tool executes inside the sandbox of a registered repository |
| Repository content, issue text, files the model reads | untrusted | prompt injection: steer the model within its sandbox |
| The model's own output | untrusted | whatever the sandbox and permission mode allow |
| The network | untrusted | observe and tamper with traffic |
| Other local macOS users | untrusted | read files that permissions let them read |

Security goals: a remote task can only **write inside its own worktree** (plus
the git paths a commit needs), **cannot push**, **cannot switch a tool to
metered billing**, and nothing **secret** leaves the computer except the
runner's own Valley token (to Valley) and Claude Code's OAuth token (to
`api.anthropic.com`, for the quota read).

`SAFETY_RULES` in `adapters/base.py` is advisory text for the model. It is not
a control; the controls are listed in "How a remote prompt is contained" below.

## Findings

| ID | Severity | Location | Status |
| --- | --- | --- | --- |
| H-1 | High | `adapters/codex.py` `_writable_extras` | Fixed in 0.3.0 |
| H-2 | High | `worktree.py`, `agent.py` (runner git calls in a model-written directory) | Fixed in 0.3.0 |
| H-3 | High | `adapters/codex.py` `build_cmd` (resume) | Fixed in 0.3.0 |
| H-4 | High | `adapters/claude.py` `build_cmd` (prompt as last argv) | Fixed in 0.3.0 |
| H-5 | High | `adapters/claude.py` (project settings in the worktree) | Fixed in 0.3.0 |
| M-1 | Medium | `agent.py` completed/failed events, `process.py` `tail_text` | Mitigated in 0.3.0 |
| M-2 | Medium | `process.py` `run_streaming` environment | Fixed in 0.3.0 |
| M-3 | Medium | `cloud.py`, `adapters/claude_usage.py` | Fixed in 0.3.0 |
| M-4 | Medium | `config.py` `ensure_dirs` (`~/.timetrace` permissions) | Fixed in 0.3.0 |
| L-1 | Low | `cli.py` `cmd_agent_install` | Fixed in 0.3.0 |
| L-2 | Low | `agent.py` log path from server job id | Fixed in 0.3.0 |
| L-3 | Low | `db.py` outbox retention | Fixed in 0.3.0 |
| L-4 | Low | `credentials.py` Keychain item ACL | Accepted |
| L-5 | Low | `tiers.py` account digests | Accepted |
| L-6 | Low | `db.py` `remote_claim.prompt` retention | Open |
| I-1 … I-6 | Info | see below | — |

### H-1 Codex sandbox could write the main repository's `.git`

*Scenario.* Codex ran with `--add-dir <main repo>/.git` so that `git commit`
works in a linked worktree. That made the **shared** `.git/config` and
`.git/hooks/` writable from the sandbox. A hostile prompt, or prompt injection
from repository content, could set `core.fsmonitor = <command>` or drop a
`pre-commit` hook. The next unsandboxed git process runs it with full user
rights and network: the runner's own `git diff` (checkpoint on a quota block)
or the user's next `git status`/`git commit` in the main checkout.

*Fix.* The sandbox now gets only what a commit writes: the worktree's admin
dir (`.git/worktrees/<name>`), `.git/objects`, and the directory of its branch
ref and reflog (`refs/heads/timetrace`, `logs/refs/heads/timetrace`)
(`worktree.sandbox_write_roots`). Verified with `codex sandbox`: the commit
succeeds; writes to `.git/config`, `.git/hooks` and `refs/heads/main` fail.
Codex itself keeps `<worktree>/.git` read-only.

### H-2 Runner ran git in a directory the model had rewritten

*Scenario.* Even with H-1 fixed, the model can write the worktree (including
its `.git` pointer file for Claude's edit tools) and, for Codex, the
per-worktree admin dir (`config.worktree`, `commondir`). Pointing `.git` or
`commondir` at an attacker-made directory, or adding `core.fsmonitor` /
`include.path` / a filter driver to `config.worktree`, turns the runner's next
git call in that worktree (snapshot, resume preparation) into command
execution outside the sandbox.

*Fix.* `worktree.verify_metadata(path, repo)` runs before every runner git call
in an execution worktree after a model run (checkpoint capture, resume
preparation in `ensure`, resume verification in `agent`). It requires that
`.git` is a regular file pointing into the **registered** repository's
`worktrees/` dir, that `commondir` resolves back to that repository, and that
`config.worktree` holds nothing but the runner's own `remote.*.pushurl =
no_push://blocked` lines. Tampering blocks the job with a
`checkpoint … metadata changed` reason for manual review. The runner's git calls
there also pass `-c core.fsmonitor=false -c core.hooksPath=/dev/null` and
`--no-ext-diff` (`worktree.SAFE_GIT`).

### H-3 Codex resume ran without an explicit sandbox, and failed with `--add-dir`

*Scenario.* `codex exec resume` accepts neither `-s` nor `--add-dir`. The
resume command omitted the sandbox, so a resumed run used whatever
`~/.codex/config.toml` said (possibly `danger-full-access`), and every resume
in a linked worktree failed with "unexpected argument '--add-dir'".

*Fix.* Both start and resume pass `-c sandbox_mode="<sandbox>"`,
`-c sandbox_workspace_write.writable_roots=[…]` and
`-c sandbox_workspace_write.network_access=false` (argument parsing verified
against the installed Codex). Network off also makes `git push <url>` fail
inside the sandbox.

### H-4 A phone prompt starting with `-` was parsed as a Claude Code option

*Scenario.* The prompt is the last argv element of `claude -p …`. A prompt
such as `--settings={"hooks":{"SessionStart":[…]}}` or
`--permission-mode=bypassPermissions` is read by Claude Code's option parser as
a flag: command execution outside the permission model, triggered by whoever
can create a task (Valley or the phone account).

*Fix.* `adapters/claude.safe_positional` prefixes a space to a prompt that
starts with `-`. (Codex's prompt always starts with `SAFETY_RULES`.)

### H-5 Claude Code loaded project settings from the worktree

*Scenario.* In `-p` mode Claude Code skips the workspace-trust dialog and
loads `.claude/settings.json` / `.claude/settings.local.json` from the working
directory. Repository content, or a previous task's commit that a dependent
task builds on, could add hooks or `permissions.allow: ["Bash(*)"]`.

*Fix.* Unattended runs pass `--setting-sources user` (config key
`claude.setting_sources`, default `"user"`). The user's own
`~/.claude/settings.json` still applies.

### M-1 Secrets could leave the computer in run output

*Scenario.* Codex's sandbox can read the whole disk and Claude's stream-json
log contains tool results. A task (hostile or just unlucky: `cat .env`) puts a
credential into the log; the last 8000 bytes (`output_tail`), the model's final
message (`result_summary`) and error messages were uploaded to Valley
verbatim.

*Mitigation.* `timetrace/redact.py` masks provider token shapes (AWS/Aliyun key
ids, GitHub, OpenAI, Anthropic, Slack, Google, npm, Stripe), JWTs, PEM
private-key blocks (including blocks cut by the tail and JSON-escaped ones),
bearer/basic auth, URL passwords and secret-named assignments. `Agent._outbound`
applies it to every event field before the durable enqueue, so the outbox never
stores unmasked text, and re-bounds the tail to 8000 bytes. `upload_output_tail
= false` sends no tail at all. *Residual risk:* secrets in unknown formats, or
paraphrased by the model, are not detected; the model can still read files it
should not (reads are not sandboxed by either tool).

### M-2 Secret-named environment variables reached the AI tools

*Scenario.* Started from a shell, the runner passed its whole environment
(minus billing keys) to the tools: `GITHUB_TOKEN`, `AWS_SECRET_ACCESS_KEY`,
`NPM_TOKEN`… were readable by model-run commands and could be echoed into
uploaded output or used by a tool with network.

*Fix.* `process.run_streaming` drops every variable whose name contains
TOKEN, SECRET, PASSWORD/PASSWD, API_KEY, ACCESS_KEY, PRIVATE_KEY or
CREDENTIAL, in addition to `CLAUDE*` and the billing keys.

### M-3 Valley client: redirects, plain http, unbounded responses

*Scenario.* urllib follows redirects and copies ordinary request headers,
including `Authorization`, to the new location, also cross-host and
https→http. A `cloud_base_url` of `http://…` was accepted, sending refresh and
access tokens in clear text. `response.read()` was unbounded (a hostile or
broken server could exhaust memory). Server-supplied attempt ids were
interpolated into URL paths unescaped. The Claude usage call had the same
redirect behaviour for the OAuth token.

*Fix.* `CloudClient` requires `https` (plain `http` only for loopback test
servers), uses an opener that refuses every 3xx, sends the token with
`add_unredirected_header`, caps bodies at 1 MiB and percent-escapes path
segments. `claude_usage` uses the same opener and a 256 KiB cap. TLS
verification uses Python's default context (certificate and hostname checked).

### M-4 `~/.timetrace` was readable by other local users

*Scenario.* The data dir was created `0755` and files `0644`. macOS homes are
`0750` with group `staff`, and every local user is in `staff`, so another
account on the Mac could read prompts, run logs, the outbox and the task
worktrees.

*Fix.* `config.ensure_dirs` sets `~/.timetrace` to `0700` on every command.
`config.json` written by `timetrace config set` is `0600`.

### Low

- **L-1** The LaunchAgent plist was built by string replacement without XML
  escaping; a home path with `&` or `<` produced a broken plist. Now generated
  with `plistlib`.
- **L-2** The server-supplied job id was used in the log file name. Traversal
  failed only because `logs/remote-..` does not exist; the id is now reduced to
  `[A-Za-z0-9_-]`.
- **L-3** Acknowledged outbox rows (with output tails) were kept forever; they
  are now deleted 7 days after delivery. Unsent rows are kept until sent.
- **L-4** The runner's refresh token is a login-Keychain item created by the
  Python interpreter without a custom ACL; other code run by the same user
  through the same interpreter can read it without a prompt. Inherent to a
  user-level agent; accepted. Access tokens are never persisted.
- **L-5** Quota pools carry an 8-hex SHA-256 prefix of the tool's account id
  (unsalted). Not reversible for random account UUIDs; lets Valley group two
  computers on one account, which is the purpose. Accepted.
- **L-6** `remote_claim` keeps every received prompt in `timetrace.db`
  indefinitely. Local only (and `0700` now); retention not yet implemented.

### Info

- **I-1 Containment of a remote prompt.** Claude: `--permission-mode
  acceptEdits`, Bash limited to the `allowed_tools` git commands (no push),
  project settings ignored, prompt kept positional. Codex: seatbelt
  `workspace-write`, network off, writable roots limited to the worktree and
  its commit paths. Both: separate worktree per job, all remotes'
  `pushurl = no_push://blocked` in the worktree config, zero-spend gate
  re-checked before every spawn, billing variables removed. If the user widens
  `permission_mode` (e.g. `bypassPermissions`), `allowed_tools`, `sandbox` or
  `extra_args`, these guarantees no longer hold; that is an explicit choice.
- **I-2 Transport.** Timeouts: Valley 30 s, usage endpoint 15 s, `auth status`
  / `login status` 20 s, Codex app-server 15 s. No certificate pinning.
- **I-3 Credential reads** (`tiers.py`, `claude_usage.py`). Only the plan tier
  string and opaque digests leave `tiers.py`; the Keychain secret is read from
  `security … -w` stdout (never argv) and not logged. The OAuth access token
  goes only to the hard-coded `https://api.anthropic.com/api/oauth/usage`, and
  never when expired.
- **I-4 Pairing.** The QR code holds only the 8-character user code and the
  display name; the device code is never printed. Someone who gets a victim to
  approve *their* code binds the attacker's computer to the victim's account
  and would receive the victim's prompts; the phone must show computer name
  and platform before "确认绑定" (app/server side).
- **I-5 Zero spend.** Unchanged and sound: verdict from `claude auth status` /
  `codex login status`, API-key variables removed from every child, re-check
  with the cache bypassed immediately before a spawn.
- **I-6 Known cosmetic issue.** `--disallowedTools "Bash(git push*)"` uses the
  glob form; the documented prefix form is `Bash(git push:*)`. Push is not in
  the allow list, so it is denied either way.

## Addendum 2026-09-27: elastic pipeline v1

### chat_turn (phone conversations, read-only)

A `chat_turn` job runs the prompt in the workspace's **main checkout**, not a
worktree, so the controls are read-only modes rather than isolation:

- Claude: `--permission-mode plan` (no edits, no command execution),
  `--setting-sources user` (H-5), `--strict-mcp-config` with no
  `--mcp-config` (no MCP server from user or project config, whose tools
  plan mode does not make read-only), `safe_positional` prompt (H-4). The
  task-run widenings from the config (`permission_mode`, `allowed_tools`,
  `extra_args`) are **not** applied to a chat turn.
- Codex: `-s read-only` on the first turn and `-c sandbox_mode="read-only"` on
  every turn, so `exec resume` cannot inherit a laxer `~/.codex/config.toml`
  (H-3). No writable roots, no `--add-dir`, config `sandbox` and `extra_args`
  ignored; the prompt starts with `CHAT_RULES`.
- The server-supplied `provider_session_id` lands in argv after `--resume` /
  `exec resume`; anything but `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` is refused
  before a spawn, so it can never be read as an option.
- No worktree, commit, checkpoint or runner git call. The zero-spend gate,
  lease fencing, slot and workspace locks are the same as for a task.
- `reply` (the final assistant message) goes through `redact.redact` and is
  cut to 32 KB UTF-8 on a character boundary, both when built and again in
  `Agent._outbound`. *Residual risk:* reads are not sandboxed; the model can
  read and paraphrase any file the user can, as in a task run (M-1).

### `.timetrace/out/result.json` (structured task results)

The model writes this file, so `timetrace/results.py` treats it as hostile input:

- Opened with `O_NOFOLLOW | O_NONBLOCK`, must be a regular file (a symlink to a
  secret or a FIFO is refused), and a symlinked `.timetrace` / `.timetrace/out` makes
  the result invalid. Read at most 64 KB + 1 byte; larger is invalid.
- Must be a UTF-8 JSON object. Only `artifacts` (list of `{kind ∈
  doc|commit|link|note, ref ≤ 512 chars, content?, commit_sha? (hex)}`) and
  `pipeline_draft` (object) survive; any malformed element makes the whole
  result invalid (`artifacts: []`, event message gets "结构化结果无效" and the
  event carries `result_invalid: true`, so Valley need not parse the
  message). The job still completes.
- `kind=doc` refs are resolved and must stay inside the execution worktree
  and outside any `.git` component before their content (≤ 64 KB) is attached;
  the whole `artifacts` list is bounded to 64 KB. HEAD is read with
  `worktree.head`, i.e. only after `verify_metadata` and with `SAFE_GIT` (H-2).
- Every string is passed through `redact.redact` when collected and again in
  `Agent._outbound`.
- `/.timetrace/out/` is appended to the repository's `info/exclude` (common dir,
  outside the Codex writable roots) by `worktree.ensure`, so `git add -A`
  never commits it. `git add -f` by the model still could; the content is
  then an ordinary committed file, reviewed like any other change.

## Addendum 2026-09-27: elastic pipeline v2

### Folder workspaces (`kind = folder`)

A folder workspace has no git history to isolate a run in, so the control is
**write confinement plus detection** rather than a separate checkout:

- Registration: `timetrace workspace add` detects the kind (`.git` present → git)
  or takes `--kind`. `git` still requires a repository. A folder workspace
  may not be `/`, the home directory or any ancestor of it, or `~/.timetrace`, an
  ancestor of it or anything inside it; it may not have a `.git` entry in
  itself, a child or a grandchild (best effort: the scan stops after 20,000
  directories and is not repeated at run time); and it may neither contain
  nor lie inside any registered workspace (a git workspace may not overlap a
  registered folder either; re-registering the same path only updates it).
  Existing rows migrate to `git`. The dispatch gate accepts a non-repository only when
  the registered kind is `folder`; the kind comes from the local database,
  never from the job.
- Output directory: `<workspace>/timetrace-out/<name>/`, where `name` is the job's
  `output_name` only if it matches `[A-Za-z0-9._-]{1,80}` and is not all dots,
  else the plan id reduced to that alphabet. `timetrace-out` and `<name>` are
  created with `lstat` checks and never through a symlink; after the run
  both must still be plain directories or the job fails.
- Codex: `-s workspace-write`, `-C <output dir>`, and
  `sandbox_workspace_write.writable_roots=[<output dir>]`, network off, all
  pinned through `-c` so `exec resume` cannot fall back (H-3). The configured
  `sandbox` and `extra_args` are **not** applied. The rest of the disk stays
  readable, as for every Codex run.
- Claude: cwd is the output directory, `--add-dir <workspace>`,
  `--permission-mode acceptEdits`, `--restricted --strict-mcp-config`, no
  `--allowedTools`, `--disallowedTools Bash` (kept as a second layer: deny
  rules take precedence over allow rules), `safe_positional` prompt (H-4);
  configured `permission_mode`, `allowed_tools` and `extra_args` are **not**
  applied. `--restricted` (Claude Code 2.1.283 help text) removes the
  command/code-running tools and WebFetch, ignores the user, project and
  local settings files (managed settings still apply), confines the file
  tools to the working directories (cwd + `--add-dir`), refuses
  `bypassPermissions` and leaves writes to settings, git and
  tool-configuration files to a person; `--strict-mcp-config` without any
  `--mcp-config` loads no MCP server. `--setting-sources` is therefore not
  passed for a folder run.
  *Note:* `--add-dir` makes the workspace eligible for Claude's auto-accepted
  edit tools, so for Claude "read-only source material" is enforced by
  detection, not prevention. User `permissions.allow` entries (e.g.
  `Edit(~/**)`) and user MCP servers no longer reach a folder run (closed
  by `--restricted --strict-mcp-config`). *Remaining:* the flags need a
  Claude Code that has them (2.1.283 verified from `--help` only; an older
  CLI rejects the run instead of silently widening it), and managed
  (policy) settings still apply by design.
- Detection: before the spawn the runner records path, type, size, mtime
  and ctime of every entry except the run's own `timetrace-out/<name>/` — other
  runs' output directories are covered too (symlinks recorded by target
  path and never followed; ctime cannot be set back with `touch -r`/`utime`). Above 50,000 entries only the
  top two levels are recorded (directory mtimes included) and the event says
  so. Any difference after the run — whatever the tool reported — fails the
  job with up to 20 changed paths and deletes any checkpoint. The check
  also runs on every exit path once the tool was spawned — stop, cancel,
  lease fence, adapter exception — and the changed paths are appended to
  whichever terminal event is sent (a fenced run, which can send nothing
  under the lost lease, logs them in `daemon.log`). However the tool's leader
  exits, the runner then SIGKILLs its whole process group, so a detached
  child cannot keep writing after the after-snapshot. *Residual risk:* a
  change the model makes and fully reverts (same bytes and metadata) within
  the run is not visible; with the 50k cap, a same-size in-place edit three
  or more levels deep is not detected; edits made *through* a symlink the
  user placed in the workspace to a location outside it change only the
  target, which is not walked, and are not detected; a child that calls
  `setsid` itself leaves the process group and is not killed; and damage is
  detected, not undone (the user restores from their own backups).
- No worktree, branch, commit or runner git call happens for a folder job.
  A quota-blocked folder run checkpoints `git_head = "folder"` and a digest
  of the output directory's entries; resume refuses if that digest changed.
- The completion event carries a `folder` artifact (`ref`
  `timetrace-out/<name>`, `content` = `path<TAB>bytes` lines, ≤ 64 KB, `.timetrace/`
  omitted, symlinks listed by `lstat` and never followed) merged with the
  `result.json` artifacts, redacted and bounded as before. `folder` is now an
  allowed `result.json` artifact kind; `doc` refs are resolved inside the
  output directory.
- A fresh (non-resume) run first renames an existing `.timetrace/out` (or a
  symlink there, never followed) to `.timetrace/out.prev-<time>`, so a reused
  output directory or worktree cannot report the previous run's
  `result.json`; worktrees exclude `/.timetrace/out.prev-*/` from commits.
- `chat_turn` on a folder workspace is unchanged: read-only in the workspace
  root (plan mode / `-s read-only`).

### Parallel jobs

- `max_parallel_per_tool` (config, 1–8 per tool, default claude 2 / codex
  2) and `max_parallel` (overall cap, 1–8; default 0 = the sum of the
  per-tool values; a value the user set is honoured as the cap, 1 keeps the
  runner sequential); both reported in the inventory. The
  daemon claims on its loop thread and runs each job on a worker with its
  **own SQLite connection**; refresh-token use and outbox flushing are
  serialized by locks, so parallel jobs never race a token rotation or send
  one batch twice. Upkeep (quota, inventory) stays on the loop thread.
- Locks: `coding-slot.lock`, `coding-slot-1.lock` … (at most `max_parallel`
  jobs from the agent; the local scheduler uses slot 0 only). An AI job
  (task, chat or review turn) first takes one of its provider's own slots,
  `coding-slot-<tool>-<i>.lock` (i < `max_parallel_per_tool[tool]`, a tool
  not named gets 1), then an overall slot; a failure on the second releases
  the first. Per-tool slots have the same crash fence, skip-if-fenced and
  shutdown release as the overall ones; a job whose tool is full is deferred
  ("tool busy: <tool>") like one that finds the runner full.
  Scheduler runs (`timetrace run`) and agent jobs (`timetrace agent run`) never
  overlap: both take the process-wide `agent.lock`, so only one of the two
  daemons runs at a time. A slot left with a crash fence is skipped while
  another slot is free; the fence surfaces (manual recovery) only when no
  slot is free. *Known limitation:* a job deferred because every slot is
  busy or fenced is not handed back to Valley early — releasing a claim
  needs a server endpoint that does not exist yet — so it waits for its
  lease to expire. A new per-plan lock (`plan-<sha>.lock`) keeps one Plan from running
  twice at once. The workspace lock now covers only worktree preparation for
  a git workspace (shared `.git` writes; waiting up to 30 s within the
  lease) and the **whole run** for a folder workspace (its change check
  needs a quiet folder). Chat turns take the workspace lock only on folder
  workspaces. All locks keep the durable crash fence (manual clearance).
- Sub-task branches: the job's `branch_name` is used only if it matches
  `timetrace/[a-z0-9_./-]{1,100}` with no `..`, empty, dot-leading or `.lock`
  component and no trailing `.`; otherwise `timetrace/<id>`. It is made unique per
  job as `<branch_name>-<8 hex of the job id>` (falling back to `timetrace/<id>`
  if that is no longer valid), so two jobs never share a branch; a resume
  takes the branch recorded in the checkpoint. A new branch that is a
  directory prefix of an existing one, or has one as its prefix, is refused
  before `git worktree add` with a clear (Chinese) failure message.
  `git worktree add` and the other git calls in `worktree.ensure` run with
  `core.hooksPath=/dev/null` and `core.fsmonitor=false` (SAFE_GIT), so the
  repository's post-checkout hook never runs. The Codex writable
  roots still include only the branch's ref directory (e.g.
  `refs/heads/timetrace/<stage>/`), which also holds sibling sub-task branches —
  the same exposure `refs/heads/timetrace/` had for all `timetrace/*` branches in v1.
- `result.json` may carry `subtasks` (≤ 30 objects; `key` lowercased, then
  `[a-z0-9][a-z0-9_.-]{0,39}` (≤ 40, the same bound as Valley) with no `..`, trailing `.` or `.lock`, unique
  after lowercasing; `depends_on` lowercased the same way; `title` ≤ 200; `brief` ≤ 8000;
  `depends_on` list of keys; `tool` ≤ 32; `estimate_minutes` integer 0–10080).
  Unknown fields are dropped, any malformed element invalidates the whole
  result as before, and all strings are redacted. Graph checks (cycles,
  dangling dependencies) are Valley's.

### Shutdown

- `timetrace agent run` handles SIGTERM (launchd) and SIGINT (Ctrl-C): no new
  claims or spawns, every running task/chat/review/check job's cancel event is set (its
  process group is killed), the job is reported `waiting_input` with
  "dispatch blocked: runner_stopped" (plus any folder changes), and its
  locks are released. The parallel loop waits at most 15 s for its workers
  (launchd's ExitTimeOut is 20 s); a worker still stuck then keeps its
  durable lock fences for manual recovery. A worker's exception is logged
  with its class, message and job id. *Remaining:* a run that completes in
  the instant the stop arrives may be reported as stopped instead of
  completed; the local scheduler (`timetrace run`) has no such handling.

### `ios/scripts/asc_submit.py` (store submission)

- Runs on the Mac only; uses `ASC_KEY_ID` / `ASC_ISSUER_ID` /
  `ASC_KEY_PATH`. The ES256 JWT is signed by the system `openssl` reading the
  `.p8` by path (the key never enters argv, stdout or logs); the token lives
  15 minutes, is sent as an unredirected `Authorization` header to the
  hard-coded `https://api.appstoreconnect.apple.com`, never to the asset
  upload URLs, and is never printed. Non-https URLs are refused.
- `plan` is offline. `submit` validates the store folder first and makes no
  API call when validation fails.

## Addendum 2026-09-28: task pipeline (acceptance)

### review_turn (AI acceptance review, read-only)

- Same argv as a chat turn, with `REVIEW_RULES` in place of `CHAT_RULES`:
  Claude `--permission-mode plan`, `--setting-sources user`,
  `--strict-mcp-config` (no MCP server), `safe_positional` prompt, always a
  new `--session-id` (never `--resume`); Codex `-s read-only` plus
  `-c sandbox_mode="read-only"`, no writable roots, always a new thread. The
  configured `permission_mode`, `allowed_tools`, `sandbox` and `extra_args`
  do not apply. Same zero-spend gate, lease renewal / fencing, stop and
  cancel handling as a chat turn (one generic `Agent._run_leased`), and it
  holds one slot of its tool plus an overall slot.
- Where it runs: a **fresh detached worktree** of the step's commit under
  `~/.timetrace/reviews/<job>` (made with `SAFE_GIT` — no hooks, no fsmonitor —
  while holding the workspace lock; deleted, never following symlinks, and
  its registration pruned on every exit path). Not the main checkout: plan
  mode cannot run `git show`, so the model could not see the branch; not the
  step's own worktree: the review must see what was committed, not
  uncommitted leftovers, and must never share a directory with a run that
  may resume. Folder workspaces: the step's `timetrace-out/<output_name>/`
  (plain directory, `output_name` validated as for task runs), under the
  whole-run workspace lock like a chat turn.
- Which commit: the job's `branch_name` only when it is a valid `timetrace/*`
  task branch that exists locally (else, with `source_job_id`, the branch
  that step job created: `<name>-<8 hex>` or `timetrace/<n>`); the commit id is
  resolved with `rev-parse --verify` and only the id is passed on. Task
  completion events now report `branch` so Valley can name it exactly.
  `base_ref` is used only if it is `HEAD`, a hex id or a plain branch name
  (no `..`, `@{`, leading `-`, `.lock`), else the workspace's default branch;
  `git diff --stat <base>...<commit>` runs with `--no-ext-diff --no-textconv`
  on resolved ids and is cut to 8 KB. An unknown branch fails the job before
  any spawn ("本机找不到要复核的分支").
- Verdict: parsed from the final reply only (the last fenced ```json block
  holding a verdict, else the last `{...}` object); `verdict` ∈ pass|fail,
  `reasons` a list of strings — at most 20 kept, each cut to 500 characters,
  redacted (again in `_outbound`). Anything else is `verdict: "invalid"`
  with the message note "复核结论无效" — never a pass. `.timetrace/out/result.json`
  is deliberately **not** read: a read-only run cannot write it, so the only
  such file in the checkout would be one the step's own model committed
  (`git add -f`) to forge its review. A quota block or failure sends
  `waiting_quota` / `failed` without any verdict (Valley falls back to
  human confirmation).
- *Residual risk:* the reviewed content is written by the model under
  review and can try to steer the reviewer (comments addressed to "the
  reviewer", a `CLAUDE.md` / `AGENTS.md` in the branch, which the tools
  load as project instructions). `REVIEW_RULES` tells the reviewer to treat
  all of it as material, but that is a prompt, not a control. A pass is
  advisory: Valley must keep `submit_review` manual and advance
  automatically only where the user delegated it.

### check jobs (locally registered commands)

- Registration is local only: `timetrace workspace check add <workspace> <name>
  -- <argv…>` stores the argv list (≤ 100 arguments, ≤ 4096 characters
  each, no NUL) in the `workspace_check` table (created idempotently;
  removed with its workspace); `name` is `[a-z0-9_-]{1,40}`. The inventory
  carries only `checks: [names]` per workspace, never a command. A job's
  `check_name` is looked up by (registered workspace id, name); an invalid
  or unknown name fails "本机没有这个检查" before anything runs. No other job
  field (prompt, command-like fields) reaches argv, cwd or the environment.
- Execution: `subprocess` argv, no shell (a user who wants one registers
  `sh -c …` explicitly), stdin `/dev/null`, own session / process group,
  SIGTERM then SIGKILL of the whole group on timeout (30 min), cancel,
  stop or lease loss, and SIGKILL of the group after every exit. Same lease
  renewal and stop handling as the other jobs; no zero-spend gate (no AI
  tool runs); it holds an overall slot (not a tool slot).
- Where: a **fresh detached worktree** of the step commit under
  `~/.timetrace/checks/<job>` (same creation / removal and branch resolution
  as a review, push URL blocked as in task worktrees), so build outputs
  never dirty the step's worktree or move its branch, and the check tests
  exactly the committed result. Folder workspaces: the step's output
  directory, under the whole-run workspace lock.
- Environment: removed are every variable in the billing lists, every name
  starting with `ANTHROPIC_`, `OPENAI_`, `CLAUDE`, `CODEX_`, `GEMINI_`,
  `CURSOR_`, every secret-named variable (M-2 pattern) unless listed in
  config `check_env_keep` (never re-admits an AI tool variable), and config
  `check_env_drop`. Network is not restricted (spec).
- Output: the log is started fresh; the `$ <command>` header written by
  `run_streaming` is dropped, so the registered command line is not
  uploaded; a program that cannot start reports exit 127 without naming it.
  `check_result.output_tail` is redacted and bounded to 16 KB (again in
  `_outbound`), and empty when `upload_output_tail` is false.
- *Residual risk (by design):* a check runs code the model wrote (tests,
  build scripts, package manifests) **unsandboxed, as the user, with
  network**. Stripping credentials from the environment does not stop that
  code from reading files the user can read (SSH keys, keychains the user
  unlocked, `~/.timetrace`) or from using the network. Register checks only for
  repositories whose AI-written code you would run locally anyway; a child
  that calls `setsid` escapes the group kill.

## Addendum 2026-09-29: conversation import

### import_parse (shared conversation → import proposal, read-only)

- Job fields read: `kind`, `prompt` (the whole conversation plus Valley's
  output contract; non-empty string), `provider`, `tool_profile_id`,
  `import_id` (`[A-Za-z0-9_-]{1,64}`, else failed "invalid import" before
  anything runs). `workspace_id` is optional and ignored: no workspace,
  repository or checkout is involved, and no job field other than `prompt`
  reaches argv (the job id only names the scratch directory, reduced by
  `_safe_id`).
- Argv: the chat-turn argv with `IMPORT_RULES` in place of `CHAT_RULES`
  (`adapter.parse_import`): Claude `--permission-mode plan`,
  `--setting-sources user`, `--strict-mcp-config` (no MCP server),
  `safe_positional` prompt, always a new `--session-id` (never `--resume`);
  Codex `exec -s read-only -C <dir>` plus `-c sandbox_mode="read-only"`,
  `--skip-git-repo-check`, no writable roots, always a new thread. The
  configured `permission_mode`, `allowed_tools`, `sandbox` and `extra_args`
  do not apply. Same zero-spend gate, lease renewal / fencing, stop and
  cancel handling as a chat turn (`Agent._run_leased`); it holds one slot of
  its tool plus an overall slot.
- Where: a **fresh empty directory** `~/.timetrace/imports/<job>` (0700, under a
  0700 `imports` root that must be a real directory, not a symlink), made
  after the slot is taken and deleted — never following symlinks — on every
  exit path (completion, failure, crash, cancel, stop). A leftover directory
  of the same name is replaced. The tool therefore sees no project files,
  no `CLAUDE.md` / `AGENTS.md` and no repository settings.
- Result: parsed from the final reply only — the last fenced ```json block
  holding an object, else the last top-level `{...}` object (found with a
  JSON decoder, so braces inside strings or nested objects are never
  mistaken for one; bounded to the last 256 K characters and 4096 decode
  attempts; deep nesting that exhausts the recursion limit is treated as
  invalid). It must be an object, and at most 64 KB as compact UTF-8 JSON
  **after** every string, keys included, is redacted (again in
  `_outbound`). Anything else sends the completion without `import_result`
  and with the message "completed; 解析结果无效". The shape is not
  validated here: Valley validates candidates, project ids, types, tags and
  pipeline drafts and must treat the whole object as untrusted.
- Completion event: `import_result` (object, only when valid), `reply`
  (the final message, redacted, ≤ 32 KB), `result_summary` (first 1000
  characters of the reply), `output_tail` (per `upload_output_tail`).
  Quota block / failure: `waiting_quota` / `failed` exactly as for a chat
  turn, never with `import_result`.
- Local storage: the claim row keeps neither the prompt (the conversation)
  nor a workspace id; the run log under `~/.timetrace/logs/` holds the tool's
  stream as for any other job.
- *Residual risk:* the conversation is written by someone else (the shared
  page) and is fed to the model verbatim; it can try to steer the proposal
  (a different project, a harmful pipeline step). `IMPORT_RULES` tells the
  model to treat it as material, but that is a prompt, not a control: the
  run cannot write or reach MCP servers, and nothing is created until the
  user reviews and commits the proposal on the phone. Plan mode and the
  Codex read-only sandbox still let the model **read** files the user can
  read (by absolute path) and put them in its reply; the reply is redacted
  for known secret patterns only. The same holds for chat turns.

## Addendum 2026-09-30: protocol 2 (remote control, approvals, self-check) — 0.4.0

Reviewed against Claude Code 2.1.285 and Codex CLI 0.155.1; server contract
Valley `docs/remote-control-protocol.md` (protocol 2).

### Claude task runs are bidirectional stream-json

- Argv (`adapters/claude.build_stream_cmd`): `-p --input-format stream-json
  --output-format stream-json --verbose --permission-mode default
  --permission-prompts host --permission-prompt-tool stdio
  --disallowedTools "Bash(git push:*)" "Bash(git push*)"`, the configured
  `--allowedTools`, `--setting-sources user` (H-5), `--append-system-prompt
  SAFETY_RULES`. The prompt is **no longer an argv element**: it and every
  appended instruction are stream-json user messages on stdin, so H-4 no
  longer applies to task runs (`safe_positional` stays for the text-mode
  paths). `acceptEdits` (the old default) becomes `default` so that every
  edit reaches the rule table below; an explicitly widened
  `permission_mode` (e.g. `bypassPermissions`) is still honoured and still
  voids these guarantees (I-1).
- Protocol confirmed from local sources, not guessed: the CLI's own zod
  schemas for the `can_use_tool` control request and the permission result
  (`{behavior: allow, updatedInput?, toolUseID?}` /
  `{behavior: deny, message, interrupt?, toolUseID?}`), the print-mode
  routing, and the Agent SDK 0.3.156 host; plus a live probe of the control
  channel with an `initialize` request only (no user message, no model
  call). **Finding:** `--permission-prompts host` alone does *not* send
  prompts to the stdio host — the CLI routes them there only with
  `--permission-prompt-tool stdio` (what the SDK passes); without it an
  "ask" is resolved locally and denied. Both flags are passed.
  `permission_mode default` is a hidden alias in 2.1.285 (`--help` lists
  `manual`); both parse.
- Unknown control requests (hook callbacks, MCP messages, elicitation) get
  an error response; no hooks or SDK MCP servers are ever registered.
  `control_cancel_request` withdraws a pending prompt.
- stdin closes once every written user message has been answered by a
  `result` (or after 5 s of silence following a result, when the CLI folded
  a mid-turn message into the running turn); the process group is SIGKILLed
  after every exit, as before. Tests: a fake CLI (`tests/fake_claude_stream.py`)
  that speaks the confirmed protocol (`tests/test_claude_stream.py`).

### Local permission rules (`timetrace/approvals.py`)

Evaluated before anything is sent to the phone; a phone approval can never
override a deny.

- deny: `git push` anywhere in a compound command, `git remote
  add|set-url|remove|rename|…`, `git config` writes, creating / renaming /
  deleting branches, `git switch`, `git checkout -b/-B/--orphan`,
  `git worktree|update-ref|symbolic-ref|filter-branch|replace`, `sudo`;
  file writes outside the worktree (paths resolved with symlinks followed),
  into its `.git` or CI configuration; any read or command touching a
  credential location (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.netrc`,
  `~/.config/gh`, `~/.docker/config.json`, `~/.kube`, `~/.timetrace` outside
  the worktree itself, `~/.claude/.credentials.json`, `~/.claude.json`,
  `~/.codex/auth.json`, `Library/Keychains`, `security find-*-password`).
- allow: reads and edits inside the worktree; one simple command (no `;`,
  `&`, `|`, redirection, backticks or `$(`) from `ls cat head tail wc pwd
  grep rg find file stat which` or `git status|diff|log|show|rev-parse|
  ls-files|blame|add|commit`, without git global options (`-c`, `-C`, …),
  without `--output` / `--ext-diff` / `--pre` / `find -exec|-delete|…`,
  and with every path argument inside the worktree.
- ask: everything else → `approval_requested` with the redacted input
  (`redact_deep`, strings cut, ≤ 3.5 KB of JSON as Valley measures it) and a
  redacted one-line summary. No decision within 10 minutes (or Valley's
  `reason: "expired"`) → deny with 「用户未批准，请换一种不需要该操作的做法或结束并说明」.
  The wait pauses Claude's `timeout_seconds` clock; the lease keeps being
  renewed by the job thread, so it never expires during a wait.
- `remember` ("本任务内同类都允许"): tool + the first two words of a simple
  command, in memory for this job only; compound commands are never
  remembered or matched; deny rules are checked first.
- *Residual risk:* the shell parsing is heuristic. Obfuscated commands
  (`eval`, variables, `sh -c "…"`) are not auto-allowed but fall to "ask",
  and the phone can approve them; a phone-approved command runs as the user
  **without an OS sandbox** (Claude Code has none here), with network. The
  worktree `pushurl = no_push://blocked` still stops a plain `git push`, but
  an approved command can push by URL or do anything else the user can.
  `remember` of e.g. `npm run` covers every script. Approve only what you
  understand; the summary and input shown are redacted, not complete.

### Controls (interrupt / append)

- Controls and decisions arrive only on the lease-renewal response of the
  job's own attempt, are de-duplicated by id, and apply to task jobs only
  (chat, review, check and import jobs never get a RunControl).
- Interrupt kills the tool's process group (as a cancel does), keeps the
  worktree and branch, and writes a checkpoint (`reason
  interrupted_by_user`, verified with `verify_metadata` before the runner's
  git snapshot, H-2). Completion that reached the computer first wins; a
  cancel (`desired_action`) or shutdown still dominates. A resume job
  (`resume_of_job_id`) goes through the existing checkpoint validation and
  resumes the checkpoint's session and branch; `resume_note` is sent as the
  next user message (Codex: after SAFETY_RULES, as every Codex prompt).
- Append: Claude → a stream-json user message (never argv). Codex, or a
  Claude session that no longer reads stdin → `queued_next_turn`, then after
  the turn `exec resume` / `--resume` in the same job with exactly the same
  sandbox and read-only parameters (H-3), after re-running the zero-spend
  gate; a closed gate fences the job as before. The text is not echoed back
  in any event. SAFETY_RULES stays the system prompt of the session.
- Folder workspaces: Claude runs with stream-json input for appends but
  `--permission-prompts none` (nothing is ever asked; restricted mode as
  before). Interrupt runs the folder change check; changes outside the
  output directory fail the job instead.

### Self-check, pause, sleep prevention, reset credits, audit

- Self-check (`timetrace/health.py`): login state from the cached zero-spend
  verdict (`claude auth status` / `codex login status`) and the presence of
  the local login; `shutil.disk_usage` of the workspace volumes; `git status
  --porcelain --untracked-files=no` in each registered **main checkout**
  with `SAFE_GIT` (no hooks, no fsmonitor). Only ok/expired/missing, a
  number and booleans leave the computer.
- `timetrace agent pause|resume` writes `~/.timetrace/local_pause.json`
  (0600, separate from runner_state.json so the agent cannot overwrite it);
  Valley cannot lift it.
- `caffeinate -i -w <agent pid>` (fixed argv, `/usr/bin/caffeinate`) while
  any job runs; it exits by itself with the agent.
- Reset credits: a short-lived `codex app-server` (sanitized environment,
  20 s timeout, killed afterwards) with `initialize` → `initialized` →
  `account/rateLimits/read`. `app_server_request` refuses
  `account/rateLimitResetCredit/*` (test: the fake app-server's transcript
  never contains the consume method). Only credit ids, types, statuses,
  times and descriptions (≤ 200 chars) are reported, keyed by the opaque
  account digest (L-5). Any failure reports `status: unknown`, never 0.
- `~/.timetrace/audit.log`: created 0600 with `O_APPEND | O_NOFOLLOW`,
  bounded fields, local only. Approval entries hold the redacted summary,
  never the input.

### Verification

`tests/test_claude_stream.py`, `tests/test_approvals.py`,
`tests/test_remote_control.py`, `tests/test_remote_jobs.py`,
`tests/test_self_check.py`, `tests/test_claude_adapter.py`
(`StreamCmdTest`, `StreamAdapterTest`), `tests/test_cli.py`,
`tests/test_cloud.py`.

## Verification

- Conversation import: `tests/test_import_parse.py`.

- Task pipeline: `tests/test_per_tool_parallel.py`, `tests/test_checks.py`,
  `tests/test_review_and_check.py`.
- v2: `tests/test_folder_workspace.py`, `tests/test_parallel.py`,
  `tests/test_shutdown.py`, `tests/test_process.py`,
  `ios/scripts/tests/test_asc_submit.py` (fake HTTP layer, no network).
- Unit tests for every fix (`tests/test_worktree.py`, `test_codex_adapter.py`,
  `test_claude_adapter.py`, `test_cloud.py`, `test_claude_usage.py`,
  `test_redact.py`, `test_agent.py`, `test_process.py`, `test_config.py`,
  `test_cli.py`, `test_db.py`).
- Sandbox behaviour checked with `codex sandbox -c … -- sh -c '…'` on Codex
  0.155.1: commit in a linked worktree succeeds with the narrowed roots;
  `.git/config`, `.git/hooks`, other branch refs and the worktree's `.git` file
  are not writable; network is unreachable.
