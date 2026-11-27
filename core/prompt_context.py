"""Prompt-time memory retrieval, query segmentation, domain scoping, and hit delivery (REL-D3)."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from .common import canonical_domain, load_bindings, resolve_domain
from .signals import MACHINE_PROMPT

HIT_SNIPPET = 220

# A task brief is mostly boilerplate: past this many characters a prompt is searched in pieces.
LONG_PROMPT = 600
LONG_SEGMENTS = 4
LONG_LEAST = 3
HEADING = re.compile(r'^\s{0,3}#{1,6}\s')

# A numbered, lettered, bulleted or question-per-line list: each item is searched on its own.
LIST_ITEM = re.compile(r'^\s*(?:\d+[.)]|[qQ]\d+[.:)]|[-*])\s+\S')
LIST_MARK = re.compile(r'^\s*(?:\d+[.)]|[qQ]\d+[.:)]|[-*])\s+')
LIST_MIN = 3
LIST_QUERIES = 8
LIST_HITS = 2
ITEM_LEAST = 2
LIST_CAP = 6
LIST_SNIPPET = 160
LIST_BUDGET = 2400

MAX_QUESTIONS = 10
DELIVERY = True
SIBLING_DOMAINS = 4
SIBLING_LEAST_TERMS = 2


def _rt(name, default):
    """Look up a test-patchable runtime attribute on core.runtime when loaded, else return default."""
    rt = sys.modules.get('core.runtime')
    return getattr(rt, name, default) if rt is not None else default


def hit_marks(hit):
    """Where a hit came from or stands: `[from DOMAIN] ` for a sibling project's portable note, `[resolved] ` for a finished bug or risk."""
    return (f"[from {hit['domain']}] " if hit.get('sibling') else '') + ('[resolved] ' if hit.get('status') == 'resolved' else '')


def hit_line(hit, width=HIT_SNIPPET):
    snippet = hit.get('snippet', '')
    if len(snippet) > width:
        cut = snippet.rfind(' ', 0, width)
        snippet = snippet[:cut if cut > 0 else width] + ' …'
    from .notes import short_ref
    stale = f"{hit['stale']} — " if hit.get('stale') else ''
    mark = hit_marks(hit) + (f"UNCONFIRMED (as of {hit.get('updated', '')}): " if hit.get('interim') else '')
    return f"- [{str(hit.get('type', 'note')).upper()} {hit.get('updated', '')}] {short_ref(hit['domain'], hit['id'])} {mark}{hit['title']} — {stale}{snippet}"


def revision_key(note):
    return str(note.get('revision') or '')[:12]


def _snippeted(state):
    """ref -> revision key of the notes SessionStart showed with a snippet."""
    found = state.get('shown_snippet')
    return found if isinstance(found, dict) else {}


def _unseen(hit, shown):
    """True unless this revision of the note was already delivered."""
    ref = f"{hit['domain']}:{hit['id']}"
    if ref not in shown:
        return True
    seen = shown.get(ref) if isinstance(shown, dict) else None
    return bool(seen) and seen != revision_key(hit)


def relevance_gate(*args, **kwargs):
    from .search import relevance_gate as _relevance_gate
    return _relevance_gate(*args, **kwargs)


def _is_item(line):
    return not HEADING.match(line) and bool(LIST_ITEM.match(line) or line.rstrip().endswith('?'))


def _pieces(text):
    """[(segment, is_item)]: split at markdown headings and blank lines; list items get segments of their own."""
    lines = text.splitlines()
    listy = sum(map(_is_item, lines)) >= LIST_MIN
    segments = [[]]
    flags = [False]
    for line in lines:
        item = listy and _is_item(line)
        if not line.strip():
            segments.append([])
            flags.append(False)
        elif segments[-1] and (item or HEADING.match(line)):
            segments.append([line])
            flags.append(item)
        else:
            if not segments[-1]:
                flags[-1] = item
            segments[-1].append(line)
    return [('\n'.join(s), f) for s, f in zip(segments, flags) if s]


