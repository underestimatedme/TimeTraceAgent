# A2 objective check contracts

The existing `check` job still runs a local registered argv in an independent checkout. A2 adds an explicit operator-owned coverage contract; ordinary checks and AI reviews do not acquire automatic final acceptance.

Register a command as before, then bind an exact finite goal to its trusted checker:

```sh
timetrace workspace check add PROJECT unit -- /absolute/python3 -I /outside/project/trusted_check.py
timetrace workspace check trust PROJECT unit \
  --objective 'The complete, finite behavior this checker verifies' \
  --checker /outside/project/trusted_check.py \
  --protect test_existing_behavior.py
```

The command must reference the external checker and an absolute executable outside the work repository. `--protect` may be repeated for repository-relative fixtures. The operator is responsible for including all checker dependencies and accurately describing coverage. This is not an OS sandbox or proof against hostile checkers. Checks execute under the existing local account and credential-stripped environment; do not register untrusted commands.

`objective_check` is an additive SQLite table. The contract digest binds argv, full objective, protocol 1, 120-second timeout, external checker/executable bytes and protected fixture bytes. Re-registering a command invalidates its objective contract. Inventory exposes only `{name, objective, version}`. The original command, paths and file hashes remain local.

An A2 check job carries `check_contract_version`. Before execution the Agent checks this version against local registration. Before and after the command it verifies trusted files and protected fixtures, then checks that tracked files match HEAD and no uncommitted sources appeared (only generated Python bytecode under `__pycache__` is allowed). All checks use a detached copy; modified checker/test/source snapshots cannot authorize automatic receipt.

The measured result includes `contract_version`, `integrity_valid`, `snapshot_clean`, and `failure_class`. Exit 0 is pass; exit 1 is the registered convention for a repairable assertion; other exits and timeouts are infrastructure failures. Author external checkers to reserve exit 1 for assertions, not missing dependencies or unexpected exceptions. Valley also validates name, exit code, actual output commit, stage iteration and active policy; a model's `verdict=pass` cannot override measured failure.

A task claim may carry `autonomy_deadline`. The existing lease deadline calculation caps both initial and renewed leases at that absolute round deadline, so pause/resume or renewal cannot prolong the round. Process cancellation and outbox receipts reuse existing Agent behavior.

Real opt-in driver: `tests/live_objective_autonomy.py`, run by Valley's existing `TestAutonomyLiveCodex`. `TIMETRACE_A2_SCENARIO=success|repair|exhausted`; it uses an existing locally verified subscription and a fresh loopback fixture/repository. It never calls accept/redo APIs. `repair` deliberately asks the real model to leave the initial defect; `exhausted` asks it to keep the defect across retries. Both are explicit fault-injection samples, not natural model failure-rate measurements. No production credential/config files belong in reports.
