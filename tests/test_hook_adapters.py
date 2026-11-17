"""The harness adapters' wire behaviour: bytes in, ASCII JSON out, quiet failures (HOOK-3)."""
import io,json,sys,unittest
from unittest.mock import patch

TITLE='Cache λ 漢字'
# (module, a context event, a tool event) per adapter.
ADAPTERS=(('claude_hooks','user-prompt','subagent-start'),('codex_hooks','user-prompt','subagent-start'),('copilot_hooks','userPromptTransformed','subagentStart'))

def run(module,event,payload,stdout_encoding='cp1252'):
    """Run an adapter's main as the harness would: argv event, UTF-8 bytes on stdin, a cp1252 stdout pipe."""
    mod=__import__('hooks.'+module,fromlist=['main'])
    stdin=io.TextIOWrapper(io.BytesIO(json.dumps(payload,ensure_ascii=False).encode('utf-8')),encoding='utf-8')
    raw=io.BytesIO();stdout=io.TextIOWrapper(raw,encoding=stdout_encoding,write_through=True)
    err=io.StringIO()
    with patch('sys.argv',['x',event]),patch('sys.stdin',stdin),patch('sys.stdout',stdout),patch('sys.stderr',err):
        code=mod.main()
    return code,raw.getvalue().decode(stdout_encoding),err.getvalue()

class HookAdapters(unittest.TestCase):
    def test_non_ascii_context_survives_a_cp1252_pipe(self):
        for module,event,_ in ADAPTERS:
            with self.subTest(module=module),patch('core.runtime.process',return_value={'context':TITLE}):
                code,out,err=run(module,event,{'cwd':'.','session_id':'s','prompt':TITLE})
                self.assertEqual(code,0);self.assertEqual(err,'')
                self.assertIn(TITLE,json.dumps(json.loads(out),ensure_ascii=False))

    def test_an_import_failure_is_json_and_shown_only_on_context_events(self):
        for module,context_event,tool_event in ADAPTERS:
            with self.subTest(module=module),patch.dict(sys.modules,{'core.runtime':None}):
                code,out,err=run(module,context_event,{'cwd':'.','session_id':'s'})
                self.assertEqual(code,0)
                shown=err if module=='copilot_hooks' else json.loads(out)['systemMessage']
                self.assertIn('Living Memory unavailable',shown)
                code,out,err=run(module,tool_event,{'cwd':'.','session_id':'s'})
                self.assertEqual(code,0);self.assertEqual(err,'')
                self.assertNotIn('unavailable',out)
                if out.strip():self.assertEqual(json.loads(out),{})

if __name__=='__main__':unittest.main()
