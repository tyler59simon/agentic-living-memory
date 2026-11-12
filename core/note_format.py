"""The note body layout shared by canonical files and their Obsidian copies.

A note file is frontmatter, a `# Title` heading derived from the title property, then the body:

    The summary: one or two sentences, with no heading     always present
    > **Important:** a caveat the reader must see          optional
    ## <the type's sections, or sections named by content>  optional (TYPE_SECTIONS)
    ## Details                                             optional
    ## Links                                               optional
    ## Action items                                        optional; plan, bug and risk notes only

Empty sections are left out. Text inside a section is kept as written; only the headings, their order
and the spacing between them are normalized, so `format_body` is idempotent. A body with no section
headings (plain text) is all summary.

Notes written before this layout used `# 🎯 Title`, `## 📌 Executive Summary`, a `---` rule, `## 📝 Key
Details`, `## 🗂️ Resource Links`, `## 🟩 Action Items` and a closing `> ℹ️ **Important Note:**` callout.
Those still parse, and a note takes the current layout the next time it is written.
"""
from __future__ import annotations
import re

TITLE_MARK = '# '
LEGACY_TITLE_MARK = '# 🎯'
SECTIONS = (
    ('details', 'Details'),
    ('links', 'Links'),
    ('actions', 'Action items'),
)
HEADINGS = dict(SECTIONS)
# Headings that name a layout section, compared on their letters only. The second name of each is the
# heading notes used before the current layout.
ALIASES = {
    'summary': ('summary', 'executive summary'),
    'details': ('details', 'key details'),
    'links': ('links', 'resource links'),
    'actions': ('action items',),
}
LABELS = {'summary': 'Summary', 'important': 'Important', **HEADINGS}
CALLOUT = '> **Important:**'
ORDER = ['summary', 'important', 'details', 'links', 'actions']
# Open steps are the point of these types; elsewhere they are a moment in time, not lasting knowledge.
ACTION_TYPES = frozenset({'plan', 'bug', 'risk'})
# The sections a note of each type is written with, in order, between the summary and Details. Types
# not listed use Details; knowledge notes use sections named after their content.
TYPE_SECTIONS = {
    'decision': ('Why', 'Alternatives considered'),
    'bug': ('Cause', 'Fix', 'How to check'),
    'runbook': ('When to use', 'When not to use', 'Steps', 'Check', 'Roll back', 'If it fails'),
    'research': ('Findings', 'Sources', 'Open questions'),
}
# Where each kind of section sits: typed and custom sections (rank 2) come before Details.
_RANK = {'summary': 0, 'important': 1, None: 2, 'details': 3, 'links': 4, 'actions': 5}

_H2 = re.compile(r'^##\s+(.*?)\s*$')
_H1 = re.compile(r'^#(?!#)\s')
_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})')
# The callout: "> **Important:** text", and the earlier "> ℹ️ **Important Note:**". Bold or a colon is
# required, so an ordinary quote that starts with the word "Important" stays a quote.
_CALLOUT = re.compile(r'^>\s*(?:ℹ️|ℹ|\[!\w+\])?\s*(?:\*\*\s*Important(?: Note)?\s*:?\s*\*\*\s*:?|Important(?: Note)?\s*:)\s*(.*)$', re.I)
# Markup that carries no content: bullets, checkboxes, a bare "**Label:**", quote markers. A bold span counts
# only as a label (it ends in a colon): "**Never deploy on Fridays**" is content written in bold.
_EMPTY_LINE = re.compile(r'^\s*(?:>\s*)?(?:[-*+]\s*)?(?:\[[ xX]\]\s*)?(?:\*\*[^*]*:\*\*:?|\*\*[^*]*\*\*:|\*\*\s*\*\*|\*[^*]*:\*)?\s*$')


def _letters(text):
    return ' '.join(re.sub(r'[^a-z ]', '', text.casefold()).split())


def _key(heading):
    text = _letters(heading)
    for key, names in ALIASES.items():
        if text in names:
            return key
    return None


def _same_heading(a, b):
    """Headings compared without case, punctuation or extra spaces ("Roll back" == "roll back:")."""
    def norm(text):
        return ' '.join(re.sub(r'[^\w ]', ' ', str(text).casefold()).split())
    return norm(a) == norm(b)


def _trim(lines):
    """Drop blank lines and decorative `---` rules at the edges of a section."""
    while lines and (not lines[0].strip() or lines[0].strip() == '---'):
        lines = lines[1:]
    while lines and (not lines[-1].strip() or lines[-1].strip() == '---'):
        lines = lines[:-1]
    return lines


