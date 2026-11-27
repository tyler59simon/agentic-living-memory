"""Native Google Antigravity CLI hooks.

Wire reference: https://antigravity.google/docs/hooks
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from secrets import token_hex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hooks._io import emit, read_payload

EVENTS = ('SessionStart', 'PreInvocation', 'PreToolUse', 'Stop')
# Give the shared runtime the tool and argument names it uses for path policy and tracking.
TOOLS = {
    'run_command': ('run_shell_command', 'CommandLine', 'command'),
    'read_file': ('read', 'Path', 'path'),
    'view_file': ('read', 'AbsolutePath', 'path'),
    'list_dir': ('list', 'DirectoryPath', 'path'),
    'find_by_name': ('glob', 'SearchDirectory', 'path'),
    'grep_search': ('grep', 'SearchPath', 'path'),
    'write_file': ('write', 'Path', 'path'),
    'write_to_file': ('write', 'TargetFile', 'path'),
    'edit_file': ('edit', 'Path', 'path'),
    'replace_file_content': ('edit', 'TargetFile', 'path'),
    'multi_replace_file_content': ('multiedit', 'TargetFile', 'path'),
    'sed_file': ('edit', 'TargetFile', 'path'),
    'notebook_edit': ('notebookedit', 'NotebookPath', 'path'),
}
NORMAL_STOPS = {'no_tool_call', 'model_stop'}
TRANSCRIPT_LIMIT = 256 * 1024


def _path(value):
    if isinstance(value, str) and value and '\0' not in value:
        path = Path(value).expanduser()
        if path.is_absolute():
            return path
    return None


def _conversation(payload):
    session = payload.get('conversationId')
    if not isinstance(session, str) or not session:
        return None, None
    artifact = _path(payload.get('artifactDirectoryPath'))
    transcript = _path(payload.get('transcriptPath'))
    if (transcript is None or transcript.name not in {'transcript_full.jsonl', 'transcript.jsonl'}
            or transcript.parent.name != 'logs' or transcript.parent.parent.name != '.system_generated'):
        transcript = None
    if transcript is not None:
        transcript_artifact = transcript.parents[2]
        if artifact is None:
            artifact = transcript_artifact
        elif artifact != transcript_artifact:
            transcript = None
    if artifact is None or artifact.parts[-3:] != ('antigravity-cli', 'brain', session):
        return None, None
    return session, transcript


def _working_directory(payload):
    cwd = _path(payload.get('cwd'))
    if cwd is not None:
        return cwd
    workspaces = payload.get('workspacePaths')
    if isinstance(workspaces, list) and len(workspaces) == 1:
        return _path(workspaces[0])
    return None


def _step_kind(transcript, index):
    """Read the type of one trajectory step for turn identity. Captures never use its content."""
    if transcript is None:
        return None
    try:
        with transcript.open('rb') as stream:
            start = max(0, stream.seek(0, os.SEEK_END) - TRANSCRIPT_LIMIT)
            stream.seek(start)
            rows = stream.read(TRANSCRIPT_LIMIT).splitlines()
    except OSError:
        return None
    for raw in reversed(rows):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get('step_index') == index:
            return row.get('type')
    return None


def _contexts(*results):
    steps = [
        {'systemMessage': {'systemMessage': result['context']}}
        for result in results if result.get('context')
    ]
    return {'injectSteps': steps} if steps else {}


def _tool(payload, base, root):
    from core.runtime import process

    tool = payload.get('toolCall')
    if not isinstance(tool, dict) or not isinstance(tool.get('name'), str):
        return {'decision': 'allow'}
    native = tool['name']
    if native not in TOOLS or not isinstance(tool.get('args'), dict):
        return {'decision': 'allow'}
    name, key, target = TOOLS[native]
    args = tool['args']
    value = args.get(key)
    if target == 'path' and not isinstance(value, str):
        value = next((args[k] for k in ('FilePath', 'TargetFile', 'AbsolutePath', 'Path')
                      if isinstance(args.get(k), str)), None)
    normalized = {target: value} if isinstance(value, str) else {}
    if native == 'find_by_name' and isinstance(args.get('Pattern'), str):
        normalized['pattern'] = args['Pattern']
    tool_cwd = _path(args.get('Cwd')) if native == 'run_command' else None
    data = {**base, 'tool_name': name, 'tool_input': normalized}
    if tool_cwd is not None:
        data['cwd'] = str(tool_cwd)
    result = process('antigravity', 'pre-tool-use', data, root)
    if result.get('deny'):
        return {'decision': 'deny', 'reason': result['deny']}
    if result.get('allow'):
        # auto_approve skips the prompt the default policy would show but still honors rules the user configured.
        return {'decision': 'auto_approve', 'reason': result['allow']}
    return {'decision': 'allow'}


def _turn_event(event, payload, base, root, transcript, state):
    from core.runtime import _update, process, session_off

    session = base['session_id']
    prior = state.get('antigravity_turn') or {}
    subagent = base.get('agent_id')
    prompt = payload.get('lastUserInput')
    has_input = isinstance(prompt, str) and bool(prompt.strip())
    if session_off(state):
        if event == 'SessionStart' and has_input:
            return _contexts(process('antigravity', 'session-start', base, root))
        if event == 'PreInvocation' and payload.get('invocationNum') == 0 and has_input:
            return _contexts(process('antigravity', 'user-prompt', base, root))
        return {'decision': 'allow'} if event == 'Stop' else {}
    if event == 'SessionStart':
        # The CLI starts child conversations without a user prompt. Wait for their first invocation.
        if has_input and not subagent and state.get('harness') != 'antigravity':
            return _contexts(process('antigravity', 'session-start', base, root))
        return {}
    if event == 'PreInvocation':
        invocation = payload.get('invocationNum')
        steps = payload.get('initialNumSteps')
        if type(invocation) is not int or invocation != 0 or type(steps) is not int or steps < 0:
            return {}
        if subagent or (steps == 0 and not has_input):
            if not state.get('antigravity_subagent'):
                _update(root, session, lambda s: s.update(antigravity_subagent=True))
                return _contexts(process('antigravity', 'subagent-start', {**base, 'session_id': 'unknown'}, root))
            return {}
        if not has_input or steps == 0 or prior.get('steps') == steps:
            return {}
        # A background notification can reset invocationNum while carrying the previous lastUserInput.
        # Use the step type to distinguish that case from consecutive identical user prompts.
        salt = prior.get('salt') or token_hex(16)
        signature = hashlib.sha256((salt + '\0' + prompt).encode()).hexdigest()
        kind = _step_kind(transcript, steps - 1)
        if kind != 'USER_INPUT' and (kind is not None or signature == prior.get('prompt_hash')):
            return {}
        if prior.get('phase') in {'open', 'continuing'}:
            _stop(base, root, prior, quiet=True)
        briefing = {}
        if state.get('harness') != 'antigravity':
            briefing = process('antigravity', 'session-start', base, root)
        turn = {'id': token_hex(16), 'phase': 'open', 'steps': steps, 'salt': salt, 'prompt_hash': signature}
        result = process('antigravity', 'user-prompt', {**base, 'turn_id': turn['id'], 'prompt': prompt}, root)
        _update(root, session, lambda s: s.update(antigravity_turn=turn))
        return _contexts(briefing, result)
    if subagent or prior.get('phase') not in {'open', 'continuing'} or payload.get('fullyIdle') is not True:
        return {'decision': 'allow'}
    reason = str(payload.get('terminationReason', '')).lower()
    execution = payload.get('executionNum')
    normal = reason in NORMAL_STOPS and not payload.get('error')
    if normal and prior.get('phase') == 'continuing' and execution is not None and execution == prior.get('execution'):
        return {'decision': 'allow'}
    quiet = (not normal or prior.get('phase') == 'continuing'
             or payload.get('continuationActive') is True or payload.get('continuation_active') is True)
    return _stop(base, root, prior, quiet, execution)


def _stop(base, root, turn, quiet, execution=None):
    from core.runtime import _update, process

    result = process('antigravity', 'stop', {**base, 'turn_id': turn['id'], 'stop_hook_active': quiet}, root)
    reason = result.get('continue_reason') if not quiet else None
    marker = {**turn, 'phase': 'continuing' if reason else 'closed', 'execution': execution}
    _update(root, base['session_id'], lambda s: s.update(antigravity_turn=marker))
    return {'decision': 'continue', 'reason': reason} if reason else {'decision': 'allow'}


def handle(event, payload):
    neutral = {'decision': 'allow'} if event in {'PreToolUse', 'Stop'} else {}
    if event not in EVENTS or payload.get('hookEventName', event) != event:
        return neutral
    session, transcript = _conversation(payload)
    cwd = _working_directory(payload)
    if session is None or cwd is None or os.environ.get('LIVING_MEMORY_MAINTENANCE_CHILD') == '1':
        return neutral
    # Fast short-circuits before importing core.runtime or taking .antigravity.lock (REL-P3).
    if event == 'PreInvocation':
        invocation = payload.get('invocationNum')
        steps = payload.get('initialNumSteps')
        if type(invocation) is not int or invocation != 0 or type(steps) is not int or steps < 0:
            return {}
    if event == 'PreToolUse':
        tool = payload.get('toolCall')
        if not isinstance(tool, dict) or not isinstance(tool.get('name'), str):
            return {'decision': 'allow'}
        native = tool['name']
        if native not in TOOLS or not isinstance(tool.get('args'), dict):
            return {'decision': 'allow'}
        name, key, target = TOOLS[native]
        if name in {'read', 'glob', 'grep'}:
            from core.hookfast import quick
            args = tool['args']
            value = args.get(key)
            if target == 'path' and not isinstance(value, str):
                value = next((args[k] for k in ('FilePath', 'TargetFile', 'AbsolutePath', 'Path')
                              if isinstance(args.get(k), str)), None)
            normalized = {target: value} if isinstance(value, str) else {}
            if native == 'find_by_name' and isinstance(args.get('Pattern'), str):
                normalized['pattern'] = args['Pattern']
            fast_payload = {'session_id': session, 'cwd': str(cwd), 'tool_name': name, 'tool_input': normalized}
            aid = payload.get('agentId') or payload.get('agent_id')
            if aid:
                fast_payload['agent_id'] = aid
            if quick(fast_payload) is not None:
                return {'decision': 'allow'}

    from core.common import file_lock
    from core.repository import read_json
    from core.runtime import HOOK_LOCK_TIMEOUT, _pointer, env_off
    from core.storage import resolve_root

    base = {'session_id': session, 'cwd': str(cwd)}
    # Pass Antigravity's artifact/scratch directory so scratch writes are not counted as project mutations (REL-U5).
    scratch = payload.get('artifactDirectoryPath') or payload.get('scratchpad_dir') or payload.get('scratchpadDir')
    if isinstance(scratch, str) and scratch:
        base['scratchpad_dir'] = scratch
    # Pass every mode, not only plan, so the runtime sees a prompt that leaves plan mode.
    if isinstance(payload.get('permissionMode'), str) and payload['permissionMode']:
        base['permission_mode'] = payload['permissionMode']
    root = resolve_root()[0]
    if env_off():
        return _tool(payload, base, root) if event == 'PreToolUse' else neutral
    pointer = _pointer(root, session)
    # Serialize boundary hooks independently of the shared runtime's state lock.
    with file_lock(pointer.with_suffix('.antigravity.lock'), timeout=HOOK_LOCK_TIMEOUT):
        state = read_json(pointer, {})
        if not isinstance(state, dict):
            state = {}
        agent_id = payload.get('agentId') or payload.get('agent_id')
        if agent_id or state.get('antigravity_subagent'):
            base['agent_id'] = agent_id or session
        if event == 'PreToolUse':
            return _tool(payload, base, root)
        return _turn_event(event, payload, base, root, transcript, state)


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else ''
    try:
        output = handle(event, read_payload())
    except Exception as exc:
        if event in {'SessionStart', 'PreInvocation'}:
            print(f'Living Memory unavailable ({type(exc).__name__})', file=sys.stderr)
        output = {'decision': 'allow'} if event in {'PreToolUse', 'Stop'} else {}
    emit(output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
