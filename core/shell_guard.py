"""Shell command parsing, auto-allow confinement, read-only shell analysis, and subcommand guards (REL-D3)."""
from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'living_memory.py'
GLOBAL_OPTIONS = {'--root', '--domain', '--cwd', '--actor'}
VALUE_OPTIONS = ('--root', '--domain', '--cwd', '--actor')

# The bulk writes (delete, merge, retag, migrate, restore-backup, import, ingest, overlay-teardown) are left out on purpose:
# they are direct and undoable, and the harness asks the user before each one (the user chose this gate on 2026-11-26).
ALLOWED_SUBCOMMANDS = {
    'ask', 'types', 'sources', 'source-sync', 'ingest-scan', 'search', 'read', 'taxonomy', 'doctor', 'history', 'logs',
    'capture', 'capture-finalize', 'validate', 'update', 'supersede', 'review', 'backup', 'open', 'check', 'stats',
    'confirm', 'timeline', 'archive', 'rewrite', 'promote', 'projects', 'systems', 'views', 'wiki', 'resolve', 'reopen',
    'restore', 'log', 'maintenance', 'rebaseline', 'recap', 'status', 'help', 'propose', 'proposals', 'doc',
}
# Subagents are read-only: these are denied for them even though the main agent gets them auto-allowed. Their one write is
# propose, a draft in .turns/ that only the main agent turns into a note (proposals accept, or capture --from-proposal with an edited note) or rejects.
MUTATING_SUBCOMMANDS = {
    'capture', 'capture-finalize', 'update', 'supersede', 'backup', 'check', 'confirm', 'resolve', 'reopen', 'restore',
    'off', 'on', 'undo', 'archive', 'rewrite', 'remap', 'promote', 'source', 'project', 'system', 'type',
    'delete', 'merge', 'retag', 'migrate', 'restore-backup', 'import', 'ingest', 'overlay-teardown', 'setup', 'upgrade',
    'sync', 'overlay-build', 'overlay-sync-all',
}

# Hivemind sets HIVEMIND_SESSION for every program it starts in a terminal. Undo, the direct writes and the bulk writes there are the user's, done in Hivemind.
HIVEMIND_GOVERNED = {
    'undo', 'recover', 'upgrade', 'setup', 'archive', 'rewrite', 'remap', 'promote', 'source', 'project', 'system', 'type',
    'delete', 'merge', 'retag', 'migrate', 'restore-backup', 'import', 'ingest', 'overlay-teardown', 'sync', 'overlay-build', 'overlay-sync-all',
}
HIVEMIND_DENY = (
    'Undo, setup, upgrade, the direct writes (archive, rewrite, remap, promote, taxonomy add, source, project, system, type), the bulk writes (delete, merge, retag, '
    'migrate, restore-backup, import, ingest, overlay-teardown), a vault sync run by hand and folder notebook writes (settling a page proposal included) are for the user to do in Hivemind, '
    'not for an agent. Stage a page edit with doc propose; for the rest, tell the user what to change.'
)
# `doc` actions that write. `doc propose` is missing on purpose: it only stages an edit for the user, so agents may run it.
DOC_WRITE_ACTIONS = {'undo', 'folder'}
# What agents may run. Anything else after `doc`, an unknown word included, counts as a write.
DOC_AGENT_ACTIONS = {'read', 'propose'}
PRIVATE_DENY = 'Living Memory: private notebooks are excluded from agent sessions, so --include-private is not available here.'
SERVE_DENY = 'Living Memory: serve is the request server for apps such as Hivemind, not for agents; run the command itself.'
SUBAGENT_DENY = 'Living Memory: subagents are read-only. Stage the finding with propose for the main agent instead of running '


def active(script):
    from .launcher import active as _active
    return _active(script)


def _current_script():
    rt = sys.modules.get('core.runtime')
    return getattr(rt, 'SCRIPT', SCRIPT) if rt is not None else SCRIPT


def _norm(path):
    return os.path.normcase(str(path).replace('/', '\\') if os.name == 'nt' else str(path))


def shell_command(args):
    if isinstance(args, dict):
        value = args.get('command', args.get('cmd'))
        if isinstance(value, list):
            value = value[-1] if value else ''
        return value if isinstance(value, str) else ''
    return args if isinstance(args, str) else ''


