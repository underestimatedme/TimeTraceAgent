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

## Registered component workflow

A complete objective can optionally pin a DAG of existing single-objective checks. Register each component first. Each component's objective becomes its exact execution brief, so include only that component's scope. The complete objective describes the final integrated behavior; its checker must verify the whole result.

Create an operator-owned JSON file outside the model's checkout:

```json
[
  {"key":"addition","title":"Addition","check_name":"addition","depends_on":[]},
  {"key":"subtraction","title":"Subtraction","check_name":"subtraction","depends_on":[]}
]
```

Then pass `--workflow /outside/project/workflow.json` to the existing `workspace check trust PROJECT complete --objective '...' --checker ...` command. The command snapshots each component's current objective and version, and hashes the full manifest into the complete contract (protocol 2). Inventory includes public `steps`; argv and local paths still remain local. Changing/removing any referenced registration withdraws the complete template until explicitly re-registered. Root check claims also recheck referenced registrations locally.

Components must be topologically ordered, with unique keys and earlier dependencies, 1–7 components maximum; nesting is rejected. Valley appends one dedicated final integration stage depending on all components. It runs the complete checker on the final merged commit. Use `completion_version=2` in a capable client. No new Agent merge or retry engine is added: existing accepted input merges, `repair_inputs`, and independent checks apply. All N stages share 2 repairs, N+2 total check jobs, and the same absolute deadline.

Opt-in driver `tests/live_objective_workflow.py` uses `TIMETRACE_A2_SCENARIO=dag_success|dag_repair` with the existing Valley loopback fixture. It creates two independent branches and a final calculator module. `dag_repair` injects one initial failure per component and verifies that the two repairs affect distinct steps. It checks Git ancestry of both accepted branches in the final commit and reruns the integration checker separately. No accept/redo calls or production credentials are used.
