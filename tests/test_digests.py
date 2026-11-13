"""Digest notes (remediation phase 5): `sources` provenance (path + sha256) on any note, staleness when the file changes, the STALE prefix
where a digest is injected, review and follow-through, ingest-scan's ranked worklist, and capture-key idempotency."""
import contextlib,hashlib,io,json,os,shutil,sys,unittest
from pathlib import Path
from unittest.mock import patch
from test_core import StoreFixture
from core import digests,runtime,sources
from core.cli import main
from core.notes import find_note
from core.repository import clean_sources,inventory,validate

README='# Billing Service\n\n## Retry policy\n\nFailed payment captures are retried with exponential backoff. The first retry waits 30 seconds.\n'
ADR='# 0003 Use event sourcing\n\nStatus: accepted\n\nThe ledger is an append-only event log.\n'
LEDGER='"""Ledger."""\n\ndef reconcile_ledger(db):\n    return db.read("balance_snapshot")\n'+'# padding\n'*60
MAIN='import ledger\n\ndef main():\n    ledger.reconcile_ledger(None)\n'
UTIL='def tiny():\n    return 1\n'

def sha(text):return hashlib.sha256(text.encode()).hexdigest()
def put(path,text):Path(path).write_bytes(text.encode())   # bytes as written: no newline translation, so the hash is sha(text)

class Digests(StoreFixture):
    """A bound project holding a git repo `kb/` with docs and code."""
    def setUp(self):
        super().setUp()
        self.project=self.base/'project';self.repo=self.project/'kb'
        for rel,text in {'README.md':README,'docs/adr/0003-use-event-sourcing.md':ADR,'src/ledger.py':LEDGER,'src/main.py':MAIN,'src/util.py':UTIL,
                         'docs/guide.md':'# Guide\n\nHow to run the billing service locally.\n','tests/test_ledger.py':'import ledger\n'+'# padding\n'*60}.items():
            path=self.repo/rel;path.parent.mkdir(parents=True,exist_ok=True);put(path,text)
        (self.repo/'.git').mkdir()
        self.scratch=self.base/'scratch';self.scratch.mkdir()
    def cli(self,*argv,domain='demo'):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            try:code=main(['--root',str(self.root),'--cwd',str(self.project),*(['--domain',domain] if domain else []),*argv])
            except SystemExit as exc:code=exc.code
        text=out.getvalue() or err.getvalue()
        return code,(json.loads(text) if text.strip().startswith(('{','[')) else text)
    def digest(self,path,title=None,*extra,detail='**Rule:** failed payment captures are retried with exponential backoff (README.md:5).'):
        return self.cli('capture','--title',title or f'{path}: digest','--summary','What it is.','--detail',detail,
                        '--subject','project','--concern','architecture','--source',path,'--file',path,*extra)
    def note_of(self,made):return find_note(made['id'],'demo',self.root)[1]
    def hook(self,event,session='s1',**extra):
        return runtime.process('claude',event,{'cwd':str(self.project),'session_id':session,'prompt':'go','scratchpad_dir':str(self.scratch),**extra},self.root)

