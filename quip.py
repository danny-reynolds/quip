#!/usr/bin/env python3
"""quip: a bank of lines (quips, status messages, error jokes, boot text).

Ask for a category and get back a random line, optionally filtered by
silliness level (1 to 5), width and placeholders. It works as a command
(``quip get boot --max 3``) and as a library (``from quip import pick``).

The lines live in plain-text files, one per category, in a ``lines/``
folder. Each line is ``level | text``. See README.md for the details.

Standard library only, Python 3.8 or later. Only ``install``, ``update``
and ``autoupdate`` ever touch the network; ``get`` and the rest never do.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import warnings
from fractions import Fraction
from pathlib import Path
from typing import Dict, Iterator, List, NamedTuple, Optional, Sequence, Tuple, Union

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

__version__ = "1.0.0"

__all__ = [
    "Line",
    "Problem",
    "QuipError",
    "QuipUsageError",
    "UnknownCategoryError",
    "NoLinesError",
    "QuipDataError",
    "QuipInstallError",
    "QuipWarning",
    "pick",
    "pick_many",
    "pick_many_lines",
    "load",
    "categories",
    "add_line",
    "check",
    "export",
    "reset",
    "resolve_lines_dir",
    "resolve_state_path",
    "main",
]

MIN_LEVEL = 1
MAX_LEVEL = 5
WIDTH_LIMIT = 78
STATE_VERSION = 1

CATEGORY_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
NAME_RE = re.compile(r"[a-z_][a-z0-9_]*\Z")
# Order matters: escapes first, then placeholders, then any brace left over.
TOKEN_RE = re.compile(r"\{\{|\}\}|\{([a-z_][a-z0-9_]*)\}|[{}]")

FILE_HEADER = """\
# quip lines: {category}
# One entry per line: a level, a pipe character, then the text.
# Level is 1 (straight-faced) to 5 (properly daft). 1 to 3 are work-safe.
# {{name}} is a placeholder, filled with --set name=value. Write {{{{ and }}}}
# for literal braces. Lines starting with # are comments.

"""

CategorySpec = Union[str, Sequence[str], Dict[str, object]]
PathLike = Union[str, "os.PathLike[str]"]


# --------------------------------------------------------------------------
# Types and errors


class Line(NamedTuple):
    category: str
    level: int
    text: str


class Problem(NamedTuple):
    severity: str  # "error" or "warning"
    file: str
    line: int  # 0 when the problem is with the whole file
    message: str

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        return f"{self.severity}: {where}: {self.message}"


class QuipError(Exception):
    """Base class for everything quip raises on purpose."""


class QuipUsageError(QuipError, ValueError):
    """Bad arguments: conflicting options, a bad name or level."""


class UnknownCategoryError(QuipError, LookupError):
    """No such category in the lines folder."""


class NoLinesError(QuipError, LookupError):
    """Nothing is left once the filters are applied."""


class QuipDataError(QuipError):
    """A lines file is malformed, or there's no lines folder at all."""


class QuipInstallError(QuipError):
    """Install, update or uninstall failed. Nothing was changed."""


class QuipWarning(UserWarning):
    """Something is off but a line was still produced."""


def _warn(message: str) -> None:
    warnings.warn(message, QuipWarning, stacklevel=3)


# --------------------------------------------------------------------------
# Paths


def _home() -> Optional[Path]:
    try:
        home = os.path.expanduser("~")
    except Exception:
        return None
    if not home or home == "~":
        return None
    return Path(home)


def _xdg(var: str, fallback: str) -> Optional[Path]:
    """An XDG base folder. Relative values are ignored, as the spec says."""
    value = os.environ.get(var)
    if value and os.path.isabs(value):
        return Path(value)
    home = _home()
    return home / fallback if home else None


def _data_home() -> Optional[Path]:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) if appdata else None
    return _xdg("XDG_DATA_HOME", ".local/share")


def _cache_home() -> Optional[Path]:
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) if local else None
    return _xdg("XDG_CACHE_HOME", ".cache")


def _lines_dir_candidates(lines_dir: Optional[PathLike] = None) -> Iterator[Tuple[Path, str]]:
    if lines_dir is not None:
        yield Path(lines_dir), "--dir"
        return
    env = os.environ.get("QUIP_DIR")
    if env:
        yield Path(env), "$QUIP_DIR"
    yield Path(__file__).resolve().parent / "lines", "next to quip.py"
    data = _data_home()
    if data is not None:
        rule = "%APPDATA%" if os.name == "nt" else "$XDG_DATA_HOME or ~/.local/share"
        yield data / "quip" / "lines", rule
    yield Path(sys.prefix) / "share" / "quip" / "lines", "sys.prefix"


def resolve_lines_dir(lines_dir: Optional[PathLike] = None) -> Tuple[Path, str]:
    """The lines folder to use, and the rule that chose it."""
    tried = []
    for path, rule in _lines_dir_candidates(lines_dir):
        if path.is_dir():
            return path, rule
        tried.append(str(path))
    if lines_dir is not None:
        raise QuipUsageError(f"lines folder not found: {lines_dir}")
    raise QuipDataError("no lines folder found; tried " + ", ".join(tried))


def resolve_state_path(state_path: Optional[PathLike] = None) -> Tuple[Optional[Path], str]:
    """The no-repeat state file, and the rule that chose it.

    Returns ``(None, reason)`` when there is nowhere sensible to put it.
    """
    if state_path is not None:
        return Path(state_path), "--state"
    env = os.environ.get("QUIP_STATE")
    if env:
        return Path(env), "$QUIP_STATE"
    cache = _cache_home()
    if cache is None:
        return None, "no home folder"
    if os.name == "nt":
        rule = "%LOCALAPPDATA%"
    elif os.environ.get("XDG_CACHE_HOME", "").startswith("/"):
        rule = "$XDG_CACHE_HOME"
    else:
        rule = "~/.cache"
    return cache / "quip" / "state.json", rule


# --------------------------------------------------------------------------
# Parsing


def _check_category_name(category: object) -> str:
    if not isinstance(category, str) or not CATEGORY_RE.match(category):
        raise QuipUsageError(
            f"bad category name {category!r}: use lowercase letters, digits, '-' and '_'"
        )
    return category


