"""Copilot CLI native camelCase adapter, including transformed prompt retrieval.

Event names and output keys follow the Copilot CLI hooks reference:
https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-hooks-reference
"""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
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
        raw=sys.stdin.buffer.read().decode('utf-8-sig')
        payload=json.loads(raw) if raw.strip() else {}
        if not isinstance(payload,dict):raise ValueError('Hook payload must be an object')
        out=handle(event,payload)
    except Exception as exc:  # Never break the host; make the failure visible on stderr.
        sys.stderr.write('Living Memory unavailable: '+str(exc)[:300]+'\n');out={}
    sys.stdout.write(json.dumps(out,ensure_ascii=False)+'\n');return 0

if __name__=='__main__':raise SystemExit(main())