class Provenance(Digests):
    def test_capture_stores_the_path_relative_to_the_bound_folder_and_the_current_hash(self):
        code,made=self.digest('kb/README.md');self.assertEqual(code,0,made)
        self.assertEqual(self.note_of(made)['sources'],[{'path':'kb/README.md','sha256':sha(README)}])
        self.assertEqual(made['status'],'created')

    def test_an_absolute_path_and_a_cwd_relative_one_store_the_same_path(self):
        code,made=self.digest(str(self.repo/'README.md'));self.assertEqual(code,0,made)
        self.assertEqual(self.note_of(made)['sources'][0]['path'],'kb/README.md')

    def test_a_path_outside_the_bound_folders_and_registered_sources_is_refused(self):
        outside=self.base/'elsewhere.md';outside.write_text('x',encoding='utf-8')
        code,error=self.digest(str(outside));self.assertNotEqual(code,0);self.assertIn('outside the bound folders',json.dumps(error))
        code,error=self.digest('kb/missing.md');self.assertNotEqual(code,0);self.assertIn('no such file',json.dumps(error))

    def test_a_file_of_a_registered_source_outside_the_bound_folders_is_named_by_its_source(self):
        wiki=self.base/'wiki';wiki.mkdir();put(wiki/'Retry-Policy.md','# Retry\n\nBackoff.\n')
        from core.repository import canonical_json
        put(self.root/'sources.json',canonical_json({'version':1,'sources':[{'name':'wiki','domain':'demo','kind':'git-wiki','path':str(wiki),'include':[],'exclude':[]}],'implicit':{}}))
        code,made=self.digest(str(wiki/'Retry-Policy.md'));self.assertEqual(code,0,made)
        self.assertEqual(self.note_of(made)['sources'][0]['path'],'wiki:Retry-Policy.md')
        self.assertEqual(digests.changed(self.note_of(made)['sources'],'demo',self.root),[])
        put(wiki/'Retry-Policy.md','# Retry\n\nChanged.\n')
        self.assertEqual(digests.changed(self.note_of(made)['sources'],'demo',self.root),['wiki:Retry-Policy.md'])

    def test_update_source_rehashes_one_path_and_keeps_the_others(self):
        code,made=self.digest('kb/README.md');ref=f"demo:{made['id']}"
        self.assertEqual(self.cli('update',ref,'--source','kb/docs/guide.md')[0],0)
        paths=[s['path'] for s in self.note_of(made)['sources']];self.assertEqual(paths,['kb/README.md','kb/docs/guide.md'])
        put(self.repo/'README.md',README+'more\n')
        self.assertEqual(self.cli('update',ref,'--source','kb/README.md')[0],0)
        found={s['path']:s['sha256'] for s in self.note_of(made)['sources']}
        self.assertEqual(found['kb/README.md'],sha(README+'more\n'));self.assertEqual(len(found),2)

    def test_sources_validation(self):
        good={'path':'a.md','sha256':'0'*64}
        self.assertEqual(clean_sources([dict(good,path='a\\b.md')]),[{'path':'a/b.md','sha256':'0'*64}])
        for bad in ('x',[{'path':'a.md'}],[dict(good,sha256='xyz')],[dict(good,sha256='A'*64)],[dict(good,path='p'*301)],[dict(good,path='')],[dict(good,extra=1)],[dict(good,path=3)],[dict(good,path=str(i)) for i in range(21)],['a.md']):
            with self.subTest(bad=str(bad)[:40]),self.assertRaises(ValueError):clean_sources(bad)
        self.assertEqual(len(clean_sources([dict(good,path=str(i)) for i in range(20)])),20)
        made=self.note('Plain note','Nothing digested.');note=self.note_of(made)
        for bad in ('x',[{'path':'a.md','sha256':'zz'}]):
            with self.subTest(note=str(bad)),self.assertRaises(ValueError):validate({**note,'sources':bad},'demo',self.root)

    def test_a_payload_capture_may_carry_sources_and_a_flag_beside_it_is_refused(self):
        body=json.dumps({'title':'Payload digest','body':'A note.\n','tags':['subject/project','concern/architecture'],'sources':[{'path':'kb/README.md','sha256':sha(README)}]})
        with patch('sys.stdin',io.StringIO(body)):code,made=self.cli('capture')
        self.assertEqual(code,0,made);self.assertEqual(self.note_of(made)['sources'][0]['path'],'kb/README.md')
        with patch('sys.stdin',io.StringIO(body)):code,error=self.cli('capture','--source','kb/README.md')
        self.assertNotEqual(code,0);self.assertIn('--source',json.dumps(error))