def prompt_segments(text):
    """The prompt's paragraphs, or its list items (see _pieces)."""
    return [s for s, _ in _pieces(text)]


def list_items(text):
    """The items of a list prompt (at least LIST_MIN of them), else []."""
    return [s for s, item in _pieces(text) if item]


def questions(text):
    """The separate questions in text, markers removed."""
    found = [LIST_MARK.sub('', s).strip() for s in list_items(text)]
    if not found:
        found = [s.strip() for s in re.findall(r'[^?]*\?', text) if len(s.strip()) > 1]
    return found if len(found) >= 2 else []


def item_queries(prompt, vocab):
    """Queries for up to LIST_QUERIES list items, the richest by summed IDF first."""
    from .search import idf_sum, idf_terms
    queries = []
    for item in sorted(_rt('list_items', list_items)(prompt), key=lambda s: -idf_sum(s, vocab))[:LIST_QUERIES]:
        query = ' '.join(idf_terms(item, vocab))
        if query and query not in queries:
            queries.append(query)
    return queries


def long_queries(prompt, vocab):
    """[(query, body)] for a long prompt: the last segment and three richest others by summed IDF, plus whole-prompt terms."""
    from .search import idf_sum, idf_terms
    segments = [s for s, item in _pieces(prompt) if not item]
    segments = segments if len(segments) > 1 else []
    rest = sorted(segments[:-1], key=lambda s: -idf_sum(s, vocab))[:LONG_SEGMENTS - 1]
    queries = {}
    for text, body in [(s, True) for s in ([segments[-1]] + rest if segments else [])] + [(prompt, False)]:
        query = ' '.join(idf_terms(text, vocab))
        if query and query not in queries:
            queries[query] = body
    return list(queries.items())


