# Project planning jobs (A1)

Companion to the Valley and TimeTrace `codex/autonomous-progress-a1` branches.

Inventory advertises `planning_version: 1`. A planning-capable runner also needs existing protocol 2 and workflow inputs v1. Older runners never receive this new kind. Keep existing inventory fields and lease / outbox contracts.

A claim has `kind: planning`, a canonical `planning_job_id`, the chosen tool profile and existing account/model routing requirements. It does not identify a repository to modify. The encrypted server prompt contains only the selected project's allowed context.

The agent uses the same isolated text-job transport as `import_parse`: fresh private empty directory, a read-only adapter call, fresh session, subscription verification, exact identity/model gate, fenced lease and event outbox. It returns `planning_result` as bounded structured JSON. It never emits `import_result` for a planning job and does not retain the plaintext prompt in the claim row. Scratch files are removed afterwards. Log output still follows the existing redaction and retention behavior.

Valley validates the draft and performs canonical Task/Pipeline application. Agent planning is not permission to execute it directly. Subsequent task / check / review jobs continue using the existing protocol. Registration of a local check remains an explicit local setup action; the server sends only its registered name. A selected check runs after output is committed, in an isolated checkout. The model is told not to request the same shell test again.

`tests/live_autonomy.py` is an explicit opt-in driver run by Valley's `TestAutonomyLiveCodex`; it accepts only a loopback fixture and a fresh test directory, uses the existing selected adapter configuration, and reports independent tests and the explicit final test-user acceptance separately. Default provider is Codex; `TIMETRACE_A1_LIVE_PROVIDER=claude` selects an already configured Claude subscription. It never switches automatically after failure. See the TimeTrace A1 QA record for invocation and limits.