class Staleness(Digests):
    def setUp(self):
        super().setUp()
        code,self.made=self.digest('kb/README.md');self.assertEqual(code,0,self.made)
        self.ref=f"demo:{self.made['id']}";self.short='demo:'+self.made['id'][:8]
    def sources(self):return self.note_of(self.made)['sources']

    def test_a_changed_or_missing_file_makes_the_digest_stale(self):
        self.assertEqual(digests.changed(self.sources(),'demo',self.root),[])
        put(self.repo/'README.md',README+'new\n')
        self.assertEqual(digests.changed(self.sources(),'demo',self.root),['kb/README.md'])
        self.assertEqual(digests.stale_text(['kb/README.md']),'STALE: kb/README.md changed since this digest')
        (self.repo/'README.md').unlink()
        self.assertEqual(digests.changed(self.sources(),'demo',self.root),['kb/README.md'])
        self.assertEqual(digests.stale_text(['a','b','c','d']),'STALE: a, b, c and 1 more changed since this digest')

    def test_a_note_in_a_domain_without_bound_folders_is_never_called_stale(self):
        self.assertEqual(digests.changed(self.sources(),'_global',self.root),[])

    def test_the_prompt_hit_says_stale_until_the_digest_is_updated(self):
        prompt='how does the exponential backoff retry for failed payment captures work'
        self.assertNotIn('STALE',runtime.context(str(self.project),prompt,self.root,'demo')[0])
        put(self.repo/'README.md',README+'new\n')
        text=runtime.context(str(self.project),prompt,self.root,'demo',shown=())[0]
        self.assertIn('STALE: kb/README.md changed since this digest — ',text);self.assertIn('README.md: digest',text)

    def test_session_start_lines_carry_the_prefix(self):
        from core.session_start import build_context
        put(self.repo/'README.md',README+'new\n')
        text=build_context(str(self.project),self.root)[0]
        self.assertIn('STALE: kb/README.md changed since this digest — kb/README.md: digest',text)
        self.assertEqual(self.cli('confirm',self.short)[0],0)
        self.assertNotIn('STALE',build_context(str(self.project),self.root)[0])

    def test_a_file_hint_carries_the_prefix(self):
        put(self.repo/'README.md',README+'new\n')
        self.hook('user-prompt')
        text=self.hook('pre-tool-use',tool_name='Edit',tool_input={'file_path':'kb/README.md'})['context']
        self.assertIn('Living Memory notes about kb/README.md',text);self.assertIn('STALE: kb/README.md changed since this digest — ',text)

    def test_ask_and_search_mark_the_hit_and_hide_the_source_list(self):
        question='exponential backoff retry failed payment captures'
        code,found=self.cli('ask',question);self.assertEqual(code,0,found)
        self.assertNotIn('stale',found['notes'][0]);self.assertNotIn('sources',found['notes'][0])
        put(self.repo/'README.md',README+'new\n')
        code,found=self.cli('ask',question);hit=found['notes'][0]
        self.assertEqual(hit['stale'],'STALE: kb/README.md changed since this digest');self.assertTrue(hit['snippet'].startswith('STALE: kb/README.md'))
        self.assertNotIn('sources',hit)
        code,found=self.cli('search','exponential backoff retry');self.assertEqual(found['results'][0]['stale'],hit['stale'])

    def test_review_lists_stale_digests_and_confirm_or_update_clears_them(self):
        code,found=self.cli('review');self.assertEqual((found['due'],found['stale_digests']),(0,0))
        put(self.repo/'README.md',README+'new\n')
        code,found=self.cli('review');self.assertEqual((found['due'],found['stale_digests']),(1,1))
        self.assertEqual(found['notes'][0]['ref'],self.ref);self.assertIn('kb/README.md changed',found['notes'][0]['stale'])
        self.assertEqual(self.cli('confirm',self.ref)[0],0)
        self.assertEqual(self.sources()[0]['sha256'],sha(README+'new\n'))
        self.assertEqual(self.cli('review')[1]['due'],0)
        put(self.repo/'README.md',README+'newer\n')
        self.assertEqual(self.cli('review')[1]['stale_digests'],1)
        self.assertEqual(self.cli('update',self.ref,'--set-detail','**Rule:** backoff doubles each time.')[0],0)
        self.assertEqual(self.sources()[0]['sha256'],sha(README+'newer\n'));self.assertEqual(self.cli('review')[1]['stale_digests'],0)

    def test_a_tag_only_update_does_not_pretend_the_file_was_checked(self):
        put(self.repo/'README.md',README+'new\n')
        self.assertEqual(self.cli('update',self.ref,'--context','x')[0],0)
        self.assertEqual(self.cli('review')[1]['stale_digests'],1)

    def test_editing_a_digested_file_asks_the_digest_at_stop(self):
        self.hook('user-prompt');hint=self.hook('pre-tool-use',tool_name='Edit',tool_input={'file_path':'kb/README.md'})
        self.assertIn('Living Memory notes about kb/README.md',hint['context'])
        reason=self.hook('stop')['continue_reason']
        self.assertIn('You edited kb/README.md',reason);self.assertIn(self.short,reason);self.assertIn('CMD confirm '+self.short,reason)

    def test_a_digest_with_only_a_source_and_no_files_entry_still_follows_its_file(self):
        code,other=self.cli('capture','--title','Guide digest','--summary','How to run it.','--detail','**Run:** make run (docs/guide.md:1).','--subject','project','--concern','architecture','--source','kb/docs/guide.md')
        self.assertEqual(code,0,other);self.assertNotIn('files',self.note_of(other))
        self.hook('user-prompt');text=self.hook('pre-tool-use',tool_name='Edit',tool_input={'file_path':'kb/docs/guide.md'})['context']
        self.assertIn('Guide digest',text)
        self.assertIn('Guide digest',self.hook('stop')['continue_reason'])

