"""Native Claude Code lifecycle adapter; installed directly, without a plugin."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

NAMES={'session-start':'SessionStart','user-prompt':'UserPromptSubmit','subagent-start':'SubagentStart','pre-tool-use':'PreToolUse','session-end':'SessionEnd','pre-compact':'PreCompact'}
# Only context-bearing events report an unavailable store; per-tool banners would be noise.
REPORTING={'session-start','user-prompt'}

def handle(event,payload,harness='claude'):
    from core.runtime import process
    result=process(harness,event,payload);out={}
    if result.get('context'):out['hookSpecificOutput']={'hookEventName':NAMES.get(event,event),'additionalContext':result['context']}
    for decision in ('deny','allow'):
        if result.get(decision):
            # A file hint (additionalContext) and a permission decision travel together.
            out.setdefault('hookSpecificOutput',{'hookEventName':'PreToolUse'}).update(permissionDecision=decision,permissionDecisionReason=result[decision]);break
    if result.get('continue_reason'):
        if harness=='claude':
            # Claude Code continues the turn on Stop additionalContext like decision=block, with the same loop
            # protections, but labels it "Stop hook feedback" instead of a hook error.
            out['hookSpecificOutput']={'hookEventName':'Stop','additionalContext':result['continue_reason']}
        else:out.update(decision='block',reason=result['continue_reason'])
    if result.get('warning'):out['systemMessage']=result['warning']
    return out

def main(harness='claude'):
    event=sys.argv[1] if len(sys.argv)>1 else ''
    try:
        raw=sys.stdin.buffer.read().decode('utf-8-sig')
        payload=json.loads(raw) if raw.strip() else {}
        if not isinstance(payload,dict):raise ValueError('Hook payload must be an object')
        if event=='pre-tool-use':
            # Read, Glob and Grep reach this hook only to find codebase maps; once a repo needs nothing, skip the runtime.
            from core.hookfast import quick
            fast=quick(payload)
            if fast is not None:sys.stdout.write(json.dumps(fast)+'\n');return 0
        if event=='pre-compact':
            # PreCompact stdout is not a JSON reply: Claude Code hands it to the summarizer as instructions.
            from core.runtime import process
            text=process(harness,event,payload).get('instructions')
            if text:sys.stdout.write(text+'\n')
            return 0
        out=handle(event,payload,harness)
    except Exception as exc:  # A memory failure must never break the host session.
        if event=='pre-compact':return 0  # any stdout would become summary instructions
        out={'systemMessage':'Living Memory unavailable: '+str(exc)[:300]} if event in REPORTING else {}
    sys.stdout.write(json.dumps(out)+'\n');return 0

if __name__=='__main__':raise SystemExit(main())