def split(body):
    """Ordered (key, heading, text) sections. Key is None for a heading this layout does not define.
    Text before the first heading is returned under key 'preamble'. Lines inside a fenced code block
    are text, so an example heading in a fence is kept where it was written."""
    body = strip_title(body or '')
    sections = [['preamble', None, []]]
    fence = None
    for line in body.replace('\r\n', '\n').split('\n'):
        mark = _FENCE.match(line)
        if fence:
            sections[-1][2].append(line)
            if mark and mark[1][0] == fence[0] and len(mark[1]) >= len(fence) and not line.strip().strip(fence[0]):
                fence = None
            continue
        if mark:
            fence = mark[1]
        heading = None if mark else _H2.match(line)
        callout = None if mark else _CALLOUT.match(line)
        if heading:
            sections.append([_key(heading[1]), heading[1], []])
        elif callout and line.lstrip().startswith('>'):
            sections.append(['important', None, [callout[1]] if callout[1].strip() else []])
        elif sections[-1][0] == 'important' and line.lstrip().startswith('>'):
            sections[-1][2].append(re.sub(r'^\s*>\s?', '', line))
        elif sections[-1][0] == 'important' and not line.strip():
            sections[-1][2].append('')
        else:
            if sections[-1][0] == 'important':
                # Text after the callout ends it; keep it rather than lose it.
                sections.append([None, None, []])
            sections[-1][2].append(line)
    return [(key, heading, '\n'.join(_trim(lines))) for key, heading, lines in sections]


def parts(body):
    """split() of the formatted body with the leading text labelled 'summary': the sections as stored."""
    found = split(format_body(body))
    return [('summary', None, found[0][2])] + found[1:]


_LEGACY_SUMMARY = re.compile(r'(?im)^##[^\n]*Executive Summary[^\n]*\n(.*?)(?=\n---|\n## |\Z)', re.S)
_SUMMARY_END = re.compile(r'\n(?:##\s|>\s*(?:ℹ️|ℹ)?\s*\**\s*Important)', re.I)


def summary_text(body):
    """The summary of a stored body without a full parse, for checks that run over every note: the text
    before the first heading or callout, or a legacy Executive Summary section."""
    body = strip_title(body or '')
    legacy = _LEGACY_SUMMARY.search(body)
    if legacy:
        return legacy[1].strip()
    return _SUMMARY_END.split('\n' + body.replace('\r\n', '\n'), 1)[0].strip()


def _is_empty(text):
    return all(_EMPTY_LINE.match(line) for line in text.split('\n'))


def strip_title(body, heading=False):
    """The body without its title heading. A legacy `# 🎯 Title` line is always removed. With `heading`
    (a note file being read, whose first line is the title by construction), any leading `# ` line is."""
    text = (body or '').lstrip()
    first = text.split('\n', 1)[0]
    if first.startswith(LEGACY_TITLE_MARK) or (heading and _H1.match(first + ' ')):
        text = text.split('\n', 1)[1] if '\n' in text else ''
    return text.strip()


def _callout(text):
    lines = text.split('\n')
    return '\n'.join([f'{CALLOUT} {lines[0]}'.rstrip()] + [f'> {line}'.rstrip() for line in lines[1:]])


def _block(key, heading, text):
    if key == 'summary':
        return text
    if key == 'important':
        return _callout(text)
    if key in HEADINGS:
        return f'## {HEADINGS[key]}\n{text}'
    if heading:
        return f'## {heading}\n{text}'
    return text


def format_body(body):
    """Normalize headings, the order of the summary and callout, and spacing; drop empty sections. Idempotent."""
    found = split(body)
    has_summary = any(key == 'summary' for key, _, _ in found)
    collected = []
    preamble = found[0][2]
    for key, heading, text in found[1:]:
        if key == 'summary' and preamble:
            text = (preamble + '\n\n' + text).strip()
            preamble = ''
        collected.append((key, heading, text))
    if not has_summary:
        # Text with no heading of its own, which can only follow a callout ("> **Important:** ...\n\nText"),
        # is the summary. A named section is not: a note that starts with "## Why" has no summary yet, and keeps its heading.
        loose = next((i for i, part in enumerate(collected) if part[0] is None and not part[1]), None)
        if not preamble and loose is not None and all(part[0] == 'important' for part in collected[:loose]):
            preamble = collected.pop(loose)[2]
        collected.insert(0, ('summary', None, preamble))
    elif preamble:
        collected.insert(0, (None, None, preamble))
    # The summary comes first and the callout right after it; everything else keeps its written order.
    collected.sort(key=lambda part: {'summary': 0, 'important': 1}.get(part[0], 2))
    blocks = [_block(key, heading, text) for key, heading, text in collected if not _is_empty(text)]
    return '\n\n'.join(blocks).strip()


