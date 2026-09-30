# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.3.x (current `main`) | yes |
| older | no, please upgrade |

Security fixes land on `main` and ship in the next release.

## Reporting a vulnerability

Please report privately to **security@atlaspaces.com**. Do not open a public
issue for anything that could put users' code, credentials or accounts at risk.

Include what you can: affected version or commit, steps to reproduce, the
impact you expect, and whether you would like to be credited. We aim to
acknowledge within 3 working days and to agree on a fix and disclosure date
with you. Please give us a reasonable time to ship a fix before disclosing.

In scope: the runner in this repository (`timetrace`), its handling of prompts from
the phone, the sandbox and worktree isolation of AI tool runs, credential
handling, and what it sends to the TimeTrace cloud. The iOS app and the cloud
service have their own reporting path through the same address.

## What leaves this computer

To the TimeTrace cloud (`cloud_base_url`, HTTPS only), authenticated with this
computer's runner token:

- **Pairing:** the computer's name (hostname), platform (`darwin`) and timetrace version.
- **Inventory:** for each registered repository an opaque id (hash of its
  path), its folder name and default branch name; for each AI tool its name,
  whether it passed the zero-spend check, whether the binary is installed and
  the subscription tier (e.g. `max`, `plus`).
- **Quota readings:** percent used, reset time and window length per quota
  window, grouped by an opaque 8-hex digest of the tool account.
- **Task events:** status (`running`, `completed`, `failed`, …), timestamps,
  error messages, up to 1000 characters of the model's final message, and, when
  `upload_output_tail` is `true` (the default), up to 8000 bytes from the end of
  the run log. Every text field is passed through secret redaction first
  (`timetrace/redact.py`). Paths and code that appear in those texts are sent.

To Anthropic: Claude Code's own OAuth access token is sent to
`https://api.anthropic.com/api/oauth/usage` to read your quota, the same call
Claude Code's `/usage` makes.

The AI tools themselves (Claude Code, Codex) talk to their vendors as they
always do; timetrace does not change what they send.

## What never leaves this computer

- Claude Code and Codex logins: OAuth tokens (except the usage call above),
  refresh tokens, API keys, the Keychain items and auth files they live in.
- Your repositories: files, diffs, commits and branches stay local. Task
  branches are never pushed.
- Absolute repository paths (only the folder name is sent).
- Full run logs (only the redacted tail, and only if `upload_output_tail` is on).

Turn uploads of run output off with:

```sh
timetrace config set upload_output_tail false
```

Redaction is best effort: it recognises common credential formats, not every
possible secret. If your repositories hold secrets in unusual formats, switch
the tail off.

## Hardening notes for self-hosters

`cloud_base_url` must be `https://` (plain `http://` is accepted only for
`localhost` / `127.0.0.1` test servers). The client refuses redirects and
responses larger than 1 MiB. See `docs/SECURITY_REVIEW.md` for the full review
and threat model.
