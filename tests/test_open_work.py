import contextlib,io,json,unittest
from test_core import StoreFixture
from core import runtime
from core.cli import main
from core.capture import finalize,status
from core.notes import find_note,open_work,check_action
from core.note_format import action_items,set_action,render
from core.session_start import build_context

class OpenWork(StoreFixture):
    def plan(self,title,actions,done=(),kind='plan',**kw):
        return self.note(title,render('A plan.',actions=actions,done=done),type=kind,**kw)
    def cli(self,*argv):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=main(['--root',str(self.root),'--domain','demo',*argv])
        return code,json.loads(out.getvalue() or err.getvalue())
    def project(self):return str(self.base/'project')

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
                                             'updated':result['items'][1]['updated'],'index':2,'item':'Add test'})
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
        from core.governance import undo,apply
        p=self.plan('Undo plan',['Only step'])
        result=check_action(p['id'],'demo',self.root,'only step')
        plan=undo(self.root,result['operation_id']);apply(self.root,plan['plan_id'],plan['digest'])
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

if __name__=='__main__':unittest.main()
