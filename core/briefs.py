"""Project briefs: one `brief` note per bound project folder, the living summary of what the project is, who owns
what, where it stands and what was decided, which tickets and documents alone do not give the next session.

A brief is keyed like a map (core/maps.py): the first entry of its `files`, resolved against the domain's bound
folder ('.' is that folder itself). A session finds the brief whose folder holds its cwd, the deepest first."""
from __future__ import annotations
import sqlite3
from pathlib import Path
from .common import domain_prefixes,resolve_domain,memory_root
from .maps import _rows,map_domains,repo_of,repo_name

BRIEF_BUDGET=2500
LABELS='Goals and scope, Stakeholders and decision-makers, Team and ownership, Current phase, status and blockers, Key decisions, Active risks, Glossary'
MIN_NOTES=3

def find_brief(cwd,root=None,bindings=None):
    """The active brief of the project folder holding cwd (the deepest such folder), else None."""
    from .common import load_bindings
    root=Path(root or memory_root()).resolve();bindings=bindings if bindings is not None else load_bindings(root)
    try:here=Path(cwd).resolve()
    except (OSError,ValueError):return None
    best=None
    for domain in map_domains(here,bindings,root):
        for note in _rows(domain,root,'brief'):
            folder=repo_of(note,domain,bindings)
            if folder and here.is_relative_to(folder) and (best is None or len(folder.parts)>len(best[0].parts)):best=(folder,dict(note,domain=domain))
    return best[1] if best else None

def project_dir(cwd,bindings):
    """The folder a new brief for cwd is keyed by: the deepest folder bound to cwd's domain that holds it, else cwd."""
    here=Path(cwd).resolve();domain=resolve_domain(here,bindings)
    return next((p for p in domain_prefixes(domain,bindings) if here.is_relative_to(p)),here)

def project_key(folder,bindings):
    """What `--file` should say for a project folder: '.' for the domain's first bound folder (the one '.' means), else its path."""
    prefixes=domain_prefixes(resolve_domain(folder,bindings),bindings)
    return '.' if prefixes and prefixes[0]==folder else Path(folder).as_posix()

def active_count(domain,root,limit=MIN_NOTES):
    """Active notes in a domain, counted from its index (no rebuild: hooks must stay fast), capped at limit."""
    from .search import domain_dir
    try:
        db=sqlite3.connect((domain_dir(domain,root)/'search.sqlite3').as_uri()+'?mode=ro',uri=True)
        try:return min(limit,db.execute("SELECT COUNT(*) FROM notes WHERE status='active'").fetchone()[0])
        finally:db.close()
    except (ValueError,sqlite3.Error):return 0

def updated_day(hit):return str(hit.get('updated') or '')[:10]

def brief_block(hit,budget=BRIEF_BUDGET):
    """The brief's body for a prompt; bounded."""
    from .notes import short_ref
    from .session_start import _cap
    head=(f"Project brief — {short_ref(hit['domain'],hit['id'])} {hit['title']} (updated {updated_day(hit)}). "
          'Update it when goals, ownership, phase or key decisions change.')
    return head+'\n'+_cap(str(hit.get('body','')).strip(),max(400,budget-len(head)-1))

def pointer_line(hit):
    """One line for SessionStart: the brief exists, where, and how fresh."""
    from .notes import short_ref
    return f"Project brief: {short_ref(hit['domain'],hit['id'])} {hit['title']} (updated {updated_day(hit)})"
