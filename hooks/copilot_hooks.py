"""Copilot CLI native camelCase adapter, including transformed prompt retrieval.

Event names and output keys follow the Copilot CLI hooks reference:
https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-hooks-reference
"""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hooks._io import emit,read_payload
EVENTS={'sessionStart':'session-start','userPromptTransformed':'user-prompt','subagentStart':'subagent-start','preToolUse':'pre-tool-use','agentStop':'stop'}

def handle(event,payload):
    from core.runtime import process
    event=EVENTS.get(event,event);result=process('copilot',event,payload);out={}
    if result.get('context'):
        if event=='user-prompt':out['modifiedTransformedPrompt']=str(payload.get('transformedPrompt',payload.get('prompt','')))+'\n\n'+result['context']
        else:out['additionalContext']=result['context']
    for decision in ('deny','allow'):
        if result.get(decision):out.update(permissionDecision=decision,permissionDecisionReason=result[decision]);break
    if result.get('continue_reason'):out.update(decision='block',reason=result['continue_reason'])
    return out

def main():
    event=sys.argv[1] if len(sys.argv)>1 else ''
    try:
        payload=read_payload()
        if EVENTS.get(event,event)=='pre-tool-use':
            from core.hookfast import quick
            fast=quick(payload)
            if fast is not None:emit(fast);return 0
        out=handle(event,payload)
    except Exception as exc:  # Never break the host; context-bearing events show the failure on stderr.
        if event in ('sessionStart','userPromptTransformed'):sys.stderr.write('Living Memory unavailable: '+str(exc)[:300]+'\n')
        out={}
    emit(out);return 0

if __name__=='__main__':raise SystemExit(main())
