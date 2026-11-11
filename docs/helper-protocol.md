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
| `hello` | `app` | `helper`, `proto`, `home`, `host`, `platform`, `python`, `launchd`, `agents{claude,codex: {path, version} \| null}`, `push` (has an APNs key) |
| `push.register` | `token` (hex), `env` (`sandbox`/`production`), `host` (the app's id for this host), `topic` (bundle id) | `configured` |
| `push.test` | — | `sent`, `errors[]` |
| `watch` | — | like `sessions.list`, and from now on `session` events for every session |
| `sessions.list` | — | `sessions[]`: Steer sessions plus desktop Claude / Codex history (last 30 days) |
| `session.open` | `sid`, `agent`, `cwd`, `title` (for desktop sessions) | snapshot: `session`, `items[]` (last 400), `earlier`, `requests[]`; then live events for it |
| `session.close` | `sid` | stops live events for it |
| `session.create` | `agent`, `cwd`, `text`, `images`, `model?`, `mode?` | `session`; sends the first message when given |
| `session.send` | `sid`, `text`, `images[{mime, data(base64)}]` | `id` of the new user item. While running: Codex steers, Claude queues. Codex: each `$name` naming a skill also attaches that skill |
| `session.interrupt` | `sid` | — |
| `session.respond` | `sid`, `rid`, `decision` (`allow`/`allowSession`/`deny`), `message`, `answers{questionId: label \| [labels]}` | — |
| `session.setMode` / `session.setModel` | `sid`, `mode` / `model` | `session` |
| `session.rename` | `sid`, `title` | `session` |
| `session.archive` | `sid`, `agent`, `cwd`, `archived` | — |
| `files.search` | `cwd`, `query`, `limit` | `files[]` (relative paths, fuzzy ranked) |
| `dirs.list` | `path` | `path`, `parent`, `entries[{name, path, git}]`, `git` |
| `dirs.create` | `path` (parent), `name` | `path` of the new folder; an existing folder of that name is returned as is. Errors `bad_name` (empty, `/`, `.`, `..`), `not_found`, `exists` (a file has the name) |
| `projects.recent` | — | `projects[{path, name, updatedAt, agents, git}]` |
| `agent.info` | `agent`, `cwd?` | `models[]`, `commands[]` (last known, any folder), `modes[]` |
| `agent.commands` | `agent`, `cwd`, `reload?` | `commands[{name, description, argumentHint, kind (skill\|builtin)}]` for that folder: Claude's `initialize` list (a short-lived `claude -p` with hooks off, no message sent), Codex's `skills/list`. Cached 10 minutes |
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
source (steer|desktop), live, error, suggestion, openInTerminal, terminal{via,status}, handback`

- `suggestion`: Claude's predicted next user message (`--prompt-suggestions`),
  present only while the session is idle; cleared when a turn starts.
- `openInTerminal` (Claude only): a terminal on the host has this session open
  right now, per Claude Code's registry `~/.claude/sessions/<pid>.json`
  (`kind: interactive`, `entrypoint: cli`, live pid). `terminal.via` is
  `herdr`, `tmux` or `shell` (the pane whose shell is the nearest ancestor of
  that process); `terminal.status` is the registry's (`idle`, `working`, …).
- `handback` (Claude only): Steer took the session over from that pane and
  will reopen it there (`herdr` / `tmux`).

## Sessions open in a terminal

**Codex** needs nothing special: the helper joins Codex's shared app-server
daemon (`codex app-server daemon version` → `socketPath`, a WebSocket over a
Unix socket; started with `daemon start` when needed), which `codex` in a
terminal also uses. A thread open in both is one live thread: each side sees
the other's turns as they stream, and an approval request reaches both (the
first answer wins; the other side gets `serverRequest/resolved`). Opening a
session subscribes with a bare `thread/resume`; policy, model and sandbox
travel with each `turn/start`. Without the daemon the helper falls back to a
private `codex app-server` child (`STEER_CODEX_DAEMON=0` forces that).

**Claude** has no second-client protocol, so one process owns a session at a
time:

- *Following*: while a terminal has the session open, the helper tails its
  transcript (new lines become `item` events) and mirrors the registry
  status as `state`. Every Claude session a terminal has open is listed.
- *Taking over*: `session.send` returns at once with a queued user item; the
  helper waits until the terminal is idle, closes it there (Ctrl+C in a herdr
  or tmux pane, SIGTERM in a plain shell), restarts its own process with
  `--resume` and sends.
- *Handing back*: once the phone's turn is done and the session has been idle
  for `STEER_HANDBACK_DELAY` seconds (60), the helper stops its process and
  types `claude <the terminal's own flags> --resume <id>` into the original
  pane, if that pane is still at its shell prompt.
- `session.interrupt` on a session a herdr/tmux pane is running sends Escape
  there.

## Notifications

The helper pushes straight to APNs (no relay): an ES256 token signed with
the team's APNs key by the system `openssl`, sent with `curl --http2`. Only
turns started from the phone (`session.send` / `session.create`) notify:
when they need an answer (`等你批准：…`, `等你回答：…`, `等你批准计划`), and
when they end (`做完了：<last reply>`, or `出错了：…`; interrupted turns stay
quiet). Payload: `aps.alert{title: "<project> · <title>", body}`, `aps.thread-id`
and `sid`, `host` (as registered) so a tap opens the session;
`apns-collapse-id` is the session id. Tokens Apple rejects as gone are
dropped. The key is installed per host with
`python3 ~/.local/share/steer/steerd.py push-key AuthKey_<id>.p8 <key id> <team id>`
(→ `~/.local/share/steer/apns/`, mode 600); registered devices live in
`push-devices.json`.

## Housekeeping

- While any session runs or waits for an answer, the helper holds
  `caffeinate -i -s` so the Mac does not go to sleep under it.
- `shutdown` (and so `steerd.py stop`) refuses only while the helper itself
  holds work: its own running Claude process, pending requests, a takeover or
  handback, or Codex turns on a private app-server. The app installs a newer
  `steerd.py` whenever it connects; when the old daemon was busy it keeps
  asking it to step down every 30 s and reconnects to the new one.

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
