"""Compaction: PreCompact steers the summary, SessionStart(compact) hands off, recall reaches the old transcript."""
import contextlib,io,json,os
from unittest.mock import patch

from test_core import StoreFixture
from core import runtime,recall
from core.cli import main


def claude_line(kind,content,**extra):
    return {'type':kind,'timestamp':'2026-11-08T10:00:00Z','message':{'role':kind,'content':content},**extra}

BOUNDARY={'type':'system','subtype':'compact_boundary','content':'Conversation compacted','timestamp':'2026-11-08T11:00:00Z'}


class CompactionFixture(StoreFixture):
    def setUp(self):
        super().setUp()
        self.transcript=self.base/'transcript.jsonl'
        self.write([
            claude_line('user','Why does the upload test fail?'),
            claude_line('assistant',[{'type':'thinking','thinking':'hidden reasoning about uploads'},
                                     {'type':'text','text':'The flaky upload test fails because the S3 mock returns 307 redirects.'},
                                     {'type':'tool_use','name':'Bash','input':{'command':'pytest tests/test_upload.py'}}]),
            claude_line('user',[{'type':'tool_result','tool_use_id':'x','content':'FAILED test_upload - assert 307 == 200'}]),
            claude_line('user','<system-reminder>injected upload context</system-reminder>We chose boto stubs over moto because moto is slow.'),
            claude_line('assistant',[{'type':'text','text':'subagent chatter about uploads'}],isSidechain=True),
            claude_line('user','Summary of uploads',isCompactSummary=True),
            BOUNDARY,
            claude_line('user','after the compaction: upload redirects again'),
        ])
        self.payload={'cwd':str(self.base/'project'),'session_id':'session','transcript_path':str(self.transcript)}

    def write(self,entries):
        self.transcript.write_text(''.join(json.dumps(e)+'\n' for e in entries),encoding='utf-8')

    def start(self,source='compact',harness='claude'):
        return runtime.process(harness,'session-start',{**self.payload,'source':source},self.root)

    def prompt(self,text):
        return runtime.process('claude','user-prompt',{**self.payload,'prompt':text},self.root).get('context','')


class Transcript(CompactionFixture):
    def test_records_keep_only_the_conversation_before_the_latest_compaction(self):
        rows,compacted=recall.records(self.transcript)
        self.assertTrue(compacted)
        self.assertEqual([r['role'] for r in rows],['user','assistant','tool','output','user'])
        text=' '.join(r['text'] for r in rows)
        for absent in ('hidden reasoning','injected upload context','subagent chatter','Summary of uploads','after the compaction'):
            self.assertNotIn(absent,text)
        self.assertIn('Bash pytest tests/test_upload.py',text)
        self.assertEqual(len(recall.records(self.transcript,'all')[0]),6)

    def test_excerpts_mask_private_spans_and_redact_credentials(self):
        self.write([claude_line('user','The deploy token is <private>hunter2 staging password</private> -----BEGIN RSA PRIVATE KEY----- abc'),BOUNDARY])
        hits,_=recall.search(self.transcript,'deploy token')
        self.assertEqual(len(hits),1)
        self.assertIn('[private]',hits[0]['excerpt']);self.assertIn('<redacted',hits[0]['excerpt'])
        self.assertNotIn('hunter2',hits[0]['excerpt'])

    def test_codex_rollout(self):
        self.write([{'type':'response_item','timestamp':'2026-11-08T10:00:00Z','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'<environment_context>cwd</environment_context>'}]}},
                    {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'Rename the billing worker'}]}},
                    {'type':'response_item','payload':{'type':'function_call','name':'shell','arguments':'{"command":"git mv billing.py invoices.py"}'}},
                    {'type':'response_item','payload':{'type':'function_call_output','output':'renamed'}},
                    {'type':'response_item','payload':{'type':'reasoning','summary':[]}},
                    {'type':'compacted','payload':{'message':''}}])
        rows,compacted=recall.records(self.transcript)
        self.assertTrue(compacted)
        self.assertEqual([(r['role'],r['text']) for r in rows],[('user','Rename the billing worker'),('tool','shell git mv billing.py invoices.py'),('output','renamed')])

    def test_unreadable_or_missing_transcript_gives_nothing(self):
        self.assertEqual(recall.records(self.base/'missing.jsonl'),([],False))
        self.assertEqual(recall.auto(None,'upload redirects'),(None,[]))