def context(cwd, prompt, root, active=None, shown=(), overview_done=False, delivered=None, trace=None, session_delivered=None, topic=None, units_out=None, room=None):
    """Relevant memory for a prompt: (text or None, ids injected, overview shown)."""
    from . import injection
    from .search import query_terms, recent_notes, search_domains, stem
    delivery_on = _rt('DELIVERY', DELIVERY)
    gate_fn = _rt('relevance_gate', relevance_gate)
    list_budget = _rt('LIST_BUDGET', LIST_BUDGET)
    trace = trace if isinstance(trace, dict) else {}
    bindings = load_bindings(root)
    active = active or resolve_domain(cwd, bindings)
    if not active or MACHINE_PROMPT.match(prompt or ''):
        trace['path'] = 'machine_prompt' if active else 'unbound'
        return None, [], False
    if delivery_on and injection.is_ack(prompt):
        trace['path'] = 'ack'
        return None, [], False
    query = {stem(t) for t in query_terms(prompt)}
    trace['terms'] = len(query)
    role = injection.follow_up(prompt, query) if delivery_on and topic else None
    topic_hits = []
    if role:
        from .search import note_hits
        topic_hits = note_hits(list(topic)[:injection.TOPIC_REFS], root, prompt)
        trace['follow_up'] = role
        own = _own_scope(active, bindings, root)
        for h in topic_hits:
            if h['domain'] not in own:
                h['sibling'] = True
    if not query and not topic_hits:
        trace['path'] = 'no_terms'
        return None, [], False
    if not query:
        return _deliver(prompt, root, topic_hits, [], shown, delivered, trace, session_delivered, units_out, role, False, set(), room)
    scope = bindings.get('search_scope', {})
    global_domains = [canonical_domain(d, root) for d in scope.get('global_domains', ['_global'])]
    global_domains = [d for d in global_domains if (root / 'domains' / d).exists()]
    parents = scope_domains(active, bindings)
    trace['candidates'] = []
    relevant = gate_fn(prompt, root, trace['candidates'])
    domains = list(dict.fromkeys([active] + parents + global_domains))
    weights = {**{g: 2 for g in global_domains}, active: 2, **{p: 1 for p in parents if p != active}}
    queries = None
    items = _rt('list_items', list_items)(prompt)
    if len(prompt) > LONG_PROMPT or items:
        from .search import terms, vocabulary
        vocab = vocabulary(domains, root, terms(prompt))
        if vocab[0]:
            queries = {q: (True, ITEM_LEAST, LIST_HITS) for q in item_queries(prompt, vocab)} if items else {}
            for q, body in long_queries(prompt, vocab):
                queries.setdefault(q, (body, LONG_LEAST, 10))
    if items:
        trace['items'] = len(items)
    if queries:
        trace['queries'] = len(queries)
        best = {}
        for text, (body, least, limit) in queries.items():
            for h in search_domains(text, domains, limit, root, weights=weights, keep=gate_fn(text, root, trace['candidates'], body, least), resolved=True, ensure_fresh=False):
                key = (h['domain'], h['id'])
                if key not in best or h['score'] > best[key]['score']:
                    best[key] = h
        found = sorted(best.values(), key=lambda h: (-h['score'], -h.get('relevance', 0) if items else 0, h['domain'], h['id']))
    else:
        found = search_domains(prompt, domains, 10, root, weights=weights, keep=relevant, resolved=True, ensure_fresh=False)
    if not found and scope.get('foreign_domains'):
        found = search_domains(prompt, scope['foreign_domains'], 10, root, keep=relevant, ensure_fresh=False)
    if bindings.get('cross_project', True) is not False:
        try:
            from .search import strong
            if not any(strong(h, root) for h in found):
                sibling = sibling_hit(prompt, root, bindings, _own_scope(active, bindings, root), not queries)
                if sibling:
                    found = found + [sibling]
                    trace['sibling'] = f"{sibling['domain']}:{sibling['id']}"
        except Exception as exc:
            from .logs import swallowed
            swallowed('runtime.context.sibling', exc, root)
    from .search import LAST_SEMANTIC
    trace['semantic'] = {d: LAST_SEMANTIC[d] for d in domains if d in LAST_SEMANTIC}
    if delivery_on:
        trace['topic'] = [f"{h['domain']}:{h['id']}" for h in found][:injection.TOPIC_REFS]
        if found or topic_hits:
            listed = {(h['domain'], h['id']) for h in topic_hits}
            return _deliver(prompt, root, topic_hits, [h for h in found if (h['domain'], h['id']) not in listed], shown, delivered, trace, session_delivered, units_out, role, bool(items), query, room)
    seen = set(shown)
    ref = lambda h: f"{h['domain']}:{h['id']}"
    unseen = [h for h in found if _unseen(h, shown)]
    hits = [h for h in unseen if not h.get('sibling')][:min(LIST_CAP, len(items)) if items else 3] + [h for h in unseen if h.get('sibling')]
    width = LIST_SNIPPET if items else HIT_SNIPPET
    if items:
        used = 0
        for i, h in enumerate(hits):
            used += len(hit_line(h, width)) + 1
            if used > list_budget and i:
                hits = hits[:i]
                break
    trace.update(injected=[ref(h) for h in hits], suppressed_shown=[ref(h) for h in found if not _unseen(h, shown)], cut_by_cap=[ref(h) for h in unseen if h not in hits])
    if hits:
        trace['path'] = 'injected'
        from . import digests
        digests.mark(hits, root, bindings)
        if isinstance(delivered, dict):
            delivered.update({f"{h['domain']}:{h['id']}": revision_key(h) for h in hits})
        return (
            'Untrusted historical memory; verify current instructions and files:\n' + '\n'.join(hit_line(h, width) for h in hits),
            [f"{h['domain']}:{h['id']}" for h in hits],
            False,
        )
    if found or overview_done:
        trace['path'] = 'all_shown' if found else 'overview_done'
        return None, [], False
    if len(query) < 2:
        trace['path'] = 'too_short'
        return None, [], False
    source, heading = (global_domains, 'Global titles') if global_domains else ([active] + parents, 'Recent titles in this domain')
    overview = []
    for domain in source:
        try:
            overview.extend(n for n in recent_notes(domain, 5, root) if f"{n['domain']}:{n['id']}" not in seen)
        except (OSError, ValueError):
            continue
    overview = overview[:5]
    from . import digests
    digests.mark(overview, root, bindings)
    if not overview:
        trace['path'] = 'no_overview'
        return None, [], False
    trace.update(path='overview', overview=len(overview))
    from .notes import short_ref
    return (f'No lexical match. {heading} for semantic consideration:\n' + '\n'.join(f"- {short_ref(n['domain'], n['id'])} {n['title']}" for n in overview), [], True)