# Outside quotes these run, chain, redirect or expand. Parentheses open a subshell in Bash and evaluate an
# expression in PowerShell: `search (Remove-Item x)` runs the cmdlet before the script sees its arguments.
# Braces are a script block in PowerShell and expand in Bash ({a,/etc/passwd} becomes two arguments).
SHELL_META = frozenset(';|&<>`$(){}\r\n')
# PowerShell only: splatting, array expressions and here-strings.
POWERSHELL_META = frozenset('@')
# PowerShell's stop-parsing token: what follows is no longer read by the rules tracked here.
STOP_PARSING = '--%'
# PowerShell reads these as quotation marks too, so a curly quote can close a string an ASCII quote opened.
POWERSHELL_QUOTES = frozenset('\u2018\u2019\u201a\u201b\u201c\u201d\u201e')
SHELL_GRAMMARS = ('posix', 'powershell')


def shell_grammar(tool):
    """The quoting rules a shell tool's command follows: 'posix', 'powershell', or None when the tool does not say."""
    return 'posix' if tool == 'bash' else 'powershell' if tool == 'powershell' else None


def _words(rest, grammar):
    """The argument words of a command line that cannot run or expand anything, else None."""
    words = []
    word = None
    state = None
    i = 0
    ps = grammar == 'powershell'
    while i < len(rest):
        ch = rest[i]
        nxt = rest[i + 1:i + 2]
        if ps and ch in POWERSHELL_QUOTES:
            return None
        if state == "'":
            if ch != "'":
                word += ch
            elif ps and nxt == "'":
                word += "'"
                i += 1
            else:
                state = None
        elif state == '"':
            if ch == '"':
                if ps and nxt == '"':
                    word += '"'
                    i += 1
                else:
                    state = None
            elif ch in '$`' or (not ps and ch == '\\' and nxt in ('$', '`', '"', '\\', '\n', '\r', '')):
                return None
            else:
                word += ch
        elif ch in "'\"":
            state = ch
            word = word or ''
        elif ch == '\\' and not ps:
            if nxt in ('', '\n', '\r'):
                return None
            word = (word or '') + nxt
            i += 1
        elif ch == '#' and word is None:
            return None
        elif ch in SHELL_META or (ps and ch in POWERSHELL_META and word is None):
            return None
        elif ch.isspace():
            if word is not None:
                words.append(word)
                word = None
        else:
            word = (word or '') + ch
        i += 1
    if state is not None:
        return None
    if word is not None:
        words.append(word)
    return None if ps and STOP_PARSING in words else words


def _parsed(text, grammar=None):
    """The argument words of an exact, unchained living_memory.py invocation (interpreter and script paths each double- or single-quoted, or bare `alm` while launcher.active; an optional leading `&`), once per shell grammar that may apply, else None."""
    script = _current_script()
    match = re.match(r'''\s*(?:&\s*)?(?:"([^"]+)"|'([^']+)')\s+(?:"([^"]+)"|'([^']+)')(.*)\Z''', text, re.S)
    if match:
        if _norm(match[1] or match[2]) != _norm(sys.executable) or _norm(match[3] or match[4]) != _norm(script):
            return None
        rest = match[5]
    else:
        bare = re.match(r'\s*(?:&\s*)?alm(?=\s|\Z)(.*)\Z', text, re.S)
        if not bare or not active(script):
            return None
        rest = bare[1]
    found = [_words(rest, g) for g in ((grammar,) if grammar in SHELL_GRAMMARS else SHELL_GRAMMARS)]
    return None if any(words is None for words in found) else found


def _option_width(word):
    """How many words an option takes as argparse reads it: 2 for --root, --domain, --cwd and --actor and their abbreviations (--dom), 1 for any other option and for --opt=value."""
    if '=' in word:
        return 1
    if word in GLOBAL_OPTIONS:
        return 2
    return 2 if len(word) >= 3 and word.startswith('--') and any(o.startswith(word) for o in VALUE_OPTIONS) else 1


def _subcommand(words):
    index = 0
    while index < len(words):
        token = words[index]
        if token.startswith('-'):
            index += _option_width(token)
            continue
        return token
    return None


def memory_subcommand(text, grammar=None):
    """Subcommand of an exact, unchained living_memory.py invocation, else None. grammar: see shell_grammar."""
    parsed = _parsed(text, grammar)
    if not parsed:
        return None
    found = {_subcommand(words) for words in parsed}
    return found.pop() if len(found) == 1 else None


def _names_memory(tokens):
    return any(
        t.lower().endswith(('living_memory.py', 'living_memory'))
        or re.split(r'[\\/]', t)[-1].lower() in ('alm', 'alm.exe', 'alm.cmd')
        for t in tokens
    )


