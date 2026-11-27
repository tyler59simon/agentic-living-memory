"""Native Codex lifecycle adapter over the shared script runtime."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hooks._io import emit,read_payload

EVENTS={'session-start':'SessionStart','user-prompt':'UserPromptSubmit','subagent-start':'SubagentStart','pre-tool-use':'PreToolUse','stop':'Stop','pre-compact':'PreCompact'}
# Only context-bearing events report an unavailable store; per-tool banners would be noise.
REPORTING={'session-start','user-prompt'}
def handle(event,payload):
    from core.runtime import process
    result=process('codex',event,payload)
    body={'hookEventName':EVENTS.get(event,event)};out={}
    if result.get('context'):body['additionalContext']=result['context']
    if result.get('deny'):body.update(permissionDecision='deny',permissionDecisionReason=result['deny'])
    if len(body)>1:out['hookSpecificOutput']=body
    if result.get('continue_reason'):out.update(decision='block',reason=result['continue_reason'])
    if result.get('warning'):out['systemMessage']=result['warning']
    if out or event=='stop':emit(out)
def main():
    event=sys.argv[1] if len(sys.argv)>1 else ''
    try:
        payload=read_payload()
        if event=='pre-tool-use':
            from core.hookfast import quick
            if quick(payload) is not None:return 0
        handle(event,payload)
    except Exception as exc:  # A memory failure must never break the host session.
        if event in REPORTING:emit({'systemMessage':'Living Memory unavailable: '+str(exc)[:300]})
        elif event=='stop':emit({})
    return 0
if __name__=='__main__':raise SystemExit(main())