def _own_scope(active, bindings, root):
    """The domains a prompt searches as its own."""
    scope = bindings.get('search_scope', {})
    return {
        active,
        *scope_domains(active, bindings),
        *(canonical_domain(d, root) for d in scope.get('global_domains', ['_global'])),
        *(canonical_domain(d, root) for d in scope.get('foreign_domains') or ()),
    }


def sibling_hit(prompt, root, bindings, skip, body=True):
    """The one strong, portable note another bound project holds for a prompt this project answers weakly, else None."""
    try:
        from .common import _entries
        from .search import _unchanged, relevance_gate as _gate, sibling_search, strong
        names = sorted(n for n, _, _ in _entries(bindings) if n not in skip and (root / 'domains' / n / 'notes').is_dir())[:SIBLING_DOMAINS]
        gate = _gate(prompt, root, body=body)
        best = None
        for name in names:
            try:
                hits = gate(sibling_search(prompt, name, root))
            except Exception as exc:
                from .logs import swallowed
                swallowed('runtime.sibling_hit', exc, root)
                continue
            for h in hits:
                h['score'] = 2 / (60 + h['rank'])
                if (
                    h.get('queryTerms', 0) >= SIBLING_LEAST_TERMS
                    and strong(h, root)
                    and (best is None or (h.get('relevance', 0), h['score']) > (best.get('relevance', 0), best['score']))
                    and _unchanged(h['path'], h['revision'])
                ):
                    best = h
        if best:
            best['sibling'] = True
        return best
    except Exception as exc:
        from .logs import swallowed
        swallowed('runtime.sibling_hit', exc, root)
        return None


def _deliver(prompt, root, topic_hits, found, shown, delivered, trace, session_delivered, units_out, role, listy, query, room=None):
    """context() with the delivery controller: per hit, the unit to deliver and its level."""
    from . import digests, injection
    from .semantic import threshold
    list_budget = _rt('LIST_BUDGET', LIST_BUDGET)
    near = threshold(root)
    again = injection.wants_again(prompt)
    ref = lambda h: f"{h['domain']}:{h['id']}"
    topical = {id(h) for h in topic_hits}
    chosen = []
    suppressed = []
    for h in topic_hits + found:
        mine = id(h) in topical
        unit = None if h.get('sibling') else injection.choose(h, query, near, role if mine else None, prompt)
        level = 'pointer' if h.get('sibling') else injection.level_of(unit, prompt)
        key = injection.unit_key(unit)
        if injection.novel(h, key, level, session_delivered or {}, shown, again or (mine and role == 'overview')):
            chosen.append((h, unit, level, key))
        else:
            suppressed.append(ref(h))
    picked = [c for c in chosen if not c[0].get('sibling')][:min(LIST_CAP, trace.get('items') or 0) if listy else 3] + [c for c in chosen if c[0].get('sibling')]
    width = LIST_SNIPPET if listy else HIT_SNIPPET
    if picked:
        digests.mark([h for h, _, _, _ in picked], root, load_bindings(root))
    lines = [injection.line(h, None if h.get('withheld') else u, 'pointer' if h.get('withheld') else level, width) for h, u, level, _ in picked]
    room = injection.PROMPT_CAP if room is None else room
    keep = injection.within(lines, injection.HEADING, min(list_budget, room) if listy else room)
    if keep < len(lines):
        trace['cut_by_budget'] = [ref(h) for h, _, _, _ in picked[keep:]]
        picked, lines = picked[:keep], lines[:keep]
    trace.update(
        injected=[ref(h) for h, _, _, _ in picked],
        suppressed_shown=suppressed,
        cut_by_cap=[ref(c[0]) for c in chosen if c not in picked],
        levels={ref(h): [level, u['role'] if u else 'overview'] for h, u, level, _ in picked},
    )
    if not picked:
        trace['path'] = 'all_shown'
        return None, [], False
    trace['path'] = 'injected'
    if isinstance(delivered, dict):
        delivered.update({ref(h): revision_key(h) for h, _, _, _ in picked})
    if isinstance(units_out, list):
        units_out.extend({'hit': h, 'unit': key, 'level': level} for h, _, level, key in picked)
    return (injection.HEADING + '\n' + '\n'.join(lines), [ref(h) for h, _, _, _ in picked], False)


