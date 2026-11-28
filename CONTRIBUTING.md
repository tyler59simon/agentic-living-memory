# Contributing

## Set up

Python 3.10 to 3.15 with SQLite FTS5. There are no runtime dependencies. `sqlite-vec` is optional (`pip install sqlite-vec==0.1.9`).

Run from the checkout with `py -3 living_memory.py ...` (Windows) or `python3 living_memory.py ...`. `install.py` installs the copy that harness hooks use; see [INSTALLATION.md](INSTALLATION.md). `pip install -e .` puts an `alm` command on PATH that runs this checkout, which is handy for development but does not wire any hooks.

## Tests

The suite uses the standard library's `unittest`. Run it from the repository root:

```
python -m unittest discover -s tests
```

One file: `python -m unittest discover -s tests -p "test_delivery.py"`. Tests use temporary stores and never read your configured store. [VALIDATION.md](VALIDATION.md) covers the benchmarks, the retrieval evaluation and the functionality simulator (`scripts/simulate.py`), which drives scripted sessions through the real hook adapters.

## Writing

Docs, comments, commit messages and notes follow [WRITING.md](WRITING.md): plain verbs, no filler or significance claims, exact commands and paths.

## Pull request checklist

- The full test suite passes on your platform.
- New behavior has a test; a bug fix has a test that failed before the fix.
- The JSON output of `alm version` keeps its fields; Hivemind reads them.
- No new runtime dependency.
- Docs that describe the changed behavior are updated, and `CHANGELOG.md` has an entry under the unreleased version.

By contributing, you agree that your contribution is licensed under the Apache License 2.0 (see [LICENSE](LICENSE)).
