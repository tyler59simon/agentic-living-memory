"""Every CLI write to an existing note carries the revision its caller read (the user's rule, 2026-11-26): a missing one is
refused with revision_required, an old one with stale (exit 6), and each receipt returns the new revisions. These tests call
core.cli.main directly, without tests/revision_fill, so nothing fills the revision in for them."""
import contextlib,io,json,subprocess,sys,threading,unittest
from pathlib import Path
from core.cli import main
from core.notes import find_note
from core.repository import digest
from core.init_domain import init_domain
from test_core import StoreFixture

STALE='0'*64


class Fixture(StoreFixture):
    def setUp(self):
        super().setUp()
        (self.root/'tag_taxonomy.json').write_text(json.dumps({'tags':['subject/project','concern/architecture','concern/decision','concern/knowledge','concern/workflow']}))
    def cli(self,*argv):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            try:code=main(['--root',str(self.root),'--domain','demo',*argv])
            except SystemExit as exc:code=exc.code
        text=out.getvalue() or err.getvalue()
        return code,json.loads(text)
    def rev(self,note):return find_note(note['id'],'demo',self.root)[1]['revision']
    def ref(self,note):return 'demo:'+note['id']
    def plan(self,title='Ship it'):
        return self.note(title,'Ship the change.\n\n## Action Items\n\n- [ ] Write the tests\n',type='plan')


class EachWriteNeedsTheRevision(Fixture):
    def commands(self):
        """(name, argv without a revision, argv with a stale one) for each command that writes the note it names."""
        n=self.note();p=self.plan();done=self.plan('Finished work')
        main_args=[
            ('update',['update',self.ref(n),'--summary','The cache can be rebuilt.']),
            ('archive',['archive',self.ref(n)]),
            ('resolve',['resolve',self.ref(p)]),
            ('confirm',['confirm',self.ref(n)]),
            ('check',['check',self.ref(p),'Write the tests']),
            ('delete',['delete',self.ref(done)]),
        ]
        return main_args,n,p,done

    def test_a_write_without_a_revision_is_refused_and_writes_nothing(self):
        argvs,n,p,_=self.commands()
        for name,argv in argvs:
            with self.subTest(name):
                before=(self.rev(n),self.rev(p))
                code,result=self.cli(*argv)
                self.assertEqual((code,result['error']['code']),(2,'revision_required'),result)
                self.assertIn('read ',result['error']['next'])
                self.assertEqual((self.rev(n),self.rev(p)),before)

    def test_a_write_with_an_old_revision_is_stale_and_writes_nothing(self):
        argvs,n,p,_=self.commands()
        for name,argv in argvs:
            with self.subTest(name):
                before=(self.rev(n),self.rev(p))
                code,result=self.cli(*argv,'--expected-revision',STALE)
                self.assertEqual((code,result['error']['code']),(6,'stale'),result)
                self.assertTrue(result['error']['message'].startswith('stale_revision: '))
                self.assertEqual((self.rev(n),self.rev(p)),before)

    def test_reopen_and_restore_need_the_revision_too(self):
        p=self.plan();self.assertEqual(self.cli('resolve',self.ref(p),'--expected-revision',self.rev(p))[0],0)
        self.assertEqual(self.cli('reopen',self.ref(p))[1]['error']['code'],'revision_required')
        self.assertEqual(self.cli('reopen',self.ref(p),'--expected-revision',self.rev(p))[0],0)
        n=self.note();self.assertEqual(self.cli('archive',self.ref(n),'--expected-revision',self.rev(n))[0],0)
        self.assertEqual(self.cli('restore',self.ref(n))[1]['error']['code'],'revision_required')
        self.assertEqual(self.cli('restore',self.ref(n),'--expected-revision',self.rev(n))[0],0)

    def test_a_payload_revision_counts_and_a_twelve_character_prefix_is_enough(self):
        n=self.note()
        code,result=self.cli('update',self.ref(n),'--expected-revision',self.rev(n)[:12],'--summary','The cache can be rebuilt.')
        self.assertEqual(code,0,result)
        short=self.cli('update',self.ref(n),'--expected-revision',self.rev(n)[:11],'--summary','Shorter.')
        self.assertEqual(short[0],2);self.assertIn('invalid revision',short[1]['error']['message'])
        payload=self.base/'update.json'
        payload.write_text(json.dumps({'body':'Rebuilt from the payload.'}))
        self.assertEqual(self.cli('update',self.ref(n),'--payload-file',str(payload))[1]['error']['code'],'revision_required')
        payload.write_text(json.dumps({'body':'Rebuilt from the payload.','expected_revision':self.rev(n)}))
        self.assertEqual(self.cli('update',self.ref(n),'--payload-file',str(payload))[0],0)

    def test_the_receipt_returns_the_new_revision_of_each_note_it_wrote(self):
        n=self.note();p=self.plan()
        written=self.cli('update',self.ref(n),'--expected-revision',self.rev(n),'--summary','The cache can be rebuilt.')[1]
        self.assertEqual(written['revisions'],{self.ref(n):self.rev(n)})
        self.assertEqual(written['revisions'][self.ref(n)],digest(find_note(n['id'],'demo',self.root)[0].read_bytes()))
        ticked=self.cli('check',self.ref(p),'Write the tests','--expected-revision',self.rev(p))[1]
        self.assertEqual(ticked['revisions'],{self.ref(p):self.rev(p)})
        # The next write can pass the receipt's revision without reading again.
        again=self.cli('update',self.ref(n),'--expected-revision',written['revisions'][self.ref(n)],'--summary','Rebuilt nightly.')
        self.assertEqual(again[0],0,again[1])

    def test_the_receipt_covers_a_note_in_a_nested_folder(self):
        n=self.note();path=find_note(n['id'],'demo',self.root)[0]
        nested=path.parent/'old'/path.name;nested.parent.mkdir();path.rename(nested)
        written=self.cli('update',self.ref(n),'--expected-revision',self.rev(n),'--summary','The cache can be rebuilt.')[1]
        self.assertEqual(written['revisions'],{self.ref(n):digest(nested.read_bytes())})

    def test_the_python_calls_need_revisions_too(self):
        from core import governance
        target=self.note('Cache policy');source=self.note('Cache rebuild')
        with self.assertRaisesRegex(ValueError,'revision_required'):
            governance.merge(self.root,{'target':self.ref(target),'sources':[self.ref(source)+'@'+self.rev(source)],'body':'Merged.'})
        with self.assertRaisesRegex(ValueError,'revision_required'):
            governance.merge(self.root,{'target':self.ref(target)+'@'+self.rev(target),'sources':[self.ref(source)],'body':'Merged.'})
        with self.assertRaisesRegex(ValueError,'revision_required'):governance.promote(self.root,[self.ref(target)])
        init_domain('other',[str(self.base/'other')],self.root)
        with self.assertRaisesRegex(ValueError,'revision_required'):
            governance.remap(self.root,[{'before':{'domain':'demo','id':target['id']},'after':{'domain':'other','id':target['id']}}])
        self.assertEqual(find_note(target['id'],'demo',self.root)[1]['status'],'active')