def _parse(path: Path, category: str) -> Tuple[List[Tuple[int, Line]], List[Problem]]:
    """Every entry in a file with its line number, plus any problems."""
    entries: List[Tuple[int, Line]] = []
    problems: List[Problem] = []
    try:
        raw = path.read_bytes()
    except OSError as e:
        return entries, [Problem("error", str(path), 0, f"cannot read: {e.strerror}")]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return entries, [Problem("error", str(path), 0, f"not valid UTF-8 (byte {e.start})")]
    if text.startswith("﻿"):
        text = text[1:]
    for number, rawline in enumerate(text.split("\n"), 1):
        stripped = rawline.strip()
        if not stripped or stripped.startswith("#"):
            continue
        level, sep, body = stripped.partition("|")
        level, body = level.strip(), body.strip()
        if not sep:
            problems.append(Problem("error", str(path), number, "no '|' between level and text"))
        elif not (level.isdigit() and level.isascii() and MIN_LEVEL <= int(level) <= MAX_LEVEL):
            problems.append(
                Problem("error", str(path), number, f"level must be 1 to 5, not {level!r}")
            )
        elif not body:
            problems.append(Problem("error", str(path), number, "empty text"))
        else:
            entries.append((number, Line(category, int(level), body)))
    return entries, problems


def _category_path(category: str, lines_dir: Optional[PathLike]) -> Path:
    _check_category_name(category)
    folder, _ = resolve_lines_dir(lines_dir)
    path = folder / f"{category}.txt"
    if not path.is_file():
        import difflib

        known = categories(folder)
        close = difflib.get_close_matches(category, known, n=1)
        hint = f"; did you mean {close[0]!r}?" if close else " (see quip list)"
        raise UnknownCategoryError(f"unknown category {category!r}{hint}")
    return path


def load(category: str, lines_dir: Optional[PathLike] = None) -> List[Line]:
    """Every line in a category, in file order."""
    path = _category_path(category, lines_dir)
    entries, problems = _parse(path, category)
    if problems:
        p = problems[0]
        raise QuipDataError(f"{p.file}:{p.line}: {p.message}" if p.line else f"{p.file}: {p.message}")
    return [line for _, line in entries]


def categories(lines_dir: Optional[PathLike] = None) -> List[str]:
    """The names of every category in the lines folder, sorted."""
    folder, _ = resolve_lines_dir(lines_dir)
    return sorted(
        p.stem for p in folder.glob("*.txt") if p.is_file() and CATEGORY_RE.match(p.stem)
    )


