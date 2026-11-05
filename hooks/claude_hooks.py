"""Native Claude Code lifecycle adapter; installed directly, without a plugin."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

NAMES={'session-start':'SessionStart','user-prompt':'UserPromptSubmit','subagent-start':'SubagentStart','pre-tool-use':'PreToolUse','session-end':'SessionEnd'}
# Only context-bearing events report an unavailable store; per-tool banners would be noise.
REPORTING={'session-start','user-prompt'}

def handle(event,payload,harness='claude'):
    from core.runtime import process
    result=process(harness,event,payload);out={}
    if result.get('context'):out['hookSpecificOutput']={'hookEventName':NAMES.get(event,event),'additionalContext':result['context']}
    for decision in ('deny','allow'):
        if result.get(decision):
            out['hookSpecificOutput']={'hookEventName':'PreToolUse','permissionDecision':decision,'permissionDecisionReason':result[decision]};break
    if result.get('continue_reason'):out.update(decision='block',reason=result['continue_reason'])
    if result.get('warning'):out['systemMessage']=result['warning']
    return out

def main(harness='claude'):
    event=sys.argv[1] if len(sys.argv)>1 else ''
    try:
        raw=sys.stdin.buffer.read().decode('utf-8-sig')
        payload=json.loads(raw) if raw.strip() else {}
        if not isinstance(payload,dict):raise ValueError('Hook payload must be an object')
        out=handle(event,payload,harness)
    except Exception as exc:  # A memory failure must never break the host session.
        out={'systemMessage':'Living Memory unavailable: '+str(exc)[:300]} if event in REPORTING else {}
    sys.stdout.write(json.dumps(out)+'\n');return 0

if __name__=='__main__':raise SystemExit(main())