def sections(body):
    """Section key -> text for the sections this layout defines."""
    found = {}
    for key, _, text in parts(body):
        if key in ORDER and not _is_empty(text):
            found[key] = text
    return found


def named_sections(body):
    """[(heading, text)] for the sections the layout does not define: the type's sections and any others."""
    return [(heading, text) for key, heading, text in parts(body) if key is None and heading and not _is_empty(text)]


def check(body, note_type):
    """Raise ValueError when a body breaks the layout rules for its type."""
    found = sections(body)
    if not found.get('summary', '').strip():
        raise ValueError('Note needs a summary: start the body with one or two sentences, or pass --summary')
    if 'actions' in found and note_type not in ACTION_TYPES:
        raise ValueError('Action items are only for plan, bug and risk notes; record lasting facts in the body instead')


def _bullets(items, mark='- '):
    lines = []
    for item in items:
        item = str(item).strip()
        if not item:
            continue
        lines.append(item if re.match(r'^(?:[-*+]\s|\d+\.\s)', item) else mark + item)
    return '\n'.join(lines)


def render(summary='', details=(), links=(), actions=(), done=(), important='', body='', named=(), note_type=None):
    """A body from structured parts. Lists become bullets; text that is already a bullet is kept.
    `named` is [(heading, text)] for the type's sections or others. `body`, when given, supplies sections
    that the structured parts do not replace."""
    replace = {}
    if summary:
        replace['summary'] = str(summary).strip()
    if details:
        replace['details'] = _bullets(details)
    if links:
        replace['links'] = _bullets(links)
    if actions or done:
        replace['actions'] = '\n'.join([f'- [ ] {a.strip()}' for a in actions if a.strip()] +
                                       [f'- [x] {d.strip()}' for d in done if d.strip()])
    if important:
        replace['important'] = str(important).strip()
    out = _with_sections(replace, body)
    for heading, text in named:
        out = set_section(out, heading, text, note_type)
    return out


def _insert_at(found, rank):
    """Index before the first section that the layout puts after `rank`, else the end."""
    return next((i for i, part in enumerate(found) if _RANK.get(part[0], 2) > rank), len(found))


def _join(found):
    return format_body('\n\n'.join(_block(key, heading, text) for key, heading, text in found if text.strip() or key == 'summary'))


def _with_sections(replace, body=''):
    """`body` with the sections in `replace` (key -> text) put in place of its own, or added where the layout puts them."""
    replace = dict(replace)
    # Sections of the existing body (including headings this layout does not define) keep their place.
    found = [list(part) for part in parts(body)] if body else [['summary', None, '']]
    for part in found:
        if part[0] in replace:
            part[2] = replace.pop(part[0])
    for key in [k for k in ORDER if k in replace]:
        found.insert(_insert_at(found, _RANK[key]), [key, None, replace[key]])
    return _join(found)


def set_section(body, heading, text, note_type=None):
    """`body` with the section named `heading` holding `text`. A heading that names a layout section
    (Summary, Details, Links, Action items, Important) replaces that section. Another heading replaces the
    section with that heading, or is added: a section of the note type's own goes in the type's order,
    any other one after the last such section, and both before Details, Links and Action items."""
    text = str(text or '').strip()
    if not text:
        raise ValueError(f'--section {heading!r} needs text; use --drop-section to remove a section')
    key = 'important' if _letters(heading) in ('important', 'important note') else _key(heading)
    if key:
        return _with_sections({key: text}, body)
    found = [list(part) for part in parts(body)] if body else [['summary', None, '']]
    for part in found:
        if part[0] is None and part[1] and _same_heading(part[1], heading):
            part[2] = text
            return _join(found)
    order = [h for h in TYPE_SECTIONS.get(note_type, ())]
    position = next((i for i, h in enumerate(order) if _same_heading(h, heading)), None)
    at = _insert_at(found, 2)
    if position is not None:
        # Before the first of the type's own sections that comes later in its order.
        later = [i for i, part in enumerate(found) if part[0] is None and part[1]
                 and any(_same_heading(part[1], h) for h in order[position + 1:])]
        if later:
            at = min(later)
    found.insert(at, [None, str(heading).strip(), text])
    return _join(found)


def drop_section(body, heading):
    """`body` without the section named `heading` (the summary cannot be dropped)."""
    key = 'important' if _letters(heading) in ('important', 'important note') else _key(heading)
    if key == 'summary':
        raise ValueError('The summary cannot be dropped; replace it with --summary')
    found = [list(part) for part in parts(body)]
    kept = [part for part in found if not ((key and part[0] == key) or
                                           (not key and part[0] is None and part[1] and _same_heading(part[1], heading)))]
    if len(kept) == len(found):
        raise ValueError(f'No section named {heading!r} to drop')
    return _join(kept)


