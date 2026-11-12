"""Advisory writing checks for note text (WRITING.md). Each returns a short message; the note is still saved.

The checks look for the habits that make a note read like chatbot output (after humanizer v4.0.0,
https://github.com/blader/humanizer, and Wikipedia's "Signs of AI writing"), and for notes whose shape
does not fit their type. Code, commands, paths, URLs and [[links]] are left out of the prose checks."""
from __future__ import annotations
import re

from .note_format import format_body, sections, named_sections

_FENCED = re.compile(r'(?ms)^ {0,3}(`{3,}|~{3,}).*?^ {0,3}\1[^\n]*$')
_SKIP = re.compile(r'`[^`\n]*`|\[\[[^\]\n]*\]\]|\]\([^)\n]*\)|https?://\S+|\b[A-Za-z]:[\\/]\S+|(?<!\w)[~.]?/[\w./~-]+')
_LEADING_LABEL = re.compile(r'^\s*(?:>\s*)?(?:[-*+]\s+|\d+\.\s+)?\*\*[^*\n]+\*\*:?')
_BULLET = re.compile(r'^\s*(?:[-*+]|\d+\.)\s+(?!\[[ xX]\])')

DASH = re.compile(r'[—–]| -- ')
CURLY = re.compile(r'[“”‘’]')
EMOJI = re.compile('[\U0001F300-\U0001FAFF☀-➿⭐⬆⤴⤵\U0001F000-\U0001F2FF]')
RESIDUE = re.compile(r"(?i)\b(?:i hope this helps|let me know|great question|you'?re absolutely right|would you like|certainly!|of course!)")
# Words models use far more often than people do. Words with common literal uses in engineering notes
# (key, robust, gate, landscape, highlight, enhance) are left to WRITING.md rather than flagged here.
AI_WORDS = re.compile(r'(?i)\b(?:delve[sd]?|delving|tapestry|testament|pivotal|crucial(?:ly)?|seamless(?:ly)?|showcas(?:e|es|ed|ing)|'
                      r'underscor(?:es|ed|ing)|vibrant|meticulous(?:ly)?|additionally|bolster(?:s|ed|ing)?|garner(?:s|ed|ing)?|'
                      r'intricate|intricacies|interplay|enduring)\b')
STAGING = re.compile(r"(?i)\b(?:serves as|stands as|plays an? (?:key|crucial|pivotal|vital|significant) role|"
                     r"it'?s (?:important|worth) (?:to note|noting)|it is (?:important|worth) (?:to note|noting)|"
                     r"in summary|in conclusion|at its core|let'?s dive|here'?s the thing|"
                     r"not (?:just|only|merely)\b[^.\n]{1,60}?\bbut\b)")
LONG = 60
# Map and brief notes are dense orientation notes with fixed labelled bullets.
DENSE_TYPES = ('map', 'brief')


def prose(text):
    """Text with fenced code, inline code, links, URLs and paths blanked out."""
    return _SKIP.sub(' ', _FENCED.sub(' ', text or ''))


def _quote(match, text):
    start, end = max(0, match.start() - 20), min(len(text), match.end() + 20)
    return ' '.join(text[start:end].split())


def _words(text):
    return len(prose(text).split())


def style_warnings(note):
    title = str(note.get('title') or '')
    # The layout's own markup (including an earlier layout's emoji headings) is not the writer's text.
    body = format_body(str(note.get('body') or ''))
    note_type = note.get('type')
    text = prose(title + '\n' + body)
    found = []
    for pattern, message in (
            (DASH, 'em or en dash ("{q}"); use a comma, colon, period or parentheses'),
            (CURLY, 'curly quotation marks ("{q}"); use straight quotes'),
            (EMOJI, 'emoji ("{q}"); notes carry no decoration'),
            (RESIDUE, 'chat phrasing ("{q}"); a note is not a reply'),
            (AI_WORDS, 'stock AI word ("{q}"); say the plain thing'),
            (STAGING, 'inflated or staged phrasing ("{q}"); state the fact directly (use is/has, drop significance claims)')):
        match = pattern.search(text)
        if match:
            found.append('style: ' + message.format(q=_quote(match, text)))
    for line in prose(body).split('\n'):
        rest = _LEADING_LABEL.sub('', line, count=1)
        if re.search(r'\*\*[^*\n]+\*\*', rest):
            found.append('style: bold used for emphasis ("' + ' '.join(line.split())[:60] + '"); keep bold for a leading label')
            break
    if note_type in DENSE_TYPES:
        return found
    found_sections = sections(body)
    summary = found_sections.get('summary', '')
    if _words(summary) > LONG:
        found.append(f'style: the summary has {_words(summary)} words; open with the fact or decision in one or two sentences and move the rest into sections')
    tail = summary.rstrip()
    if tail and not re.search(r'[.!?:;)\]`"\']$|\]\]$', tail) and not re.search(r'https?://\S+$', tail):
        found.append('style: the summary seems to stop mid-sentence ("...' + tail[-40:] + '"); check that the whole text was saved')
    for line in body.split('\n'):
        count = _words(_BULLET.sub('', line, count=1))
        if _BULLET.match(line) and count > LONG:
            found.append(f'style: a bullet has {count} words ("' + ' '.join(line.split())[:50] + '..."); one fact per bullet')
            break
    found += shape_warnings(note_type, body, found_sections)
    return found


def _has(body, found_sections, *names):
    """Whether the body has a section, or a Details label, with one of these names."""
    names = {n.casefold() for n in names}
    if any(' '.join(h.casefold().split()).rstrip(':') in names for h, _ in named_sections(body)):
        return True
    labels = re.findall(r'(?m)^\s*(?:[-*+]\s+)?\*\*([^*\n]+?):?\*\*', found_sections.get('details', ''))
    return any(label.strip().rstrip(':').casefold() in names for label in labels)


# A short note may give its reason, cause or fix in the summary itself.
_SAYS_WHY = re.compile(r'(?i)\b(?:because|since|so that|to avoid|in order to|why|rationale|reason)\b')
_SAYS_CAUSE = re.compile(r'(?i)\b(?:caus(?:e|es|ed)|because|fix(?:es|ed)?|workaround|hypothes[ie]s|root cause)\b')


def shape_warnings(note_type, body, found_sections=None):
    found_sections = found_sections if found_sections is not None else sections(body)
    text = prose(body)
    if note_type == 'decision' and not (_has(body, found_sections, 'why', 'rationale', 'reason') or _SAYS_WHY.search(text)):
        return ["shape: a decision note says why: add --section 'Why' '...'"]
    if note_type == 'bug' and not (_has(body, found_sections, 'cause', 'root cause', 'hypothesis', 'fix', 'workaround')
                                   or _SAYS_CAUSE.search(text)):
        return ["shape: a bug note has a Cause (or Hypothesis) and a Fix: add --section 'Cause' '...' and --section 'Fix' '...'"]
    if note_type == 'research' and not (_has(body, found_sections, 'sources', 'source') or found_sections.get('links')
                                        or re.search(r'https?://', body)):
        return ["shape: a research note cites its sources: add --section 'Sources' '...'"]
    if note_type == 'knowledge':
        details = [line for line in found_sections.get('details', '').split('\n') if _BULLET.match(line)]
        labelled = [line for line in details if _LEADING_LABEL.match(line)]
        if len(labelled) >= 2 and len(labelled) == len(details) and not named_sections(body):
            return ["shape: a knowledge note reads better as short sections named after what they explain "
                    "(--section 'Heading' 'Text') than as labelled bullets"]
    return []