class BatchedFollowThrough(Digests):
    """Digests of the files edited in one turn are asked about in one Stop sentence."""
    FILES=['kb/src/ledger.py','kb/src/main.py','kb/src/util.py','kb/src/mod1.py','kb/src/mod2.py','kb/src/mod3.py']   # code, so no document reminder takes them
    def setUp(self):
        super().setUp()
        for name in ('mod1','mod2','mod3','plain'):put(self.repo/'src'/(name+'.py'),f'def {name}():\n    return 1\n')
    def made(self,count):
        shorts={}
        for path in self.FILES[:count]:
            code,made=self.digest(path);self.assertEqual(code,0,made);shorts[path]='demo:'+made['id'][:8]
        return shorts
    def edit(self,path):
        return self.hook('pre-tool-use',tool_name='Edit',tool_input={'file_path':path})
    def state(self):return json.loads(runtime._pointer(self.root,'s1').read_text(encoding='utf-8'))

    def test_three_edited_digests_give_one_sentence_naming_all_three(self):
        shorts=self.made(3);self.hook('user-prompt')
        for path in shorts:self.edit(path)
        self.assertEqual(len(self.state()['hinted_digests']),3)
        reason=self.hook('stop')['continue_reason']
        self.assertEqual(reason.count('You edited'),1);self.assertIn('whose digests may be outdated',reason)
        for path,short in shorts.items():self.assertIn(f'{short} ({path})',reason)
        self.assertIn('CMD confirm REF --session s1',reason);self.assertIn('CMD update REF --session s1 --set-detail',reason)
        self.assertLessEqual(len(reason),runtime.STOP_TEXT_CAP)
        self.assertEqual(len(self.state()['followed']),3)

    def note_of_short(self,short):return find_note(short.split(':',1)[1],'demo',self.root)[1]

    def test_confirming_a_digest_from_the_reminder_clears_its_stale_flag(self):
        shorts=self.made(2);self.hook('user-prompt')
        for path in shorts:put(self.project/path,(self.project/path).read_text(encoding='utf-8')+'# edited\n');self.edit(path)
        self.assertEqual(self.cli('review')[1]['stale_digests'],2)
        self.assertIn('digests may be outdated',self.hook('stop')['continue_reason'])
        for short in shorts.values():self.assertEqual(self.cli('confirm',short,'--session','s1')[0],0)
        self.assertEqual(self.cli('review')[1]['stale_digests'],0)
        note=self.note_of_short(shorts[self.FILES[0]]);self.assertEqual(digests.stale_note(dict(note,domain='demo'),self.root),'')

    def test_six_edited_digests_name_five(self):
        shorts=self.made(6);self.hook('user-prompt')
        for path in shorts:self.edit(path)
        reason=self.hook('stop')['continue_reason']
        named=[s for s in shorts.values() if s+' (' in reason]
        self.assertEqual(named,list(shorts.values())[:runtime.STOP_DIGESTS]);self.assertLessEqual(len(reason),runtime.STOP_TEXT_CAP)
        self.assertEqual(len(self.state()['followed']),runtime.STOP_DIGESTS)

    def test_a_plain_note_listing_the_file_keeps_its_own_sentence(self):
        shorts=self.made(3);plain=self.note('Util gotcha','tiny returns one.',files=['kb/src/plain.py'])
        self.hook('user-prompt')
        for path in [*shorts,'kb/src/plain.py']:self.edit(path)
        reason=self.hook('stop')['continue_reason']
        self.assertIn('whose digests may be outdated',reason)
        self.assertIn(f"You edited kb/src/plain.py, which note demo:{plain['id'][:8]} Util gotcha covers",reason)
        self.assertEqual(reason.count('You edited'),2);self.assertLessEqual(len(reason),runtime.STOP_TEXT_CAP)

    def test_a_digest_changed_only_by_a_shell_command_is_not_asked_about(self):
        shorts=self.made(1);self.hook('user-prompt')
        put(self.repo/'src'/'ledger.py',LEDGER+'# changed by a script\n')
        self.hook('pre-tool-use',tool_name='Bash',tool_input={'command':'make format'})
        self.assertNotIn('hinted_digests',self.state())
        self.assertNotIn(list(shorts.values())[0],self.hook('stop').get('continue_reason',''))
        self.assertEqual(self.cli('review')[1]['stale_digests'],1)   # stays STALE for a later ingest_run.py

