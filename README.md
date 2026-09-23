# pane-pwd

A [Herdr](https://herdr.dev) plugin that puts each pane's current working
directory on that pane's border, so you can see what folder every pane is
executing from at a glance.

```
┌─ ~/dev/herder-plugins/pwd ───┬─ ~/dev/markdown-preview-tool ───┐
│ $                            │ ● claude                        │
│                              │   Reading src/renderer.rs…      │
│                              │                                 │
└──────────────────────────────┴─────────────────────────────────┘
```

A tab holding a single pane has no border to draw on, so the path goes in the
tab name instead - and moves back to the borders as soon as you split:

```
  ~/dev/notes-app/web-client                  ← tab name

  ● claude
    Waiting for input…
```

This is aimed at panes running coding agents, where you cannot simply type
`pwd` to find out where the agent is working. Nothing is ever written into the
pane itself - the plugin only sets Herdr's own pane label, so the process
inside the pane never sees it.

## Install

Copy-paste these two lines. That is the whole installation.

```sh
herdr plugin install <org>/herdr-pane-pwd
herdr plugin action invoke pane-pwd.start
```

Directory names appear within a second. There is nothing to build, no
dependencies to install, and no config file to write - the defaults work.

To confirm it took:

```sh
herdr plugin list          # should list pane-pwd as enabled
herdr pane list            # every pane should now have a "label"
```

The second line is only needed once, to start the watcher in the session you
are already in; after that it starts itself every time Herdr starts.

**Requirements:** Herdr 0.8.0 or newer, Python 3.9 or newer, Linux or macOS.
Both are almost certainly already on your machine; nothing else is needed.

### How it works

Herdr draws a pane's manual label on its split border, and reports each pane's
live directory as `foreground_cwd` on `herdr pane list`. This plugin is the
loop between those two facts: read the directories, write the labels.

Herdr has no "the working directory changed" event, so a small detached
watcher polls `herdr pane list` once a second (configurable). The `[[startup]]`
hook only spawns that watcher and exits, keeping it the one-shot initializer
Herdr's plugin contract asks for. The `pane.created`, `pane.moved`, and
`pane.focused` event hooks are pure latency reduction - a freshly split pane
gets labelled immediately instead of on the next tick.

`scripts/run.sh` probes the usual interpreter locations rather than trusting
`python3` to resolve, because the Herdr server's `PATH` is often smaller than
an interactive shell's.

## Caveats

- **Pane borders only exist in tabs with more than one pane.** A solo pane's
  rect fills the whole tab area with no border reserved, and no setting
  changes that. `TAB_LABELS=single` (the default) covers this by putting the
  path in the tab name whenever a tab holds exactly one pane, and handing the
  tab back to its generated name the moment you split it.
- **Polling, not events.** A `cd` shows up within `INTERVAL_MS` (1s default),
  not instantly.
- **It uses the pane *label*, the same field `herdr pane rename` writes.** See
  ownership below.

## Actions

| Action | What it does |
| --- | --- |
| `pane-pwd.sync` | One reconciliation pass over every pane, now. |
| `pane-pwd.start` | Start or restart the watcher. |
| `pane-pwd.stop` | Stop the watcher and clear every label it owns. |
| `pane-pwd.status` | Print watcher state, resolved config, and owned labels as JSON. |

Invoke one with `herdr plugin action invoke pane-pwd.<id>`, or bind a key in
`~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+d"
type = "plugin_action"
command = "pane-pwd.stop"
description = "toggle off pane directory labels"
```

Five things can trigger a reconciliation at once (the watcher tick plus four
hooks), so each pass takes an exclusive `flock` before touching state. Without
it, overlapping passes misread each other's labels as hand-written ones and
permanently released those panes - a race this plugin hit on a real server
before the lock was added.

## Ownership: your manual names always win

The plugin only overwrites a label while that label still matches what it last
wrote. Rename a pane yourself with `herdr pane rename` (or the right-click pane
menu) and pane-pwd releases that pane permanently - it will not fight you, and
it will not clear your name on stop. Set `RESPECT_MANUAL=false` to reverse
that, or list specific panes in `SKIP_PANES`.

A label that already reads exactly as pane-pwd would write it is adopted
rather than released, so losing the state file re-syncs cleanly instead of
abandoning every pane.

## Single-pane tabs

A tab with one pane has no border, so the path goes in the tab name instead.
The handoff is automatic in both directions:

| You do this | Result |
| --- | --- |
| Tab has one pane | Tab name becomes the path |
| You split it | Tab name reverts to its generated name; both pane borders carry the path |
| You close back to one pane | Tab name becomes the path again |
| You named the tab yourself | Left alone, permanently |

Herdr's generated tab name is a display counter, not the `number` field a tab
reports - they genuinely diverge (a tab reporting `number: 4` can display as
`2`). The plugin treats any all-digit name as generated and records the exact
name it replaced, so restoring puts back what you actually saw.

## Configuration

Copy `config.example.env` into the plugin's config directory:

```sh
cp config.example.env "$(herdr plugin config-dir pane-pwd)/config.env"
herdr plugin action invoke pane-pwd.start
```

| Key | Default | Meaning |
| --- | --- | --- |
| `INTERVAL_MS` | `1000` | Poll interval; the worst-case lag after a `cd`. |
| `STYLE` | `short` | Every pane: `basename`, `repo`, `short`, or `full`. |
| `FOCUSED_STYLE` | `short` | Same choices, for the focused pane only. |
| `MAX_LEN` | `40` | Label cap; longer labels elide from the left with `…`. |
| `BRANCH` | `off` | Git branch after the path: `off`, `on`, or `focused`. |
| `BRANCH_FORMAT` | ` ({branch})` | How the branch is appended; `{branch}` is the name. |
| `BRANCH_MAX_LEN` | `20` | Branch cap; longer names elide from the right with `…`. |
| `PREFIX` | *(empty)* | Literal text in front of every label. |
| `HOME_LABEL` | `~` | How `$HOME` itself renders. |
| `RESPECT_MANUAL` | `true` | Leave hand-renamed panes alone. |
| `SKIP_PANES` | *(empty)* | Comma-separated pane ids to never touch. |
| `TAB_LABELS` | `single` | Tab-name fallback: `single`, `always`, or `off`. |
| `MAX_FAILURES` | `10` | Consecutive API failures before the watcher exits. |

`STYLE` applies to every pane and `FOCUSED_STYLE` overrides it for the
focused one. Both default to `short`, so all panes read the same; set
`STYLE=basename` if you would rather have short labels everywhere and the
full path only where your cursor is.

The styles, for `/Users/you/dev/herder-plugins/pwd` inside the
`herder-plugins` checkout:

| Style | Label |
| --- | --- |
| `short` *(default)* | `~/dev/herder-plugins/pwd` |
| `basename` | `pwd` |
| `repo` | `herder-plugins/pwd` |
| `full` | `/Users/you/dev/herder-plugins/pwd` |

### Git branch

`BRANCH=on` appends the checked-out branch to every label, and
`BRANCH=focused` only to the focused pane's. It is read from `.git/HEAD`
directly, worktrees and submodules included, so it adds no `git` process per
pane per tick. A detached HEAD shows the 7-character commit hash.

```
BRANCH=on
MAX_LEN=40
BRANCH_MAX_LEN=20
# on feature/ABC-1234-add-login:
# → …rder-plugins/pwd (feature/ABC-1234-ad…)
```

The branch is cut from the right, keeping the part that identifies it. It
counts toward `MAX_LEN`, so a long branch shortens the path rather than
growing the label; the path keeps at least 8 characters before the branch is
cut further to fit.

Any key can also be set per-invocation as `HERDR_PANE_PWD_<KEY>`.

## Development

To work on the plugin, link a clone of the working tree instead of installing
it - `plugin link` points at the directory rather than copying it, so your
edits are live:

```sh
git clone https://github.com/<org>/herdr-pane-pwd.git
herdr plugin link "$PWD/herdr-pane-pwd"
herdr plugin action invoke pane-pwd.start
```

Run the tests with:

```sh
python3 test/test_pane_pwd.py
```

The suite runs against `test/fake-herdr.py`, a stand-in CLI that serves a
canned pane list and records every `pane rename`, so the behaviour is
exercised without a running Herdr server.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