def _doc_action(words):
    """The action of a `doc` command (the first word after `doc` that is not an option or an option's value), or None."""
    index = 0
    while index < len(words) and words[index] != 'doc':
        index += _option_width(words[index]) if words[index].startswith('-') else 1
    index += 1
    while index < len(words):
        word = words[index]
        if word.startswith('-'):
            index += _option_width(word)
            continue
        return word
    return None


def doc_write(text, grammar):
    """True when a `doc` command may write: any action but the reads and `doc propose`, and any action it cannot name."""
    return any(_doc_action(words) not in DOC_AGENT_ACTIONS for words in _parsed(text, grammar) or [] if _subcommand(words) == 'doc')


def serve_command(text, grammar):
    """True when a shell command runs `serve`, the JSON-lines server for apps."""
    parsed = _parsed(text, grammar)
    if parsed:
        return any(_subcommand(words) == 'serve' for words in parsed)
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = [t for t in re.split(r'''[\s;&|()"'`{}<>=,]+''', text) if t]
        first = next((i for i, t in enumerate(tokens) if _names_memory([t])), None)
        return first is not None and 'serve' in tokens[first + 1:]
    for index, token in enumerate(tokens):
        if _names_memory([token]):
            index += 1
            while index < len(tokens) and tokens[index].startswith('-'):
                index += _option_width(tokens[index])
            if index < len(tokens) and tokens[index] == 'serve':
                return True
    return False


SHELL_WRAPPERS = frozenset({'bash', 'sh', 'zsh', 'dash', 'pwsh', 'powershell', 'cmd', 'eval'})
PREFIX_COMMANDS = frozenset({'env', 'sudo', 'nohup', 'exec', 'time', 'command', 'xargs', 'nice', 'start', 'call'})
INTERPRETER = re.compile(r'(?:python[\d.]*|py)(?:\.exe)?', re.I)
ASSIGNMENT = re.compile(r'[A-Za-z_]\w*=')


def _command_words(text, ps):
    """([[(word, quoted)]], [command text run inside $( ) or backticks]) for a shell command line, or None when a quote is left open."""
    commands = [[]]
    nested = []
    word = None
    quoted = False
    state = None
    i = 0
    n = len(text)

    def finish():
        nonlocal word, quoted
        if word is not None:
            commands[-1].append((word, quoted))
        word = None
        quoted = False

    def substitution(at):
        depth = 0
        for j in range(at + 1, n):
            if text[j] == '(':
                depth += 1
            elif text[j] == ')':
                depth -= 1
                if depth == 0:
                    return text[at + 2:j], j
        return text[at + 2:], n - 1

    def backticks(at):
        end = text.find('`', at + 1)
        end = n - 1 if end < 0 else end
        return text[at + 1:end], end

    while i < n:
        ch = text[i]
        nxt = text[i + 1:i + 2]
        if state == "'":
            if ch != "'":
                word += ch
            elif ps and nxt == "'":
                word += "'"
                i += 1
            else:
                state = None
        elif state == '"':
            if ch == '"':
                if ps and nxt == '"':
                    word += '"'
                    i += 1
                else:
                    state = None
            elif ch == '$' and nxt == '(':
                inner, end = substitution(i)
                nested.append(inner)
                word += text[i:end + 1]
                i = end
            elif ch == '`' and not ps:
                inner, end = backticks(i)
                nested.append(inner)
                word += text[i:end + 1]
                i = end
            elif ch == '\\' and not ps and nxt in ('$', '`', '"', '\\', '\n', '\r'):
                word += nxt
                i += 1
            else:
                word += ch
        elif ch in "'\"":
            state = ch
            word = word or ''
            quoted = True
        elif ch == '\\' and not ps and nxt and nxt in ' \t\'"$`\\;&|<>(){}\n\r':
            word = (word or '') + nxt
            i += 1
        elif ch == '`' and ps:
            if nxt in ('\n', '\r'):
                finish()
            elif nxt:
                word = (word or '') + nxt
            i += 1
        elif ch == '`':
            inner, end = backticks(i)
            nested.append(inner)
            word = (word or '') + text[i:end + 1]
            i = end
        elif ch == '$' and nxt == '(':
            inner, end = substitution(i)
            nested.append(inner)
            word = (word or '') + text[i:end + 1]
            i = end
        elif ch in ';|&(){}\n\r':
            finish()
            commands.append([])
        elif ch.isspace() or ch in '<>':
            finish()
        else:
            word = (word or '') + ch
        i += 1
    if state is not None:
        return None
    finish()
    return [c for c in commands if c], nested