class Hooks(CompactionFixture):
    def test_pre_compact_records_the_compaction_and_steers_the_claude_summary(self):
        note=self.note()
        runtime._update(self.root,'session',lambda s:s.update(saved=[f"demo:{note['id']}"]))
        result=runtime.process('claude','pre-compact',{**self.payload,'trigger':'manual'},self.root)
        self.assertIn(runtime.UNCAPTURED,result['instructions'])
        self.assertIn('Cache',result['instructions'])   # the saved note, by title
        state=json.loads(runtime._pointer(self.root,'session').read_text())
        self.assertEqual(state['compactions'][-1]['trigger'],'manual');self.assertEqual(state['transcript'],str(self.transcript))
        self.assertNotIn('instructions',runtime.process('codex','pre-compact',self.payload,self.root))  # Codex ignores stdout

    def test_claude_adapter_prints_plain_instructions_and_nothing_on_error(self):
        from hooks import claude_hooks
        broken=self.base/'broken';broken.mkdir();(broken/'bindings.json').write_text('{not json')
        for root,expect in ((self.root,'Living Memory asks this summary'),(broken,'')):
            stdin=io.TextIOWrapper(io.BytesIO(json.dumps({**self.payload,'trigger':'auto'}).encode()))
            with patch.dict(os.environ,{'LIVING_MEMORY_ROOT':str(root)}),patch('sys.argv',['x','pre-compact']),patch('sys.stdin',stdin),patch('sys.stdout',new_callable=io.StringIO) as out:
                claude_hooks.main()
            self.assertTrue(out.getvalue().startswith(expect) if expect else out.getvalue()=='',out.getvalue())

    def test_compact_session_start_hands_off_saved_and_surfaced_notes(self):
        saved=self.note();surfaced=self.note('Upload pipeline','The upload pipeline retries on 307.')
        runtime._update(self.root,'session',lambda s:s.update(saved=[f"demo:{saved['id']}"],surfaced=[f"demo:{surfaced['id']}"]))
        context=self.start()['context']
        self.assertIn('Before this compaction',context)
        self.assertIn('Saved this session: demo:'+saved['id'][:8]+' Cache',context)
        self.assertIn('Surfaced earlier this session',context);self.assertIn('Upload pipeline',context)
        self.assertIn("recall 'query' --session session",context)
        self.assertLess(context.index('Before this compaction'),context.index('Living Memory context for domain'))
        self.assertNotIn('Before this compaction',self.start('startup')['context'])
        self.assertNotIn(runtime.UNCAPTURED,self.start(harness='codex')['context'])   # Codex summaries are not steered

    def test_prompt_injection_is_remembered_as_surfaced_across_session_start(self):
        note=self.note('Upload pipeline','The upload pipeline retries on 307.')
        self.start('startup');self.prompt('how does the upload pipeline retry')
        self.start('startup')   # resets shown, keeps surfaced
        state=json.loads(runtime._pointer(self.root,'session').read_text())
        self.assertIn(f"demo:{note['id']}",state['surfaced']);self.assertNotIn('shown',state)


class AutoRecall(CompactionFixture):
    def test_prompts_after_compaction_recall_matching_transcript_detail_once(self):
        self.start('startup')
        self.assertNotIn('From before compaction',self.prompt('why do upload tests fail with redirects'))  # not compacted yet
        self.start()
        first=self.prompt('why do upload tests fail with redirects')
        self.assertIn('From before compaction',first);self.assertIn('307',first)
        self.assertNotIn('S3 mock returns 307',self.prompt('why do upload tests fail with redirects'))   # each record once
        self.assertNotIn('From before compaction',self.prompt('add a dark mode toggle to settings'))

    def test_auto_recall_stops_after_the_first_prompts(self):
        self.start()
        for _ in range(runtime.AUTO_RECALL_PROMPTS):self.prompt('unrelated words entirely')
        self.assertNotIn('From before compaction',self.prompt('why do upload tests fail with redirects'))

    def test_one_word_prompts_recall_nothing(self):
        self.start()
        self.assertNotIn('From before compaction',self.prompt('upload'))


class Command(CompactionFixture):
    def cli(self,*args):
        out=io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(io.StringIO()):code=main(['--root',str(self.root),*args])
        return code,(json.loads(out.getvalue()) if out.getvalue() else None)

    def test_recall_command_searches_the_session_transcript(self):
        self.start()
        code,result=self.cli('recall','boto moto','--session','session')
        self.assertEqual(code,0);self.assertTrue(result['compacted'])
        self.assertEqual(result['results'][0]['role'],'user');self.assertIn('moto is slow',result['results'][0]['excerpt'])
        code,result=self.cli('recall','after the compaction','--session','session','--all')
        self.assertTrue(any('after the compaction' in h['excerpt'] for h in result['results']))

    def test_recall_without_a_recorded_transcript_fails_cleanly(self):
        self.assertEqual(self.cli('recall','upload','--session','nobody')[0],2)

    def test_recall_is_auto_allowed_and_open_to_subagents(self):
        self.assertIn('recall',runtime.ALLOWED_SUBCOMMANDS);self.assertNotIn('recall',runtime.MUTATING_SUBCOMMANDS)
        self.start()
        self.assertEqual(self.cli('--actor','subagent','recall','boto','--session','session')[0],0)


class Setup(StoreFixture):
    def test_pre_compact_is_wired_for_claude_and_codex_only(self):
        from integrations.setup import hooks
        self.assertEqual(hooks('claude')['PreCompact'][0]['hooks'][0]['args'][1],'pre-compact')
        self.assertIn('pre-compact',json.dumps(hooks('codex')['PreCompact']))
        self.assertNotIn('PreCompact',hooks('factory'))
        self.assertNotIn('preCompact',hooks('copilot')['hooks'])