class NotesACommandAlsoWrites(Fixture):
    def test_supersede_needs_both_revisions(self):
        old=self.note();new=self.note('Cache two','The cache has two tiers.')
        self.assertEqual(self.cli('supersede',self.ref(old),'--by',self.ref(new)+'@'+self.rev(new))[1]['error']['code'],'revision_required')
        self.assertEqual(self.cli('supersede',self.ref(old),'--expected-revision',self.rev(old),'--by',self.ref(new))[1]['error']['code'],'revision_required')
        stale=self.cli('supersede',self.ref(old),'--expected-revision',self.rev(old),'--by',self.ref(new)+'@'+STALE)
        self.assertEqual(stale[0],6,stale[1]);self.assertEqual(find_note(old['id'],'demo',self.root)[1]['status'],'active')
        code,result=self.cli('supersede',self.ref(old),'--expected-revision',self.rev(old),'--by',self.ref(new)+'@'+self.rev(new))
        self.assertEqual(code,0,result);self.assertEqual(find_note(old['id'],'demo',self.root)[1]['status'],'superseded')

    def test_capture_supersedes_needs_the_old_notes_revision(self):
        old=self.note()
        base=('capture','--title','Cache has two tiers','--summary','A hot tier sits in front.','--type','decision','--section','Why','Latency.')
        self.assertEqual(self.cli(*base,'--supersedes',self.ref(old))[1]['error']['code'],'revision_required')
        self.assertEqual(self.cli(*base,'--supersedes',self.ref(old)+'@'+STALE)[0],6)
        code,result=self.cli(*base,'--supersedes',self.ref(old)+'@'+self.rev(old))
        self.assertEqual(code,0,result);self.assertIn(self.ref(old),result['revisions'])

    def test_promote_and_remap_need_each_notes_revision(self):
        n=self.note()
        self.assertEqual(self.cli('promote',self.ref(n))[1]['error']['code'],'revision_required')
        self.assertEqual(self.cli('promote',self.ref(n)+'@'+STALE)[0],6)
        init_domain('other',[str(self.base/'other')],self.root)
        mapping={'before':{'domain':'demo','id':n['id']},'after':{'domain':'other','id':n['id']}}
        payload=self.base/'remap.json';payload.write_text(json.dumps([mapping]))
        self.assertEqual(self.cli('remap','--payload-file',str(payload))[1]['error']['code'],'revision_required')
        payload.write_text(json.dumps([{**mapping,'revision':STALE}]));self.assertEqual(self.cli('remap','--payload-file',str(payload))[0],6)
        payload.write_text(json.dumps([{**mapping,'revision':self.rev(n)}]));self.assertEqual(self.cli('remap','--payload-file',str(payload))[0],0)

    def test_listings_carry_revisions(self):
        p=self.plan()
        item=self.cli('open')[1]['items'][0];self.assertEqual(item['revision'],self.rev(p))
        row=next(r for r in self.cli('timeline')[1]['notes'] if r['ref']==self.ref(p));self.assertEqual(row['revision'],self.rev(p))
        self.assertEqual(self.cli('read',self.ref(p),'--metadata-only')[1]['revision'],self.rev(p))