class Idempotency(Digests):
    def test_the_same_file_content_is_digested_once_whatever_the_wording(self):
        code,first=self.digest('kb/README.md');self.assertEqual(first['status'],'created')
        code,again=self.digest('kb/README.md','kb/README.md: another wording');self.assertEqual((code,again['status'],again['id']),(0,'existing',first['id']))
        self.assertEqual(len(inventory('demo',self.root)),1)

    def test_a_changed_file_replaces_the_old_digest_instead_of_duplicating_it(self):
        code,first=self.digest('kb/README.md')
        put(self.repo/'README.md',README+'## New section\n\nTimeouts are 5 seconds.\n')
        code,second=self.digest('kb/README.md',None,detail='**Rule:** timeouts are 5 seconds (README.md:7).');self.assertEqual((code,second['status']),(0,'created'),second)
        self.assertEqual(second['superseded'],[f"demo:{first['id']}"])
        active=[n for n in inventory('demo',self.root) if n.get('status','active')=='active'];self.assertEqual([n['id'] for n in active],[second['id']])
        self.assertEqual(digests.changed(active[0]['sources'],'demo',self.root),[])

    def test_a_changed_file_whose_digest_still_reads_true_gets_its_hash_refreshed(self):
        code,first=self.digest('kb/README.md');put(self.repo/'README.md',README+'\nMore detail.\n')
        code,second=self.digest('kb/README.md');self.assertEqual((code,second['status']),(0,'created'),second)   # same title and body: not a duplicate of the stale note
        self.assertEqual(second['superseded'],[f"demo:{first['id']}"])
        active=[n for n in inventory('demo',self.root) if n.get('status','active')=='active'];self.assertEqual([n['id'] for n in active],[second['id']])
        self.assertEqual(digests.changed(active[0]['sources'],'demo',self.root),[]);self.assertEqual(self.cli('review')[1]['stale_digests'],0)

    def test_content_that_reverts_to_an_archived_digest_is_not_answered_with_the_archived_note(self):
        code,first=self.digest('kb/README.md');put(self.repo/'README.md',README+'v2\n')
        code,second=self.digest('kb/README.md',None,detail='**Rule:** the first retry waits 30 seconds (README.md:5).');self.assertEqual(second['superseded'],[f"demo:{first['id']}"])
        put(self.repo/'README.md',README);code,third=self.digest('kb/README.md');self.assertEqual((code,third['status']),(0,'created'),third)
        self.assertNotIn(third['id'],(first['id'],second['id']));active=[n for n in inventory('demo',self.root) if n.get('status','active')=='active']
        self.assertEqual([n['id'] for n in active],[third['id']]);self.assertEqual(active[0]['sources'],[{'path':'kb/README.md','sha256':sha(README)}])

    def test_digests_of_different_files_may_have_similar_titles_but_a_plain_note_may_not_copy_one(self):
        self.digest('kb/README.md','kb/README.md: billing service overview');code,other=self.digest('kb/docs/guide.md','kb/docs/guide.md: billing service overview')
        self.assertEqual((code,other['status']),(0,'created'),other)
        code,error=self.cli('capture','--title','kb/README.md: billing service overview!','--summary','A plain note.','--subject','project','--concern','architecture')
        self.assertNotEqual(code,0);self.assertIn('possible_duplicate',json.dumps(error))

    def test_a_digest_of_other_files_does_not_replace_a_note(self):
        code,first=self.digest('kb/README.md');code,other=self.digest('kb/docs/guide.md')
        self.assertEqual(other['status'],'created');self.assertNotIn('superseded',other);self.assertEqual(len(inventory('demo',self.root)),2)

