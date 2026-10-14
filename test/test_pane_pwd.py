#!/usr/bin/env python3
"""Behaviour tests for pane-pwd. Run: python3 test/test_pane_pwd.py"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

FAKE = ROOT / "test" / "fake-herdr.py"
HOME = str(Path(os.path.expanduser("~")).resolve())


def pane(pane_id, cwd, label=None, focused=False, tab_id="w1:t1"):
    return {"pane_id": pane_id, "foreground_cwd": cwd, "label": label,
            "focused": focused, "tab_id": tab_id}


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.panes_path = base / "panes.json"
        self.calls_path = base / "calls.jsonl"
        self.calls_path.touch()
        self.tabs_path = base / "tabs.json"

        os.environ["HERDR_PLUGIN_ROOT"] = str(ROOT)
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = str(base / "config")
        os.environ["HERDR_PLUGIN_STATE_DIR"] = str(base / "state")
        os.environ["HERDR_BIN_PATH"] = str(FAKE)
        os.environ["FAKE_PANES"] = str(self.panes_path)
        os.environ["FAKE_CALLS"] = str(self.calls_path)
        os.environ.pop("FAKE_TABS", None)
        os.environ.pop("HERDR_SOCKET_PATH", None)
        for key in [k for k in os.environ if k.startswith("HERDR_PANE_PWD_")]:
            del os.environ[key]

        # Imported after the env is set, since the module reads it lazily.
        import pane_pwd
        self.mod = pane_pwd

    def tearDown(self):
        self.tmp.cleanup()

    def set_panes(self, panes):
        self.panes_path.write_text(json.dumps(panes))

    def calls(self):
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()]

    def set_tabs(self, tabs):
        self.tabs_path.write_text(json.dumps(tabs))
        os.environ["FAKE_TABS"] = str(self.tabs_path)

    def tab_calls(self):
        return [c[1:] for c in self.calls() if c and c[0] == "tab"]

    def pane_calls(self):
        return [c for c in self.calls() if not (c and c[0] == "tab")]

    def sync(self, **overrides):
        overrides.setdefault("tab_labels", "off")
        # Most cases assert on short, stable labels. The shipped default is
        # STYLE=short; TestDefaults covers that separately.
        overrides.setdefault("style", "basename")
        overrides.setdefault("focused_style", "basename")
        for key, value in overrides.items():
            os.environ["HERDR_PANE_PWD_" + key.upper()] = str(value)
        cfg = self.mod.load_config()
        return self.mod.sync(cfg), cfg


class TestRendering(Harness):
    def test_basename_style_uses_the_last_component(self):
        self.set_panes([pane("w1:p1", "/Users/x/dev/herder-plugins/pwd")])
        self.sync()
        self.assertEqual(self.calls(), [["w1:p1", "pwd"]])

    def test_focused_style_overrides_style_for_the_focused_pane(self):
        self.set_panes([pane("w1:p1", HOME + "/dev/thing", focused=True),
                        pane("w1:p2", HOME + "/dev/other")])
        self.sync(style="basename", focused_style="short")
        self.assertEqual(sorted(self.calls()),
                         [["w1:p1", "~/dev/thing"], ["w1:p2", "other"]])

    def test_home_itself_renders_as_tilde(self):
        self.set_panes([pane("w1:p1", HOME)])
        self.sync()
        self.assertEqual(self.calls(), [["w1:p1", "~"]])

    def test_root_renders_as_slash(self):
        self.set_panes([pane("w1:p1", "/")])
        self.sync()
        self.assertEqual(self.calls(), [["w1:p1", "/"]])

    def test_long_paths_elide_from_the_left(self):
        deep = "/" + "/".join("segment%02d" % i for i in range(12))
        self.set_panes([pane("w1:p1", deep, focused=True)])
        _, cfg = self.sync(max_len=20, style="full", focused_style="full")
        label = self.calls()[0][1]
        self.assertEqual(len(label), 20)
        self.assertTrue(label.startswith("…"))
        self.assertTrue(deep.endswith(label[1:]))

    def test_repo_style_is_relative_to_the_checkout_root(self):
        repo = Path(self.tmp.name) / "myrepo"
        (repo / ".git").mkdir(parents=True)
        (repo / "src" / "inner").mkdir(parents=True)
        self.set_panes([pane("w1:p1", str(repo / "src" / "inner"))])
        self.sync(style="repo", focused_style="repo")
        self.assertEqual(self.calls(), [["w1:p1", "myrepo/src/inner"]])

    def test_repo_style_outside_a_checkout_falls_back_to_a_short_path(self):
        self.set_panes([pane("w1:p1", "/opt")])
        self.sync(style="repo", focused_style="repo")
        self.assertEqual(self.calls(), [["w1:p1", "/opt"]])

    def test_prefix_is_prepended(self):
        self.set_panes([pane("w1:p1", "/tmp/alpha")])
        self.sync(prefix="> ")
        self.assertEqual(self.calls(), [["w1:p1", "> alpha"]])

    def test_cwd_is_used_when_foreground_cwd_is_absent(self):
        self.set_panes([{"pane_id": "w1:p1", "cwd": "/tmp/beta", "label": None, "focused": False}])
        self.sync()
        self.assertEqual(self.calls(), [["w1:p1", "beta"]])


class TestBranch(Harness):
    def make_repo(self, head, name="myrepo"):
        repo = Path(self.tmp.name) / name
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text(head + "\n")
        return repo

    def test_branch_is_off_by_default(self):
        repo = self.make_repo("ref: refs/heads/main")
        self.set_panes([pane("w1:p1", str(repo))])
        self.sync()
        self.assertEqual(self.calls(), [["w1:p1", "myrepo"]])

    def test_branch_on_appends_the_branch(self):
        repo = self.make_repo("ref: refs/heads/main")
        self.set_panes([pane("w1:p1", str(repo))])
        self.sync(branch="on")
        self.assertEqual(self.calls(), [["w1:p1", "myrepo (main)"]])

    def test_branch_focused_only_labels_the_focused_pane(self):
        repo = self.make_repo("ref: refs/heads/main")
        self.set_panes([pane("w1:p1", str(repo), focused=True),
                        pane("w1:p2", str(repo))])
        self.sync(branch="focused")
        self.assertEqual(self.calls(), [["w1:p1", "myrepo (main)"], ["w1:p2", "myrepo"]])

    def test_detached_head_shows_a_short_hash(self):
        repo = self.make_repo("0123456789abcdef0123456789abcdef01234567")
        self.set_panes([pane("w1:p1", str(repo))])
        self.sync(branch="on")
        self.assertEqual(self.calls(), [["w1:p1", "myrepo (0123456)"]])

    def test_worktree_gitdir_file_is_followed(self):
        real = Path(self.tmp.name) / "main-checkout" / ".git" / "worktrees" / "wt"
        real.mkdir(parents=True)
        (real / "HEAD").write_text("ref: refs/heads/feature/x\n")
        wt = Path(self.tmp.name) / "wt"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: %s\n" % real)
        self.set_panes([pane("w1:p1", str(wt))])
        self.sync(branch="on")
        self.assertEqual(self.calls(), [["w1:p1", "wt (feature/x)"]])

    def test_outside_a_checkout_there_is_no_suffix(self):
        self.set_panes([pane("w1:p1", "/opt")])
        self.sync(branch="on")
        self.assertEqual(self.calls(), [["w1:p1", "opt"]])

    def test_long_branches_elide_from_the_right(self):
        repo = self.make_repo("ref: refs/heads/feature/ABC-1234-add-a-very-long-thing")
        self.set_panes([pane("w1:p1", str(repo))])
        self.sync(branch="on", branch_max_len=16)
        self.assertEqual(self.calls(), [["w1:p1", "myrepo (feature/ABC-123…)"]])

    def test_the_branch_shortens_the_path_not_the_total(self):
        repo = self.make_repo("ref: refs/heads/main", name="a-rather-long-repository-name")
        self.set_panes([pane("w1:p1", str(repo))])
        self.sync(branch="on", max_len=20)
        label = self.calls()[0][1]
        self.assertEqual(len(label), 20)
        self.assertTrue(label.startswith("…"))
        self.assertTrue(label.endswith(" (main)"))

    def test_the_path_keeps_room_when_the_branch_is_long(self):
        repo = self.make_repo("ref: refs/heads/feature/ABC-1234-add-a-very-long-thing",
                              name="a-rather-long-repository-name")
        self.set_panes([pane("w1:p1", str(repo))])
        self.sync(branch="on", max_len=20, branch_max_len=80)
        label = self.calls()[0][1]
        self.assertEqual(len(label), 20)
        self.assertEqual(label, "…ry-name (feature/…)")

    def test_a_branch_switch_relabels(self):
        repo = self.make_repo("ref: refs/heads/main")
        self.set_panes([pane("w1:p1", str(repo))])
        self.sync(branch="on")
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/dev\n")
        self.set_panes([pane("w1:p1", str(repo), label="myrepo (main)")])
        self.sync(branch="on")
        self.assertEqual(self.calls()[-1], ["w1:p1", "myrepo (dev)"])

    def test_custom_format_is_used(self):
        repo = self.make_repo("ref: refs/heads/main")
        self.set_panes([pane("w1:p1", str(repo))])
        self.sync(branch="on", branch_format=" @ {branch}")
        self.assertEqual(self.calls(), [["w1:p1", "myrepo @ main"]])

    def test_an_escaped_glyph_matches_the_pasted_glyph(self):
        repo = self.make_repo("ref: refs/heads/main")
        self.set_panes([pane("w1:p1", str(repo))])
        self.sync(branch="on", branch_format=" (\\uF418 {branch})")
        escaped = self.calls()[-1][1]
        self.set_panes([pane("w1:p1", str(repo))])
        self.sync(branch="on", branch_format=" (\uf418 {branch})")
        self.assertEqual(escaped, "myrepo (\uf418 main)")
        self.assertEqual(self.calls()[-1][1], escaped)

    def test_long_escapes_reach_the_supplementary_planes(self):
        os.environ["HERDR_PANE_PWD_BRANCH_FORMAT"] = " \\U000F062C {branch}"
        self.assertEqual(self.mod.load_config()["branch_format"], " \U000F062C {branch}")

    def test_an_escaped_control_character_is_still_stripped(self):
        os.environ["HERDR_PANE_PWD_BRANCH_FORMAT"] = " \\u001b[32m{branch}"
        self.assertEqual(self.mod.load_config()["branch_format"], " [32m{branch}")

    def test_invalid_escapes_are_left_as_typed(self):
        os.environ["HERDR_PANE_PWD_BRANCH_FORMAT"] = " \\uD800 \\U00110000 \\uZZZZ {branch}"
        self.assertEqual(self.mod.load_config()["branch_format"],
                         " \\uD800 \\U00110000 \\uZZZZ {branch}")

    def test_escapes_are_read_from_config_env(self):
        cfg_dir = Path(os.environ["HERDR_PLUGIN_CONFIG_DIR"])
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.env").write_text('BRANCH_FORMAT=" (\\uF418 {branch})"\n')
        self.assertEqual(self.mod.load_config()["branch_format"], " (\uf418 {branch})")

    def test_a_format_without_the_placeholder_falls_back(self):
        os.environ["HERDR_PANE_PWD_BRANCH_FORMAT"] = " [oops]"
        self.assertEqual(self.mod.load_config()["branch_format"], " ({branch})")


class TestOwnership(Harness):
    def test_repeat_syncs_do_not_rename_again(self):
        self.set_panes([pane("w1:p1", "/tmp/alpha")])
        self.sync()
        self.sync()
        self.sync()
        self.assertEqual(self.calls(), [["w1:p1", "alpha"]])

    def test_a_directory_change_relabels(self):
        self.set_panes([pane("w1:p1", "/tmp/alpha")])
        self.sync()
        panes = json.loads(self.panes_path.read_text())
        panes[0]["foreground_cwd"] = "/tmp/gamma"
        self.set_panes(panes)
        self.sync()
        self.assertEqual(self.calls(), [["w1:p1", "alpha"], ["w1:p1", "gamma"]])

    def test_a_manual_rename_is_never_overwritten(self):
        self.set_panes([pane("w1:p1", "/tmp/alpha")])
        self.sync()
        panes = json.loads(self.panes_path.read_text())
        panes[0]["label"] = "build server"
        self.set_panes(panes)
        (renamed, manual, _, _), _ = self.sync()
        self.assertEqual((renamed, manual), (0, 1))
        # Still released after the directory moves on.
        panes = json.loads(self.panes_path.read_text())
        panes[0]["foreground_cwd"] = "/tmp/delta"
        self.set_panes(panes)
        self.sync()
        self.assertEqual(self.calls(), [["w1:p1", "alpha"]])

    def test_respect_manual_false_takes_the_pane_back(self):
        self.set_panes([pane("w1:p1", "/tmp/alpha", label="mine")])
        self.sync(respect_manual="false")
        self.assertEqual(self.calls(), [["w1:p1", "alpha"]])

    def test_skip_panes_are_left_untouched(self):
        self.set_panes([pane("w1:p1", "/tmp/alpha"), pane("w1:p2", "/tmp/beta")])
        self.sync(skip_panes="w1:p1")
        self.assertEqual(self.calls(), [["w1:p2", "beta"]])

    def test_closed_panes_are_pruned_from_state(self):
        self.set_panes([pane("w1:p1", "/tmp/alpha"), pane("w1:p2", "/tmp/beta")])
        self.sync()
        self.set_panes([pane("w1:p2", "/tmp/beta", label="beta")])
        self.sync()
        self.assertEqual(list(self.mod.load_owned()), ["w1:p2"])

    def test_clear_only_removes_labels_we_still_own(self):
        self.set_panes([pane("w1:p1", "/tmp/alpha"), pane("w1:p2", "/tmp/beta")])
        self.sync()
        panes = json.loads(self.panes_path.read_text())
        panes[0]["label"] = "hand written"
        self.set_panes(panes)
        cfg = self.mod.load_config()
        self.assertEqual(self.mod.clear_owned(cfg), 1)
        self.assertEqual(self.calls()[-1], ["w1:p2", "--clear"])
        self.assertEqual(self.mod.load_owned(), {})

    def test_malformed_pane_ids_are_ignored(self):
        self.set_panes([{"pane_id": "../evil", "foreground_cwd": "/tmp/x", "label": None},
                        pane("w1:p1", "/tmp/alpha")])
        self.sync()
        self.assertEqual(self.calls(), [["w1:p1", "alpha"]])


class TestTabLabels(Harness):
    """A single-pane tab has no border, so the tab label carries the path."""

    def tabs(self, *specs):
        return [{"tab_id": t, "workspace_id": "w1", "number": n, "label": lbl,
                 "focused": n == 1, "pane_count": c}
                for (t, n, lbl, c) in specs]

    def test_a_single_pane_tab_gets_the_path_as_its_tab_name(self):
        self.set_panes([pane("w1:p1", HOME + "/dev/thing", focused=True)])
        self.set_tabs(self.tabs(("w1:t1", 1, "1", 1)))
        self.sync(tab_labels="single", focused_style="short")
        self.assertEqual(self.tab_calls(), [["w1:t1", "~/dev/thing"]])

    def test_a_single_pane_tab_shows_the_branch_even_when_focused_only(self):
        # The tab name stands in for the focused view, so BRANCH=focused
        # applies there even when the pane itself is not focused.
        repo = Path(self.tmp.name) / "myrepo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        self.set_panes([pane("w1:p1", str(repo))])
        self.set_tabs(self.tabs(("w1:t1", 1, "1", 1)))
        self.sync(tab_labels="single", branch="focused")
        self.assertEqual(self.tab_calls(), [["w1:t1", "myrepo (main)"]])
        self.assertEqual(self.pane_calls(), [["w1:p1", "myrepo"]])

    def test_splitting_hands_the_tab_name_back_to_herdr(self):
        self.set_panes([pane("w1:p1", HOME + "/dev/thing", focused=True)])
        self.set_tabs(self.tabs(("w1:t1", 1, "1", 1)))
        self.sync(tab_labels="single", focused_style="short")
        # Second pane appears in the same tab; borders now carry the path.
        self.set_panes([pane("w1:p1", HOME + "/dev/thing", label="~/dev/thing", focused=True),
                        pane("w1:p2", "/tmp/other")])
        self.sync(tab_labels="single", focused_style="short")
        self.assertEqual(self.tab_calls(),
                         [["w1:t1", "~/dev/thing"], ["w1:t1", "1"]])

    def test_closing_back_to_one_pane_relabels_the_tab(self):
        self.set_panes([pane("w1:p1", HOME + "/dev/thing", focused=True),
                        pane("w1:p2", "/tmp/other")])
        self.set_tabs(self.tabs(("w1:t1", 1, "1", 2)))
        self.sync(tab_labels="single", focused_style="short")
        self.assertEqual(self.tab_calls(), [])
        self.set_panes([pane("w1:p1", HOME + "/dev/thing", label="~/dev/thing", focused=True)])
        self.sync(tab_labels="single", focused_style="short")
        self.assertEqual(self.tab_calls(), [["w1:t1", "~/dev/thing"]])

    def test_generated_name_is_detected_when_it_differs_from_number(self):
        """Seen live: Herdr reported number=4 while displaying the tab as "2".

        The generated name is a display counter, so an all-digit label is
        automatic regardless of what `number` says, and restore must put the
        displayed name back rather than the number.
        """
        self.set_panes([pane("w1:p1", HOME + "/dev/thing", focused=True)])
        self.set_tabs(self.tabs(("w1:t1", 4, "2", 1)))
        self.sync(tab_labels="single", focused_style="short")
        self.assertEqual(self.tab_calls(), [["w1:t1", "~/dev/thing"]])
        cfg = self.mod.load_config()
        self.mod.clear_owned(cfg)
        self.assertEqual(self.tab_calls()[-1], ["w1:t1", "2"])

    def test_a_hand_named_tab_is_left_alone(self):
        self.set_panes([pane("w1:p1", HOME + "/dev/thing", focused=True)])
        self.set_tabs(self.tabs(("w1:t1", 1, "deploy", 1)))
        self.sync(tab_labels="single", focused_style="short")
        self.assertEqual(self.tab_calls(), [])

    def test_always_mode_labels_split_tabs_too(self):
        self.set_panes([pane("w1:p1", HOME + "/dev/thing", focused=True),
                        pane("w1:p2", "/tmp/other")])
        self.set_tabs(self.tabs(("w1:t1", 1, "1", 2)))
        self.sync(tab_labels="always", focused_style="short")
        self.assertEqual(self.tab_calls(), [["w1:t1", "~/dev/thing"]])

    def test_off_mode_never_touches_tabs(self):
        self.set_panes([pane("w1:p1", HOME + "/dev/thing", focused=True)])
        self.set_tabs(self.tabs(("w1:t1", 1, "1", 1)))
        self.sync(tab_labels="off", focused_style="short")
        self.assertEqual(self.tab_calls(), [])

    def test_stop_restores_the_auto_number(self):
        self.set_panes([pane("w1:p1", HOME + "/dev/thing", focused=True)])
        self.set_tabs(self.tabs(("w1:t1", 1, "1", 1)))
        self.sync(tab_labels="single", focused_style="short")
        cfg = self.mod.load_config()
        self.mod.clear_owned(cfg)
        self.assertEqual(self.tab_calls()[-1], ["w1:t1", "1"])
        self.assertEqual(self.mod.load_owned_tabs(), {})


class TestConcurrency(Harness):
    """Regression cover for a race seen against a real Herdr server.

    The manifest fires sync from a watcher, an action, and three event hooks,
    so passes genuinely overlap. Before locking, a pass that read owned.json
    before a concurrent pass wrote it saw its own labels as unrecognized and
    released those panes as manually named.
    """

    def test_overlapping_syncs_do_not_release_each_others_labels(self):
        panes = [pane("w1:p%d" % i, "/tmp/dir%d" % i) for i in range(1, 7)]
        self.set_panes(panes)
        os.environ["HERDR_PANE_PWD_TAB_LABELS"] = "off"
        script = str(ROOT / "src" / "pane_pwd.py")
        procs = [
            subprocess.Popen([sys.executable, script, "sync"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ)
            for _ in range(6)
        ]
        for proc in procs:
            self.assertEqual(proc.wait(), 0)
        owned = self.mod.load_owned()
        self.assertEqual(len(owned), 6, "every pane should still be owned, got %r" % owned)
        self.assertEqual(owned["w1:p3"], "/tmp/dir3")

    def test_a_lost_state_file_re_adopts_rather_than_abandoning(self):
        self.set_panes([pane("w1:p1", "/tmp/alpha")])
        self.sync()  # harness pins STYLE=basename
        Path(os.environ["HERDR_PLUGIN_STATE_DIR"], "owned.json").unlink()
        (renamed, manual, _, _), _ = self.sync()
        self.assertEqual((renamed, manual), (0, 0))
        self.assertEqual(self.mod.load_owned(), {"w1:p1": "alpha"})
        # And it keeps tracking the directory afterwards.
        panes = json.loads(self.panes_path.read_text())
        panes[0]["foreground_cwd"] = "/tmp/zeta"
        self.set_panes(panes)
        self.sync()
        self.assertEqual(self.calls()[-1], ["w1:p1", "zeta"])


class TestDefaults(Harness):
    """The shipped defaults, asserted with no overrides applied."""

    def test_every_pane_shows_the_short_path_by_default(self):
        cfg = self.mod.load_config()
        self.assertEqual(cfg["style"], "short")
        self.assertEqual(cfg["focused_style"], "short")
        self.assertEqual(cfg["tab_labels"], "single")

    def test_focused_and_unfocused_panes_read_the_same_by_default(self):
        self.set_panes([pane("w1:p1", HOME + "/dev/thing", focused=True),
                        pane("w1:p2", HOME + "/dev/other")])
        os.environ["HERDR_PANE_PWD_TAB_LABELS"] = "off"
        self.mod.sync(self.mod.load_config())
        self.assertEqual(sorted(self.calls()),
                         [["w1:p1", "~/dev/thing"], ["w1:p2", "~/dev/other"]])


class TestConfig(Harness):
    def test_config_env_file_is_read(self):
        cfg_dir = Path(os.environ["HERDR_PLUGIN_CONFIG_DIR"])
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.env").write_text("# comment\nSTYLE = full\nMAX_LEN=\"12\"\n")
        cfg = self.mod.load_config()
        self.assertEqual(cfg["style"], "full")
        self.assertEqual(cfg["max_len"], 12)

    def test_bad_values_fall_back_to_defaults(self):
        os.environ["HERDR_PANE_PWD_STYLE"] = "nonsense"
        os.environ["HERDR_PANE_PWD_INTERVAL_MS"] = "not-a-number"
        cfg = self.mod.load_config()
        self.assertEqual(cfg["style"], "short")
        self.assertEqual(cfg["interval_ms"], 1000)

    def test_interval_is_clamped(self):
        os.environ["HERDR_PANE_PWD_INTERVAL_MS"] = "5"
        self.assertEqual(self.mod.load_config()["interval_ms"], 100)


class TestFailures(Harness):
    def test_a_failing_pane_list_raises_and_changes_nothing(self):
        os.environ["HERDR_BIN_PATH"] = "/nonexistent/herdr"
        cfg = self.mod.load_config()
        with self.assertRaises(self.mod.HerdrError):
            self.mod.sync(cfg)
        self.assertEqual(self.calls(), [])


class TestCli(Harness):
    def test_status_reports_not_running(self):
        out = subprocess.run(
            [sys.executable, str(ROOT / "src" / "pane_pwd.py"), "status"],
            capture_output=True, text=True, env=os.environ, check=True,
        ).stdout
        self.assertFalse(json.loads(out)["running"])

    def test_launcher_script_runs(self):
        self.set_panes([pane("w1:p1", "/tmp/alpha")])
        out = subprocess.run(
            ["sh", str(ROOT / "scripts" / "run.sh"), "sync"],
            capture_output=True, text=True, env=os.environ, check=True,
        ).stdout
        self.assertIn("1/1 pane(s) relabelled", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
