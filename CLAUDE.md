# quip

A random-line tool: `quip.py` is the whole thing (library and CLI), and the
lines are in `lines/<category>.txt` as `level | text`.

## Rules

- **Standard library only.** No dependencies. Python 3.8 compatible: no
  `match`, no `X | Y` types at runtime, no `tomllib`, no 3.9+ helpers such as
  `str.removeprefix` or `Path.is_relative_to`. It must run on 32-bit ARMv7
  Debian, fully offline.
- **No network**, except in `install`, `update` and `autoupdate`. `get` and
  every other command must never touch it. Keep network modules imported
  lazily inside those functions.
- **Keep `quip.py` a single file.** Importing it must have no side effects.
- **Lines are original only.** No quotes or near-quotes from games, films, TV,
  books or songs, and no reworded catchphrases.
- **Voice:** dry, deadpan, British. Follow the silliness scale (README) and
  keep levels 1 to 3 work-safe: no swearing or rudeness below level 4.
  Plain ASCII, 78 characters or fewer, `...` for in-progress lines.
- **The repo is public.** Nothing committed (code, docs, examples, commit
  messages) may contain secrets, tokens, private hostnames, infrastructure
  details or links to the owner's private wiki. Commit as the GitHub noreply
  address, which is set in this clone's git config.
- Work on `main`. No pull requests, no other branches.

## Before committing

```sh
python3 -m unittest
python3 quip.py check
```

## After adding or changing lines

Regenerate the tables with `python3 quip.py export --format markdown` and
update the line tables on the quip spec page in the owner's wiki, through the
wiki MCP if it's connected. Otherwise, tell the owner it needs doing.

## Releasing

1. Bump `VERSION` and `__version__` in `quip.py` together (a test checks
   they match).
2. Commit, then `git tag vX.Y.Z`.
3. Push the commit and the tag: `git push origin main vX.Y.Z`.

Installed copies pick up the new version within a day. Until the tag is
pushed, they get a 404 and try again the next day.