class Scan(Digests):
    def setUp(self):
        super().setUp()
        put(self.repo/'src'/'server.py','def serve():\n    pass\n'+'# padding\n'*60)
        put(self.repo/'pom.xml','<project>\n'+'<!-- padding -->\n'*40+'</project>\n')
        for rel,text in {'tests/fixtures/kb_repo/docs/adr/0003-use-event-sourcing.md':ADR,'docs/adr/README.md':'# Decision records\n\nIndex of the records in this folder.\n'}.items():
            (self.repo/rel).parent.mkdir(parents=True,exist_ok=True);put(self.repo/rel,text)
        sources.sync(self.root,'demo')
    def scan(self,*argv):
        code,found=self.cli('ingest-scan',*argv);self.assertEqual(code,0,found);return found
    def test_the_worklist_is_ranked_adrs_readmes_docs_entry_points_then_modules(self):
        items=self.scan('--limit','100')['items'];paths=[i['path'] for i in items]
        self.assertEqual(paths[0],'kb/docs/adr/0003-use-event-sourcing.md')
        self.assertEqual(paths[1:4],['kb/README.md','kb/docs/adr/README.md','kb/docs/guide.md'])
        kinds=[i['kind'] for i in items];self.assertEqual(kinds[:4],['adr','readme','readme','doc'])
        entry=[i['path'] for i in items if i['kind'] in ('entry','build')];self.assertEqual(entry,['kb/pom.xml','kb/src/main.py','kb/src/server.py'])
        self.assertLess(paths.index('kb/src/server.py'),paths.index('kb/src/ledger.py'))   # entry points before modules
        self.assertLess(paths.index('kb/src/ledger.py'),paths.index('kb/src/util.py'))      # a referenced, larger module before a tiny one
        self.assertEqual(items[paths.index('kb/src/util.py')]['kind'],'other')
        self.assertEqual(items[paths.index('kb/tests/test_ledger.py')]['kind'],'test')
        for item in items:self.assertEqual(set(item)-{'digest','file'},{'path','source','kind','size','status','abs'});self.assertTrue(Path(item['abs']).is_file())
        self.assertTrue(all(i['status']=='new' for i in items))

    def test_a_fixture_adr_is_a_test_and_an_adr_folders_readme_is_a_readme(self):
        by={i['path']:i for i in self.scan('--limit','100')['items']};paths=list(by)
        self.assertEqual(by['kb/tests/fixtures/kb_repo/docs/adr/0003-use-event-sourcing.md']['kind'],'test')
        self.assertEqual(by['kb/docs/adr/README.md']['kind'],'readme');self.assertEqual([i['path'] for i in by.values() if i['kind']=='adr'],['kb/docs/adr/0003-use-event-sourcing.md'])
        self.assertLess(paths.index('kb/docs/adr/README.md'),paths.index('kb/docs/guide.md'))
        self.assertEqual(digests.classify('tests/fixtures/pom.xml'),(5,'test'));self.assertEqual(digests.classify('docs/adr/README.md'),(1,'readme'))

    def test_references_count_other_files_that_mention_the_module(self):
        found=self.scan('--limit','100');modules=[i for i in found['items'] if i['kind']=='module'];self.assertEqual(modules[0]['path'],'kb/src/ledger.py')

    def test_status_follows_the_digests(self):
        code,made=self.digest('kb/README.md');self.digest('kb/docs/guide.md')
        put(self.repo/'docs'/'guide.md','# Guide\n\nChanged.\n')
        found=self.scan('--limit','100');by={i['path']:i for i in found['items']}
        self.assertEqual((by['kb/README.md']['status'],by['kb/README.md']['digest']),('current',f"demo:{made['id']}"))
        self.assertEqual(by['kb/docs/guide.md']['status'],'stale');self.assertIn('digest',by['kb/docs/guide.md'])
        self.assertEqual(by['kb/src/main.py']['status'],'new');self.assertNotIn('digest',by['kb/src/main.py'])
        self.assertEqual((found['counts']['current'],found['counts']['stale']),(1,1));self.assertEqual(found['total'],sum(found['counts'].values()))
        changed=self.scan('--limit','100','--changed');self.assertEqual({i['status'] for i in changed['items']},{'new','stale'});self.assertNotIn('kb/README.md',[i['path'] for i in changed['items']])

    def test_a_superseded_digest_no_longer_counts(self):
        code,made=self.digest('kb/README.md');self.cli('supersede',f"demo:{made['id']}",'--by',self.digest('kb/docs/guide.md')[1]['id'])
        by={i['path']:i for i in self.scan('--limit','100')['items']};self.assertEqual(by['kb/README.md']['status'],'new')

    def test_limit_source_and_errors(self):
        self.assertEqual(len(self.scan('--limit','3')['items']),3)
        name=next(s['name'] for s in sources.resolve(self.root,'demo'));self.assertEqual(self.scan('--source',name)['total'],self.scan()['total'])
        code,error=self.cli('ingest-scan','--source','nope');self.assertNotEqual(code,0);self.assertIn('unknown source',json.dumps(error))

    def test_no_index_says_to_sync_and_subagents_may_scan(self):
        os.unlink(sources.db_path(self.root,'demo'))
        code,error=self.cli('ingest-scan');self.assertNotEqual(code,0);self.assertIn('source-sync',json.dumps(error))
        sources.sync(self.root,'demo')
        code,found=self.cli('--actor','subagent','ingest-scan','--limit','2');self.assertEqual(code,0);self.assertIn('ingest-scan',runtime.ALLOWED_SUBCOMMANDS)
        self.assertNotEqual(self.cli('--actor','subagent','capture','--title','x','--summary','y')[0],0)

if __name__=='__main__':unittest.main()