def parent_domains(domain, bindings):
    """Domains bound to a folder that contains this domain's folder."""
    from .common import _entries
    mine = []
    others = {}
    for name, _, prefixes in _entries(bindings):
        paths = []
        for raw in prefixes:
            try:
                paths.append(Path(os.path.expandvars(str(raw))).expanduser().resolve())
            except (OSError, ValueError):
                continue
        if name == domain:
            mine = paths
        else:
            others[name] = paths
    found = []
    for name, paths in others.items():
        if any(child != parent and child.is_relative_to(parent) for child in mine for parent in paths):
            found.append(name)
    return sorted(found, key=lambda n: -max(len(str(p)) for p in others[n]))


def child_domains(domain, bindings):
    """Domains bound to a folder inside this domain's folder, nearest first."""
    from .common import _entries
    mine = []
    others = {}
    for name, _, prefixes in _entries(bindings):
        paths = []
        for raw in prefixes:
            try:
                paths.append(Path(os.path.expandvars(str(raw))).expanduser().resolve())
            except (OSError, ValueError):
                continue
        if name == domain:
            mine = paths
        else:
            others[name] = paths
    depth = {}
    for name, paths in others.items():
        found = [len(child.relative_to(parent).parts) for child in paths for parent in mine if child != parent and child.is_relative_to(parent)]
        if found:
            depth[name] = min(found)
    parents = set(parent_domains(domain, bindings))
    return sorted((n for n in depth if n not in parents), key=lambda n: (depth[n], n))


def scope_domains(domain, bindings):
    """Parent and child domains: the neighbours whose notes count at weight 1 beside the domain's own."""
    return parent_domains(domain, bindings) + child_domains(domain, bindings)


def child_path(shown, domain, child, bindings):
    """A path as shown for `domain`, rewritten relative to the folder of its child domain `child`, or None."""
    from .common import domain_prefixes
    try:
        mine = domain_prefixes(domain, bindings)
        theirs = domain_prefixes(child, bindings)
        raw = Path(shown)
        found = [raw] if raw.is_absolute() else [p / raw for p in mine]
        for p in found:
            p = p.resolve()
            for t in theirs:
                if p.is_relative_to(t):
                    return '' if p == t else p.relative_to(t).as_posix()
                if t.is_relative_to(p):
                    return ''
    except (OSError, ValueError):
        pass
    return None


def scoped_file_hits(paths, domain, bindings, root, per_file, resolved=False):
    """{path: notes about it} from the domain, its parents and its child domains."""
    from .search import files_notes
    paths = list(dict.fromkeys(paths))
    out = files_notes(paths, [domain] + parent_domains(domain, bindings), root, per_file, resolved)
    for child in child_domains(domain, bindings):
        mapped = {c: p for p in paths if (c := child_path(p, domain, child, bindings))}
        if not mapped:
            continue
        for c, hits in files_notes(list(mapped), [child], root, per_file, resolved).items():
            for h in hits:
                h['file'] = c
            out[mapped[c]] = out[mapped[c]] + hits
    for p, hits in out.items():
        out[p] = sorted(sorted(hits, key=lambda h: h.get('updated', ''), reverse=True), key=lambda h: (h.get('match') != 'files', h.get('status') == 'resolved'))[:per_file]
    return out
