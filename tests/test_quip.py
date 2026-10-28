"""Tests for quip. Run with: python3 -m unittest

Everything uses temp folders for lines, state and HOME, and fake systemctl,
crontab and loginctl commands, so nothing touches real data, units or cron.
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import io
import json
import os
import random
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
import warnings
from collections import Counter
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import quip  # noqa: E402

PY = sys.executable


def write_lines(folder: Path, category: str, body: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{category}.txt"
    path.write_text(body, encoding="utf-8")
    return path


def run_main(*argv: str):
    """Run quip.main and return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = quip.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.lines = self.tmp / "lines"
        self.state = self.tmp / "state" / "state.json"
        self.addCleanup(self._tmp.cleanup)
        self.lines.mkdir()
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for var in ("QUIP_DIR", "QUIP_STATE"):
            os.environ.pop(var, None)

    def cli(self, *argv: str):
        return run_main(*argv[:1], "--dir", str(self.lines), "--state", str(self.state), *argv[1:])


# --------------------------------------------------------------------------


class ParsingTests(TempCase):
    def test_comments_blank_lines_and_pipe_in_text(self) -> None:
        write_lines(
            self.lines,
            "boot",
            "# a comment\n\n   # indented comment\n2 | A | B | C\n 3|tight  \n",
        )
        self.assertEqual(
            quip.load("boot", self.lines),
            [quip.Line("boot", 2, "A | B | C"), quip.Line("boot", 3, "tight")],
        )

    def test_hash_in_text_is_not_a_comment(self) -> None:
        write_lines(self.lines, "boot", "1 | Item #4 ready. # not a comment\n")
        self.assertEqual(quip.load("boot", self.lines)[0].text, "Item #4 ready. # not a comment")

    def test_bad_level(self) -> None:
        for bad in ("0 | x", "6 | x", "two | x", "1.5 | x", " | x"):
            write_lines(self.lines, "boot", f"1 | fine\n{bad}\n")
            with self.assertRaises(quip.QuipDataError) as cm:
                quip.load("boot", self.lines)
            self.assertIn("boot.txt:2", str(cm.exception))

    def test_empty_text_and_missing_pipe(self) -> None:
        write_lines(self.lines, "boot", "2 |   \n")
        with self.assertRaisesRegex(quip.QuipDataError, "boot.txt:1: empty text"):
            quip.load("boot", self.lines)
        write_lines(self.lines, "boot", "2 no pipe\n")
        with self.assertRaisesRegex(quip.QuipDataError, "no '\\|'"):
            quip.load("boot", self.lines)

    def test_malformed_line_exits_3(self) -> None:
        write_lines(self.lines, "boot", "9 | x\n")
        code, out, err = self.cli("get", "boot")
        self.assertEqual((code, out), (3, ""))
        self.assertIn("boot.txt:1", err)

    def test_bad_category_name(self) -> None:
        write_lines(self.lines, "Boot", "1 | x\n")
        self.assertEqual(quip.categories(self.lines), [])
        with self.assertRaises(quip.QuipUsageError):
            quip.load("Boot", self.lines)
        problems = quip.check(self.lines)
        self.assertTrue(any(p.severity == "error" and "category name" in p.message for p in problems))

    def test_unknown_category_suggests(self) -> None:
        write_lines(self.lines, "boot", "1 | x\n")
        with self.assertRaisesRegex(quip.UnknownCategoryError, "did you mean 'boot'"):
            quip.load("boto", self.lines)
        self.assertTrue(issubclass(quip.UnknownCategoryError, LookupError))

    def test_crlf_and_bom(self) -> None:
        (self.lines / "boot.txt").write_bytes(b"\xef\xbb\xbf1 | one\r\n2 | two\r\n")
        self.assertEqual([l.text for l in quip.load("boot", self.lines)], ["one", "two"])


class FilterTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        write_lines(self.lines, "boot", "".join(f"{n} | level {n}\n" for n in range(1, 6)))

    def levels(self, **kw) -> set:
        return {int(t.split()[-1]) for t in quip.pick_many("boot", 10, lines_dir=self.lines, **kw)}

    def test_level_filters(self) -> None:
        self.assertEqual(self.levels(), {1, 2, 3, 4, 5})
        self.assertEqual(self.levels(level=4), {4})
        self.assertEqual(self.levels(min_level=4), {4, 5})
        self.assertEqual(self.levels(max_level=2), {1, 2})
        self.assertEqual(self.levels(min_level=2, max_level=3), {2, 3})

    def test_level_conflicts(self) -> None:
        with self.assertRaises(quip.QuipUsageError):
            quip.pick("boot", level=2, max_level=3, lines_dir=self.lines)
        with self.assertRaises(quip.QuipUsageError):
            quip.pick("boot", min_level=4, max_level=2, lines_dir=self.lines)
        with self.assertRaises(quip.QuipUsageError):
            quip.pick("boot", level=6, lines_dir=self.lines)
        self.assertEqual(self.cli("get", "boot", "--level", "2", "--min", "1")[0], 2)
        self.assertEqual(self.cli("get", "boot", "--level", "7")[0], 2)

    def test_cli_levels(self) -> None:
        code, out, _ = self.cli("get", "boot", "--level", "3", "--show-level")
        self.assertEqual((code, out), (0, "3\tlevel 3\n"))

    def test_width(self) -> None:
        write_lines(self.lines, "w", "1 | short\n1 | a much longer line\n")
        self.assertEqual(quip.pick_many("w", 5, width=5, lines_dir=self.lines), ["short"])
        with self.assertRaises(quip.NoLinesError):
            quip.pick("w", width=4, lines_dir=self.lines)

    def test_nothing_eligible_exits_1(self) -> None:
        code, out, err = self.cli("get", "boot", "--width", "3")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("width 3", err)


class PlaceholderTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        write_lines(
            self.lines,
            "p",
            "1 | Hello {name}.\n1 | {{braces}} stay\n2 | plain\n2 | {a} and {b}\n",
        )

    def test_unfilled_lines_are_skipped(self) -> None:
        self.assertEqual(
            sorted(quip.pick_many("p", 10, lines_dir=self.lines)), ["plain", "{braces} stay"]
        )

    def test_filled(self) -> None:
        got = quip.pick_many("p", 10, values={"name": "Bob", "a": "x", "b": "y"}, lines_dir=self.lines)
        self.assertEqual(sorted(got), ["Hello Bob.", "plain", "x and y", "{braces} stay"])

    def test_partially_filled_is_skipped(self) -> None:
        got = quip.pick_many("p", 10, values={"a": "x"}, lines_dir=self.lines)
        self.assertNotIn("x and {b}", got)
        self.assertEqual(len(got), 2)

    def test_value_with_equals_and_braces(self) -> None:
        code, out, _ = self.cli("get", "p", "--level", "1", "--set", "name=a=b{c}", "--count", "5")
        self.assertEqual(code, 0)
        self.assertIn("Hello a=b{c}.", out.splitlines())

    def test_no_format_evaluation(self) -> None:
        write_lines(self.lines, "f", "1 | {name.__class__} {0} {name!r}\n")
        self.assertEqual(quip.check(self.lines)[0].severity, "error")

    def test_width_uses_rendered_text(self) -> None:
        write_lines(self.lines, "w", "1 | {x}\n")
        self.assertEqual(quip.pick("w", width=3, values={"x": "abc"}, lines_dir=self.lines), "abc")
        with self.assertRaises(quip.NoLinesError):
            quip.pick("w", width=3, values={"x": "abcd"}, lines_dir=self.lines)

    def test_bad_set(self) -> None:
        self.assertEqual(self.cli("get", "p", "--set", "noequals")[0], 2)
        self.assertEqual(self.cli("get", "p", "--set", "Bad=1")[0], 2)

    def test_no_lines_reason_mentions_placeholders(self) -> None:
        write_lines(self.lines, "only", "1 | {hostname} up\n")
        code, _, err = self.cli("get", "only")
        self.assertEqual(code, 1)
        self.assertIn("--set for: hostname", err)


class NoRepeatTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        write_lines(self.lines, "n", "".join(f"{1 + i % 5} | line {i}\n" for i in range(10)))

    def pick(self, seed: int, **kw) -> str:
        return quip.pick(
            "n",
            no_repeat=True,
            rng=random.Random(seed),
            lines_dir=self.lines,
            state_path=self.state,
            **kw,
        )

    def test_full_cycle_then_reset(self) -> None:
        first = [self.pick(i) for i in range(10)]
        self.assertEqual(len(set(first)), 10)
        second = [self.pick(100 + i) for i in range(10)]
        self.assertEqual(len(set(second)), 10)

    def test_filters_cycle_independently(self) -> None:
        low = [self.pick(i, max_level=1) for i in range(2)]
        self.assertEqual(len(set(low)), 2)  # lines 0 and 5
        everything = [self.pick(50 + i) for i in range(8)]
        self.assertEqual(len(set(everything)), 8)
        self.assertFalse(set(low) & set(everything))

    def test_unfilled_lines_not_consumed(self) -> None:
        write_lines(self.lines, "h", "1 | a\n1 | b\n1 | needs {x}\n")
        for i in range(4):
            quip.pick("h", no_repeat=True, rng=random.Random(i), lines_dir=self.lines, state_path=self.state)
        data = json.loads(self.state.read_text())
        x_id = quip._line_id(quip.Line("h", 1, "needs {x}"))
        self.assertNotIn(x_id, data["used"]["h"])
        got = quip.pick("h", no_repeat=True, values={"x": "1"}, lines_dir=self.lines, state_path=self.state)
        self.assertEqual(got, "needs 1")

    def test_count_with_no_repeat(self) -> None:
        a = quip.pick_many("n", 6, no_repeat=True, lines_dir=self.lines, state_path=self.state)
        b = quip.pick_many("n", 4, no_repeat=True, lines_dir=self.lines, state_path=self.state)
        self.assertEqual(len(set(a + b)), 10)

    def test_removed_lines_are_pruned(self) -> None:
        for i in range(3):
            self.pick(i)
        write_lines(self.lines, "n", "1 | brand new\n")
        self.pick(9)
        data = json.loads(self.state.read_text())
        self.assertEqual(data["used"]["n"], [quip._line_id(quip.Line("n", 1, "brand new"))])

    def test_corrupt_state_is_overwritten(self) -> None:
        self.state.parent.mkdir(parents=True)
        self.state.write_text("{not json")
        self.assertTrue(self.pick(1))
        self.assertEqual(json.loads(self.state.read_text())["version"], 1)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs a non-root POSIX user")
    def test_unwritable_state_still_prints(self) -> None:
        locked = self.tmp / "ro"
        locked.mkdir()
        os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, locked, stat.S_IRWXU)
        code, out, err = run_main(
            "get", "n", "--no-repeat", "--dir", str(self.lines), "--state", str(locked / "sub" / "s.json")
        )
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("line "))
        self.assertIn("warning", err)
        code, out, err = run_main(
            "get", "n", "--no-repeat", "--dir", str(self.lines), "--state", str(locked / "s.json")
        )
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("line "))
        self.assertIn("warning", err)

    def test_no_home_still_prints(self) -> None:
        for var in ("XDG_CACHE_HOME", "LOCALAPPDATA"):
            os.environ.pop(var, None)
        with mock.patch.object(quip, "_home", return_value=None):
            self.assertEqual(quip.resolve_state_path()[0], None)
            code, out, err = run_main("get", "n", "--no-repeat", "--dir", str(self.lines))
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("line "))
        self.assertIn("no-repeat is off", err)

    def test_reset(self) -> None:
        for i in range(3):
            self.pick(i)
        self.assertEqual(self.cli("reset", "n")[0], 0)
        self.assertEqual(json.loads(self.state.read_text())["used"], {})


class MixingTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        for name in ("a", "b", "c"):
            write_lines(self.lines, name, "".join(f"2 | {name} {i}\n" for i in range(20)))

    def counts(self, spec, count, seed=0) -> Counter:
        got = quip.pick_many_lines(spec, count, rng=random.Random(seed), lines_dir=self.lines)
        self.assertEqual(len(got), len({(l.category, l.text) for l in got}))
        return Counter(l.category for l in got)

    def test_exact_quotas(self) -> None:
        for seed in range(20):
            self.assertEqual(self.counts({"a": 70, "b": 30}, 10, seed), Counter(a=7, b=3))

    def test_cli_weights_with_percent(self) -> None:
        code, out, _ = self.cli("get", "a:70%", "b:30", "--count", "10", "--show-category", "--seed", "4")
        self.assertEqual(code, 0)
        self.assertEqual(Counter(l.split("\t")[0] for l in out.splitlines()), Counter(a=7, b=3))

    def test_equal_shares(self) -> None:
        self.assertEqual(self.counts(["a", "b", "c"], 9), Counter(a=3, b=3, c=3))

    def test_weights_are_normalised(self) -> None:
        self.assertEqual(self.counts({"a": 3, "b": 1}, 8), Counter(a=6, b=2))
        self.assertEqual(self.counts({"a": "0.5", "b": "1.5"}, 8), Counter(a=2, b=6))

    def test_remainder_draw_near_70_30(self) -> None:
        firsts = Counter(
            quip.pick_many_lines({"a": 70, "b": 30}, 1, rng=random.Random(s), lines_dir=self.lines)[0].category
            for s in range(2000)
        )
        self.assertAlmostEqual(firsts["a"] / 2000, 0.7, delta=0.04)

    def test_shortfall_is_redistributed(self) -> None:
        write_lines(self.lines, "small", "2 | s1\n2 | s2\n")
        got = self.counts({"small": 50, "a": 25, "b": 25}, 10)
        self.assertEqual(got["small"], 2)
        self.assertEqual(got["a"] + got["b"], 8)
        self.assertEqual(got["a"], 4)

    def test_fewer_than_asked(self) -> None:
        write_lines(self.lines, "small", "2 | s1\n2 | s2\n")
        code, out, _ = self.cli("get", "small", "--count", "5")
        self.assertEqual((code, len(out.splitlines())), (0, 2))

    def test_typo_fails_whole_call(self) -> None:
        code, out, err = self.cli("get", "a:50", "bb:50")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unknown category 'bb'", err)

    def test_mixed_weighted_and_unweighted(self) -> None:
        self.assertEqual(self.cli("get", "a:50", "b")[0], 2)
        with self.assertRaises(quip.QuipUsageError):
            quip.pick({"a": 1, "b": None}, lines_dir=self.lines)

    def test_duplicate_and_bad_weights(self) -> None:
        self.assertEqual(self.cli("get", "a", "a")[0], 2)
        self.assertEqual(self.cli("get", "a:0", "b:1")[0], 2)
        self.assertEqual(self.cli("get", "a:x", "b:1")[0], 2)

    def test_interleaved(self) -> None:
        got = quip.pick_many_lines(["a", "b"], 20, rng=random.Random(1), lines_dir=self.lines)
        cats = [l.category for l in got]
        self.assertNotEqual(cats, sorted(cats))

    def test_seed_reproducible(self) -> None:
        one = self.cli("get", "a", "b", "c", "--count", "6", "--seed", "42")[1]
        two = self.cli("get", "a", "b", "c", "--count", "6", "--seed", "42")[1]
        self.assertEqual(one, two)

    def test_json(self) -> None:
        code, out, _ = self.cli("get", "a:1", "b:1", "--count", "4", "--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(len(data), 4)
        self.assertEqual(set(data[0]), {"category", "level", "text"})

    def test_library_forms(self) -> None:
        self.assertTrue(quip.pick({"a": 70, "b": 30}, lines_dir=self.lines))
        self.assertTrue(quip.pick(["a", "b"], lines_dir=self.lines))
        with self.assertRaises(quip.QuipUsageError):
            quip.pick_many("a", 0, lines_dir=self.lines)


class AddTests(TempCase):
    def test_new_category_gets_header(self) -> None:
        code, _, _ = self.cli("add", "fresh", "2", "Loading", "the", "lot.")
        self.assertEqual(code, 0)
        text = (self.lines / "fresh.txt").read_text()
        self.assertTrue(text.startswith("# quip lines: fresh\n"))
        self.assertTrue(text.endswith("2 | Loading the lot.\n"))
        self.assertEqual(quip.load("fresh", self.lines), [quip.Line("fresh", 2, "Loading the lot.")])

    def test_duplicate_refused(self) -> None:
        write_lines(self.lines, "boot", "1 | Hello there.\n")
        code, _, err = self.cli("add", "boot", "3", "HELLO THERE.")
        self.assertEqual(code, 2)
        self.assertIn("already in boot", err)

    def test_missing_trailing_newline(self) -> None:
        write_lines(self.lines, "boot", "1 | first")
        quip.add_line("boot", 2, "second", self.lines)
        self.assertEqual((self.lines / "boot.txt").read_text(), "1 | first\n2 | second\n")

    def test_long_line_warns(self) -> None:
        code, _, err = self.cli("add", "boot", "1", "x" * 80)
        self.assertEqual(code, 0)
        self.assertIn("80 characters", err)

    def test_bad_input(self) -> None:
        self.assertEqual(self.cli("add", "Bad", "1", "x")[0], 2)
        self.assertEqual(self.cli("add", "boot", "6", "x")[0], 2)
        self.assertEqual(self.cli("add", "boot", "1", "stray { brace")[0], 2)


class CheckTests(TempCase):
    def test_problems(self) -> None:
        write_lines(
            self.lines,
            "a",
            "1 | ok {name} {{fine}}\n1 | stray } here\n2 | Dup\n3 | dup\n1 | " + "y" * 79 + "\n1 | café\n",
        )
        write_lines(self.lines, "b", "1 | ok {name} {{fine}}\n")
        problems = quip.check(self.lines)
        messages = [(p.severity, Path(p.file).name, p.line, p.message.split(" ")[0]) for p in problems]
        self.assertIn(("error", "a.txt", 2, "stray"), messages)
        self.assertIn(("error", "a.txt", 4, "duplicate"), messages)
        self.assertIn(("warning", "a.txt", 5, "79"), messages)
        self.assertIn(("warning", "a.txt", 6, "non-ASCII"), messages)
        self.assertIn(("warning", "b.txt", 1, "also"), messages)
        code, out, _ = self.cli("check")
        self.assertEqual(code, 3)
        self.assertIn("error:", out)

    def test_warnings_only_exit_0(self) -> None:
        write_lines(self.lines, "a", "1 | " + "z" * 90 + "\n")
        self.assertEqual(self.cli("check")[0], 0)

    def test_shipped_lines_are_clean(self) -> None:
        self.assertEqual(quip.check(ROOT / "lines"), [])


class ExportListWhereTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        write_lines(self.lines, "boot", "1 | Up on {hostname}.\n4 | a | b\n")
        write_lines(self.lines, "error", "2 | Nope.\n")

    def test_markdown(self) -> None:
        code, out, _ = self.cli("export")
        self.assertEqual(code, 0)
        self.assertEqual(
            out,
            "### boot\n\n| Level | Line | Needs |\n|---|---|---|\n"
            "| 1 | Up on {hostname}. | hostname |\n| 4 | a \\| b |  |\n\n"
            "### error\n\n| Level | Line | Needs |\n|---|---|---|\n| 2 | Nope. |  |\n",
        )

    def test_json(self) -> None:
        code, out, _ = self.cli("export", "--format", "json")
        self.assertEqual(json.loads(out)["error"], [{"level": 2, "text": "Nope."}])

    def test_list(self) -> None:
        code, out, _ = self.cli("list")
        self.assertEqual(out, "boot   2  (1:1 2:0 3:0 4:1 5:0)\nerror  1  (1:0 2:1 3:0 4:0 5:0)\n")
        self.assertEqual(self.cli("list", "--names")[1], "boot\nerror\n")

    def test_where(self) -> None:
        code, out, _ = self.cli("where")
        self.assertEqual(code, 0)
        self.assertIn(f"lines  {self.lines}  (--dir)", out)
        self.assertIn(f"state  {self.state}  (--state)", out)

    def test_dir_env_and_options_before_command(self) -> None:
        os.environ["QUIP_DIR"] = str(self.lines)
        self.assertEqual(run_main("where")[1].splitlines()[0], f"lines  {self.lines}  ($QUIP_DIR)")
        self.assertEqual(run_main("--dir", str(self.lines), "list", "--names")[1], "boot\nerror\n")

    def test_missing_dir_is_usage_error(self) -> None:
        self.assertEqual(run_main("list", "--dir", str(self.tmp / "nope"))[0], 2)

    def test_stdout_only_holds_lines(self) -> None:
        code, out, err = self.cli("get", "error")
        self.assertEqual(out, "Nope.\n")
        self.assertNotIn("\x1b", out)


class ModuleTests(unittest.TestCase):
    def test_import_has_no_side_effects(self) -> None:
        result = subprocess.run(
            [PY, "-c", "import quip"], cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_list_runs_clean_with_warnings_as_errors(self) -> None:
        result = subprocess.run(
            [PY, "-W", "error", "-I", "quip.py", "list"], cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        self.assertIn(b"boot", result.stdout)

    def test_version_matches(self) -> None:
        self.assertEqual((ROOT / "VERSION").read_text().strip(), quip.__version__)
        self.assertEqual(run_main("--version"), (0, f"quip {quip.__version__}\n", ""))

    def test_get_never_touches_network(self) -> None:
        def refuse(*a, **kw):
            raise AssertionError("network access attempted")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(socket, "socket", refuse), mock.patch.object(
            socket, "create_connection", refuse
        ):
            code, out, _ = run_main("get", "boot", "status", "--count", "3", "--no-repeat", "--state", tmp + "/s.json")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 3)

    @unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "needs a git checkout")
    def test_no_secrets_or_private_refs_in_repo(self) -> None:
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=str(ROOT), stdout=subprocess.PIPE, universal_newlines=True
        ).stdout.split()
        pattern = re.compile(r"(?i)https?://wiki\.|\b192\.168\.\d|\b10\.\d+\.\d+\.\d+|\bghp_\w{20}|BEGIN [A-Z ]*PRIVATE KEY")
        for name in tracked:
            if name == "tests/test_quip.py":
                continue
            path = ROOT / name
            if path.is_file() and path.suffix in (".py", ".md", ".sh", ".txt", ".toml", ""):
                hits = pattern.findall(path.read_text(encoding="utf-8", errors="replace"))
                self.assertEqual(hits, [], name)

    def test_readme_install_commands(self) -> None:
        readme = (ROOT / "README.md").read_text()
        self.assertIn(
            "curl -fsSL https://raw.githubusercontent.com/danny-reynolds/quip/main/install.sh | sh", readme
        )
        self.assertIn(
            "wget -qO- https://raw.githubusercontent.com/danny-reynolds/quip/main/install.sh | sh", readme
        )


class ExamplesTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX shell scripts")
    def test_examples_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "quip"
            fake.write_text("#!/bin/sh\nexit 1\n")
            fake.chmod(0o755)
            env = dict(os.environ, QUIP=str(fake), PATH="/usr/bin:/bin")
            for script, fallback in (("plymouth-message.sh", "Starting up."), ("motd.sh", "Nothing to report.")):
                out = subprocess.run(
                    ["sh", str(ROOT / "examples" / script)], env=env, stdout=subprocess.PIPE, universal_newlines=True
                ).stdout
                self.assertEqual(out.strip(), fallback, script)

    @unittest.skipIf(os.name == "nt", "POSIX shell scripts")
    def test_examples_use_quip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{os.path.dirname(PY)}:/usr/bin:/bin"  # so the shebang finds python3
            env = dict(os.environ, QUIP=str(ROOT / "quip.py"), QUIP_STATE=str(Path(tmp) / "s.json"), PATH=path)
            fallbacks = {"Starting up.", "Nothing to report."}
            for script in ("plymouth-message.sh", "motd.sh"):
                out = subprocess.run(
                    ["sh", str(ROOT / "examples" / script)], env=env, stdout=subprocess.PIPE, universal_newlines=True
                ).stdout.strip()
                self.assertTrue(out, script)
                self.assertNotIn("{", out)
                self.assertNotIn(out, fallbacks)


