"""The note body layout shared by canonical files and their Obsidian copies.

A note file is frontmatter, a `# 🎯 Title` heading derived from the title property, then sections:

    ## 📌 Executive Summary      always present
    ---
    ## 📝 Key Details            optional
    ## 🗂️ Resource Links         optional
    ## 🟩 Action Items           optional; plan, bug and risk notes only
    > ℹ️ **Important Note:**     optional closing callout

Empty sections are left out. Text inside a section is kept as written; only the headings, their order
and the spacing between them are normalized, so `format_body` is idempotent. A body with no section
headings (plain text, or a note written before this layout) becomes the Executive Summary.
"""
from __future__ import annotations
import re

TITLE_MARK = '# 🎯 '
SECTIONS = (
    ('summary', '📌 Executive Summary'),
    ('details', '📝 Key Details'),
    ('links', '🗂️ Resource Links'),
    ('actions', '🟩 Action Items'),
)
HEADINGS = dict(SECTIONS)
CALLOUT = '> ℹ️ **Important Note:**'
ORDER = [key for key, _ in SECTIONS] + ['important']
# Open steps are the point of these types; elsewhere they are a moment in time, not lasting knowledge.
ACTION_TYPES = frozenset({'plan', 'bug', 'risk'})

_H2 = re.compile(r'^##\s+(.*?)\s*$')
_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})')
_CALLOUT = re.compile(r'^>\s*(?:ℹ️|ℹ|\[!\w+\])?\s*\**\s*Important Note:?\s*\**:?\s*(.*)$', re.I)
# Markup that carries no content: bullets, checkboxes, a bare "**Label:**", quote markers. A bold span counts
# only as a label (it ends in a colon): "**Never deploy on Fridays**" is content written in bold.
_EMPTY_LINE = re.compile(r'^\s*(?:>\s*)?(?:[-*+]\s*)?(?:\[[ xX]\]\s*)?(?:\*\*[^*]*:\*\*:?|\*\*[^*]*\*\*:|\*\*\s*\*\*|\*[^*]*:\*)?\s*$')


def _key(heading):
    words = re.sub(r'[^a-z ]', '', heading.casefold()).split()
    text = ' '.join(words)
    for key, label in SECTIONS:
        if text == ' '.join(re.sub(r'[^a-z ]', '', label.casefold()).split()):
            return key
    return None


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
        callout = _CALLOUT.match(line)
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


def _is_empty(text):
    return all(_EMPTY_LINE.match(line) for line in text.split('\n'))


def strip_title(body):
    """The body without the derived `# 🎯 Title` line."""
    text = (body or '').lstrip()
    if text.startswith(TITLE_MARK.rstrip()):
        text = text.split('\n', 1)[1] if '\n' in text else ''
    return text.strip()


def format_body(body):
    """Normalize headings, order of the summary and spacing; drop empty sections. Idempotent."""
    parts = split(body)
    has_summary = any(key == 'summary' for key, _, _ in parts)
    collected = []
    preamble = parts[0][2]
    for key, heading, text in parts[1:]:
        if key == 'summary' and preamble:
            text = (preamble + '\n\n' + text).strip()
            preamble = ''
        collected.append((key, heading, text))
    if not has_summary:
        if not preamble and collected and collected[0][0] is None:
            # A note whose first section is unnamed: that section is the summary.
            preamble = collected.pop(0)[2]
        collected.insert(0, ('summary', None, preamble))
    elif preamble:
        collected.insert(0, (None, None, preamble))
    # The summary comes first; everything else keeps its written order.
    collected.sort(key=lambda part: part[0] != 'summary')
    blocks = []
    for key, heading, text in collected:
        if _is_empty(text):
            continue
        if key == 'important':
            blocks.append(CALLOUT + '\n' + '\n'.join(('> ' + line).rstrip() for line in text.split('\n')))
        elif key in HEADINGS:
            blocks.append('## ' + HEADINGS[key] + '\n' + text)
        elif heading:
            blocks.append('## ' + heading + '\n' + text)
        else:
            blocks.append(text)
        if key == 'summary':
            blocks.append('---')
    if blocks and blocks[-1] == '---':
        blocks.pop()
    return '\n\n'.join(blocks).strip()


