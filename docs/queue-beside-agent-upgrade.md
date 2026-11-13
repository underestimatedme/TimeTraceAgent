# Running the local queue beside the online agent: upgrade and rollback

`timetrace run` (the local queue) can run beside `timetrace agent run` only when **both** are the release that
ships this change (0.6.5) or newer. Up to 0.6.4 both commands take `agent.lock` exclusively, and an older
`timetrace run` resets every `running` task to `runnable` as soon as it holds that lock. So the new code never
runs beside an older agent, and an older queue cannot start while any newer agent or queue is online: the kernel
lock decides, nothing is guessed.

**Unreleased candidates must not be mixed in.** Commit 73dcbc1 (and 73b5971) of the branch
`claude/queue-alongside-agent` are unpublished candidates that print the same version, 0.6.4, as the released
0.6.4 but use the old exclusive lock plus `lsof`/`ps` detection. They do not hold `agent.lock` shared, so they
neither get the guarantees below nor can be told apart from 0.6.4 by `--version`. Do not run them against a home
that also runs a released or newer build, and do not install them anywhere but an isolated test home.

| online now | starting | result |
| --- | --- | --- |
| nothing | agent or queue (any version) | starts |
| older agent or queue (≤ 0.6.4) | new queue | **refused**: `another timetrace agent is already running … upgrade the agent first` |
| older agent or queue | new agent | refused |
| new agent | new queue | starts, runs beside it |
| new agent and/or queue | older agent or queue | refused, also in the moment the agent leaves |
| new queue (agent gone) | new agent | starts and joins |
| new queue **running alone** (it holds `agent.lock` exclusively) | new agent | **refused** until that queue is stopped: start the agent first, or stop the queue |
| new agent or queue | a second new agent or a second new queue | refused |

The default is therefore safe at every step of a mixed upgrade: the worst outcome is a refusal, never two
executors on one task.

## Read-only checks on a real home

Use only these on the real `~/.timetrace`. **Never start an older `timetrace run` or `timetrace agent run` there
to see whether it is refused**: when the lock or the home does not match what you expect it can take the lock,
reset `running` tasks and consume the real queue.

```bash
H="${TIMETRACE_HOME:-$HOME/.timetrace}"      # the default home when TIMETRACE_HOME is not set
lsof "$H/agent.lock" "$H/agent-role.lock" "$H/scheduler.lock"
cat "$H/local_pause.json" 2>/dev/null        # absent or {"paused": false, …}: this computer accepts work
```

`lsof` lists each process holding a lock (no output: nobody does). `agent-role.lock` and `scheduler.lock`
exist only for 0.6.5 and newer, so a running agent without `agent-role.lock` held is an older one. The refusal
of an older `timetrace run` can be demonstrated only in an isolated fixture: a temporary `TIMETRACE_HOME` with a
fake tool and no credentials, as in the matrix test of this repository (`tests/test_queue_alongside_agent.py`).

## Pause state: record it, change it only if you made it

`timetrace agent pause` and `resume` write `$H/local_pause.json`, which survives restarts, so a pause made for
an upgrade is still in force after the new agent starts and the agent will not take work until it is lifted. The
local pause also wins over the phone: nothing server-side can lift it.

1. **Before anything else, record** `cat "$H/local_pause.json"` and note whether the computer was accepting work
   (no file, or `"paused": false`) and, if it was already paused, that someone else's pause is in force.
2. Pause with `timetrace agent pause` only if it was accepting work, and wait for running jobs to finish.
3. After the upgrade or rollback, run `timetrace agent resume` **only if** the computer was accepting work before
   *and* this procedure paused it. A computer that was already paused stays paused. Then re-read the file and the
   agent's inventory (`accepting_local`) to confirm the state is the recorded one.

## Upgrade (start order: agent first, then the queue)

1. Record the pause state as above. If the computer was accepting work, pause it and wait until it is idle.
2. Stop `timetrace run` if it is running (an older queue holds the lock exclusively and must be gone before
   anything new can take it). A **new** queue running alone also holds the lock exclusively and keeps a new agent
   out, so it must be stopped too.
3. Install the new release for both commands.
4. Start the agent (it now holds `agent.lock` shared). Then start `timetrace run`; its first line says
   `beside an online agent (shared coding slots)`. With the opposite order the queue runs alone (exclusive) and the
   agent is refused until the queue is stopped.
5. Check with the read-only commands above: `lsof` lists the agent on `agent.lock` and the queue as well.
6. Restore the pause state as recorded (only the case described above).

If the queue is started while the agent is still an older release it refuses with the message above: nothing to
undo.

## Rollback

1. Record the pause state; pause only if the computer was accepting work.
2. Stop `timetrace run` first, then stop the agent. While either new process is alive an older agent or queue is
   refused, which is the intended fence, not an error to work around.
3. Check that `lsof "$H/agent.lock"` prints nothing.
4. Switch back to the older release and start it. A task left `running` by a queue that was stopped hard is
   reset to `runnable` by an **older** queue when it starts, and resumes with its recorded session once any
   crash fence on its slot files has been cleared by hand (the existing manual-recovery rule). That older queue
   has no "unverified" state and resumes even when no fence was left, so after a hard stop run `timetrace ls`
   first and stop the older queue before it starts if a task is `running` and its tool may still be writing.
   The current release does not do this: it ends such a task's record as `待核对` (no exit code), marks the task
   `failed` with its session and worktree kept, never runs it by itself whether or not a fence exists, and
   waits for an explicit `timetrace retry <id>`; the fence, if there is one, stays and still refuses after the
   retry until it is cleared by hand.
5. Restore the pause state as recorded.

Do not delete `agent.lock`, `agent-role.lock` or `scheduler.lock` to get past a refusal: the files are inert,
the locks live in the processes that hold them.

The matrix above is exercised by `tests/test_queue_alongside_agent.py`.
