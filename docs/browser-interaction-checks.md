# Real preview interaction acceptance

`todo_browser` is an explicit finite, operator-owned objective contract. Web project setup has a one-click preset; it supplies behavior requirements and stable DOM hooks automatically. No user shell commands, model-authored tests, or extra orchestration service are needed.

The Runner prepares pinned Playwright/Chromium and probes a real browser launch plus Docker before acknowledging configuration. It preserves the Python virtual environment path; resolving that symlink would silently select a Python without the installed browser dependency. Failed preparation reports `browser_runtime_unavailable` through the existing revision/ACK config channel.

For each independent `check` job:

1. Existing A2 detached checkout selects the current execution's exact output commit.
2. Existing `app_preview.collect` creates the immutable bundle. `RuntimePreviews` copies the same commit into its network-isolated backend container; no user preview data is reused. Checker containers have a 180-second hard lifetime and are removed on completion.
3. `preview_renderer.js` is built verbatim from TimeTrace `web/src/app-preview.ts`. The browser uses the same opaque `sandbox="allow-scripts"` iframe, CSP and source/nonce-checked fetch bridge. It never adds `allow-forms` to make tests pass. Browser external routes are blocked; no personal browser profile is used.
4. The trusted suite uses real browser fill/click/Enter/check actions, waits for visible assertions, and verifies HTTP writes. It checks click add, Enter add, complete/restore, all/active/completed filters, empty input, duplicates and reloading in a fresh page against the same backend instance. Reload checks both title and completed state. A new backend instance is used for every retry.
5. The report names failed phase, expected/actual behavior, HTTP method/path/status, renderer hash and commit. Existing `check_result.output_tail`, failure notes and `repair_inputs` deliver it to the model. The Web execution panel exposes the independent check record.
6. Exit 1 is an application assertion. Exit 2, launch errors, missing dependency, timeout or integrity change are infrastructure failures. Existing A2 policy keeps the two-repair / N+2-check / 24-hour budgets and accepts only evidence bound to the current contract/iteration/output. No new retry or receipt engine exists.

The generated external checker, interpreter, browser-check implementation, backend runtime helper, bundle collector and renderer are hashed into the registration. Changes withdraw the contract until re-registration. Models cannot weaken the oracle by editing repository tests. This follows the existing trusted-local-operator model; it is not a defense against a malicious process with full access to the Runner's account.

## Renderer update

From TimeTrace/web, run `node scripts/build-preview-checker.mjs /absolute/TimeTraceAgent/timetrace` before releasing changes to `src/app-preview.ts`. Commit the generated JS and SHA alongside the Runner release. Package data includes both. Release verification compares the recorded SHA to the Web source; a changed renderer invalidates old contracts rather than silently changing accepted coverage.

## Coverage and limits

The preset is for a TODO app with the documented selectors, a committed `index.html`, and `.keji/preview.json` using the existing Python/Node isolated runtime. It does not infer arbitrary app requirements. It verifies data across page reopen within a preview instance, not after that temporary instance expires, nor production database durability. It does not claim design quality, all browser engines, accessibility completeness or arbitrary business coverage. Other tasks retain their existing final-review or file-check mode.

## Verification

- `test_browser_checks.py`: malformed contracts, duplicate suites, dependency readiness, pinned code and Python virtual environment preservation.
- `TIMETRACE_BROWSER_LIVE=1 python -m pytest tests/test_browser_checks_live.py`: original submit bug, broken filtering, broken reload persistence must fail; repaired historical application must pass all six groups.
- `tests/live_browser_autonomy.py` via Valley `TestAutonomyLiveCodex`: real model and real orchestration, original submit defect, automatic repair/recheck/final receipt. Never calls accept/redo APIs. This is deliberate fault injection, not a model quality benchmark.