def _memory_option_words(text, ps, depth=0):
    """The option words (unquoted, starting with --) of every living_memory.py or alm command in a shell command line."""
    if depth > 4:
        return None
    scanned = _command_words(text, ps)
    if scanned is None:
        return None
    work, nested = scanned
    found = []
    for inner in nested:
        words = _memory_option_words(inner, ps, depth + 1)
        if words is None:
            return None
        found += words
    while work:
        words = work.pop()
        i = 0
        while i < len(words) and (
            words[i][0].lower() in PREFIX_COMMANDS
            or ASSIGNMENT.match(words[i][0])
            or words[i][0].startswith('-')
            or re.fullmatch(r'/[A-Za-z]', words[i][0])
        ):
            i += 1
        head = words[i:]
        if not head:
            continue
        first = re.split(r'[\\/]', head[0][0])[-1].lower()
        if first.removesuffix('.exe') in SHELL_WRAPPERS:
            for word, quoted in head[1:]:
                if quoted:
                    inner = _memory_option_words(word, ps, depth + 1)
                    if inner is None:
                        return None
                    found += inner
            work.append([w for w in head[1:] if not w[1]])
            continue
        rest = None
        if _names_memory([head[0][0]]):
            rest = head[1:]
        elif INTERPRETER.fullmatch(first):
            for k, (word, _) in enumerate(head[1:], 1):
                if word.startswith('-'):
                    continue
                if _names_memory([word]):
                    rest = head[k + 1:]
                break
        if rest is not None:
            found += [w for w, quoted in rest if not quoted and w.startswith('--')]
    return found


def include_private(text, grammar):
    """True when a memory command asks for private notebooks: --include-private or an argparse abbreviation of it (3+ chars)."""
    def flag(word):
        name = word.partition('=')[0]
        return len(name) >= 3 and name.startswith('--') and '--include-private'.startswith(name)

    for g in (grammar,) if grammar in SHELL_GRAMMARS else SHELL_GRAMMARS:
        words = _memory_option_words(text, g == 'powershell')
        if words is None:
            tokens = [t for t in re.split(r'''[\s;&|()"'`{}<>,]+''', text) if t]
            if _names_memory(tokens) and any(flag(t) for t in tokens):
                return True
        elif any(flag(w) for w in words):
            return True
    return False


PAGE_ID = re.compile(r'[a-f0-9]{12}')


def _settles_page(words):
    """True when these words accept or reject a page edit (a 12-digit hex ID); a staged note's 8-digit ID is the main agent's to settle."""
    return bool({'accept', 'reject'} & set(words)) and any(PAGE_ID.fullmatch(w) for w in words)


def page_settle(text, grammar):
    """True when an exact `proposals` command accepts or rejects a page edit."""
    return any(_subcommand(words) == 'proposals' and _settles_page(words) for words in _parsed(text, grammar) or [])


def _subcommand_rest(words, name):
    """Argument words following subcommand `name`, skipping global options and their values via _option_width, else None."""
    index = 0
    while index < len(words):
        token = words[index]
        if token.startswith('-'):
            index += _option_width(token)
            continue
        return words[index + 1:] if token == name else None
    return None


def config_write(text, grammar):
    """True when a `config` command writes (`config set`); `config get` only reads."""
    for words in _parsed(text, grammar) or []:
        rest = _subcommand_rest(words, 'config')
        if rest is not None and 'set' in rest:
            return True
    return False


def hivemind_governed(text, grammar):
    """True when a shell command undoes an operation or makes a direct or bulk write guarded in Hivemind terminals."""
    parsed = _parsed(text, grammar)
    subs = [_subcommand(words) for words in parsed or []]
    if parsed and all(sub and not sub.startswith('-') for sub in subs):
        return any(
            sub in HIVEMIND_GOVERNED
            or (sub == 'taxonomy' and 'add' in (_subcommand_rest(words, 'taxonomy') or ()))
            or (sub == 'config' and 'set' in (_subcommand_rest(words, 'config') or ()))
            or (sub == 'doc' and _doc_action(words) not in DOC_AGENT_ACTIONS)
            or (sub == 'proposals' and _settles_page(words))
            for sub, words in zip(subs, parsed)
        )
    tokens = [t for t in re.split(r'''[\s;&|()"'`{}<>=,]+''', text) if t]
    return _names_memory(tokens) and (
        any(t in HIVEMIND_GOVERNED for t in tokens)
        or ('taxonomy' in tokens and 'add' in tokens)
        or ('config' in tokens and 'set' in tokens)
        or ('doc' in tokens and any(t in DOC_WRITE_ACTIONS for t in tokens))
        or ('proposals' in tokens and _settles_page(tokens))
    )


