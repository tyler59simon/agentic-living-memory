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

- Release hardening from the 2026-11-27 audit: hook guards and archive safety (REL-S1 to REL-S6), store size and hot-path speed (REL-P1 to REL-P7), CLI and multi-harness usability (REL-U1 to REL-U8), and dead code removal and module splits (REL-D1 to REL-D4).
- `install.py` writes the shell profile through a temp file and a rename, and writes a symlinked profile at its target.
- Docs: the CLI examples that failed now run (update with `--expected-revision`, supersede by flags, merge with `@REVISION`); the docs name the digest refresh as an opt-in model call, and correct the facet, harness, dashboard and guarded-folder counts and the retrieval cost; install commands use `py -3` on Windows and `python3` on macOS and Linux instead of `py -3.15`; `install.py --wire-all` and `alm upgrade --rewire` are documented.
- Docs: `SCRIPT_REFERENCE.md`, `PROTOCOL.md` and `ARCHITECTURE.md` are split into headed sections and short paragraphs, with the wording unchanged. The retired notebook design, the NS-2A and NS-2B review records and the raw sqlite-vec evaluation results moved to `docs/archive/`.
