# quip

A bank of lines (quips, status messages, error jokes, boot text) sorted into
categories, each with a silliness level from 1 to 5. Ask for a line and you
get a random one, filtered by category and silliness if you like.

```console
$ quip get boot --max 3
Persuading the hardware that it has definitely done this before.
$ quip get technical:40 workshop:20 selfaware:20 concerning:10 goblin:10 --count 3
Hammering the bugs flat...
Rehydrating cached nonsense...
Simulating effort convincingly...
```

One Python file, standard library only, Python 3.8 or later, no network at
call time. It works as a command and as a library.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/danny-reynolds/quip/main/install.sh | sh
```

or, with `wget`:

```sh
wget -qO- https://raw.githubusercontent.com/danny-reynolds/quip/main/install.sh | sh
```

To read the script first:

```sh
curl -fsSL https://raw.githubusercontent.com/danny-reynolds/quip/main/install.sh -o install.sh && less install.sh && sh install.sh
```

It needs `python3` (3.8+), `tar`, and `curl` or `wget`. No root and no GitHub
account. It puts the code and lines in `~/.local/share/quip` and a `quip`
command in `~/.local/bin`. If that folder wasn't on your `PATH` yet, log in
again (or run `. ~/.profile`).

Options go after `sh -s --`:

```sh
curl -fsSL .../install.sh | sh -s -- --no-auto-update   # no daily update
curl -fsSL .../install.sh | sh -s -- --uninstall        # remove it again
```

| Variable | Effect |
|---|---|
| `QUIP_REF` | Install a particular tag or branch (`v1.0.0`, `main`) |
| `QUIP_TARBALL` | Install from a local tarball, no network |
| `QUIP_HOME` | Where the code and lines go (default `~/.local/share/quip`) |
| `QUIP_BIN` | Where the `quip` command goes (default `~/.local/bin`) |

Boot scripts, systemd services and cron don't see your `PATH`, so call it by
full path there, for example `/home/you/.local/bin/quip`.

### Updates

Installed copies check once a day for a new release and update themselves,
through a systemd user timer (or cron if there's no systemd). They follow
tagged releases, not every commit, and never check at call time, so
`quip get` stays instant and offline. If a new release fails its self-check,
the old one is put back.

```sh
quip update            # update now, if there's a newer release
quip update --check    # just say whether there is one
quip autoupdate off    # stop the daily check (on, status)
```

On a headless machine, user timers only run while you're logged in, so run
`sudo loginctl enable-linger $USER` once. The installer tells you if that's
needed.

Failed updates are logged to `~/.cache/quip/update.log` (the last 50 lines).

### Other ways to run it

From a checkout: `ln -s "$PWD/quip.py" ~/.local/bin/quip`. It finds the
`lines/` folder next to the real `quip.py`.

With pip: `pip install --user .` installs the module, a `quip` command and the
lines (to `~/.local/share/quip/lines`).

On Windows, run `python quip.py ...` from a checkout. `install`, `update` and
`autoupdate` are POSIX only for now.

## Silliness levels

| Level | Meaning |
|---|---|
| 1 | Straight-faced. Reads like genuine output. |
| 2 | Dry. Plainly a joke, delivered deadpan. |
| 3 | Clearly a joke, still restrained. |
| 4 | Daft. Open absurdity. May be mildly rude. |
| 5 | Properly daft. Surreal, over the top, or sweary. |

**`--max 3` is the work-safe setting.** Levels 1 to 3 never swear.

## Commands

```
quip get <category[:weight]> ... [--level N | --min N --max N] [--width W]
         [--set NAME=VALUE ...] [--no-repeat] [--count N] [--seed S]
         [--show-level] [--show-category] [--json]
quip list [--names]
quip add <category> <level> <text...>
quip check
quip reset [category]
quip export [--format markdown|json]
quip where
quip install [--from DIR] [--no-auto-update]
quip uninstall
quip update [--check] [--quiet] [--ref REF]
quip autoupdate on|off|status
quip --version
```

Every command also takes `--dir PATH` (lines folder) and `--state PATH`
(no-repeat state file).

- **`get`** prints one line, or `--count N` distinct lines. Lines with
  placeholders such as `{hostname}` are only used when you fill them with
  `--set hostname=...`. `--width` counts the text after filling them in.
- **Mixing:** `quip get boot:70 status:30 --count 10` gives exactly 7 and 3,
  shuffled together. Weights are proportions (`boot:3 status:1` works too),
  and with no weights every category gets an equal share. With a single line,
  the weights are the odds.
- **`--no-repeat`** doesn't repeat a line until every other line that matches
  the same filters has been used. It keeps a small state file
  (`~/.cache/quip/state.json`). If it can't write that, you still get a line,
  with a warning.
- **`check`** validates every file. Run it after editing.
- **`where`** says which lines folder and state file are in use, and why.

Exit codes: 0 fine, 1 nothing matched, 2 usage error, 3 data error, 4 install
or update failed (nothing changed). Only the lines go to stdout.

## Library

```python
from quip import pick, pick_many

pick("boot", max_level=3, width=76)
pick("status", values={"hostname": "forge", "count": "12"})
pick_many({"boot": 70, "status": 30}, 10)
pick("error", max_level=3, no_repeat=True)
```

Also `pick_many_lines`, `load`, `categories`, `add_line`, `check`, `export`
and `reset`. Errors subclass `QuipError`. `NoLinesError` and
`UnknownCategoryError` are also `LookupError`s.

## The lines

One file per category in `lines/`, one entry per line:

```
# comments start with #
2 | Loading the important bits first, as is customary.
3 | {hostname} is doing nothing, and doing it with real commitment.
```

Write `{{` and `}}` for literal braces. Everything is plain text, so a shell
can read it without Python:

```sh
cut -s -d'|' -f2- lines/boot.txt       # all the text
grep -E '^[1-3] \|' lines/boot.txt     # work-safe only
```

Every line is original. Nothing is quoted from games, films, TV, books or
songs.