# Options that read a file into a note. Auto-approval covers only files the session could already see.
FILE_OPTIONS = ('--content-file', '--body-file', '--payload-file', '--spec-file')


def _option_values(words, names):
    """Values given to these options as `--opt VALUE` or `--opt=VALUE`, including argparse abbreviations (--content)."""
    found = []
    for index, word in enumerate(words):
        if not word.startswith('--') or len(word) < 4:
            continue
        name, sep, value = word.partition('=')
        if any(option.startswith(name) for option in names):
            found.append(value if sep else words[index + 1] if index + 1 < len(words) else '')
    return found


def remote_sync(text, grammar, root):
    """True when an exact `source-sync` command would reach the network."""
    parsed = _parsed(text, grammar)
    if not parsed:
        return False
    try:
        from . import sources
        remote = {s['name'] for s in sources.load(root)['sources'] if s.get('kind') in sources.REMOTE}
    except (ValueError, OSError):
        remote = set()
    for words in parsed:
        rest = _subcommand_rest(words, 'source-sync')
        if rest is None:
            continue
        for word in rest:
            if word in remote or (word.startswith('--a') and '--all-remote'.startswith(word.split('=')[0])):
                return True
    return False


def views_write(text, grammar):
    """True when a `views` command writes: add, update, remove, published, or render except `--format json` with no --out."""
    for words in _parsed(text, grammar) or []:
        rest = _subcommand_rest(words, 'views')
        if not rest:
            continue
        action = next((w for w in rest if not w.startswith('-')), 'list')
        if action in {'add', 'update', 'remove', 'published'}:
            return True
        if action == 'render' and (_option_values(rest, ('--out',)) or not _json_format(rest)):
            return True
    return False


def _json_format(words):
    """True when these words end up as `--format json`: argparse keeps the last value."""
    return _option_values(words, ('--format',))[-1:] == ['json']


def wiki_build(text, grammar):
    """True when a `wiki` command is `--agent --build`, or an argparse abbreviation of --build."""
    for words in _parsed(text, grammar) or []:
        rest = _subcommand_rest(words, 'wiki')
        if rest is None:
            continue
        if any(len(name) >= 4 and '--build'.startswith(name) for name in (w.partition('=')[0] for w in rest if w.startswith('--'))):
            return True
    return False


def wiki_write(text, grammar):
    """True when a `wiki` command writes a file or spends money: anything but `--format json` with no --out, and any --build."""
    if wiki_build(text, grammar):
        return True
    for words in _parsed(text, grammar) or []:
        rest = _subcommand_rest(words, 'wiki')
        if rest is None:
            continue
        if _option_values(rest, ('--out',)) or not _json_format(rest):
            return True
    return False


def maintenance_write(text, grammar):
    """True when a `maintenance` command runs or changes maintenance: run, enable or disable."""
    for words in _parsed(text, grammar) or []:
        rest = _subcommand_rest(words, 'maintenance')
        if rest is not None and {'run', 'enable', 'disable'} & set(rest):
            return True
    return False


def maintenance_paid(text, grammar):
    """True when a `maintenance` command enables or triggers paid model runs (`enable` or `run`)."""
    for words in _parsed(text, grammar) or []:
        rest = _subcommand_rest(words, 'maintenance')
        if rest is not None and {'run', 'enable'} & set(rest):
            return True
    return False


def rebaseline_write(text, grammar):
    """True when a `rebaseline` command may write (anything but --dry-run)."""
    for words in _parsed(text, grammar) or []:
        rest = _subcommand_rest(words, 'rebaseline')
        if rest is not None and '--dry-run' not in rest:
            return True
    return False


def proposals_write(text, grammar):
    """True when a `proposals` command settles one (accept or reject); list and show only read."""
    for words in _parsed(text, grammar) or []:
        rest = _subcommand_rest(words, 'proposals')
        if rest is not None and {'accept', 'reject'} & set(rest):
            return True
    return False


