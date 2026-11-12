# Running the local queue beside the online agent: upgrade and rollback

`timetrace run` (the local queue) can run beside `timetrace agent run` only when **both** are this version or
newer. Up to 0.6.4 both commands take `agent.lock` exclusively, and an older `timetrace run` resets every
`running` task to `runnable` as soon as it holds that lock. So the new code never runs beside an older agent, and
an older queue cannot start while any newer agent or queue is online: the kernel lock decides, nothing is guessed.

| online now | starting | result |
| --- | --- | --- |
| nothing | agent or queue (any version) | starts |
| older agent or queue (≤ 0.6.4) | new queue | **refused**: `another timetrace agent is already running … upgrade the agent first` |
| older agent or queue | new agent | refused |
| new agent | new queue | starts, runs beside it |
| new agent and/or queue | older agent or queue | refused, also in the moment the agent leaves |
| new queue (agent gone) | new agent | starts and joins |
| new agent or queue | a second new agent or a second new queue | refused |

The default is therefore safe at every step of a mixed upgrade: the worst outcome is a refusal, never two
executors on one task.

## Upgrade (agent first, then the queue)

1. Pause the agent and wait until it is idle, as for any agent upgrade; stop `timetrace run` if it is running
   (an older queue holds the lock exclusively and must be gone before anything new can take it).
2. Install the new release for both commands.
3. Start the agent (it now holds `agent.lock` shared). Then start `timetrace run`; its first line says
   `beside an online agent (shared coding slots)`.
4. Check, read-only: `lsof "$TIMETRACE_HOME/agent.lock"` lists the agent and the queue, and starting an older
   `timetrace run` now prints `another timetrace agent is already running` and exits 1.

If the queue is started before the agent is upgraded it refuses with the message above: nothing to undo.

## Rollback

1. Stop `timetrace run` first, then pause and stop the agent. While either new process is alive an older
   agent or queue is refused, which is the intended fence, not an error to work around.
2. Check `lsof "$TIMETRACE_HOME/agent.lock"` prints nothing.
3. Switch back to the older release and start it. A task left `running` by a queue that was stopped hard is
   reset to `runnable` by whichever queue starts next and resumes with its recorded session once any crash
   fence on its slot files has been cleared by hand (the existing manual-recovery rule).

Do not delete `agent.lock`, `agent-role.lock` or `scheduler.lock` to get past a refusal: the files are inert,
the locks live in the processes that hold them.

The matrix above is exercised by `tests/test_queue_alongside_agent.py`.
