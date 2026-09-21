#!/usr/bin/env python3
"""Stand-in for the herdr CLI so sync() can be exercised without a server.

Reads the pane list from $FAKE_PANES (a JSON array of PaneInfo dicts) and
appends every `pane rename` invocation to $FAKE_CALLS as one JSON line.
"""
import json
import os
import sys

args = sys.argv[1:]

if args[:2] == ["pane", "list"]:
    panes = json.load(open(os.environ["FAKE_PANES"]))
    json.dump({"id": "t", "result": {"type": "pane_list", "panes": panes}}, sys.stdout)
    sys.exit(0)

if args[:2] == ["workspace", "list"]:
    json.dump({"id": "t", "result": {"type": "workspace_list",
               "workspaces": [{"workspace_id": "w1"}]}}, sys.stdout)
    sys.exit(0)

if args[:2] == ["tab", "list"]:
    panes = json.load(open(os.environ["FAKE_PANES"]))
    tabs = json.load(open(os.environ["FAKE_TABS"])) if os.environ.get("FAKE_TABS") else None
    if tabs is None:
        # Default: one tab per distinct tab_id, auto-named by position.
        tabs = []
        for i, tid in enumerate(dict.fromkeys(p.get("tab_id", "w1:t1") for p in panes), 1):
            tabs.append({"tab_id": tid, "workspace_id": "w1", "number": i,
                         "label": str(i), "focused": i == 1,
                         "pane_count": sum(1 for p in panes if p.get("tab_id", "w1:t1") == tid)})
        json.dump(tabs, open(os.environ["FAKE_TABS_OUT"], "w")) if os.environ.get("FAKE_TABS_OUT") else None
    json.dump({"id": "t", "result": {"type": "tab_list", "tabs": tabs}}, sys.stdout)
    sys.exit(0)

if args[:2] == ["tab", "rename"]:
    with open(os.environ["FAKE_CALLS"], "a") as handle:
        handle.write(json.dumps(["tab"] + args[2:]) + "\n")
    if os.environ.get("FAKE_TABS"):
        tabs = json.load(open(os.environ["FAKE_TABS"]))
        for t in tabs:
            if t["tab_id"] == args[2]:
                t["label"] = args[3]
        json.dump(tabs, open(os.environ["FAKE_TABS"], "w"))
    json.dump({"id": "t", "result": {"type": "ok"}}, sys.stdout)
    sys.exit(0)

if args[:2] == ["pane", "rename"]:
    with open(os.environ["FAKE_CALLS"], "a") as handle:
        handle.write(json.dumps(args[2:]) + "\n")
    # Mutate the pane list so the next sync sees the new label, exactly as a
    # real server would.
    path = os.environ["FAKE_PANES"]
    panes = json.load(open(path))
    for pane in panes:
        if pane.get("pane_id") == args[2]:
            pane["label"] = None if args[3] == "--clear" else args[3]
    json.dump(panes, open(path, "w"))
    json.dump({"id": "t", "result": {"type": "ok"}}, sys.stdout)
    sys.exit(0)

sys.stderr.write("fake-herdr: unexpected args %r\n" % (args,))
sys.exit(2)