def taxonomy_write(text, grammar):
    """True when a `taxonomy` command adds tags (`taxonomy add`); plain `taxonomy` only reads."""
    for words in _parsed(text, grammar) or []:
        rest = _subcommand_rest(words, 'taxonomy')
        if rest is not None and 'add' in rest:
            return True
    return False


def memory_write(subcommand, text, grammar):
    """True when a memory command writes anything a subagent may not."""
    if not subcommand:
        return False
    return (
        subcommand in MUTATING_SUBCOMMANDS
        or (subcommand == 'views' and views_write(text, grammar))
        or (subcommand == 'maintenance' and maintenance_write(text, grammar))
        or (subcommand == 'wiki' and wiki_write(text, grammar))
        or (subcommand == 'proposals' and proposals_write(text, grammar))
        or (subcommand == 'taxonomy' and taxonomy_write(text, grammar))
        or (subcommand == 'config' and config_write(text, grammar))
        or (subcommand == 'rebaseline' and rebaseline_write(text, grammar))
        or (subcommand == 'doc' and doc_write(text, grammar))
    )


def subagent_write_verb(text, grammar):
    """The mutating subcommand a subagent shell command attempts (exact or chained/unparseable fail-closed), else None."""
    sub = memory_subcommand(text, grammar)
    if sub:
        return sub if memory_write(sub, text, grammar) else None
    tokens = [t for t in re.split(r'''[\s;&|()"'`{}<>=,]+''', text) if t]
    if not _names_memory(tokens):
        return None
    for t in tokens:
        if t in MUTATING_SUBCOMMANDS:
            return t
    if 'taxonomy' in tokens and 'add' in tokens:
        return 'taxonomy'
    if 'config' in tokens and 'set' in tokens:
        return 'config'
    if 'proposals' in tokens and {'accept', 'reject'} & set(tokens):
        return 'proposals'
    if 'maintenance' in tokens and {'run', 'enable', 'disable'} & set(tokens):
        return 'maintenance'
    if 'views' in tokens and {'add', 'update', 'remove', 'published', 'render'} & set(tokens):
        return 'views'
    if 'wiki' in tokens:
        return 'wiki'
    if 'rebaseline' in tokens:
        return 'rebaseline'
    if 'doc' in tokens and any(t in DOC_WRITE_ACTIONS for t in tokens):
        return 'doc'
    return None


def auto_allowed(text, grammar, root, cwd, scratch=None):
    """True when an allow-listed memory command may skip the permission prompt."""
    parsed = _parsed(text, grammar)
    if not parsed or wiki_build(text, grammar) or maintenance_paid(text, grammar):
        return False
    try:
        from .paths import is_within
        store = Path(root).resolve()
        bases = [Path(cwd).resolve()] + ([Path(scratch).resolve()] if scratch else [])
        for words in parsed:
            for value in _option_values(words, ('--root',)):
                if not value or Path(os.path.expandvars(value)).expanduser().resolve() != store:
                    return False
            for value in _option_values(words, FILE_OPTIONS + ('--out',)):
                if not value:
                    return False
                path = Path(os.path.expandvars(value)).expanduser()
                path = (path if path.is_absolute() else bases[0] / path).resolve()
                if not any(is_within(path, base) for base in bases):
                    return False
        return True
    except (OSError, ValueError, RuntimeError):
        return False


