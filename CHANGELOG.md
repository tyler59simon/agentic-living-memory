# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [Semantic Versioning](https://semver.org/); the version lives in `core/__init__.py`.

## [1.0.0] - Unreleased

First versioned release. Earlier work was not versioned; `git log` has the full history.

### Added

- Canonical Markdown notes with revisions, a journaled and undoable write path, and SQLite FTS5 indexes rebuilt from the notes.
- One JSON CLI (`alm`) for capture, search, read, update, review and maintenance, with native hooks for Claude Code, Codex, Copilot, Factory and Antigravity CLI.
- Optional semantic search with a local model2vec model, and optional native vector search through `sqlite-vec`.
- Folder notebooks, project knowledge sources, Obsidian vault sync, views and backups.
- `alm --version` and `-V`; `alm version` now works before `alm setup`.
- `LICENSE` (Apache-2.0), `NOTICE`, `pyproject.toml`, `SECURITY.md` and `CONTRIBUTING.md`.
- `install.py` adds the PATH line for fish shells to `config.fish` with `fish_add_path`.

### Changed

- Release hardening: hook guards and archive safety, store size and hot-path speed, CLI and multi-harness usability, and dead code removal and module splits.
- `install.py` writes the shell profile through a temp file and a rename, and writes a symlinked profile at its target.
- The manual "Python versions" workflow installs `sqlite-vec` on every OS, allows 90 minutes per test job, and adds a job that installs the package on fresh Windows, Linux and macOS runners and drives the installed copy. `scripts/installed_smoke.py` runs its hooks with a temporary home.
- Tests no longer depend on the machine: they pass with no configured store and no `claude` on PATH, on case-sensitive file systems and on macOS paths.
- Docs: the CLI examples that failed now run (update with `--expected-revision`, supersede by flags, merge with `@REVISION`); the docs name the digest refresh as an opt-in model call, and correct the facet, harness, dashboard and guarded-folder counts and the retrieval cost; install commands use `py -3` on Windows and `python3` on macOS and Linux instead of `py -3.15`; `install.py --wire-all` and `alm upgrade --rewire` are documented.
- Docs: `SCRIPT_REFERENCE.md`, `PROTOCOL.md` and `ARCHITECTURE.md` are split into headed sections and short paragraphs, with the wording unchanged. Internal design, review and evaluation records are no longer in the repository, and `VALIDATION.md` keeps how to run each check but not the measured results.

### Fixed

- `scripts/rehearse_store.py --root DIR` no longer needs a configured store.