# --------------------------------------------------------------------------
# Install, update and auto-update


FAKE_SYSTEMCTL = r"""#!/bin/sh
# Fake `systemctl --user`. Records calls; SYSTEMD_UP decides if it "works".
echo "$*" >> "$FAKE_DIR/systemctl.log"
shift  # --user
case "$1" in
    show-environment) [ "${SYSTEMD_UP:-1}" = 1 ] ;;
    enable) touch "$FAKE_DIR/enabled" ;;
    disable) rm -f "$FAKE_DIR/enabled" ;;
    is-enabled) if [ -f "$FAKE_DIR/enabled" ]; then echo enabled; else echo disabled; exit 1; fi ;;
    *) exit 0 ;;
esac
"""

FAKE_CRONTAB = r"""#!/bin/sh
# Fake crontab, kept in a file.
case "$1" in
    -l) if [ -f "$FAKE_DIR/crontab" ]; then cat "$FAKE_DIR/crontab"; else echo "no crontab for test" >&2; exit 1; fi ;;
    -) cat > "$FAKE_DIR/crontab" ;;
esac
"""

FAKE_LOGINCTL = "#!/bin/sh\necho no\n"


def make_release(dest: Path, version: str, broken: bool = False) -> Path:
    """A GitHub-style tarball of this repo at `version`, with a top folder."""
    top = f"quip-{version}"
    stage = Path(tempfile.mkdtemp(prefix="stage-", dir=str(dest)))
    src = stage / top
    shutil.copytree(str(ROOT / "lines"), str(src / "lines"))
    code = (ROOT / "quip.py").read_text(encoding="utf-8")
    code = re.sub(r'^__version__ = "[^"]+"', f'__version__ = "{version}"', code, count=1, flags=re.M)
    (src / "quip.py").write_text(code, encoding="utf-8")
    (src / "VERSION").write_text(version + "\n")
    shutil.copy2(str(ROOT / "install.sh"), str(src / "install.sh"))
    if broken:
        (src / "lines" / "boot.txt").write_text("9 | broken\n")
    tarball = stage / f"v{version}.tar.gz"
    with tarfile.open(str(tarball), "w:gz") as tf:
        tf.add(str(src), arcname=top)
    return tarball


class FakeRepo:
    """A local HTTP server laid out like the GitHub repo."""

    def __init__(self, root: Path) -> None:
        self.root = root
        handler = functools.partial(_QuietHandler, directory=str(root))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def publish(self, version: str, tarball: Path = None, tag: bool = True) -> None:
        raw = self.root / "raw" / "main"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "VERSION").write_text(version + "\n")
        if tag:
            tags = self.root / "archive" / "refs" / "tags"
            tags.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(tarball), str(tags / f"v{version}.tar.gz"))

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