READONLY_SIMPLE = {
    'ls', 'dir', 'cat', 'type', 'head', 'tail', 'wc', 'grep', 'egrep', 'fgrep', 'cut', 'tr', 'echo', 'printf', 'pwd',
    'which', 'where', 'whoami', 'stat', 'file', 'du', 'df', 'tree', 'diff', 'cmp', 'basename', 'dirname', 'realpath',
    'readlink', 'test', '[', 'true', 'false', 'sleep', 'cd', 'pushd', 'popd', 'set-location', 'gci', 'gc', 'printenv',
    'select-string', 'select-object', 'where-object', 'sort-object', 'measure-object', 'format-table', 'format-list',
    'test-path', 'resolve-path', 'split-path', 'join-path', 'convertto-json', 'write-output', 'write-host',
    'nl', 'comm', 'paste', 'rev', 'fold', 'column', 'md5sum', 'sha1sum', 'sha256sum', 'jq', 'seq',
}
READONLY_GIT = {
    'status', 'log', 'diff', 'show', 'ls-files', 'ls-tree', 'rev-parse', 'blame', 'describe', 'shortlog', 'grep',
    'cat-file', 'rev-list', 'merge-base', 'name-rev', 'for-each-ref', 'show-ref', 'ls-remote', 'count-objects',
}
VARIABLE_PARAMETER = re.compile(r'-(?:outvariable|ov|pipelinevariable|pv|errorvariable|ev|warningvariable|wv|informationvariable|iv)(?::.*)?', re.I)
FOREACH_PROPERTIES = {'line', 'linenumber', 'path', 'filename', 'name', 'fullname', 'matches', 'length', 'count', 'basename', 'extension', 'directoryname'}
FIND_WRITERS = {'-delete', '-exec', '-execdir', '-ok', '-okdir', '-fprint', '-fprintf', '-fls', '-fprint0'}
NULL_SINKS = {'/dev/null', 'nul', '$null'}
SHELL_KEYWORDS = {'do', 'then', 'else', 'elif', 'if', 'while', 'until', '!'}
SHELL_CLOSERS = {'done', 'fi'}
SED_FLAGS = set('gpiImM0123456789')
SED_SAFE = re.compile(r'[\s0-9,$!;{}pdnNDPqQ=lgGhHx~+-]*')
AWK_UNSAFE = re.compile(r'system\s*\(|\||@load|@include|\bprintf?\b[^;}]*>')
XARGS_VALUED = {'-n', '-L', '-I', '-d', '-P', '-s', '-a', '-E', '-e'}
UNIQ_VALUED = {'-f', '-s', '-w'}


def _positional(args, valued=()):
    found = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in valued:
            skip = True
            continue
        if a.startswith('-') and a != '-':
            continue
        found.append(a)
    return found


def _sed_script_safe(script):
    text = re.sub(r'\\.', '', script)

    def body(m):
        return ' ' if set(m[2]) <= SED_FLAGS else '\0'

    text = re.sub(r's(.)(?:(?!\1).)*\1(?:(?!\1).)*\1([A-Za-z0-9]*)', body, text)
    text = re.sub(r'y(.)(?:(?!\1).)*\1(?:(?!\1).)*\1', ' ', text)
    text = re.sub(r'/[^/]*/[IM]?', ' ', text)
    return bool(SED_SAFE.fullmatch(text))


def _sed_readonly(args):
    scripts = []
    positional = []
    expect = False
    for a in args:
        if expect:
            scripts.append(a)
            expect = False
            continue
        if a in {'-e', '--expression'}:
            expect = True
            continue
        if a.startswith('--expression='):
            scripts.append(a.split('=', 1)[1])
            continue
        if a.startswith('--'):
            if a.startswith(('--in-place', '--file')):
                return False
            continue
        if a.startswith('-') and a != '-':
            flags = a[1:]
            if 'e' in flags:
                cut = flags.index('e')
                flags, attached = flags[:cut], flags[cut + 1:]
                if attached:
                    scripts.append(attached)
                else:
                    expect = True
            if 'i' in flags or 'f' in flags:
                return False
            continue
        positional.append(a)
    if not scripts and positional:
        scripts = [positional[0]]
    return bool(scripts) and not expect and all(_sed_script_safe(s) for s in scripts)


def _awk_readonly(args):
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in {'-F', '-v', '--field-separator', '--assign'}:
            skip = True
            continue
        if a.startswith(('-f', '-i', '-l', '-E', '--file', '--include', '--load', '--exec')):
            return False
        if a.startswith('-') and a != '-':
            continue
        return not AWK_UNSAFE.search(a)
    return False


def _xargs_command(words):
    index = 0
    while index < len(words):
        a = words[index].strip('"\'')
        if a in XARGS_VALUED:
            index += 2
            continue
        if a.startswith('-') and a != '-':
            index += 1
            continue
        break
    return words[index:]