def _line_id(line: Line) -> str:
    return hashlib.sha1(f"{line.level}|{line.text}".encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------
# Placeholders


def _placeholders(text: str) -> List[str]:
    return sorted({m.group(1) for m in TOKEN_RE.finditer(text) if m.group(1)})


def _stray_brace(text: str) -> Optional[int]:
    """The 1-based column of the first brace that is neither an escape nor a placeholder."""
    for m in TOKEN_RE.finditer(text):
        if m.group(0) in ("{", "}"):
            return m.start() + 1
    return None


def _render(text: str, values: Dict[str, str]) -> Optional[str]:
    """Fill placeholders. None if any placeholder has no value."""
    missing = []

    def substitute(m: "re.Match[str]") -> str:
        token = m.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        name = m.group(1)
        if name is None:
            return token
        if name in values:
            return values[name]
        missing.append(name)
        return token

    rendered = TOKEN_RE.sub(substitute, text)
    return None if missing else rendered


def _check_values(values: Optional[Dict[str, object]]) -> Dict[str, str]:
    if not values:
        return {}
    checked = {}
    for name, value in values.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise QuipUsageError(f"bad placeholder name {name!r}: use [a-z_][a-z0-9_]*")
        checked[name] = str(value)
    return checked


# --------------------------------------------------------------------------
# Selection


def _check_level(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_LEVEL <= value <= MAX_LEVEL:
        raise QuipUsageError(f"{what} must be a whole number from 1 to 5, not {value!r}")
    return value


def _level_range(
    level: Optional[int], min_level: Optional[int], max_level: Optional[int]
) -> Tuple[int, int]:
    if level is not None:
        if min_level is not None or max_level is not None:
            raise QuipUsageError("--level can't be combined with --min or --max")
        level = _check_level(level, "level")
        return level, level
    lo = MIN_LEVEL if min_level is None else _check_level(min_level, "min level")
    hi = MAX_LEVEL if max_level is None else _check_level(max_level, "max level")
    if lo > hi:
        raise QuipUsageError(f"min level {lo} is above max level {hi}")
    return lo, hi


def _parse_weight(raw: object, category: str) -> Fraction:
    if isinstance(raw, bool):
        raise QuipUsageError(f"bad weight for {category!r}: {raw!r}")
    if isinstance(raw, str):
        text = raw.strip()
        if text.endswith("%"):
            text = text[:-1]
        raw = text
    try:
        weight = Fraction(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        raise QuipUsageError(f"bad weight for {category!r}: {raw!r}") from None
    if weight <= 0:
        raise QuipUsageError(f"weight for {category!r} must be above zero")
    return weight


def _weights(category: CategorySpec) -> List[Tuple[str, Fraction]]:
    """Normalise a category spec into (name, weight) pairs."""
    if isinstance(category, str):
        items: List[Tuple[object, object]] = [(category, None)]
    elif isinstance(category, dict):
        items = list(category.items())
    else:
        items = [(name, None) for name in category]
    if not items:
        raise QuipUsageError("no category given")
    names = [_check_category_name(name) for name, _ in items]
    for name in names:
        if names.count(name) > 1:
            raise QuipUsageError(f"category {name!r} is named twice")
    weighted = [weight is not None for _, weight in items]
    if any(weighted) and not all(weighted):
        raise QuipUsageError("give a weight to every category or to none of them")
    if not any(weighted):
        return [(name, Fraction(1)) for name in names]
    return [(name, _parse_weight(weight, name)) for name, (_, weight) in zip(names, items)]


def _weighted_index(weights: Sequence[Fraction], rng: random.Random) -> int:
    total = float(sum(weights))
    target = rng.random() * total
    for i, weight in enumerate(weights):
        target -= float(weight)
        if target < 0:
            return i
    return len(weights) - 1


def _allocate(count: int, weights: Sequence[Fraction], caps: Sequence[int], rng: random.Random) -> List[int]:
    """How many lines each category gives.

    A category whose share is more than it has gives everything it has, and
    the rest is shared among the others in proportion to their weights.
    Then each category gets the whole part of its exact share, and the
    leftover goes by weighted draw on the fractional parts.
    """
    alloc = [0] * len(weights)
    remaining = min(count, sum(caps))
    open_ = [i for i, cap in enumerate(caps) if cap > 0]
    exact: Dict[int, Fraction] = {}
    while open_ and remaining > 0:
        total = sum(weights[i] for i in open_)
        exact = {i: remaining * weights[i] / total for i in open_}
        full = [i for i in open_ if exact[i] >= caps[i]]
        if not full:
            break
        for i in full:
            alloc[i] = caps[i]
            remaining -= caps[i]
        open_ = [i for i in open_ if i not in full]
        exact = {}
    for i, share in exact.items():
        alloc[i] = int(share)
    fractions = {i: share - int(share) for i, share in exact.items() if share > int(share)}
    pool = list(fractions)
    for _ in range(remaining - sum(int(share) for share in exact.values())):
        chosen = pool.pop(_weighted_index([fractions[i] for i in pool], rng))
        alloc[chosen] += 1
    return alloc


def _draw(
    candidates: List[Tuple[str, Line]], k: int, used: Optional[set], rng: random.Random
) -> List[Line]:
    """k distinct candidates. With a used set, prefer lines not yet used."""
    if used is None:
        return [line for _, line in rng.sample(candidates, k)]
    all_ids = {lid for lid, _ in candidates}
    chosen: List[Line] = []
    chosen_ids: set = set()
    for _ in range(k):
        remaining = [c for c in candidates if c[0] not in chosen_ids]
        fresh = [c for c in remaining if c[0] not in used]
        if not fresh:
            used -= all_ids
            used |= chosen_ids
            fresh = remaining
        lid, line = rng.choice(fresh)
        chosen.append(line)
        chosen_ids.add(lid)
        used.add(lid)
    return chosen


def _describe_filters(lo: int, hi: int, width: Optional[int]) -> str:
    parts = []
    if (lo, hi) != (MIN_LEVEL, MAX_LEVEL):
        parts.append(f"level {lo}" if lo == hi else f"levels {lo} to {hi}")
    if width is not None:
        parts.append(f"width {width}")
    return ", ".join(parts)


def _select(
    category: CategorySpec,
    count: int,
    *,
    level: Optional[int] = None,
    min_level: Optional[int] = None,
    max_level: Optional[int] = None,
    width: Optional[int] = None,
    values: Optional[Dict[str, object]] = None,
    no_repeat: bool = False,
    rng: Optional[random.Random] = None,
    lines_dir: Optional[PathLike] = None,
    state_path: Optional[PathLike] = None,
) -> List[Line]:
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise QuipUsageError(f"count must be 1 or more, not {count!r}")
    if width is not None and (isinstance(width, bool) or not isinstance(width, int) or width < 1):
        raise QuipUsageError(f"width must be 1 or more, not {width!r}")
    lo, hi = _level_range(level, min_level, max_level)
    specs = _weights(category)
    filled = _check_values(values)
    folder, _ = resolve_lines_dir(lines_dir)
    rng = rng if rng is not None else random.Random()

    # Load everything first, so a typo in any category fails the whole call.
    pools = []
    needs: set = set()
    for name, weight in specs:
        lines = load(name, folder)
        candidates: Dict[str, Line] = {}
        for line in lines:
            if not lo <= line.level <= hi:
                continue
            text = _render(line.text, filled)
            if text is None:
                needs.update(p for p in _placeholders(line.text) if p not in filled)
                continue
            if width is not None and len(text) > width:
                continue
            candidates.setdefault(_line_id(line), Line(name, line.level, text))
        pools.append((name, weight, lines, list(candidates.items())))

    alloc = _allocate(count, [p[1] for p in pools], [len(p[3]) for p in pools], rng)
    if not any(alloc):
        names = ", ".join(repr(name) for name, _ in specs)
        reason = f"no lines in {names}"
        filters = _describe_filters(lo, hi, width)
        if filters:
            reason += f" match {filters}"
        if needs:
            reason += "; some need --set for: " + ", ".join(sorted(needs))
        raise NoLinesError(reason)

    picked: List[Line] = []
    if no_repeat:
        with _State(resolve_state_path(state_path)) as state:
            for (name, _, lines, candidates), k in zip(pools, alloc):
                if not k:
                    continue
                used = set(state.used.get(name, []))
                picked += _draw(candidates, k, used, rng)
                existing = {_line_id(line) for line in lines}
                state.used[name] = sorted(used & existing)
            state.save()
    else:
        for (_, _, _, candidates), k in zip(pools, alloc):
            if k:
                picked += _draw(candidates, k, None, rng)
    rng.shuffle(picked)
    return picked


def pick(
    category: CategorySpec,
    *,
    level: Optional[int] = None,
    min_level: Optional[int] = None,
    max_level: Optional[int] = None,
    width: Optional[int] = None,
    values: Optional[Dict[str, object]] = None,
    no_repeat: bool = False,
    rng: Optional[random.Random] = None,
    lines_dir: Optional[PathLike] = None,
    state_path: Optional[PathLike] = None,
) -> str:
    """One random line.

    ``category`` is a name, a list of names (equal shares) or a dict of
    name to weight. Raises NoLinesError if nothing matches.
    """
    return _select(
        category,
        1,
        level=level,
        min_level=min_level,
        max_level=max_level,
        width=width,
        values=values,
        no_repeat=no_repeat,
        rng=rng,
        lines_dir=lines_dir,
        state_path=state_path,
    )[0].text


def pick_many(category: CategorySpec, count: int, **options: object) -> List[str]:
    """Up to ``count`` distinct lines, shuffled. Same options as pick()."""
    return [line.text for line in _select(category, count, **options)]  # type: ignore[arg-type]


def pick_many_lines(category: CategorySpec, count: int, **options: object) -> List[Line]:
    """Like pick_many(), but returns Line objects (category, level, text)."""
    return _select(category, count, **options)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# No-repeat state


class _State:
    """The no-repeat state file, locked while it's read and written.

    Problems here never stop a line being picked: they turn into a warning
    and a plain random pick.
    """

    def __init__(self, resolved: Tuple[Optional[Path], str]) -> None:
        self.path, self.reason = resolved
        self.used: Dict[str, List[str]] = {}
        self.writable = self.path is not None
        self._lock = None

    def __enter__(self) -> "_State":
        if self.path is None:
            _warn(f"no-repeat is off ({self.reason}); picking at random")
            return self
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            if not self.path.parent.is_dir():
                _warn(f"can't create {self.path.parent} ({e.strerror}); no-repeat is off")
                self.writable = False
                return self
        self._lock = self._take_lock()
        self.used = _read_state(self.path)
        return self

    def _take_lock(self):  # type: ignore[no-untyped-def]
        if fcntl is None or self.path is None:
            return None
        try:
            handle = open(str(self.path) + ".lock", "a")
        except OSError:
            return None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            handle.close()
            return None
        return handle

    def save(self) -> None:
        if not self.writable or self.path is None:
            return
        data = {"version": STATE_VERSION, "used": {k: v for k, v in sorted(self.used.items()) if v}}
        try:
            _write_atomic(self.path, json.dumps(data, indent=1) + "\n")
        except OSError as e:
            _warn(f"can't save no-repeat state to {self.path} ({e.strerror})")

    def __exit__(self, *exc: object) -> None:
        if self._lock is not None:
            self._lock.close()  # closing releases the flock


def _read_state(path: Path) -> Dict[str, List[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return {}
    used = data.get("used")
    if not isinstance(used, dict):
        return {}
    return {
        k: [i for i in v if isinstance(i, str)]
        for k, v in used.items()
        if isinstance(k, str) and isinstance(v, list)
    }


def _write_atomic(path: Path, text: str) -> None:
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def reset(category: Optional[str] = None, state_path: Optional[PathLike] = None) -> None:
    """Forget the no-repeat history for one category, or for all of them."""
    resolved = resolve_state_path(state_path)
    if resolved[0] is None:
        return
    with _State(resolved) as state:
        if category is None:
            state.used = {}
        else:
            state.used.pop(_check_category_name(category), None)
        state.save()


# --------------------------------------------------------------------------
# Adding, checking and exporting


def _is_ascii(text: str) -> bool:
    return all(ord(c) < 128 for c in text)


def add_line(category: str, level: int, text: str, lines_dir: Optional[PathLike] = None) -> Line:
    """Append ``level | text`` to a category, creating the file if it's new."""
    _check_category_name(category)
    _check_level(level, "level")
    text = text.strip()
    if not text:
        raise QuipUsageError("the text is empty")
    if "\n" in text or "\r" in text:
        raise QuipUsageError("the text must be a single line")
    column = _stray_brace(text)
    if column is not None:
        raise QuipUsageError(
            f"stray brace at column {column}: placeholders are {{name}}, literal braces are {{{{ and }}}}"
        )
    folder, _ = resolve_lines_dir(lines_dir)
    path = folder / f"{category}.txt"
    if path.exists():
        entries, problems = _parse(path, category)
        if problems:
            raise QuipDataError(str(problems[0]))
        for number, existing in entries:
            if existing.text.casefold() == text.casefold():
                raise QuipUsageError(f"already in {category} (line {number}): {existing.text}")
    if len(text) > WIDTH_LIMIT:
        _warn(f"the text is {len(text)} characters, over {WIDTH_LIMIT}")
    if not _is_ascii(text):
        _warn("the text has non-ASCII characters, which a console font may not draw")
    entry = f"{level} | {text}\n"
    if not path.exists():
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(FILE_HEADER.format(category=category) + entry)
    else:
        with open(path, "rb+") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() > 0:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    f.write(b"\n")
            f.write(entry.encode("utf-8"))
    return Line(category, level, text)


def check(lines_dir: Optional[PathLike] = None) -> List[Problem]:
    """Validate every lines file. Errors and warnings, in file order."""
    folder, _ = resolve_lines_dir(lines_dir)
    problems: List[Problem] = []
    seen: Dict[str, Tuple[str, str]] = {}  # casefolded text -> (category, where)
    files = sorted(p for p in folder.glob("*.txt") if p.is_file())
    if not files:
        problems.append(Problem("warning", str(folder), 0, "no categories (no .txt files)"))
    for path in files:
        category = path.stem
        if not CATEGORY_RE.match(category):
            problems.append(
                Problem("error", str(path), 0, "bad category name: use lowercase letters, digits, '-' and '_'")
            )
            continue
        entries, file_problems = _parse(path, category)
        problems += file_problems
        local: Dict[str, int] = {}
        for number, line in entries:
            def add(severity: str, message: str) -> None:
                problems.append(Problem(severity, str(path), number, message))

            column = _stray_brace(line.text)
            if column is not None:
                add("error", f"stray brace at column {column} (write {{{{ or }}}} for a literal brace)")
            key = line.text.casefold()
            if key in local:
                add("error", f"duplicate of line {local[key]}")
            else:
                local[key] = number
                if key in seen and seen[key][0] != category:
                    add("warning", f"also in {seen[key][1]}")
                else:
                    seen.setdefault(key, (category, f"{path.name}:{number}"))
            if len(line.text) > WIDTH_LIMIT:
                add("warning", f"{len(line.text)} characters, over {WIDTH_LIMIT}")
            if not _is_ascii(line.text):
                add("warning", "non-ASCII characters (a console font may not draw them)")
    return problems


def _all_lines(lines_dir: Optional[PathLike]) -> List[Tuple[str, List[Line]]]:
    folder, _ = resolve_lines_dir(lines_dir)
    return [(name, load(name, folder)) for name in categories(folder)]


def export(format: str = "markdown", lines_dir: Optional[PathLike] = None) -> str:
    """Every line, as markdown tables (one per category) or as JSON."""
    everything = _all_lines(lines_dir)
    if format == "json":
        data = {
            name: [{"level": line.level, "text": line.text} for line in lines]
            for name, lines in everything
        }
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if format != "markdown":
        raise QuipUsageError(f"unknown export format {format!r}: use markdown or json")
    blocks = []
    for name, lines in everything:
        rows = [f"### {name}", "", "| Level | Line | Needs |", "|---|---|---|"]
        for line in lines:
            text = line.text.replace("|", "\\|")
            rows.append(f"| {line.level} | {text} | {', '.join(_placeholders(line.text))} |")
        blocks.append("\n".join(rows))
    return "\n\n".join(blocks) + "\n"


# --------------------------------------------------------------------------
# Install, update and auto-update (the only code that touches the network)

DEFAULT_UPDATE_BASE = "https://github.com/danny-reynolds/quip"
ALLOWED_HOSTS = ("github.com", "raw.githubusercontent.com", "codeload.github.com")
DOWNLOAD_LIMIT = 20 * 1024 * 1024
UPDATE_LOG_LINES = 50
CRON_TAG = "# quip-autoupdate"
UNIT_NAME = "quip-update"


def _require_posix(what: str) -> None:
    if os.name == "nt":
        raise QuipUsageError(f"quip {what} isn't supported on Windows yet; run python quip.py from a checkout")


def _quip_home() -> Path:
    env = os.environ.get("QUIP_HOME")
    if env:
        return Path(env)
    data = _data_home()
    if data is None:
        raise QuipInstallError("no home folder; set QUIP_HOME")
    return data / "quip"


def _quip_bin() -> Path:
    env = os.environ.get("QUIP_BIN")
    if env:
        return Path(env)
    home = _home()
    if home is None:
        raise QuipInstallError("no home folder; set QUIP_BIN")
    return home / ".local" / "bin"


def _parse_version(text: str) -> Tuple[int, ...]:
    if not re.match(r"[0-9]+(\.[0-9]+)*\Z", text):
        raise QuipInstallError(f"bad version {text!r}")
    return tuple(int(part) for part in text.split("."))


def _source_version(src: Path) -> str:
    """Check a source folder is a complete quip and return its version."""
    for name in ("quip.py", "VERSION"):
        if not (src / name).is_file():
            raise QuipInstallError(f"{src} has no {name}")
    if not (src / "lines").is_dir():
        raise QuipInstallError(f"{src} has no lines folder")
    version = (src / "VERSION").read_text(encoding="utf-8").strip()
    _parse_version(version)
    m = re.search(r'^__version__ = "([^"]+)"', (src / "quip.py").read_text(encoding="utf-8"), re.M)
    if not m or m.group(1) != version:
        found = m.group(1) if m else "nothing"
        raise QuipInstallError(f"VERSION says {version} but quip.py says {found}")
    return version


def _symlink_atomic(target: Union[str, Path], link: Path) -> None:
    tmp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(str(target), str(tmp))
    os.replace(str(tmp), str(link))


def _self_check(home: Path, version: str) -> None:
    import subprocess

    env = dict(os.environ)
    env.pop("QUIP_DIR", None)
    script = str(home / "quip.py")
    for args in (["--version"], ["check"]):
        try:
            result = subprocess.run(
                [sys.executable, script] + args,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise QuipInstallError(f"self-check couldn't run: {e}") from None
        output = result.stdout.strip()
        if result.returncode != 0:
            first = next((ln for ln in output.splitlines() if ln.startswith("error")), output[:200])
            raise QuipInstallError(f"self-check 'quip {' '.join(args)}' failed: {first}")
        if args == ["--version"] and output != f"quip {version}":
            raise QuipInstallError(f"self-check: expected 'quip {version}', got {output!r}")


def _install(src: Path, auto_update: Optional[bool], quiet: bool = False) -> str:
    """Copy a source folder into QUIP_HOME, link it into QUIP_BIN and self-check.

    auto_update True sets up the daily update, False leaves it alone and
    so does None (which is what an update uses).
    """
    import shutil
    import tempfile

    _require_posix("install")
    src = src.resolve()
    version = _source_version(src)
    home, bindir = _quip_home(), _quip_bin()
    link = bindir / "quip"
    if link.exists() and not link.is_symlink():
        raise QuipInstallError(f"{link} exists and isn't a symlink; move it out of the way first")
    prev_link = os.readlink(str(link)) if link.is_symlink() else None

    try:
        home.parent.mkdir(parents=True, exist_ok=True)
        new = Path(tempfile.mkdtemp(prefix=".quip-new-", dir=str(home.parent)))
    except OSError as e:
        raise QuipInstallError(f"can't create {home.parent}: {e.strerror}") from None
    try:
        shutil.copy2(str(src / "quip.py"), str(new / "quip.py"))
        shutil.copy2(str(src / "VERSION"), str(new / "VERSION"))
        shutil.copytree(str(src / "lines"), str(new / "lines"))
        if (home / "lines").is_dir():
            shutil.copytree(str(home / "lines"), str(new / "lines.bak"))
        os.chmod(str(new / "quip.py"), 0o755)
    except OSError as e:
        shutil.rmtree(str(new), ignore_errors=True)
        raise QuipInstallError(f"copy failed: {e}") from None

    old = home.parent / f".quip-old-{os.getpid()}"
    moved_old = swapped = False
    try:
        if home.exists():
            if old.exists():
                shutil.rmtree(str(old))
            os.rename(str(home), str(old))
            moved_old = True
        os.rename(str(new), str(home))
        swapped = True
        bindir.mkdir(parents=True, exist_ok=True)
        _symlink_atomic(home / "quip.py", link)
        _self_check(home, version)
    except (OSError, QuipInstallError) as e:
        if swapped:
            shutil.rmtree(str(home), ignore_errors=True)
        else:
            shutil.rmtree(str(new), ignore_errors=True)
        if moved_old:
            os.rename(str(old), str(home))
        try:
            if prev_link is not None:
                _symlink_atomic(prev_link, link)
            elif link.is_symlink():
                link.unlink()
        except OSError:
            pass
        reason = e if isinstance(e, QuipInstallError) else f"{type(e).__name__}: {e}"
        raise QuipInstallError(f"{reason} (rolled back, nothing changed)") from None
    if moved_old:
        shutil.rmtree(str(old), ignore_errors=True)

    if not quiet:
        print(f"Installed quip {version}")
        print(f"  code and lines: {home}")
        print(f"  command:        {link}")
    if auto_update:
        message = _schedule_on(link)
        if not quiet:
            print(message)
    if not quiet:
        _path_advice(bindir, link)
    return version


def _on_path(folder: Path) -> bool:
    target = os.path.realpath(str(folder))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and os.path.realpath(os.path.expanduser(entry)) == target:
            return True
    return False


def _path_advice(bindir: Path, link: Path) -> None:
    if not _on_path(bindir):
        home = _home()
        shown = str(bindir)
        if home is not None and bindir == home / ".local" / "bin":
            shown = "$HOME/.local/bin"
            print(
                "\n~/.local/bin isn't on your PATH yet. Debian's ~/.profile adds it at login\n"
                "once the folder exists, so log in again, or run:\n\n    . ~/.profile\n"
            )
        print(f"If 'quip' still isn't found, add this to ~/.profile:\n\n    export PATH=\"{shown}:$PATH\"\n")
    print(f"Boot scripts, services and cron don't see your PATH: call {link} by full path there.")


def uninstall() -> None:
    import shutil

    _require_posix("uninstall")
    home, link = _quip_home(), _quip_bin() / "quip"
    if link.is_symlink():
        if os.path.realpath(str(link)) == os.path.realpath(str(home / "quip.py")):
            link.unlink()
            print(f"Removed {link}")
        else:
            print(f"Left {link} alone: it doesn't point into {home}", file=sys.stderr)
    _schedule_off()
    if home.exists():
        shutil.rmtree(str(home))
        print(f"Removed {home}")
    print("The no-repeat state file was left in place.")


# ---- Downloads


def _update_base() -> str:
    return os.environ.get("QUIP_UPDATE_BASE", DEFAULT_UPDATE_BASE).rstrip("/")


def _check_url(url: str) -> None:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    custom = os.environ.get("QUIP_UPDATE_BASE")
    if custom:
        base = urlsplit(custom)
        if (parts.scheme, parts.netloc) == (base.scheme, base.netloc):
            return
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise QuipInstallError(f"refusing to fetch from {parts.scheme}://{parts.netloc}")


class _NotFound(QuipInstallError):
    pass


def _fetch(url: str, limit: int) -> bytes:
    import urllib.error
    import urllib.request

    class Redirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
            _check_url(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    _check_url(url)
    opener = urllib.request.build_opener(Redirects)
    request = urllib.request.Request(url, headers={"User-Agent": f"quip/{__version__}"})
    try:
        with opener.open(request, timeout=30) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _NotFound(f"not found: {url}") from None
        raise QuipInstallError(f"HTTP {e.code} from {url}") from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise QuipInstallError(f"can't reach {url}: {reason}") from None
    if len(data) > limit:
        raise QuipInstallError(f"{url} is bigger than expected")
    return data


def _extract(tarball: Path, dest: Path) -> Path:
    """Unpack a release tarball safely and return the folder holding quip.py."""
    import tarfile

    try:
        with tarfile.open(str(tarball), "r:gz") as tf:
            members = []
            for member in tf.getmembers():
                parts = Path(member.name).parts
                if member.name.startswith("/") or ".." in parts:
                    raise QuipInstallError(f"unsafe path in tarball: {member.name}")
                if member.isfile() or member.isdir():
                    members.append(member)
            if hasattr(tarfile, "data_filter"):
                tf.extractall(str(dest), members=members, filter="data")
            else:
                tf.extractall(str(dest), members=members)
    except (tarfile.TarError, EOFError, OSError) as e:
        raise QuipInstallError(f"bad tarball: {e}") from None
    if (dest / "quip.py").is_file():
        return dest
    found = [p.parent for p in dest.glob("*/quip.py")]
    if len(found) != 1:
        raise QuipInstallError("tarball doesn't hold exactly one quip.py")
    return found[0]


def _tarball_urls(base: str, ref: str) -> List[str]:
    if re.match(r"[0-9]+(\.[0-9]+)*\Z", ref):
        ref = "v" + ref
    return [f"{base}/archive/refs/tags/{ref}.tar.gz", f"{base}/archive/refs/heads/{ref}.tar.gz"]


def _installed_version(home: Path) -> str:
    if not (home / "quip.py").is_file():
        raise QuipInstallError(f"quip isn't installed in {home}; run the installer first")
    try:
        return (home / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0"


def update(check_only: bool = False, quiet: bool = False, ref: Optional[str] = None) -> int:
    """Install the latest release if it's newer. Returns the exit code."""
    import tempfile

    _require_posix("update")
    home = _quip_home()
    installed = _installed_version(home)
    base = _update_base()
    if ref is None:
        latest = _fetch(f"{base}/raw/main/VERSION", 64).decode("utf-8", "replace").strip()
        newer = _parse_version(latest) > _parse_version(installed)
        if check_only:
            note = " (update available)" if newer else ""
            print(f"installed {installed}, latest {latest}{note}")
            return 0
        if not newer:
            if not quiet:
                print(f"quip {installed} is up to date")
            return 0
        urls = [f"{base}/archive/refs/tags/v{latest}.tar.gz"]
    else:
        if check_only:
            raise QuipUsageError("--check can't be combined with --ref")
        urls = _tarball_urls(base, ref)

    data = None
    for url in urls:
        try:
            data = _fetch(url, DOWNLOAD_LIMIT)
            break
        except _NotFound:
            if url == urls[-1]:
                raise
    assert data is not None
    with tempfile.TemporaryDirectory(prefix="quip-update-") as tmp:
        tarball = Path(tmp) / "quip.tar.gz"
        tarball.write_bytes(data)
        src = _extract(tarball, Path(tmp) / "src")
        version = _install(src, auto_update=None, quiet=True)
    if not quiet:
        print(f"updated quip {installed} -> {version}")
    _log_update(f"updated {installed} -> {version}")
    return 0


def _log_update(message: str) -> None:
    import time

    cache = _cache_home()
    if cache is None:
        return
    path = cache / "quip" / "update.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        lines.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}")
        path.write_text("\n".join(lines[-UPDATE_LOG_LINES:]) + "\n", encoding="utf-8")
    except OSError:
        pass


# ---- The daily schedule


def _run(args: List[str], stdin: Optional[str] = None) -> Optional["subprocess.CompletedProcess[str]"]:
    import subprocess

    try:
        return subprocess.run(
            args,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None


# QUIP_SYSTEMCTL, QUIP_CRONTAB and QUIP_LOGINCTL let the tests swap in fakes,
# so they never touch the real user manager or crontab.
def _systemctl() -> List[str]:
    return [os.environ.get("QUIP_SYSTEMCTL", "systemctl"), "--user"]


def _crontab() -> str:
    return os.environ.get("QUIP_CRONTAB", "crontab")


def _systemd_ok() -> bool:
    result = _run(_systemctl() + ["show-environment"])
    return result is not None and result.returncode == 0


def _unit_dir() -> Path:
    config = _xdg("XDG_CONFIG_HOME", ".config")
    if config is None:
        raise QuipInstallError("no home folder for systemd user units")
    return config / "systemd" / "user"


def _unit_files(link: Path) -> Dict[str, str]:
    exec_path = str(link).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    service = (
        "[Unit]\n"
        "Description=Check for a new quip release\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f'ExecStart="{exec_path}" update --quiet\n'
    )
    timer = (
        "[Unit]\n"
        "Description=Daily check for a new quip release\n"
        "\n"
        "[Timer]\n"
        "OnStartupSec=10min\n"
        "OnCalendar=daily\n"
        "Persistent=true\n"
        "RandomizedDelaySec=1h\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return {f"{UNIT_NAME}.service": service, f"{UNIT_NAME}.timer": timer}


def _cron_lines(link: Path, rng: Optional[random.Random] = None) -> List[str]:
    import shlex

    rng = rng or random.Random()
    command = shlex.quote(str(link)).replace("%", "\\%") + " update --quiet"
    return [
        f"{rng.randrange(60)} {rng.randrange(10, 21)} * * * {command} {CRON_TAG}",
        f"@reboot sleep 600 && {command} {CRON_TAG}",
    ]


def _read_crontab() -> Optional[List[str]]:
    """The current crontab lines, [] if there is none, None if cron isn't usable."""
    import shutil

    if shutil.which(_crontab()) is None:
        return None
    result = _run([_crontab(), "-l"])
    if result is None:
        return None
    if result.returncode != 0:
        return [] if "no crontab" in result.stderr.lower() else None
    return result.stdout.splitlines()


def _write_crontab(lines: List[str]) -> bool:
    result = _run([_crontab(), "-"], stdin="".join(line + "\n" for line in lines))
    return result is not None and result.returncode == 0


def _linger_hint() -> Optional[str]:
    import getpass

    try:
        user = getpass.getuser()
    except Exception:
        return None
    loginctl = os.environ.get("QUIP_LOGINCTL", "loginctl")
    result = _run([loginctl, "show-user", user, "--property=Linger", "--value"])
    if result is not None and result.returncode == 0 and result.stdout.strip() == "no":
        return (
            "Timers only run while you're logged in. On a headless machine, run:\n\n"
            "    sudo loginctl enable-linger $USER\n"
        )
    return None


def _schedule_on(link: Path) -> str:
    if _systemd_ok():
        folder = _unit_dir()
        folder.mkdir(parents=True, exist_ok=True)
        for name, text in _unit_files(link).items():
            (folder / name).write_text(text, encoding="utf-8")
        _run(_systemctl() + ["daemon-reload"])
        result = _run(_systemctl() + ["enable", "--now", f"{UNIT_NAME}.timer"])
        if result is not None and result.returncode == 0:
            message = "Auto-update: on (systemd user timer, daily)."
            hint = _linger_hint()
            return message + ("\n" + hint if hint else "")
    crontab = _read_crontab()
    if crontab is not None:
        kept = [line for line in crontab if CRON_TAG not in line]
        if _write_crontab(kept + _cron_lines(link)):
            return "Auto-update: on (cron, daily and 10 minutes after boot)."
    return (
        "Auto-update: couldn't set up a timer or cron job. To check daily by hand, add\n"
        f"this to your crontab (crontab -e):\n\n    {_cron_lines(link)[0]}\n"
    )


def _schedule_off() -> None:
    systemd = _systemd_ok()
    if systemd:
        _run(_systemctl() + ["disable", "--now", f"{UNIT_NAME}.timer"])
    try:
        folder = _unit_dir()
    except QuipInstallError:
        folder = None
    if folder is not None:
        for name in _unit_files(Path("quip")):
            try:
                (folder / name).unlink()
            except OSError:
                pass
    if systemd:
        _run(_systemctl() + ["daemon-reload"])
    crontab = _read_crontab()
    if crontab and any(CRON_TAG in line for line in crontab):
        _write_crontab([line for line in crontab if CRON_TAG not in line])


def _schedule_status() -> str:
    try:
        units = (_unit_dir() / f"{UNIT_NAME}.timer").is_file()
    except QuipInstallError:
        units = False
    if units and _systemd_ok():
        result = _run(_systemctl() + ["is-enabled", f"{UNIT_NAME}.timer"])
        if result is not None and result.stdout.strip() == "enabled":
            return "on (systemd user timer)"
    crontab = _read_crontab()
    if crontab and any(CRON_TAG in line for line in crontab):
        return "on (cron)"
    return "off"


def autoupdate(action: str) -> str:
    _require_posix("autoupdate")
    if action == "status":
        status = _schedule_status()
        log = (_cache_home() or Path(".")) / "quip" / "update.log"
        try:
            last = log.read_text(encoding="utf-8").splitlines()[-1]
            status += f"\nlast update log entry: {last}"
        except (OSError, IndexError):
            pass
        return status
    if action == "off":
        _schedule_off()
        return "Auto-update: off."
    link = _quip_bin() / "quip"
    if not link.is_symlink():
        raise QuipInstallError(f"{link} not found; install quip first")
    return _schedule_on(link)


# --------------------------------------------------------------------------
# Command line


def _parse_categories(args: List[str]) -> CategorySpec:
    names, weights = [], []
    for arg in args:
        name, sep, weight = arg.partition(":")
        names.append(name)
        weights.append(weight if sep else None)
    if all(w is None for w in weights):
        return names
    if any(w is None for w in weights):
        raise QuipUsageError("give a weight to every category or to none of them")
    for name in names:
        if names.count(name) > 1:
            raise QuipUsageError(f"category {name!r} is named twice")
    return dict(zip(names, weights))


def _parse_sets(pairs: Optional[List[str]]) -> Dict[str, str]:
    values = {}
    for pair in pairs or []:
        name, sep, value = pair.partition("=")
        if not sep:
            raise QuipUsageError(f"--set needs NAME=VALUE, not {pair!r}")
        values[name] = value
    return values


def _seed(text: Optional[str]) -> random.Random:
    if text is None:
        return random.Random()
    try:
        return random.Random(int(text))
    except ValueError:
        return random.Random(text)


def _cmd_get(args: argparse.Namespace) -> int:
    lines = _select(
        _parse_categories(args.categories),
        args.count,
        level=args.level,
        min_level=args.min_level,
        max_level=args.max_level,
        width=args.width,
        values=_parse_sets(args.set),
        no_repeat=args.no_repeat,
        rng=_seed(args.seed),
        lines_dir=args.dir,
        state_path=args.state,
    )
    if args.json:
        data = [{"category": l.category, "level": l.level, "text": l.text} for l in lines]
        sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
        return 0
    for line in lines:
        prefix = ""
        if args.show_category:
            prefix += f"{line.category}\t"
        if args.show_level:
            prefix += f"{line.level}\t"
        sys.stdout.write(prefix + line.text + "\n")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    names = categories(args.dir)
    if args.names:
        for name in names:
            print(name)
        return 0
    rows = []
    for name in names:
        lines = load(name, args.dir)
        levels = " ".join(f"{n}:{sum(1 for l in lines if l.level == n)}" for n in range(1, 6))
        rows.append((name, len(lines), levels))
    name_width = max((len(r[0]) for r in rows), default=0)
    count_width = max((len(str(r[1])) for r in rows), default=0)
    for name, count, levels in rows:
        print(f"{name:<{name_width}}  {count:>{count_width}}  ({levels})")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    line = add_line(args.category, args.level, " ".join(args.text), args.dir)
    print(f"added to {line.category}: {line.level} | {line.text}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    problems = check(args.dir)
    for problem in problems:
        print(problem)
    errors = sum(1 for p in problems if p.severity == "error")
    warnings_ = len(problems) - errors
    folder, _ = resolve_lines_dir(args.dir)
    print(f"{errors} errors, {warnings_} warnings in {folder}", file=sys.stderr)
    return 3 if errors else 0


def _cmd_reset(args: argparse.Namespace) -> int:
    path, reason = resolve_state_path(args.state)
    if path is None:
        print(f"quip: nothing to reset ({reason})", file=sys.stderr)
        return 0
    reset(args.category, args.state)
    which = args.category or "every category"
    print(f"cleared no-repeat history for {which}", file=sys.stderr)
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    sys.stdout.write(export(args.format, args.dir))
    return 0


def _cmd_where(args: argparse.Namespace) -> int:
    code = 0
    try:
        folder, rule = resolve_lines_dir(args.dir)
        print(f"lines  {folder}  ({rule})")
    except QuipError as e:
        print(f"lines  (none found)  {e}")
        code = 3
    path, rule = resolve_state_path(args.state)
    print(f"state  {path if path is not None else '(none)'}  ({rule})")
    return code


def _cmd_install(args: argparse.Namespace) -> int:
    src = Path(args.from_dir) if args.from_dir else Path(__file__).resolve().parent
    _install(src, auto_update=not args.no_auto_update)
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    uninstall()
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    try:
        return update(args.check, args.quiet, args.ref)
    except QuipInstallError as e:
        _log_update(f"failed: {e}")
        raise
    except OSError as e:
        _log_update(f"failed: {e}")
        raise QuipInstallError(str(e)) from None


def _cmd_autoupdate(args: argparse.Namespace) -> int:
    print(autoupdate(args.action))
    return 0


def _parser() -> argparse.ArgumentParser:
    # --dir and --state work before or after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default=argparse.SUPPRESS, help="lines folder")
    common.add_argument("--state", default=argparse.SUPPRESS, help="no-repeat state file")

    parser = argparse.ArgumentParser(
        prog="quip",
        description="A random line from a bank of quips, status messages and boot text.",
    )
    parser.add_argument("--version", action="version", version=f"quip {__version__}")
    parser.add_argument("--dir", default=None, help="lines folder")
    parser.add_argument("--state", default=None, help="no-repeat state file")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("get", parents=[common], help="print a random line")
    p.add_argument("categories", nargs="+", metavar="CATEGORY[:WEIGHT]")
    p.add_argument("--level", type=int, help="exact silliness level, 1 to 5")
    p.add_argument("--min", dest="min_level", type=int, help="lowest level")
    p.add_argument("--max", dest="max_level", type=int, help="highest level (3 is work-safe)")
    p.add_argument("--width", type=int, help="longest line allowed, after filling placeholders")
    p.add_argument("--set", action="append", metavar="NAME=VALUE", help="fill a placeholder")
    p.add_argument("--no-repeat", action="store_true", help="don't repeat until the rest are used")
    p.add_argument("--count", type=int, default=1, help="how many distinct lines")
    p.add_argument("--seed", help="seed the random choice")
    p.add_argument("--show-level", action="store_true", help="prefix the level and a tab")
    p.add_argument("--show-category", action="store_true", help="prefix the category and a tab")
    p.add_argument("--json", action="store_true", help="print a JSON array instead")
    p.set_defaults(func=_cmd_get)

    p = sub.add_parser("list", parents=[common], help="list categories with line counts")
    p.add_argument("--names", action="store_true", help="names only, one per line")
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("add", parents=[common], help="add a line to a category")
    p.add_argument("category")
    p.add_argument("level", type=int)
    p.add_argument("text", nargs="+")
    p.set_defaults(func=_cmd_add)

    p = sub.add_parser("check", parents=[common], help="validate every lines file")
    p.set_defaults(func=_cmd_check)

    p = sub.add_parser("reset", parents=[common], help="clear no-repeat history")
    p.add_argument("category", nargs="?")
    p.set_defaults(func=_cmd_reset)

    p = sub.add_parser("export", parents=[common], help="print every line")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.set_defaults(func=_cmd_export)

    p = sub.add_parser("where", parents=[common], help="show the lines folder and state file in use")
    p.set_defaults(func=_cmd_where)

    p = sub.add_parser("install", parents=[common], help="install or reinstall this copy")
    p.add_argument("--from", dest="from_dir", metavar="DIR", help="folder with quip.py, lines/ and VERSION")
    p.add_argument("--no-auto-update", action="store_true", help="don't set up the daily update")
    p.set_defaults(func=_cmd_install)

    p = sub.add_parser("uninstall", parents=[common], help="remove the installed copy")
    p.set_defaults(func=_cmd_uninstall)

    p = sub.add_parser("update", parents=[common], help="install the latest release if it's newer")
    p.add_argument("--check", action="store_true", help="only report, change nothing")
    p.add_argument("--quiet", action="store_true", help="print nothing on success")
    p.add_argument("--ref", help="install this tag or branch instead (may be older)")
    p.set_defaults(func=_cmd_update)

    p = sub.add_parser("autoupdate", parents=[common], help="turn the daily update on or off")
    p.add_argument("action", choices=["on", "off", "status"])
    p.set_defaults(func=_cmd_autoupdate)
    return parser


def _show_warning(message, category, filename, lineno, file=None, line=None):  # type: ignore[no-untyped-def]
    print(f"quip: warning: {message}", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    """Run the command line. Returns the exit code."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    with warnings.catch_warnings():
        warnings.simplefilter("always", QuipWarning)
        warnings.showwarning = _show_warning
        try:
            return args.func(args)
        except NoLinesError as e:
            print(f"quip: {e}", file=sys.stderr)
            return 1
        except (QuipUsageError, UnknownCategoryError) as e:
            print(f"quip: error: {e}", file=sys.stderr)
            return 2
        except QuipDataError as e:
            print(f"quip: error: {e}", file=sys.stderr)
            return 3
        except QuipInstallError as e:
            if not getattr(args, "quiet", False):
                print(f"quip: error: {e}", file=sys.stderr)
            return 4
        except BrokenPipeError:
            try:
                sys.stdout = open(os.devnull, "w")
            except OSError:
                pass
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
