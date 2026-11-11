# App ↔ host helper protocol

The app opens one SSH exec channel running
`/bin/sh -c 'exec python3 "$HOME/.local/share/steer/steerd.py" attach'`.
`attach` starts the daemon if needed (a LaunchAgent on macOS, a detached
process elsewhere) and relays stdin/stdout to its Unix socket
`~/.local/share/steer/steerd.sock`. Every frame is one JSON object per line.

## Requests and replies

```json
→ {"id": 7, "op": "session.send", "sid": "…", "text": "run the tests", "images": []}
← {"id": 7, "ok": true, "result": {"id": "u-3f2a…"}}
← {"id": 8, "ok": false, "error": {"code": "unknown_session", "message": "…"}}
```

| op | params | result |
| --- | --- | --- |
| `hello` | `app` | `helper`, `proto`, `home`, `host`, `platform`, `python`, `launchd`, `agents{claude,codex: {path, version} \| null}` |
| `watch` | — | like `sessions.list`, and from now on `session` events for every session |
| `sessions.list` | — | `sessions[]`: Steer sessions plus desktop Claude / Codex history (last 30 days) |
| `session.open` | `sid`, `agent`, `cwd`, `title` (for desktop sessions) | snapshot: `session`, `items[]` (last 400), `earlier`, `requests[]`; then live events for it |
| `session.close` | `sid` | stops live events for it |
| `session.create` | `agent`, `cwd`, `text`, `images`, `model?`, `mode?` | `session`; sends the first message when given |
| `session.send` | `sid`, `text`, `images[{mime, data(base64)}]` | `id` of the new user item. While running: Codex steers, Claude queues |
| `session.interrupt` | `sid` | — |
| `session.respond` | `sid`, `rid`, `decision` (`allow`/`allowSession`/`deny`), `message`, `answers{questionId: label \| [labels]}` | — |
| `session.setMode` / `session.setModel` | `sid`, `mode` / `model` | `session` |
| `session.rename` | `sid`, `title` | `session` |
| `session.archive` | `sid`, `agent`, `cwd`, `archived` | — |
| `files.search` | `cwd`, `query`, `limit` | `files[]` (relative paths, fuzzy ranked) |
| `dirs.list` | `path` | `path`, `parent`, `entries[{name, path, git}]`, `git` |
| `projects.recent` | — | `projects[{path, name, updatedAt, agents, git}]` |
| `agent.info` | `agent` | `models[]`, `commands[]`, `modes[]` |
| `ping` | — | — |
| `shutdown` | `force?` | refuses with `busy` while a session runs, unless forced |

Modes: Claude `default`, `acceptEdits`, `auto`, `plan`, `bypassPermissions`.
Codex `untrusted`, `auto` (default), `readOnly`, `full`, mapped to
approval policy + sandbox per turn.

## Events

```json
{"ev": "session", "session": {…summary…}}
{"ev": "item", "sid": "…", "item": {…full item…}}
{"ev": "delta", "sid": "…", "itemId": "…", "text": "more streamed text"}
{"ev": "request", "sid": "…", "request": {…}}
{"ev": "resolved", "sid": "…", "rid": "…"}
```

`item` always carries the whole item; the app replaces by `id`. `delta` only
appends to an assistant item's `text`.

### Session summary

`sid, agent, cwd, title, state (idle|running|waiting), needs (permission|question|plan|null),
lastText, activity{category,target}, updatedAt (ms), runningSince (ms), model, mode,
source (steer|desktop), live, error`

### Timeline items

| kind | fields |
| --- | --- |
| `user` | `text`, `images`, `queued`, `failed` |
| `assistant` | `text`, `streaming`, `phase` |
| `tool` | `tool`, `category` (command, read, edit, search, web, agent, skill, mcp, meta, other), `target`, `detail`, `command`, `status` (running, ok, error, declined), `output` (tail), `diff`, `add`, `del`, `exitCode`, `endTs` |
| `plan` | `steps[{step, status}]`, `text` |
| `notice` | `code` (compacted, interrupted, agent_exited, send_failed, agent_error…), `text` |
| `turn` | `status` (completed, interrupted, error), `durationMs`, `costUsd`, `text` |

### Requests

| kind | fields |
| --- | --- |
| `permission` | `tool`, `category`, `target`, `detail`, `reason`, `diff`, `add`, `del`, `canAllowSession` |
| `question` | `questions[{id, header, question, options[{label, description}], multiSelect, allowOther}]` |
| `plan` | `plan` (Markdown) |

New fields may appear at any time; both sides ignore what they do not know.