class ConcurrentWriters(Fixture):
    def test_two_writers_with_the_same_read_one_wins_and_the_other_is_stale(self):
        n=self.note();read=self.rev(n);gate=threading.Barrier(2);codes=[]
        script=Path(__file__).resolve().parents[1]/'living_memory.py'
        def writer(text):
            # Two processes, as two agents would be: redirect_stdout is shared by threads in one process.
            gate.wait()
            done=subprocess.run([sys.executable,str(script),'--root',str(self.root),'--domain','demo','--json','update',self.ref(n),'--expected-revision',read,'--add-detail',text],
                                capture_output=True,text=True,encoding='utf-8')
            codes.append(done.returncode)
        threads=[threading.Thread(target=writer,args=(f'**Writer {i}:** edit {i}',)) for i in (1,2)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(sorted(codes),[0,6])
        body=find_note(n['id'],'demo',self.root)[1]['body']
        self.assertEqual(sum(f'edit {i}' in body for i in (1,2)),1)


if __name__=='__main__':
    unittest.main()


class BulkWritesNeedRevisions(Fixture):
    def test_merge_needs_the_revision_of_the_target_and_every_source_and_undo_reverses_it(self):
        target=self.note('Cache policy');source=self.note('Cache rebuild')
        payload=lambda t,s:json.dumps({'target':t,'sources':[s],'body':'The cache can be rebuilt from the notes.'})
        path=self.root/'merge.json'
        path.write_text(payload(self.ref(target),self.ref(source)+'@'+self.rev(source)),encoding='utf-8')
        code,result=self.cli('merge','--payload-file',str(path));self.assertEqual((code,result['error']['code']),(2,'revision_required'),result)
        path.write_text(payload(self.ref(target)+'@'+self.rev(target),self.ref(source)+'@'+STALE),encoding='utf-8')
        code,result=self.cli('merge','--payload-file',str(path));self.assertEqual((code,result['error']['code']),(6,'stale'),result)
        before=(self.rev(target),self.rev(source))
        path.write_text(payload(self.ref(target)+'@'+self.rev(target),self.ref(source)+'@'+self.rev(source)),encoding='utf-8')
        code,result=self.cli('merge','--payload-file',str(path));self.assertEqual(code,0,result)
        self.assertTrue(result['operation_id'].startswith('d-'));self.assertIn('demo:'+target['id'],result['revisions'])
        self.assertEqual(find_note(source['id'],'demo',self.root)[1]['status'],'superseded')
        code,undone=self.cli('undo',result['operation_id']);self.assertEqual(code,0,undone)
        self.assertEqual((self.rev(target),self.rev(source)),before)

    def test_delete_is_direct_and_undo_brings_the_note_back(self):
        n=self.note();revision=self.rev(n)
        code,result=self.cli('delete',self.ref(n),'--expected-revision',revision);self.assertEqual(code,0,result)
        self.assertEqual(result['undo'],'undo '+result['operation_id'])
        with self.assertRaises(ValueError):find_note(n['id'],'demo',self.root)
        self.assertEqual(self.cli('undo',result['operation_id'])[0],0);self.assertEqual(self.rev(n),revision)