def _readonly_segment(words):
    """True when one simple command (already split on ; & | and parentheses) provably cannot write files."""
    while words and words[0].strip('"\'').lower() in SHELL_KEYWORDS:
        words = words[1:]
    if not words:
        return True
    if len(words) == 1 and words[0].lower() in SHELL_CLOSERS:
        return True
    if words[0].lower() == 'for':
        return len(words) >= 3 and words[2].lower() == 'in'
    first = words[0].strip('"\'').replace('\\', '/').rsplit('/', 1)[-1].lower()
    first = first[:-4] if first.endswith('.exe') else first
    args = [w.strip('"\'') for w in words[1:]]
    if '-' in first and any(VARIABLE_PARAMETER.fullmatch(a) for a in args):
        return False
    if first in READONLY_SIMPLE:
        return True
    if first.startswith('get-'):
        return True
    if first in {'foreach-object', '%', 'foreach'}:
        return len(args) == 1 and args[0].lower() in FOREACH_PROPERTIES
    if first == 'find':
        return not FIND_WRITERS.intersection(args)
    if first == 'rg':
        return not any(a == '--pre' or a.startswith('--pre=') for a in args)
    if first == 'sort':
        return not any(a.startswith('-o') or a.startswith('--output') for a in args)
    if first == 'uniq':
        return len(_positional(args, UNIQ_VALUED)) <= 1
    if first == 'sed':
        return _sed_readonly(args)
    if first in {'awk', 'gawk', 'mawk'}:
        return _awk_readonly(args)
    if first == 'xargs':
        return _readonly_segment(_xargs_command(words[1:]))
    if first == 'git':
        rest = args[:]
        while rest and rest[0] == '-C' and len(rest) > 1:
            rest = rest[2:]
        if not rest or rest[0].startswith('-'):
            return False
        sub, tail = rest[0], rest[1:]
        if any(a.startswith('--output') or a == '-o' for a in tail):
            return False
        if sub in READONLY_GIT:
            return True
        if sub == 'branch':
            return all(a in {'-a', '-r', '-v', '-vv', '--list', '--show-current'} for a in tail)
        if sub == 'remote':
            return all(a in {'-v', '--verbose'} for a in tail)
        if sub == 'tag':
            return all(a in {'-l', '--list'} for a in tail)
        if sub == 'config':
            return bool(tail) and tail[0] in {'--get', '--get-all', '--list', '-l'}
        if sub == 'reflog':
            return not tail or tail[0] == 'show' or tail[0].startswith('-')
        if sub == 'stash':
            return bool(tail) and tail[0] in {'list', 'show'}
        return False
    if first in {'python', 'python3', 'py', 'node'}:
        return args in (['--version'], ['-V'], ['-v'])
    if first in {'pip', 'pip3'}:
        return bool(args) and args[0] in {'list', 'show', 'freeze', '--version'}
    if first == 'npm':
        return bool(args) and args[0] in {'ls', 'list', 'view', '--version', '-v'}
    return False


def _shell_pipelines(text):
    """[[(words, redirects, words_read_only)]]: the pipelines of a shell command line."""
    if not text or re.search(r'[`\r\n]|\$\(|[<>]\(', text):
        return None
    try:
        lexer = shlex.shlex(text, posix=False, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ''
        tokens = list(lexer)
    except ValueError:
        return None
    pipelines = [[]]
    words = []
    redirects = []
    depth = 0
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token and set(token) <= set(';&|<>()') and not token.startswith(('"', "'")):
            if set(token) & set('<>'):
                nxt = tokens[index + 1].strip('"\'').lower() if index + 1 < len(tokens) else ''
                if '<' not in token and token.endswith('&') and nxt.isdigit():
                    index += 2
                    continue
                if token in {'>', '>>'} and nxt in NULL_SINKS:
                    index += 2
                    continue
                redirects += tokens[index:index + 2]
                index += 2
                continue
            pipelines[-1].append((words, redirects, _readonly_segment(words)))
            words = []
            redirects = []
            index += 1
            depth = max(0, depth + token.count('(') - token.count(')'))
            if not depth and (';' in token or '&&' in token or '||' in token):
                pipelines.append([])
            continue
        words.append(token)
        index += 1
    pipelines[-1].append((words, redirects, _readonly_segment(words)))
    return pipelines


def readonly_shell(text):
    """True only if a shell command is provably read-only. Anything doubtful returns False."""
    pipelines = _shell_pipelines(text)
    return pipelines is not None and all(ok and not redirects for pipeline in pipelines for _, redirects, ok in pipeline)


def writing_shell_text(text):
    """The parts of a shell command that may write."""
    pipelines = _shell_pipelines(text)
    if pipelines is None:
        return text
    flow = any(
        words and (words[0].strip('"\'').lower() in SHELL_KEYWORDS | SHELL_CLOSERS | {'for', 'foreach'} or any(w.startswith('{') for w in words))
        for pipeline in pipelines
        for words, _, _ in pipeline
    )
    if flow and not all(ok and not redirects for pipeline in pipelines for _, redirects, ok in pipeline):
        return text
    parts = []
    for pipeline in pipelines:
        whole = not all(ok for _, _, ok in pipeline)
        parts += [' '.join((words if whole else []) + redirects) for words, redirects, _ in pipeline if whole or redirects]
    return ' '.join(parts)