@unittest.skipIf(os.name == "nt", "install is POSIX only")
class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.fakes = self.tmp / "fakes"
        self.fakes.mkdir()
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("crontab", FAKE_CRONTAB), ("loginctl", FAKE_LOGINCTL)):
            path = self.fakes / name
            path.write_text(body)
            path.chmod(0o755)
        self.releases = self.tmp / "releases"
        self.releases.mkdir()
        self.qhome = self.home / ".local" / "share" / "quip"
        self.qbin = self.home / ".local" / "bin"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def env(self, **extra: str) -> dict:
        env = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "USER": "test",
            "QUIP_SYSTEMCTL": str(self.fakes / "systemctl"),
            "QUIP_CRONTAB": str(self.fakes / "crontab"),
            "QUIP_LOGINCTL": str(self.fakes / "loginctl"),
            "FAKE_DIR": str(self.fakes),
        }
        env.update(extra)
        return env

    def run_cmd(self, args, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            args, env=self.env(**extra), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120
        )

    def install_sh(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        shell = shutil.which("dash") or "sh"
        return self.run_cmd([shell, str(ROOT / "install.sh")] + list(args), **extra)

    def quip_cmd(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        return self.run_cmd([str(self.qbin / "quip")] + list(args), **extra)

    def test_install_from_tarball_and_reinstall(self) -> None:
        tarball = make_release(self.releases, quip.__version__)
        result = self.install_sh("--no-auto-update", QUIP_TARBALL=str(tarball))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("isn't on your PATH", result.stdout)
        self.assertTrue((self.qbin / "quip").is_symlink())
        self.assertEqual(os.path.realpath(str(self.qbin / "quip")), str((self.qhome / "quip.py").resolve()))
        self.assertFalse((self.fakes / "systemctl.log").exists())

        # Runs by name once the folder is on PATH.
        path = f"{self.qbin}:{os.environ.get('PATH', '/usr/bin:/bin')}"
        listed = self.run_cmd(["quip", "list"], PATH=path)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("boot", listed.stdout)
        where = self.run_cmd(["quip", "where"], PATH=path)
        self.assertIn(f"lines  {self.qhome / 'lines'}", where.stdout)

        # Install again: swapped in place, old lines kept as lines.bak.
        again = self.install_sh("--no-auto-update", QUIP_TARBALL=str(tarball), PATH=path)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn("isn't on your PATH", again.stdout)
        self.assertTrue((self.qhome / "lines.bak" / "boot.txt").is_file())
        self.assertFalse(any(p.name.startswith(".quip-") for p in self.qhome.parent.iterdir()))

    def test_custom_home_and_bin(self) -> None:
        tarball = make_release(self.releases, quip.__version__)
        qhome, qbin = self.tmp / "custom" / "q", self.tmp / "custom" / "bin"
        result = self.install_sh(
            "--no-auto-update", QUIP_TARBALL=str(tarball), QUIP_HOME=str(qhome), QUIP_BIN=str(qbin)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((qhome / "quip.py").is_file())
        self.assertTrue((qbin / "quip").is_symlink())
        self.assertIn(f'export PATH="{qbin}:$PATH"', result.stdout)

    def test_corrupt_tarball_leaves_install(self) -> None:
        good = make_release(self.releases, quip.__version__)
        self.assertEqual(self.install_sh("--no-auto-update", QUIP_TARBALL=str(good)).returncode, 0)
        before = (self.qhome / "quip.py").read_bytes()
        for content in (b"", b"not a tarball at all"):
            bad = self.tmp / "bad.tar.gz"
            bad.write_bytes(content)
            result = self.install_sh("--no-auto-update", QUIP_TARBALL=str(bad))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((self.qhome / "quip.py").read_bytes(), before)

    def test_failed_self_check_rolls_back(self) -> None:
        good = make_release(self.releases, quip.__version__)
        self.assertEqual(self.install_sh("--no-auto-update", QUIP_TARBALL=str(good)).returncode, 0)
        broken = make_release(self.releases, "9.9.9", broken=True)
        result = self.install_sh("--no-auto-update", QUIP_TARBALL=str(broken))
        self.assertEqual(result.returncode, 4)
        self.assertIn("rolled back", result.stderr)
        self.assertEqual((self.qhome / "VERSION").read_text().strip(), quip.__version__)
        self.assertEqual(self.quip_cmd("--version").stdout.strip(), f"quip {quip.__version__}")
        self.assertFalse(any(p.name.startswith(".quip-") for p in self.qhome.parent.iterdir()))

    def test_failed_first_install_leaves_nothing(self) -> None:
        broken = make_release(self.releases, "9.9.9", broken=True)
        result = self.install_sh("--no-auto-update", QUIP_TARBALL=str(broken))
        self.assertEqual(result.returncode, 4)
        self.assertFalse(self.qhome.exists())
        self.assertFalse((self.qbin / "quip").is_symlink())

    def test_version_mismatch_refused(self) -> None:
        src = self.tmp / "src"
        shutil.copytree(str(ROOT / "lines"), str(src / "lines"))
        shutil.copy2(str(ROOT / "quip.py"), str(src / "quip.py"))
        (src / "VERSION").write_text("0.0.1\n")
        result = self.run_cmd([PY, str(src / "quip.py"), "install", "--from", str(src), "--no-auto-update"])
        self.assertEqual(result.returncode, 4)
        self.assertIn("VERSION says 0.0.1", result.stderr)
        self.assertFalse(self.qhome.exists())

    def test_missing_python_is_reported(self) -> None:
        bare = self.tmp / "bare"
        bare.mkdir()
        for tool in ("sh", "tar", "mktemp", "rm", "cp", "mkdir", "tr", "dash"):
            found = shutil.which(tool)
            if found:
                os.symlink(found, str(bare / tool))
        shell = shutil.which("dash") or shutil.which("sh")
        result = self.run_cmd([shell, str(ROOT / "install.sh")], PATH=str(bare))
        self.assertEqual(result.returncode, 4)
        self.assertIn("python3 not found", result.stderr)
        self.assertIn("apt install python3", result.stderr)
        self.assertFalse(self.qhome.exists())

    def test_old_python_is_reported(self) -> None:
        bare = self.tmp / "oldpy"
        bare.mkdir()
        fake = bare / "python3"
        fake.write_text(
            "#!/bin/sh\ncase \"$2\" in *platform*) echo 3.7.3 ;; *) exit 1 ;; esac\n"
        )
        fake.chmod(0o755)
        result = self.run_cmd(
            [shutil.which("dash") or "sh", str(ROOT / "install.sh")], PATH=f"{bare}:{os.environ.get('PATH')}"
        )
        self.assertEqual(result.returncode, 4)
        self.assertIn("3.7.3", result.stderr)

    def test_uninstall_keeps_state(self) -> None:
        tarball = make_release(self.releases, quip.__version__)
        self.assertEqual(self.install_sh(QUIP_TARBALL=str(tarball)).returncode, 0)
        self.assertTrue((self.fakes / "enabled").exists())
        got = self.quip_cmd("get", "boot", "--no-repeat")
        self.assertEqual(got.returncode, 0, got.stderr)
        state = self.home / ".cache" / "quip" / "state.json"
        self.assertTrue(state.is_file())
        result = self.install_sh("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.qhome.exists())
        self.assertFalse((self.qbin / "quip").is_symlink())
        self.assertFalse((self.fakes / "enabled").exists())
        self.assertFalse((self.home / ".config" / "systemd" / "user" / "quip-update.timer").exists())
        self.assertTrue(state.is_file())

    @unittest.skipUnless(shutil.which("curl") or shutil.which("wget"), "needs curl or wget")
    def test_install_sh_from_server_and_ref(self) -> None:
        repo = FakeRepo(self.tmp / "repo")
        self.addCleanup(repo.stop)
        repo.publish("1.0.0", make_release(self.releases, "1.0.0"))
        result = self.install_sh("--no-auto-update", QUIP_UPDATE_BASE=repo.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.quip_cmd("--version").stdout.strip(), "quip 1.0.0")
        repo.publish("1.1.0", make_release(self.releases, "1.1.0"))
        result = self.install_sh("--no-auto-update", QUIP_UPDATE_BASE=repo.base, QUIP_REF="v1.0.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.quip_cmd("--version").stdout.strip(), "quip 1.0.0")
        result = self.install_sh("--no-auto-update", QUIP_UPDATE_BASE=repo.base)
        self.assertEqual(self.quip_cmd("--version").stdout.strip(), "quip 1.1.0")

    def test_update(self) -> None:
        repo = FakeRepo(self.tmp / "repo")
        self.addCleanup(repo.stop)
        first = make_release(self.releases, "1.0.0")
        repo.publish("1.0.0", first)
        self.assertEqual(self.install_sh("--no-auto-update", QUIP_TARBALL=str(first)).returncode, 0)
        update = functools.partial(self.quip_cmd, "update", QUIP_UPDATE_BASE=repo.base)
        log = self.home / ".cache" / "quip" / "update.log"

        # Up to date.
        result = update()
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "quip 1.0.0 is up to date"))

        # VERSION bumped but no tag pushed yet: 404, untouched, exit 4, logged.
        repo.publish("1.1.0", tag=False)
        result = update()
        self.assertEqual(result.returncode, 4)
        self.assertIn("not found", log.read_text())
        self.assertEqual(self.quip_cmd("--version").stdout.strip(), "quip 1.0.0")

        # --check reports and changes nothing.
        result = update("--check")
        self.assertEqual(result.stdout.strip(), "installed 1.0.0, latest 1.1.0 (update available)")
        self.assertEqual(self.quip_cmd("--version").stdout.strip(), "quip 1.0.0")

        # Tag pushed: --quiet installs and prints nothing.
        repo.publish("1.1.0", make_release(self.releases, "1.1.0"))
        result = update("--quiet")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.quip_cmd("--version").stdout.strip(), "quip 1.1.0")
        self.assertTrue((self.qhome / "lines.bak").is_dir())

        # An older remote version is not installed on its own...
        repo.publish("1.0.0", make_release(self.releases, "1.0.0"))
        self.assertEqual(update().stdout.strip(), "quip 1.1.0 is up to date")
        # ...but is with --ref.
        result = update("--ref", "v1.0.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.quip_cmd("--version").stdout.strip(), "quip 1.0.0")

        # A broken release fails its self-check and rolls back.
        repo.publish("2.0.0", make_release(self.releases, "2.0.0", broken=True))
        result = update()
        self.assertEqual(result.returncode, 4)
        self.assertIn("rolled back", result.stderr)
        self.assertEqual(self.quip_cmd("--version").stdout.strip(), "quip 1.0.0")
        self.assertEqual(self.quip_cmd("check").returncode, 0)

        # Server down: exit 4, one log line, nothing printed with --quiet.
        repo.stop()
        before = len(log.read_text().splitlines())
        result = update("--quiet")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (4, "", ""))
        self.assertEqual(len(log.read_text().splitlines()), before + 1)
        self.assertEqual(self.quip_cmd("--version").stdout.strip(), "quip 1.0.0")

    def test_update_log_is_capped(self) -> None:
        tarball = make_release(self.releases, quip.__version__)
        self.assertEqual(self.install_sh("--no-auto-update", QUIP_TARBALL=str(tarball)).returncode, 0)
        for _ in range(55):
            self.quip_cmd("update", "--quiet", QUIP_UPDATE_BASE="http://127.0.0.1:9")
        log = self.home / ".cache" / "quip" / "update.log"
        self.assertEqual(len(log.read_text().splitlines()), 50)

    def test_update_not_installed(self) -> None:
        result = self.run_cmd([PY, str(ROOT / "quip.py"), "update"])
        self.assertEqual(result.returncode, 4)
        self.assertIn("isn't installed", result.stderr)

    def test_host_allowlist(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QUIP_UPDATE_BASE", None)
            for url in (
                "https://github.com/danny-reynolds/quip/raw/main/VERSION",
                "https://raw.githubusercontent.com/x",
                "https://codeload.github.com/x",
            ):
                quip._check_url(url)
            for url in ("http://github.com/x", "https://example.com/x", "https://github.com.evil.example/x", "file:///etc/passwd"):
                with self.assertRaises(quip.QuipInstallError, msg=url):
                    quip._check_url(url)
            os.environ["QUIP_UPDATE_BASE"] = "http://127.0.0.1:8000"
            quip._check_url("http://127.0.0.1:8000/raw/main/VERSION")
            with self.assertRaises(quip.QuipInstallError):
                quip._check_url("http://127.0.0.1:8001/x")

    def test_redirect_off_allowlist_refused(self) -> None:
        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header("Location", "https://example.com/VERSION")
                self.end_headers()

            def log_message(self, *a) -> None:
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with mock.patch.dict(os.environ, {"QUIP_UPDATE_BASE": base}):
            with self.assertRaisesRegex(quip.QuipInstallError, "refusing"):
                quip._fetch(base + "/raw/main/VERSION", 64)

    def test_unsafe_tarball_refused(self) -> None:
        bad = self.tmp / "evil.tar.gz"
        with tarfile.open(str(bad), "w:gz") as tf:
            info = tarfile.TarInfo("../escape.txt")
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
        with self.assertRaisesRegex(quip.QuipInstallError, "unsafe"):
            quip._extract(bad, self.tmp / "out")
        self.assertFalse((self.tmp / "escape.txt").exists())

    def test_autoupdate_systemd(self) -> None:
        tarball = make_release(self.releases, quip.__version__)
        result = self.install_sh(QUIP_TARBALL=str(tarball))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("systemd user timer", result.stdout)
        self.assertIn("enable-linger", result.stdout)
        units = self.home / ".config" / "systemd" / "user"
        service = (units / "quip-update.service").read_text()
        self.assertIn(f'ExecStart="{self.qbin / "quip"}" update --quiet', service)
        timer = (units / "quip-update.timer").read_text()
        for setting in ("OnCalendar=daily", "Persistent=true", "RandomizedDelaySec=1h", "OnStartupSec=10min"):
            self.assertIn(setting, timer)
        self.assertEqual(self.quip_cmd("autoupdate", "status").stdout.splitlines()[0], "on (systemd user timer)")
        self.assertEqual(self.quip_cmd("autoupdate", "off").returncode, 0)
        self.assertEqual(self.quip_cmd("autoupdate", "status").stdout.splitlines()[0], "off")
        self.assertFalse((units / "quip-update.timer").exists())
        self.assertIn("systemd", self.quip_cmd("autoupdate", "on").stdout)
        self.assertEqual(self.quip_cmd("autoupdate", "status").stdout.splitlines()[0], "on (systemd user timer)")

        # An update leaves the schedule alone, on or off.
        self.quip_cmd("autoupdate", "off")
        repo = FakeRepo(self.tmp / "repo")
        self.addCleanup(repo.stop)
        repo.publish("99.0.0", make_release(self.releases, "99.0.0"))
        self.assertEqual(self.quip_cmd("update", QUIP_UPDATE_BASE=repo.base).returncode, 0)
        self.assertEqual(self.quip_cmd("autoupdate", "status").stdout.splitlines()[0], "off")

    def test_autoupdate_cron_fallback(self) -> None:
        tarball = make_release(self.releases, quip.__version__)
        existing = "0 1 * * * echo keep me\n"
        # The fake keeps its table in $FAKE_DIR/crontab, so give it its own folder.
        cron_dir = self.tmp / "cron"
        cron_dir.mkdir()
        (cron_dir / "crontab").write_text(existing)
        result = self.install_sh(QUIP_TARBALL=str(tarball), SYSTEMD_UP="0", FAKE_DIR=str(cron_dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cron", result.stdout)
        table = (cron_dir / "crontab").read_text().splitlines()
        self.assertEqual(table[0], "0 1 * * * echo keep me")
        self.assertEqual(len([l for l in table if "# quip-autoupdate" in l]), 2)
        self.assertRegex(table[1], r"^\d+ \d+ \* \* \* \S+/quip update --quiet # quip-autoupdate$")
        self.assertTrue(table[2].startswith("@reboot sleep 600 && "))
        status = self.quip_cmd("autoupdate", "status", SYSTEMD_UP="0", FAKE_DIR=str(cron_dir))
        self.assertEqual(status.stdout.strip(), "on (cron)")
        # Turning it on twice doesn't duplicate.
        self.quip_cmd("autoupdate", "on", SYSTEMD_UP="0", FAKE_DIR=str(cron_dir))
        table = (cron_dir / "crontab").read_text().splitlines()
        self.assertEqual(len([l for l in table if "# quip-autoupdate" in l]), 2)
        self.quip_cmd("autoupdate", "off", SYSTEMD_UP="0", FAKE_DIR=str(cron_dir))
        self.assertEqual((cron_dir / "crontab").read_text(), existing)

    def test_autoupdate_manual_fallback(self) -> None:
        tarball = make_release(self.releases, quip.__version__)
        result = self.install_sh(
            QUIP_TARBALL=str(tarball), SYSTEMD_UP="0", QUIP_CRONTAB=str(self.tmp / "no-such-crontab")
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("crontab -e", result.stdout)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not available")
    def test_unit_files_verify(self) -> None:
        units = self.tmp / "units"
        units.mkdir()
        link = self.tmp / "bin" / "quip"
        link.parent.mkdir()
        link.write_text("#!/bin/sh\n")
        link.chmod(0o755)
        for name, text in quip._unit_files(link).items():
            (units / name).write_text(text)
        result = subprocess.run(
            ["systemd-analyze", "--user", "verify", str(units / "quip-update.timer"), str(units / "quip-update.service")],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            env=dict(os.environ, SYSTEMD_LOG_LEVEL="warning"),
        )
        self.assertEqual(result.returncode, 0, result.stdout)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not available")
    def test_shellcheck(self) -> None:
        scripts = [str(ROOT / "install.sh")] + [str(p) for p in (ROOT / "examples").glob("*.sh")]
        result = subprocess.run(["shellcheck", "-s", "sh"] + scripts, stdout=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