def sections(body):
    """Section key -> text for the sections this layout defines."""
    found = {}
    for key, _, text in split(format_body(body)):
        if key in ORDER and not _is_empty(text):
            found[key] = text
    return found


def check(body, note_type):
    """Raise ValueError when a body breaks the layout rules for its type."""
    found = sections(body)
    if not found.get('summary', '').strip():
        raise ValueError('Note needs an Executive Summary: start the body with a short summary, or pass --summary')
    if 'actions' in found and note_type not in ACTION_TYPES:
        raise ValueError('Action Items are only for plan, bug and risk notes; record lasting facts in Key Details instead')


def _bullets(items, mark='* '):
    lines = []
    for item in items:
        item = str(item).strip()
        if not item:
            continue
        lines.append(item if re.match(r'^(?:[-*+]\s|\d+\.\s)', item) else mark + item)
    return '\n'.join(lines)


def render(summary='', details=(), links=(), actions=(), done=(), important='', body=''):
    """A body from structured parts. Lists become bullets; text that is already a bullet is kept.
    `body`, when given, supplies sections that the structured parts do not replace."""
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
    return _with_sections(replace, body)


def _with_sections(replace, body=''):
    """`body` with the sections in `replace` (key -> text) put in place of its own, or added where the layout puts them."""
    replace = dict(replace)
    # Sections of the existing body (including headings this layout does not define) keep their place.
    parts = [[key, heading, text] for key, heading, text in split(format_body(body))[1:]] if body else []
    for part in parts:
        if part[0] in replace:
            part[2] = replace.pop(part[0])
    for key in [k for k in ORDER if k in replace]:
        rank = ORDER.index(key)
        # A new section goes after the last section that comes before it in the layout.
        at = max([i + 1 for i, part in enumerate(parts) if part[0] in ORDER and ORDER.index(part[0]) < rank] or [0])
        if key == 'important':
            at = len(parts)
        parts.insert(at, [key, None, replace[key]])
    out = []
    for key, heading, text in parts:
        if key == 'important':
            out.append(CALLOUT + '\n' + '\n'.join(f'> {line}'.rstrip() for line in text.split('\n')))
        elif key in HEADINGS:
            out.append(f'## {HEADINGS[key]}\n{text}')
        elif heading:
            out.append(f'## {heading}\n{text}')
        else:
            out.append(text)
    return format_body('\n\n'.join(out))


_LABEL = re.compile(r'^\s*(?:[-*+]\s+)?\*\*([^*]+?)\*\*')


def detail_label(line):
    """The bold label a Key Details bullet starts with ("* **Cause:** ..." -> "cause"), else None."""
    match = _LABEL.match(line)
    label = match[1].strip().rstrip(':').strip().casefold() if match else ''
    return label or None


def amend(body, details=(), links=(), actions=(), set_details=()):
    """The body with bullets added rather than sections replaced: `details` and `links` are appended to Key
    Details and Resource Links, `actions` become new open Action Items, and each of `set_details` replaces the
    Key Details bullet that starts with the same bold label (or is appended when no bullet has it)."""
    current = sections(body)
    replace = {}
    lines = current.get('details', '').split('\n') if current.get('details') else []
    for item in set_details:
        label = detail_label(str(item))
        if not label:
            raise ValueError("--set-detail needs a bullet that starts with a bold label, e.g. '**Cause:** ...'")
        hits = [i for i, line in enumerate(lines) if line[:1] not in (' ', '\t') and detail_label(line) == label]
        if len(hits) > 1:
            raise ValueError(f'ambiguous_detail: {len(hits)} Key Details bullets start with **{label}**; rewrite the section with --detail')
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
    """(formatted body lines, indexes of the checkbox lines in its Action Items section)."""
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
    """[(done, text)] for each checkbox in Action Items, in written order."""
    lines, found = _action_lines(body)
    return [(_ACTION.match(lines[i])[2] != ' ', _ACTION.match(lines[i])[3].strip()) for i in found]


def set_action(body, index, done=True):
    """The body with one Action Item (0-based) ticked or unticked in place; nothing else moves."""
    lines, found = _action_lines(body)
    match = _ACTION.match(lines[found[index]])
    lines[found[index]] = f"{match[1]}[{'x' if done else ' '}]{match[3]}"
    return '\n'.join(lines)


def without_actions(body):
    """The body minus its Action Items section (open steps are expected there)."""
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
