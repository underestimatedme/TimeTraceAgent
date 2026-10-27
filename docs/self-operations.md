# Product-owned inspection and repair (isolated candidate)

`operations_version: 1` advertises the optional `operations_check` job. Execution protocol remains 2 and acceptance-pack configuration remains 3. Release candidate 0.5.9rc1 is source/wheel only until explicitly rolled out; it does not replace an installed daily Agent.

A probe runs an operator-registered, hash-pinned objective checker in a fresh detached checkout, with the existing command environment, lease control and cancellation gates. It never starts a model or accepts a server-written command. The completion reports the default branch HEAD separately from the checked commit and includes contract/integrity/clean-snapshot evidence. A missing or changed contract fails closed.

After an automatic receipt, the server can name that received commit and its original default-branch HEAD. The Agent checks the received candidate while the default branch is unchanged. A new default-branch commit starts a fresh observation. A missing received commit is an infrastructure failure, not permission to fall back to a different tree. Nothing is merged or deployed by this protocol; uncommitted working-copy changes are outside the probe's scope.

Valley owns scheduling, project authorization, duplicate suppression, budgets, task creation and receipts. The Agent only observes and executes claims. `tests/live_self_operations.py` configures an isolated project, then runs the ordinary Agent while reading results. It never posts a planning job, scan, repair task or final receipt. The backend opt-in `TestSelfOperationsLiveCodex` runs the same scheduler and pipeline functions used by the product.

The live fixture needs a Python environment with this package's dependencies, Playwright/Chromium and a working subscription-based Codex login. Select its exact interpreter using `TIMETRACE_SELFOPS_PYTHON`; do not assume a mutable `python3` symlink has those dependencies. Use a new loopback database, runner identity and output directory. The fixture pauses its authorization after receiving the result. See TimeTrace's `docs/self-operations/ACCEPTANCE.md` for actual results and limits.
