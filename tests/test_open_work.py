import contextlib,io,json,unittest
from test_core import StoreFixture
from core import runtime
from core.cli import main
from core.capture import finalize,status
from core.notes import find_note,open_work,check_action,unresolved_bugs
from core.note_format import action_items,set_action,render,item_fields,set_item_fields
from core.session_start import build_context

class OpenWork(StoreFixture):
    def plan(self,title,actions,done=(),kind='plan',**kw):
        return self.note(title,render('A plan.',actions=actions,done=done),type=kind,**kw)
    def cli(self,*argv):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=main(['--root',str(self.root),'--domain','demo',*argv])
        return code,json.loads(out.getvalue() or err.getvalue())
    def project(self):return str(self.base/'project')

    def age(self,note,days):
        """Rewrite a note's `updated` stamp so it looks `days` days old (hooks only see canonical files)."""
        from datetime import datetime,timedelta,timezone
        from core.repository import serialize
        path,stored=find_note(note['id'],'demo',self.root)
        path.write_text(serialize(dict(stored,updated=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat(timespec='microseconds'))),encoding='utf-8')
    def start(self,session='auto',**extra):
        return runtime.process('claude','session-start',{'cwd':self.project(),'session_id':session,**extra},self.root)['context']
    def status(self,note):return find_note(note['id'],'demo',self.root)[1]['status']
    def disable_auto_resolve(self):
        p=self.root/'bindings.json';config=json.loads(p.read_text(encoding='utf-8-sig'));config['auto_resolve']=False;p.write_text(json.dumps(config),encoding='utf-8')

    def test_session_start_resolves_aged_finished_work_once_and_names_it(self):
        old=self.plan('Finished migration plan',[],done=['Move the data'])
        week=self.plan('Fixed crash bug',[],done=['Find cause','Add test'],kind='bug')
        yesterday=self.plan('Just finished plan',[],done=['Ship it'])
        partial=self.plan('Half done plan',['Second step'],done=['First step'])
        open_plan=self.plan('Not started plan',['Step one'])
        decision=self.note('A decision','We chose X.',type='decision')
        for n,days in ((old,4),(week,10),(yesterday,1),(partial,10),(open_plan,10),(decision,10)):self.age(n,days)
        text=self.start()
        self.assertEqual([self.status(n) for n in (old,week,yesterday,partial,open_plan,decision)],['resolved','resolved','active','active','active','active'])
        line=next(l for l in text.splitlines() if l.startswith('Auto-resolved'))
        self.assertTrue(line.startswith('Auto-resolved 2 finished note(s): '),line)
        for n,title in ((old,'Finished migration plan'),(week,'Fixed crash bug')):self.assertIn(f"{title} (reopen demo:{n['id'][:8]})",line)
        self.assertIn('1 note(s) have every Action Item done',text)   # the one finished yesterday keeps the old line
        self.assertNotIn('Finished migration plan',text.replace(line,''))
        later=self.plan('Another finished plan',[],done=['Done'])
        self.age(later,9)
        self.assertNotIn('Auto-resolved',self.start('second'))   # once a day per domain
        self.assertEqual(self.status(later),'active')
        marks=json.loads((self.root/'hooks/last-auto-resolve.json').read_text());self.assertEqual(list(marks),['demo'])
        self.assertEqual(self.cli('reopen','demo:'+old['id'])[0],0);self.assertEqual(self.status(old),'active')
    def test_the_resolve_is_recorded_as_use(self):
        n=self.plan('Finished migration plan',[],done=['Move the data']);self.age(n,5);self.start()
        from core.usage import events
        rows=events(self.root)
        self.assertIn(('resolve','demo:'+n['id'],'auto-resolve'),[(r['e'],r['ref'],r.get('k')) for r in rows])
    def test_the_flag_headless_runs_and_subagents_leave_finished_work_alone(self):
        from unittest.mock import patch
        n=self.plan('Finished migration plan',[],done=['Move the data']);self.age(n,5)
        self.disable_auto_resolve();text=self.start('off')
        self.assertEqual(self.status(n),'active');self.assertNotIn('Auto-resolved',text);self.assertIn('every Action Item done',text)
        self.assertFalse((self.root/'hooks/last-auto-resolve.json').exists())
        (self.root/'bindings.json').write_text((self.root/'bindings.json').read_text().replace('"auto_resolve": false','"auto_resolve": true').replace('"auto_resolve":false','"auto_resolve":true'))
        with patch.dict('os.environ',{'LIVING_MEMORY_HEADLESS':'1'}):self.start('headless')
        self.assertEqual(self.status(n),'active')
        self.start('sub',agent_id='a1');self.assertEqual(self.status(n),'active')
        self.assertFalse((self.root/'hooks/last-auto-resolve.json').exists())   # none of those used up the day
        self.assertIn('Auto-resolved 1',self.start('main'));self.assertEqual(self.status(n),'resolved')
    def test_a_simulated_clock_lets_a_headless_run_resolve_finished_work(self):
        from unittest.mock import patch
        from core.clock import OFFSET_ENV
        n=self.plan('Finished migration plan',[],done=['Move the data'])
        offset=str(4*86400)
        with patch.dict('os.environ',{'LIVING_MEMORY_HEADLESS':'1'}):self.start('headless')   # headless without an offset: still left alone
        self.assertEqual(self.status(n),'active')
        self.assertFalse((self.root/'hooks/last-auto-resolve.json').exists())
        with patch.dict('os.environ',{OFFSET_ENV:offset}):self.start('attended')   # not headless: the offset is ignored, so the note is too new
        self.assertEqual(self.status(n),'active')
        (self.root/'hooks/last-auto-resolve.json').unlink()
        with patch.dict('os.environ',{'LIVING_MEMORY_HEADLESS':'1',OFFSET_ENV:offset}):text=self.start('simulated')
        self.assertEqual(self.status(n),'resolved');self.assertIn('Auto-resolved 1',text)
        later=self.plan('Another finished plan',[],done=['Done'])
        with patch.dict('os.environ',{'LIVING_MEMORY_HEADLESS':'1',OFFSET_ENV:offset}):self.start('same-day')
        self.assertEqual(self.status(later),'active')   # the once-a-day marker counts simulated days
        with patch.dict('os.environ',{'LIVING_MEMORY_HEADLESS':'1',OFFSET_ENV:str(9*86400)}):self.start('next-days')
        self.assertEqual(self.status(later),'resolved')
    def test_a_pinned_note_is_not_auto_resolved(self):
        n=self.plan('Pinned finished plan',[],done=['Ship it'],pinned=True);self.age(n,5)
        self.assertNotIn('Auto-resolved',self.start());self.assertEqual(self.status(n),'active')
    def test_only_the_active_domain_is_resolved(self):
        from core.init_domain import init_domain
        from core.notes import capture
        (self.base/'project'/'pkg').mkdir(parents=True);init_domain('pkg',[str(self.base/'project'/'pkg')],self.root)
        kid=capture('Child finished plan',render('A plan.',actions=[],done=['Done']),['subject/project','concern/architecture'],'pkg',self.root,type='plan')
        path,stored=find_note(kid['id'],'pkg',self.root)
        from datetime import datetime,timedelta,timezone
        from core.repository import serialize
        path.write_text(serialize(dict(stored,updated=(datetime.now(timezone.utc)-timedelta(days=9)).isoformat(timespec='microseconds'))),encoding='utf-8')
        self.start();self.assertEqual(find_note(kid['id'],'pkg',self.root)[1]['status'],'active')
    def test_action_items_parse_and_flip_in_place(self):
        body=render('S.',actions=['Write tests','Ship it'],done=['Design'])
        self.assertEqual(action_items(body),[(False,'Write tests'),(False,'Ship it'),(True,'Design')])
        flipped=set_action(body,1)
        self.assertEqual(action_items(flipped),[(False,'Write tests'),(True,'Ship it'),(True,'Design')])
        self.assertEqual(flipped.replace('[x] Ship','[ ] Ship'),body)
    def test_open_lists_newest_first_and_skips_done_and_other_types(self):
        a=self.plan('Older rollout plan',['Step one'])
        b=self.plan('Newer bug fix',['Find cause','Add test'],kind='bug')
        self.plan('Finished migration plan',[],done=['All done'])
        self.note('A decision','We chose X.',type='decision')
        found=open_work(['demo'],self.root)
        self.assertEqual([n['id'] for _,n,_ in found],[b['id'],a['id']])
        self.assertEqual(found[0][2],[(1,'Find cause'),(2,'Add test')])
        code,result=self.cli('open')
        self.assertEqual(code,0,result);self.assertEqual((result['count'],result['notes']),(3,2))
        self.assertEqual(result['items'][1],{'ref':'demo:'+b['id'],'short_ref':'demo:'+b['id'][:8],'title':'Newer bug fix','type':'bug',
                                             'updated':result['items'][1]['updated'],'index':2,'item':'Add test','task':'Add test','due':None,'owner':None,'priority':None,'overdue':None})
        self.assertEqual(self.cli('--actor','subagent','open')[0],0)
    def test_check_by_text_index_and_undo(self):
        p=self.plan('Release plan',['Write the changelog','Write the release notes','Tag v2'])
        code,result=self.cli('check','demo:'+p['id'],'tag v2')
        self.assertEqual(code,0,result);self.assertEqual((result['item'],result['index'],result['open']),('Tag v2',3,2))
        code,result=self.cli('check','demo:'+p['id'],'Write the')
        self.assertEqual(code,2);self.assertIn('ambiguous_item',result['error']['message'])
        code,result=self.cli('check','demo:'+p['id'],'changelog');self.assertEqual(code,0,result)
        self.assertEqual(self.cli('check','demo:'+p['id'],'changelog')[1]['status'],'unchanged')
        code,result=self.cli('check','demo:'+p['id'],'--index','1','--undo');self.assertEqual(code,0,result)
        self.assertEqual(action_items(find_note(p['id'],'demo',self.root)[1]['body']),[(False,'Write the changelog'),(False,'Write the release notes'),(True,'Tag v2')])
        self.assertEqual(self.cli('check','demo:'+p['id'],'--index','9')[0],2)
        self.assertEqual(self.cli('check','demo:'+p['id'])[0],2)
    def test_check_is_journaled_and_undoable(self):
        from core.governance import undo
        p=self.plan('Undo plan',['Only step'])
        result=check_action(p['id'],'demo',self.root,'only step')
        undo(self.root,result['operation_id'])
        self.assertEqual(action_items(find_note(p['id'],'demo',self.root)[1]['body']),[(False,'Only step')])
    def test_check_rejects_notes_without_actions(self):
        n=self.note('Plain knowledge','A fact.')
        self.assertEqual(self.cli('check','demo:'+n['id'],'x')[0],2)
    def test_check_with_session_records_receipt(self):
        p=self.plan('Receipt plan',['Do it'])
        self.assertEqual(self.cli('check','demo:'+p['id'],'Do it','--session','s1','--turn','t1')[0],0)
        self.assertEqual(status('s1','t1',self.root)['notes'],['demo:'+p['id']])

    def test_second_capture_in_a_turn_joins_the_receipt(self):
        a=self.note('First note','A.');b=self.note('Second unrelated subject','B.')
        finalize('s1','t1',[],self.root);self.assertEqual(status('s1','t1',self.root)['outcome'],'nothing_durable')
        finalize('s1','t1',['demo:'+a['id']],self.root);finalize('s1','t1',['demo:'+b['id']],self.root)
        row=status('s1','t1',self.root)
        self.assertEqual((row['outcome'],row['notes']),('saved',['demo:'+a['id'],'demo:'+b['id']]))
        self.assertEqual(finalize('s1','t1',['demo:'+a['id']],self.root)['status'],'already_finalized')

    def test_session_start_lists_open_work(self):
        self.plan('Rollout plan',['Deploy to staging','Deploy to prod'])
        text,_,_=build_context(self.project(),self.root)
        self.assertIn('Open work (2 items in 1 note;',text);self.assertIn('Rollout plan — Deploy to staging',text)
    def test_open_work_degrades_to_the_count(self):
        for i in range(4):self.plan(f'Plan number {i} for area {i}',[f'Step {i} '+'x'*80],allow_similar=True)
        full,_,_=build_context(self.project(),self.root,10000)
        self.assertEqual(full.count('— Step'),3)
        lines=full.split('\n');start=next(i for i,l in enumerate(lines) if l.startswith('Other notes'));end=next(i for i,l in enumerate(lines) if l.startswith('Open work'))
        without_index=len('\n'.join(lines[:start]+lines[end:]))
        tight,_,_=build_context(self.project(),self.root,without_index-60)
        self.assertIn('Open work (4 items in 4 notes;',tight)
        self.assertNotIn('Other notes',tight)  # index lines go first
        self.assertLess(tight.count('— Step'),3)

    def test_last_session_line(self):
        n=self.note('Saved in session one','A.')
        payload={'cwd':self.project(),'session_id':'one','prompt':'go'}
        runtime.process('claude','session-start',payload,self.root);runtime.process('claude','user-prompt',payload,self.root)
        runtime.process('claude','pre-tool-use',{**payload,'tool_name':'Edit','tool_input':{'file_path':'a.py'}},self.root)
        state=json.loads(runtime._pointer(self.root,'one').read_text())
        finalize('one',state['turn'],['demo:'+n['id']],self.root)
        runtime.process('claude','stop',payload,self.root)
        sidecar=json.loads((self.root/'domains/demo/.last-session.json').read_text())
        self.assertEqual((sidecar['notes'],sidecar['files_changed'],sidecar['harness']),(['demo:'+n['id']],1,'claude'))
        self.assertNotIn('one',json.dumps(sidecar).replace(sidecar['session'],''))
        same=runtime.process('claude','session-start',payload,self.root)['context']
        self.assertNotIn('Last session',same)
        other=runtime.process('claude','session-start',{**payload,'session_id':'two'},self.root)['context']
        self.assertIn('Last session (',other);self.assertIn('claude): updated demo:'+n['id'][:8]+' Saved in session one; changed 1 file.',other)
    def test_no_sidecar_for_a_quiet_session(self):
        payload={'cwd':self.project(),'session_id':'quiet','prompt':'hi'}
        runtime.process('claude','user-prompt',payload,self.root);runtime.process('claude','stop',payload,self.root)
        self.assertFalse((self.root/'domains/demo/.last-session.json').exists())

    def bug(self,title,summary,*details,**kw):return self.note(title,render(summary,details=details),type='bug',**kw)
    def test_unresolved_bugs_are_active_bugs_without_a_recorded_cause_or_fix(self):
        self.bug('Sync drops the last row','Rows go missing after a sync.')
        self.bug('Cause known','Rows go missing.','**Root cause:** an off-by-one in the pager.')
        self.bug('Fixed already','Rows went missing.','**Fix:** pad the pager.')
        self.bug('Label without text','Rows went missing.','**Root cause:**')
        resolved=self.bug('Resolved bug','Old.');self.cli('resolve','demo:'+resolved['id'])
        self.note('A decision','We chose X.',type='decision')
        self.assertEqual(sorted(n['title'] for _,n in unresolved_bugs(['demo'],self.root)),['Label without text','Sync drops the last row'])

    def test_session_start_lists_unresolved_bugs_in_open_work(self):
        bug=self.bug('Sync drops the last row','Rows go missing after a sync.')
        text,_,_=build_context(self.project(),self.root)
        self.assertIn('Open work (1 bug without a root cause;',text)
        self.assertIn(f"- bug demo:{bug['id'][:8]} Sync drops the last row: no root cause recorded yet",text)
        self.assertEqual(self.cli('update','demo:'+bug['id'],'--set-detail','**Root cause:** an off-by-one in the pager.')[0],0)
        self.assertNotIn('Open work',build_context(self.project(),self.root)[0])

    def test_a_bug_with_open_action_items_says_so_on_its_item_line_and_both_are_counted(self):
        self.plan('Crash on empty file',['Find the cause'],kind='bug');self.plan('Rollout plan',['Deploy'])
        self.bug('Sync drops the last row','Rows go missing after a sync.')
        text,_,_=build_context(self.project(),self.root)
        self.assertIn('Open work (2 items in 2 notes, 2 bugs without a root cause;',text)
        self.assertIn('Crash on empty file — Find the cause (no root cause recorded yet)',text)
        self.assertIn('Sync drops the last row: no root cause recorded yet',text)
        self.assertNotIn('Rollout plan — Deploy (no root',text)

    def test_a_bug_recorded_as_a_pitfall_is_a_gotcha_not_open_work(self):
        self.bug('Never run mvnw.cmd from Git Bash','It exits 0 and builds nothing. Run ./mvnw instead.')
        text,_,_=build_context(self.project(),self.root)
        self.assertIn('Known gotchas',text);self.assertNotIn('Open work',text)

    def test_unexplained_bug_lines_are_the_first_thing_to_go_when_the_budget_is_tight(self):
        self.plan('Rollout plan',['Deploy to staging'])
        for i in range(3):self.bug(f'Unexplained bug number {i} in area {i}',f'The exporter output differs in area {i}.',allow_similar=True)
        trace={};full,_,_=build_context(self.project(),self.root,10000,trace=trace)
        self.assertEqual(full.count('no root cause recorded yet'),2)   # at most OPEN_BUGS lines
        self.assertIn('Open work (1 item in 1 note, 3 bugs without a root cause;',full);self.assertEqual((trace['degrade']['opened'],trace['degrade']['open_of']),(3,3))
        lines=full.split('\n');start=next(i for i,l in enumerate(lines) if l.startswith('Open work'))
        without_bugs=len('\n'.join(lines[:start+2]+lines[start+4:]))
        trace={};tight,_,_=build_context(self.project(),self.root,without_bugs,trace=trace)
        self.assertNotIn('no root cause recorded yet',tight)
        self.assertIn('Rollout plan — Deploy to staging',tight);self.assertEqual(trace['degrade']['opened'],1)
        self.assertIn('Other notes',tight)   # the bug lines went before the index did

    def test_with_bugs_only_and_no_room_the_section_goes_rather_than_leaving_a_bare_header(self):
        self.bug('Sync drops the last row','Rows go missing after a sync.')
        full,_,_=build_context(self.project(),self.root,10000);self.assertIn('Open work (1 bug',full)
        lines=full.split('\n');start=next(i for i,l in enumerate(lines) if l.startswith('Open work'))
        tight,_,_=build_context(self.project(),self.root,len('\n'.join(lines[:start]+lines[start+2:])))
        self.assertNotIn('Open work',tight);self.assertIn('Sync drops the last row',tight)   # still indexed

    def test_follow_through_on_an_open_bug_suggests_recording_the_fix_and_resolving(self):
        bug=self.bug('Lexer crash on empty file','src/lexer.py raises IndexError on an empty file.',files=['src/lexer.py'])
        payload={'cwd':self.project(),'session_id':'bug','prompt':'fix the empty file crash'}
        runtime.process('claude','session-start',payload,self.root);runtime.process('claude','user-prompt',payload,self.root)
        runtime.process('claude','pre-tool-use',{**payload,'tool_name':'Edit','tool_input':{'file_path':str(self.base/'project'/'src'/'lexer.py')}},self.root)
        reason=runtime.process('claude','stop',payload,self.root)['continue_reason']
        s='demo:'+bug['id'][:8]
        self.assertIn(f"CMD update {s} --session bug --set-detail '**Fix:** ...' then CMD resolve {s}",reason)

    # Optional due, owner and priority at the end of an action item (CON-1).
    def test_item_fields_reads_a_tail_at_the_end_of_the_text(self):
        none={'due':None,'owner':None,'priority':None}
        self.assertEqual(item_fields('Ship it (due: 2026-12-01, owner: me, priority: high)'),('Ship it',{'due':'2026-12-01','owner':'me','priority':'high'},[]))
        self.assertEqual(item_fields('Ship it (Priority: HIGH, Owner: Dana Lee, DUE: 2026-12-01)'),('Ship it',{'due':'2026-12-01','owner':'Dana Lee','priority':'high'},[]))
        self.assertEqual(item_fields('Ship it (owner: me)'),('Ship it',{**none,'owner':'me'},[]))
        self.assertEqual(item_fields('Ship it'),('Ship it',none,[]))
        for text in ('Ship it (Codex INST-7.1)','Ship it (due: 2026-12-01, note: x)','Ship it (due: 2026-12-01) and test','Ship it (due: 2026-12-01) later (see docs)'):
            self.assertEqual(item_fields(text),(text,none,[]),text)   # not a tail: plain text
        self.assertEqual(item_fields('Ship it (see docs) (due: 2026-12-01)'),('Ship it (see docs)',{**none,'due':'2026-12-01'},[]))
        task,fields,problems=item_fields('Ship it (due: 2026-13-45)');self.assertEqual((task,fields['due'],len(problems)),('Ship it',None,1));self.assertIn('YYYY-MM-DD',problems[0])
        self.assertIsNone(item_fields('Ship it (due: 12/01/2026)')[1]['due'])
        task,fields,problems=item_fields('Ship it (priority: urgent, owner: me)');self.assertEqual((fields['priority'],fields['owner'],len(problems)),(None,'me',1));self.assertIn('highest, high, medium, low, lowest',problems[0])
        task,fields,problems=item_fields('Ship it (owner: me, owner: you)');self.assertEqual((fields['owner'],len(problems)),('me',1));self.assertIn('more than once',problems[0])
    def test_set_item_fields_rewrites_one_line_only(self):
        body=render('A plan.',details=['A fact.'],actions=['First (owner: me)','Second'],done=['Third (due: 2026-01-01)'])
        after=set_item_fields(body,0,{'due':'2026-12-01','owner':None});self.assertIn('- [ ] First (due: 2026-12-01)',after)
        self.assertEqual(after.replace('First (due: 2026-12-01)','First (owner: me)'),body)   # nothing else moved
        self.assertIn('- [ ] Second (priority: low)',set_item_fields(body,1,{'priority':'LOW'}))
        self.assertIn('- [x] Third (due: 2026-01-01, owner: Dana)',set_item_fields(body,2,{'owner':'Dana'}))
        self.assertIn('- [ ] First\n',set_item_fields(body,0,{'owner':None}))   # an empty tail is dropped
        for bad in ({'due':'soon'},{'priority':'urgent'},{'owner':'a, b'},{'owner':''},{'size':'big'}):
            with self.assertRaises(ValueError):set_item_fields(body,0,bad)
    def dated(self):
        a=self.plan('Release plan',['Ship the release (due: 2099-01-01, owner: me)','Write notes (due: 2020-03-01, owner: Dana, priority: high)','Tidy up'])
        b=self.plan('Bug fix plan',['Find the cause (due: 2020-02-01)','Add a test (owner: me)'],kind='bug')
        return a,b
    def test_open_rows_carry_the_fields_and_overdue(self):
        self.dated()
        code,result=self.cli('open');self.assertEqual(code,0,result)
        rows={r['task']:r for r in result['items']}
        self.assertEqual(rows['Write notes']['item'],'Write notes (due: 2020-03-01, owner: Dana, priority: high)')
        self.assertEqual({k:rows['Write notes'][k] for k in ('due','owner','priority','overdue')},{'due':'2020-03-01','owner':'Dana','priority':'high','overdue':True})
        self.assertEqual((rows['Ship the release']['overdue'],rows['Ship the release']['owner']),(False,'me'))
        self.assertEqual({k:rows['Tidy up'][k] for k in ('due','owner','priority','overdue')},{'due':None,'owner':None,'priority':None,'overdue':None})
    def test_open_filters_sorts_and_limits(self):
        self.dated()
        tasks=lambda *argv:[r['task'] for r in self.cli('open',*argv)[1]['items']]
        self.assertEqual(tasks('--sort','due'),['Find the cause','Write notes','Ship the release','Add a test','Tidy up'])   # undated last, ties newest note first
        self.assertEqual(tasks('--due-by','2020-02-15'),['Find the cause']);self.assertEqual(tasks('--due-by','2020-03-01','--sort','due'),['Find the cause','Write notes'])
        self.assertEqual(tasks('--due-by','2019-01-01'),[])
        self.assertEqual(tasks('--owner','ME','--sort','due'),['Ship the release','Add a test'])
        self.assertEqual(tasks('--owner','me','--due-by','2099-12-31'),['Ship the release'])
        code,result=self.cli('open','--due-by','2020-03-01','--sort','due','--limit','1');self.assertEqual((result['count'],result['notes'],[r['task'] for r in result['items']]),(2,2,['Find the cause']))
        code,result=self.cli('open','--owner','me');self.assertEqual((result['count'],result['notes']),(2,2))
        self.assertEqual(self.cli('open','--due-by','soon')[0],2)
    def test_done_items_are_never_listed_with_their_fields(self):
        p=self.plan('Release plan',['Ship it (due: 2020-01-01)'])
        self.assertEqual(self.cli('check','demo:'+p['id'],'Ship it')[0],0)
        self.assertEqual(self.cli('open','--due-by','2030-01-01')[1]['count'],0)
    def items(self,note):return action_items(find_note(note['id'],'demo',self.root)[1]['body'])
    def test_update_item_sets_changes_and_clears_fields(self):
        p=self.plan('Release plan',['Write the changelog','Write the release notes (owner: me)'],done=['Tag v2']);ref='demo:'+p['id']
        code,result=self.cli('update',ref,'--item','changelog','--due','2026-12-01','--priority','High');self.assertEqual(code,0,result)
        self.assertEqual(self.items(p)[0],(False,'Write the changelog (due: 2026-12-01, priority: high)'))
        code,result=self.cli('update',ref,'--item-index','2','--owner','Dana','--due','2027-01-02');self.assertEqual(code,0,result)
        self.assertEqual(self.items(p)[1],(False,'Write the release notes (due: 2027-01-02, owner: Dana)'))
        code,result=self.cli('update',ref,'--item','changelog','--due','none','--priority','none');self.assertEqual(code,0,result)
        self.assertEqual(self.items(p),[(False,'Write the changelog'),(False,'Write the release notes (due: 2027-01-02, owner: Dana)'),(True,'Tag v2')])
        self.assertEqual(self.cli('update',ref,'--item','notes','--owner','none','--due','none')[0],0)
        self.assertEqual(self.items(p)[1],(False,'Write the release notes'))
    def test_update_item_is_refused_without_a_clear_target_or_field(self):
        p=self.plan('Release plan',['Write the changelog','Write the release notes']);ref='demo:'+p['id'];body=find_note(p['id'],'demo',self.root)[1]['body']
        for argv,fragment in ((('--item','Write the','--due','2026-12-01'),'ambiguous_item'),(('--item','nothing','--due','2026-12-01'),'No Action Item matches'),(('--item-index','9','--due','2026-12-01'),'No Action Item 9'),
                              (('--item','changelog'),'--due, --owner or --priority'),(('--due','2026-12-01'),'--item'),(('--item','changelog','--item-index','1','--due','2026-12-01'),'not both'),
                              (('--item','changelog','--due','soon'),'YYYY-MM-DD'),(('--item','changelog','--priority','urgent'),'priority must be'),
                              (('--item','changelog','--due','2026-12-01','--action','New step'),'--action'),(('--item','changelog','--due','2026-12-01','--done','Old'),'--done'),
                              (('--item','changelog','--due','2026-12-01','--replace-actions'),'--replace-actions')):
            code,result=self.cli('update',ref,*argv);self.assertEqual(code,2,(argv,result));self.assertIn(fragment,result['error']['message'],argv)
        self.assertEqual(find_note(p['id'],'demo',self.root)[1]['body'],body)
        d=self.note('A decision','We chose X.',type='decision');code,result=self.cli('update','demo:'+d['id'],'--item','x','--due','2026-12-01')
        self.assertEqual(code,2,result);self.assertIn('no action items',result['error']['message'])
    def test_update_item_is_one_journaled_undoable_update_that_leaves_the_rest_alone(self):
        p=self.note('Release plan',render('A plan.',details=['A fact.'],links=['[[Other]]'],actions=['Write the changelog','Write the release notes']),type='plan')
        ref='demo:'+p['id'];before=find_note(p['id'],'demo',self.root)[1]['body']
        code,result=self.cli('update',ref,'--item','changelog','--due','2026-12-01');self.assertEqual(code,0,result);self.assertTrue(result.get('operation_id'),result)
        self.assertEqual(find_note(p['id'],'demo',self.root)[1]['body'],before.replace('Write the changelog','Write the changelog (due: 2026-12-01)'))
        code,undone=self.cli('undo',result['operation_id']);self.assertEqual(code,0,undone);self.assertEqual(find_note(p['id'],'demo',self.root)[1]['body'],before)
    def test_item_field_problems_are_quality_warnings_on_capture_and_update(self):
        warned=lambda result:[w for w in result.get('quality_warnings',[]) if w.startswith('action item')]
        code,result=self.cli('capture','--type','plan','--title','Dated plan','--summary','A plan.','--concern','architecture','--action','Ship it (due: 2026-13-45)','--action','Write it (priority: urgent)','--action','Fine (due: 2026-12-01)')
        self.assertEqual(code,0,result);self.assertEqual(len(warned(result)),2,warned(result));self.assertIn("action item 'Ship it'",warned(result)[0]);self.assertIn('not a YYYY-MM-DD date',warned(result)[0])
        ref='demo:'+result['id']
        code,result=self.cli('update',ref,'--add-action','Late (owner: a, owner: b)');self.assertEqual(code,0,result);self.assertEqual(len(warned(result)),3)
        code,result=self.cli('update',ref,'--item','Ship it','--due','2026-12-01');self.assertEqual(code,0,result);self.assertEqual(len(warned(result)),2)
    def test_session_start_counts_and_names_overdue_items_oldest_first(self):
        from datetime import date
        from unittest.mock import patch
        self.plan('Release plan',['Ship the release (due: 2099-01-01)','Tidy up'])
        self.plan('Docs plan',['Find the cause (due: 2026-11-01)','Add a test (due: 2026-10-01)','Write docs (due: 2026-11-20)','Review (due: 2026-10-15)'])
        with patch('core.clock.today',return_value=date(2026,11,22)):text=self.start('overdue')
        self.assertIn('Open work (6 items in 2 notes, 4 overdue;',text)
        line=next(l for l in text.splitlines() if l.startswith('Overdue:'))
        self.assertTrue(line.startswith('Overdue: Add a test (due 2026-10-01) demo:'),line)
        self.assertEqual(line.count(' (due '),3);self.assertIn('Review (due 2026-10-15)',line);self.assertIn('Find the cause (due 2026-11-01)',line);self.assertNotIn('Write docs',line)
        self.assertLess(text.index('Open work'),text.index('Overdue:'));self.assertLess(text.index('Overdue:'),text.index('Docs plan — Find'))
        with patch('core.clock.today',return_value=date(2026,9,1)):text=self.start('nothing-late')
        self.assertNotIn('Overdue:',text);self.assertNotIn('overdue',text)
    def test_a_simulated_clock_makes_a_future_item_overdue(self):
        from datetime import date,timedelta
        from unittest.mock import patch
        from core.clock import OFFSET_ENV
        self.plan('Release plan',[f'Ship it (due: {(date.today()+timedelta(days=3)).isoformat()})'])
        self.assertNotIn('overdue',self.start('now'))
        with patch.dict('os.environ',{'LIVING_MEMORY_HEADLESS':'1',OFFSET_ENV:str(5*86400)}):text=self.start('later')
        self.assertIn('1 overdue',text);self.assertIn('Overdue: Ship it (due ',text)
    def test_version_reports_the_item_fields_capability(self):
        code,result=self.cli('version');self.assertEqual(code,0,result);self.assertEqual(result['capabilities']['items'],{'fields':1})

if __name__=='__main__':unittest.main()
