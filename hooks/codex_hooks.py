"""Native Codex lifecycle adapter over the shared script runtime."""
from __future__ import annotations
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.runtime import process,protected,raw_paths
from core.common import memory_root

EVENTS={'session-start':'SessionStart','user-prompt':'UserPromptSubmit','subagent-start':'SubagentStart','pre-tool-use':'PreToolUse','stop':'Stop','pre-compact':'PreCompact'}
def handle(event,payload):
    result=process('codex',event,payload)
    body={'hookEventName':EVENTS.get(event,event)};out={}
    if result.get('context'):body['additionalContext']=result['context']
    if result.get('deny'):body.update(permissionDecision='deny',permissionDecisionReason=result['deny'])
    if len(body)>1:out['hookSpecificOutput']=body
    if result.get('continue_reason'):out.update(decision='block',reason=result['continue_reason'])
    if result.get('warning'):out['systemMessage']=result['warning']
    if out or event=='stop':print(json.dumps(out,ensure_ascii=False,separators=(',',':')))
def main():
    event=sys.argv[1] if len(sys.argv)>1 else ''
    try:
        raw=sys.stdin.read();payload=json.loads(raw) if raw.strip() else {}
        if not isinstance(payload,dict):raise ValueError('Hook payload must be an object')
        handle(event,payload);return 0
    except Exception as exc:  # A memory failure must never break the host session.
        print(json.dumps({'systemMessage':'Living Memory unavailable: '+str(exc)[:300]}));return 0
if __name__=='__main__':raise SystemExit(main())