_LABEL = re.compile(r'^\s*(?:[-*+]\s+)?\*\*([^*]+?)\*\*')


def detail_label(line):
    """The bold label a Details bullet starts with ("- **Cause:** ..." -> "cause"), else None."""
    match = _LABEL.match(line)
    label = match[1].strip().rstrip(':').strip().casefold() if match else ''
    return label or None


def amend(body, details=(), links=(), actions=(), set_details=()):
    """The body with bullets added rather than sections replaced: `details` and `links` are appended to
    Details and Links, `actions` become new open Action items, and each of `set_details` replaces the
    Details bullet that starts with the same bold label (or is appended when no bullet has it)."""
    current = sections(body)
    replace = {}
    lines = current.get('details', '').split('\n') if current.get('details') else []
    for item in set_details:
        label = detail_label(str(item))
        if not label:
            raise ValueError("--set-detail needs a bullet that starts with a bold label, e.g. '**Cause:** ...'")
        hits = [i for i, line in enumerate(lines) if line[:1] not in (' ', '\t') and detail_label(line) == label]
        if len(hits) > 1:
            raise ValueError(f'ambiguous_detail: {len(hits)} Details bullets start with **{label}**; rewrite the section with --detail')
        new = _bullets([item])
        if hits:
            # A bullet's indented continuation lines belong to it and go with it.
            end = hits[0] + 1
            while end < len(lines) and lines[end][:1] in (' ', '\t'):
                end += 1
            lines[hits[0]:end] = [new]
        else:
            lines.append(new)
    if details:
        lines.append(_bullets(details))
    if set_details or details:
        replace['details'] = '\n'.join(line for line in lines if line.strip())
    if links:
        replace['links'] = '\n'.join(text for text in (current.get('links', ''), _bullets(links)) if text.strip())
    added = [f'- [ ] {a.strip()}' for a in actions if str(a).strip()]
    if added:
        replace['actions'] = '\n'.join(text for text in (current.get('actions', ''), '\n'.join(added)) if text.strip())
    return _with_sections(replace, body) if replace else format_body(body)


def section_counts(body):
    """Lines of content in each list section (details, links, actions), for reporting what an update replaced."""
    found = sections(body)
    return {key: sum(1 for line in found.get(key, '').split('\n') if line.strip()) for key in ('details', 'links', 'actions')}


_ACTION = re.compile(r'^(\s*[-*+]\s*)\[([ xX])\](.*)$')


def _action_lines(body):
    """(formatted body lines, indexes of the checkbox lines in its Action items section)."""
    lines = format_body(body).split('\n')
    found, inside = [], False
    for i, line in enumerate(lines):
        heading = _H2.match(line)
        if heading:
            inside = _key(heading[1]) == 'actions'
        elif line.lstrip().startswith('>') and _CALLOUT.match(line):
            inside = False
        elif inside and _ACTION.match(line):
            found.append(i)
    return lines, found


def action_items(body):
    """[(done, text)] for each checkbox in Action items, in written order."""
    lines, found = _action_lines(body)
    return [(_ACTION.match(lines[i])[2] != ' ', _ACTION.match(lines[i])[3].strip()) for i in found]


def set_action(body, index, done=True):
    """The body with one Action item (0-based) ticked or unticked in place; nothing else moves."""
    lines, found = _action_lines(body)
    match = _ACTION.match(lines[found[index]])
    lines[found[index]] = f"{match[1]}[{'x' if done else ' '}]{match[3]}"
    return '\n'.join(lines)


def without_actions(body):
    """The body minus its Action items section (open steps are expected there)."""
    return '\n\n'.join(text for key, _, text in split(format_body(body)) if key != 'actions')


def plain(body):
    """Readable text for snippets: headings, rules, bullets, checkboxes and emphasis removed."""
    lines = []
    for line in strip_title(body).split('\n'):
        stripped = line.strip()
        if not stripped or stripped == '---' or _H2.match(stripped):
            continue
        if _CALLOUT.match(stripped):
            stripped = _CALLOUT.match(stripped)[1]
        stripped = re.sub(r'^>\s?', '', stripped)
        stripped = re.sub(r'^(?:[-*+]\s+)?(?:\[[ xX]\]\s+)?', '', stripped)
        stripped = re.sub(r'\*\*([^*]+)\*\*', r'\1', stripped)
        stripped = re.sub(r'(?<![\w*])\*([^*\n]+)\*(?!\w)', r'\1', stripped)
        if stripped:
            lines.append(stripped)
    return ' '.join(lines)
