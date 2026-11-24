"""Folder notebook page proposals (CON-9) and the one proposals command (CON-25): `doc read --file` and `doc propose --file`
for a registered folder of Markdown outside the store, accept and undo that put the bytes back, and `proposals` for staged
notes and page edits. Design: docs/notebooks-design.md, NOTEBOOKS.md."""
import base64,json,os,unittest
from pathlib import Path
from unittest.mock import patch
from docfixture import Fixture,PAGE,junction
from core import docs,folder_docs,governance
from core.repository import digest,read_json


class Folder(Fixture):
    def setUp(self):
        super().setUp()
        self.nb=self.base/'wiki';self.nb.mkdir()
        spawn=patch('core.folder_policy._spawn');spawn.start();self.addCleanup(spawn.stop)   # no background source-sync in a unit test
        self.set()
        self.nid=folder_docs.notebook_id(self.nb)
    def set(self,*extra,folder=None):self.ok('doc','folder','set',str(folder or self.nb),*extra)
    def put(self,rel,data):
        path=self.nb/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);return path
    def read(self,rel='a.md'):return self.ok('doc','read','--file',str(self.nb/rel))
    def propose(self,rel,body,base=True,actor='subagent',**kw):
        args=['doc','propose','--file',str(self.nb/rel),'--body-file',self.body_file(body)]
        if base and (self.nb/rel).exists():args+=['--base-revision',digest((self.nb/rel).read_bytes())]
        return self.ok(*args,actor=actor,**kw)
    def record(self,pid):return read_json(self.root/'domains'/'demo'/'folder-proposals'/self.nid/f'{pid}.json')
    def listing(self,*extra):return self.ok('proposals','--kind','page',*extra)['proposals']


class ReadAndPropose(Folder):
    def test_read_a_registered_page_and_stage_an_edit(self):
        self.put('a.md',PAGE)
        read=self.read()
        self.assertEqual((read['origin'],read['domain'],read['notebook'],read['path']),('folder','demo',self.nid,'a.md'))
        self.assertEqual((read['revision'],read['size']),(digest(PAGE),len(PAGE)));self.assertEqual(read['text'].encode(),PAGE)
        self.assertEqual(read['file'],(self.nb.resolve()/'a.md').as_posix());self.assertEqual(read['folder'],self.nb.resolve().as_posix())
        new=PAGE+b'Agent line\r\n'
        staged=self.ok('doc','propose','--file',str(self.nb/'a.md'),'--base-revision',read['revision'],'--note','add a line','--body-file',self.body_file(new),actor='subagent')
        self.assertEqual(self.nb.joinpath('a.md').read_bytes(),PAGE)   # nothing written to the page
        value=self.record(staged['id'])
        self.assertEqual((value['kind'],value['schema'],value['status'],value['path'],value['note']),('folder',1,'pending','a.md','add a line'))
        self.assertEqual((value['base_revision'],base64.b64decode(value['base'])),(digest(PAGE),PAGE));self.assertEqual(value['body'].encode(),new)
        self.assertEqual((value['notebook'],value['folder']),(self.nid,self.nb.resolve().as_posix()))
        journal=self.journal(staged['operation_id']);self.assertEqual(journal['kind'],'doc_propose');self.assertTrue(journal['meta']['folder_notebook'])
        self.assertFalse((self.root/'domains'/'demo'/'docs.sqlite3').exists())   # a folder notebook has no store index to refresh

    def test_markdown_extensions_only_and_only_registered_folders(self):
        self.put('a.markdown',b'one');self.put('a.txt',b'two')
        self.ok('doc','read','--file',str(self.nb/'a.markdown'))
        for target in (self.nb/'a.txt',self.base/'elsewhere.md',self.base/'wiki'/'missing'/'x.markdown'):
            with self.subTest(target=target):
                code,text=self.fails('doc','read','--file',str(target));self.assertIn('not_found',text)
        self.fails('doc','read','demo','nb1','a.md','--file',str(self.nb/'a.markdown'))   # one form or the other
        self.fails('doc','read','demo')

    def test_a_relative_path_is_read_against_the_working_folder(self):
        self.put('a.md',b'x')
        code,result=self.cli('doc','read','--file','wiki/a.md','--cwd',str(self.base));self.assertEqual(code,0,result)

    def test_private_reads_as_not_found_and_hidden_is_readable(self):
        self.put('a.md',b'x')
        self.set('--visibility','hidden');self.read()
        self.set('--private')
        code,text=self.fails('doc','read','--file',str(self.nb/'a.md'));self.assertIn('not_found',text)
        self.fails('doc','propose','--file',str(self.nb/'a.md'),'--body-file',self.body_file(b'y'),'--base-revision',digest(b'x'),actor='subagent')
        self.assertEqual(self.ok('doc','read','--file',str(self.nb/'a.md'),'--include-private')['text'],'x')

    def test_base_revision_rules_and_new_pages(self):
        self.put('a.md',b'one')
        self.fails('doc','propose','--file',str(self.nb/'a.md'),'--body-file',self.body_file(b'two'),actor='subagent')   # an existing page needs its revision
        code,text=self.fails('doc','propose','--file',str(self.nb/'a.md'),'--body-file',self.body_file(b'two'),'--base-revision','0'*64,actor='subagent');self.assertEqual(code,6,text)
        staged=self.propose('notes/new.md',b'hello')   # a new page: no base, and its folder need not exist yet
        value=self.record(staged['id']);self.assertEqual((value['base_revision'],value['base'],value['path']),(None,None,'notes/new.md'))
        self.fails('doc','propose','--file',str(self.nb/'bad<name>.md'),'--body-file',self.body_file(b'x'),actor='subagent')
        self.fails('doc','propose','--file',str(self.nb/'a.md'),'--body-file',self.body_file(b'\xff\xfe'),'--base-revision',digest(b'one'),actor='subagent')

    def test_the_base_is_kept_only_up_to_a_size(self):
        self.put('a.md',b'x'*50)
        with patch('core.folder_docs.KEEP_BYTES',10):staged=self.propose('a.md',b'y')
        value=self.record(staged['id']);self.assertEqual((value['base'],value['base_revision']),(None,digest(b'x'*50)))

    def test_a_junction_inside_the_folder_is_refused(self):
        outside=self.base/'outside';outside.mkdir();(outside/'x.md').write_bytes(b'secret')
        junction(self.nb/'link',outside,self)
        for target in ('read','propose'):
            with self.subTest(target=target):
                args=['doc',target,'--file',str(self.nb/'link'/'x.md')]+(['--body-file',self.body_file(b'y'),'--base-revision',digest(b'secret')] if target=='propose' else [])
                code,text=self.fails(*args,actor='subagent' if target=='propose' else 'main');self.assertTrue('Links' in text or 'not_found' in text,text)

    def test_viewers_may_read_but_not_stage(self):
        self.put('a.md',b'x')
        self.ok('doc','read','--file',str(self.nb/'a.md'),actor='viewer')
        self.fails('doc','propose','--file',str(self.nb/'a.md'),'--body-file',self.body_file(b'y'),'--base-revision',digest(b'x'),actor='viewer')

    def test_the_old_page_commands_are_gone(self):
        for argv in (('doc','proposals'),('doc','proposal','abcdef123456'),('doc','accept','abcdef123456'),('doc','reject','abcdef123456')):
            with self.subTest(argv=argv):
                code,text=self.fails(*argv);self.assertIn('invalid choice',text)


class Settle(Folder):
    def test_accept_writes_the_exact_bytes_and_lists_both_ways(self):
        self.put('a.md',PAGE)
        body=b'\xef\xbb\xbf# Title\r\n\r\nEdited\r\n'
        pid=self.propose('a.md',body)['id']
        row=self.listing()[0]
        self.assertEqual((row['id'],row['kind'],row['notebook'],row['path'],row['status'],row['orphaned'],row['applied']),(pid,'page',self.nid,'a.md','pending',False,False))
        self.assertEqual((row['folder'],row['file'],row['size']),(self.nb.resolve().as_posix(),(self.nb.resolve()/'a.md').as_posix(),len(body)))
        self.assertEqual(self.ok('proposals','show',pid)['proposal']['body'].encode(),body);self.assertEqual(self.ok('proposals','show',pid)['kind'],'page')
        self.fails('proposals','accept',pid,actor='subagent');self.fails('proposals','accept',pid,actor='viewer')
        self.fails('proposals','accept',pid,env={'HIVEMIND_SESSION':'hm-1'})   # only the user settles page proposals
        self.assertEqual(self.nb.joinpath('a.md').read_bytes(),PAGE)
        done=self.ok('proposals','accept',pid)
        self.assertEqual(self.nb.joinpath('a.md').read_bytes(),body);self.assertEqual((done['kind'],done['revision']),('page',digest(body)))
        self.assertEqual(self.journal(done['operation_id'])['kind'],'doc_accept')
        settled=self.record(pid);self.assertEqual((settled['status'],settled['revision']),('accepted',digest(body)))
        self.assertEqual(self.listing(),[]);accepted=self.listing('--status','accepted')
        self.assertEqual((accepted[0]['id'],accepted[0]['applied']),(pid,True))
        self.assertTrue(self.ok('proposals','accept',pid)['unchanged'])   # settled and written: nothing more to do

    def test_a_page_changed_since_the_proposal_is_not_overwritten(self):
        self.put('a.md',b'one');pid=self.propose('a.md',b'two')['id']
        self.put('a.md',b'three')
        code,text=self.fails('proposals','accept',pid);self.assertEqual(code,6,text)
        self.assertEqual(self.nb.joinpath('a.md').read_bytes(),b'three');self.assertEqual(self.record(pid)['status'],'pending')

    def test_an_edited_accept_writes_the_merge_against_the_expected_revision(self):
        self.put('a.md',b'one\r\n');pid=self.propose('a.md',b'two\r\n')['id']
        self.put('a.md',b'three\r\n')
        code,text=self.fails('proposals','accept',pid,'--edited','--expected-revision',digest(b'one\r\n'),'--body-file',self.body_file(b'merged'));self.assertEqual(code,6,text)
        done=self.ok('proposals','accept',pid,'--edited','--expected-revision',digest(b'three\r\n'),'--body-file',self.body_file(b'two and three\r\n'))
        self.assertEqual(self.nb.joinpath('a.md').read_bytes(),b'two and three\r\n')
        settled=self.record(pid);self.assertEqual((settled['status'],settled['edited'],settled['previous_revision']),('accepted',True,digest(b'three\r\n')))
        self.assertEqual(base64.b64decode(settled['previous']),b'three\r\n')
        self.ok('undo',done['operation_id'])
        self.assertEqual(self.nb.joinpath('a.md').read_bytes(),b'three\r\n');self.assertEqual(self.record(pid)['status'],'pending')
        self.fails('proposals','accept',pid,'--body-file',self.body_file(b'x'))   # --body-file goes with --edited

    def test_a_save_made_while_the_page_is_written_rolls_the_accept_back(self):
        self.put('a.md',b'one');pid=self.propose('a.md',b'two')['id'];real=folder_docs._put
        def racing(base,rel,data,expect):
            (Path(base)/rel).write_bytes(b'saved meanwhile')   # an editor saves between the check and the write
            return real(base,rel,data,expect)
        with patch('core.folder_docs._put',racing):code,text=self.fails('proposals','accept',pid)
        self.assertEqual(code,6,text);self.assertEqual(self.nb.joinpath('a.md').read_bytes(),b'saved meanwhile')
        self.assertEqual(self.record(pid)['status'],'pending');self.assertEqual(len(self.listing()),1)

    def test_accept_creates_a_new_page_and_undo_removes_it(self):
        pid=self.propose('deep/new.md',b'fresh\n')['id'];done=self.ok('proposals','accept',pid)
        self.assertEqual(self.nb.joinpath('deep','new.md').read_bytes(),b'fresh\n')
        self.ok('undo',done['operation_id']);self.assertFalse(self.nb.joinpath('deep','new.md').exists());self.assertEqual(self.record(pid)['status'],'pending')

    def test_undo_puts_the_bytes_back_and_is_safe_to_repeat(self):
        self.put('a.md',PAGE);pid=self.propose('a.md',b'new\r\n')['id'];done=self.ok('proposals','accept',pid)
        undone=self.ok('doc','undo',done['operation_id'])
        self.assertEqual(self.nb.joinpath('a.md').read_bytes(),PAGE);self.assertEqual(self.record(pid)['status'],'pending')
        self.put('a.md',b'typed after the undo')
        self.fails('doc','undo',done['operation_id'])   # the second run changes nothing
        self.assertEqual(self.nb.joinpath('a.md').read_bytes(),b'typed after the undo')
        self.fails('undo',undone['operation_id'])   # an undo is not redone: accept the proposal again

    def test_undo_is_refused_after_a_later_change_to_the_page(self):
        self.put('a.md',b'one');pid=self.propose('a.md',b'two')['id'];done=self.ok('proposals','accept',pid)
        self.put('a.md',b'edited later')
        code,text=self.fails('undo',done['operation_id']);self.assertIn('Hivemind file history',text)
        self.assertEqual(self.nb.joinpath('a.md').read_bytes(),b'edited later');self.assertEqual(self.record(pid)['status'],'accepted')

    def test_an_accept_that_stopped_before_the_write_is_finished_by_running_it_again(self):
        self.put('a.md',b'one');pid=self.propose('a.md',b'two')['id']
        with patch('core.governance.undo',lambda *a,**k:None),patch('core.folder_docs._put',lambda *a:False):self.fails('proposals','accept',pid)
        self.assertEqual((self.record(pid)['status'],self.nb.joinpath('a.md').read_bytes()),('accepted',b'one'))
        self.assertFalse(self.listing('--status','accepted')[0]['applied'])
        done=self.ok('proposals','accept',pid);self.assertTrue(done['finished']);self.assertEqual(self.nb.joinpath('a.md').read_bytes(),b'two')
        self.assertTrue(self.listing('--status','accepted')[0]['applied'])
        self.assertTrue(self.ok('proposals','accept',pid)['unchanged'])

    def test_undo_of_an_accept_that_never_wrote_the_page_skips_the_write(self):
        self.put('a.md',b'one');pid=self.propose('a.md',b'two')['id'];ops=[]
        real=docs._commit
        def note(*a,**k):
            receipt=real(*a,**k);ops.append(receipt['operation_id']);return receipt
        with patch('core.docs._commit',note),patch('core.governance.undo',lambda *a,**k:None),patch('core.folder_docs._put',lambda *a:False):self.fails('proposals','accept',pid)
        self.ok('undo',ops[-1]);self.assertEqual(self.record(pid)['status'],'pending');self.assertEqual(self.nb.joinpath('a.md').read_bytes(),b'one')

    def test_reject(self):
        self.put('a.md',b'one');pid=self.propose('a.md',b'two')['id']
        self.fails('proposals','reject',pid,actor='subagent');self.fails('proposals','reject',pid,env={'HIVEMIND_SESSION':'hm-1'})
        self.ok('proposals','reject',pid,'--reason','not now')
        value=self.record(pid);self.assertEqual((value['status'],value['reason']),('rejected','not now'));self.assertEqual(self.nb.joinpath('a.md').read_bytes(),b'one')
        self.assertEqual(self.listing('--status','rejected')[0]['id'],pid);self.fails('proposals','accept',pid);self.fails('proposals','reject',pid)

    def test_a_proposal_of_an_unregistered_folder_is_orphaned_and_can_only_be_rejected(self):
        self.put('a.md',b'one');pid=self.propose('a.md',b'two')['id']
        self.ok('doc','folder','unset',str(self.nb),'--forget')
        row=self.listing()[0];self.assertTrue(row['orphaned'])
        code,text=self.fails('proposals','accept',pid);self.assertIn('no longer registered',text)
        self.ok('proposals','reject',pid);self.assertEqual(self.record(pid)['status'],'rejected')

    def test_a_private_folder_hides_its_proposals_from_the_listing(self):
        self.put('a.md',b'one');pid=self.propose('a.md',b'two')['id']
        self.set('--private')
        self.assertEqual(self.listing(),[]);self.assertEqual(self.ok('proposals','--kind','page','--include-private')['proposals'][0]['id'],pid)
        self.fails('proposals','show',pid)

    def test_private_subfolder_and_damaged_registry_hide_folder_proposals(self):
        self.put('sub/a.md',b'one');pid=self.propose('sub/a.md',b'two')['id']
        sidecar=self.nb/'sub'/'.hivemind'/'notebook.json'
        sidecar.parent.mkdir(parents=True,exist_ok=True)
        sidecar.write_text('{"private":true}',encoding='utf-8')
        self.assertEqual(self.listing(),[])
        self.fails('proposals','show',pid)
        self.fails('proposals','accept',pid)
        self.fails('proposals','reject',pid)
        self.assertEqual(self.ok('proposals','--kind','page','--include-private')['proposals'][0]['id'],pid)
        sidecar.unlink()
        (self.root/'notebook-folders.json').write_text('{damaged',encoding='utf-8')
        self.assertEqual(self.listing(),[])
        self.fails('proposals','show',pid)

    def test_undo_and_doc_undo_refuse_private_folder_proposals_without_include_private(self):
        self.put('a.md',b'one');prop=self.propose('a.md',b'two');pid=prop['id']
        done=self.ok('proposals','accept',pid)
        self.set('--private')
        self.fails('undo',done['operation_id'])
        self.fails('doc','undo',done['operation_id'])
        self.fails('undo',prop['operation_id'])
        self.assertEqual(self.nb.joinpath('a.md').read_bytes(),b'two')
        self.ok('undo',done['operation_id'],'--include-private')
        self.assertEqual(self.nb.joinpath('a.md').read_bytes(),b'one')

    def test_dot_folders_are_ignored_by_folder_proposals_and_aside_unlink_failure_does_not_rollback(self):
        self.put('.obsidian/readme.md',b'one')
        self.fails('doc','read','--file',str(self.nb/'.obsidian'/'readme.md'))
        self.put('a.md',b'one');pid=self.propose('a.md',b'two')['id']
        real_unlink=Path.unlink
        def flaky_unlink(p,*a,**k):
            if p.name.endswith('.aside'):raise PermissionError('locked by av')
            return real_unlink(p,*a,**k)
        with patch.object(Path,'unlink',flaky_unlink):
            self.ok('proposals','accept',pid)
        self.assertEqual(self.nb.joinpath('a.md').read_bytes(),b'two')
        self.assertEqual(self.record(pid)['status'],'accepted')
        self.assertFalse([p for p in self.nb.iterdir() if '(kept ' in p.name])

    def test_history_hides_folder_proposals_unless_include_private(self):
        self.put('a.md',b'one');pid=self.propose('a.md',b'two')['id'];done=self.ok('proposals','accept',pid)
        text=json.dumps(self.ok('history'));self.assertNotIn('folder-proposals',text);self.assertNotIn(self.nid,text)
        self.assertIn('folder-proposals',json.dumps(self.ok('history','--include-private')))
        self.ok('doc','undo',done['operation_id']);self.ok('doc','undo',self.propose('a.md',b'three')['operation_id'])   # doc undo handles these operations

    def test_a_leftover_hosted_proposal_is_not_served(self):
        old=self.root/'domains'/'demo'/'notebooks'/'nb1'/'.proposals';old.mkdir(parents=True)
        (old/'0123456789ab.json').write_text(json.dumps({'id':'0123456789ab','domain':'demo','notebook':'nb1','path':'p.md','body':'two','status':'pending','created':'2026-11-01T00:00:00Z'}))
        self.put('a.md',b'x');other=self.propose('a.md',b'y')['id']
        self.assertEqual([r['id'] for r in self.listing()],[other])
        code,text=self.fails('proposals','accept','0123456789ab');self.assertIn('not_found',text)

    def test_accept_and_reject_write_where_the_proposal_was_found_not_where_its_json_says(self):
        self.put('a.md',b'one');self.put('b.md',b'one')
        accepted=self.propose('a.md',b'two')['id'];rejected=self.propose('b.md',b'two')['id']
        for pid in (accepted,rejected):
            path=self.root/'domains'/'demo'/'folder-proposals'/self.nid/f'{pid}.json';value=json.loads(path.read_text())
            value.update(domain='evil',notebook='nb-0000000000');path.write_text(json.dumps(value))
        done=self.ok('proposals','accept',accepted);self.ok('proposals','reject',rejected)
        self.assertFalse((self.root/'domains'/'evil').exists());self.assertFalse((self.root/'domains'/'demo'/'folder-proposals'/'nb-0000000000').exists())
        for pid,status in ((accepted,'accepted'),(rejected,'rejected')):
            record=self.record(pid);self.assertEqual((record['status'],record['domain'],record['notebook']),(status,'demo',self.nid))
        self.assertEqual(self.nb.joinpath('a.md').read_bytes(),b'two')
        effects=[e['path'] for e in self.journal(done['operation_id'])['effects']]
        self.assertEqual(effects,[f'domains/demo/folder-proposals/{self.nid}/{accepted}.json'])


class Retention(Folder):
    """Journals of folder notebook operations hold page text, so finished ones are pruned (docs.prune_journals, with the daily housekeeping)."""
    def ops(self):
        self.put('a.md',b'one');made=self.propose('a.md',b'two');done=self.ok('proposals','accept',made['id'])
        return made['operation_id'],done['operation_id']
    def kept(self):return sorted(p.name for p in (self.root/'.operations').glob('doc-*'))

    def test_the_newest_operations_per_file_are_kept(self):
        first,second=self.ops();other=self.propose('a.md',b'three')['operation_id']
        self.assertEqual(docs.prune_journals(self.root,keep=1),1)   # the propose of the first proposal: its file has a newer operation
        self.assertEqual(self.kept(),sorted([second,other]))
        self.ok('doc','undo',second);self.assertEqual(self.nb.joinpath('a.md').read_bytes(),b'one')

    def test_old_operations_go_even_when_few(self):
        self.ops();self.assertEqual(docs.prune_journals(self.root,keep=100,days=-1),2);self.assertEqual(self.kept(),[])   # every operation is older than a cutoff in the future

    def test_unfinished_operations_and_missing_index_files(self):
        first,second=self.ops();third=self.propose('a.md',b'four')['operation_id']
        (self.root/'.operations'/first/'doc.json').unlink()   # read from the journal instead
        (self.root/'.operations'/third/'pending').write_text('pending\n')
        docs.prune_journals(self.root,keep=1,days=-1)
        self.assertEqual(self.kept(),[third])   # unfinished: left alone

    def test_the_scan_takes_no_lock_and_each_candidate_is_rechecked_under_it(self):
        from contextlib import contextmanager
        first,second=self.ops()
        with patch.object(docs,'store_lock',side_effect=AssertionError('locked during scan')):self.assertEqual(docs.prune_journals(self.root,keep=100),0)
        real_lock=docs.store_lock;calls=[]
        @contextmanager
        def racing_lock(root,*a,**kw):
            calls.append(root);(self.root/'.operations'/first/'pending').write_text('pending\n')
            with real_lock(root,*a,**kw):yield
        with patch.object(docs,'store_lock',side_effect=racing_lock):docs.prune_journals(self.root,keep=1)
        self.assertGreaterEqual(len(calls),1);self.assertIn(first,self.kept())


class OneCommand(Folder):
    def stage(self,title='Dates need TZ=UTC'):
        return self.ok('propose','--session','s1','--type','decision','--title',title,'--summary','tests/test_dates.py:42 fails unless TZ=UTC is set.',actor='subagent')['id']

    def test_list_mixes_both_kinds_and_filters(self):
        self.put('a.md',b'one');page=self.propose('a.md',b'two')['id'];note=self.stage()
        rows=self.ok('proposals')['proposals'];self.assertEqual({(r['id'],r['kind']) for r in rows},{(page,'page'),(note,'note')})
        self.assertEqual([r['id'] for r in self.ok('proposals','--kind','note')['proposals']],[note])
        self.assertEqual([r['id'] for r in self.ok('proposals','--kind','page','--domain','demo')['proposals']],[page])
        self.assertEqual(self.ok('proposals','--kind','page','--notebook','other-nb')['proposals'],[])
        self.ok('proposals','reject',note,'--reason','known');self.ok('proposals','reject',page)
        self.assertEqual(self.ok('proposals')['proposals'],[])
        self.assertEqual({r['status'] for r in self.ok('proposals','--status','all')['proposals']},{'rejected'})
        self.assertEqual({r['kind'] for r in self.ok('proposals','--status','rejected')['proposals']},{'note','page'})
        self.fails('proposals','--status','any')

    def test_show_accept_and_reject_each_kind(self):
        note=self.stage();shown=self.ok('proposals','show',note)
        self.assertEqual((shown['kind'],shown['proposal']['id'],shown['proposal']['status']),('note',note,'pending'))
        saved=self.ok('proposals','accept',note);self.assertEqual(saved['proposal'],{'id':note,'status':'accepted'})
        self.assertEqual([n['title'] for n in __import__('core.repository',fromlist=['x']).inventory('demo',self.root)],['Dates need TZ=UTC'])
        other=self.stage('Second finding');self.fails('proposals','reject',other)   # a note needs a reason
        self.assertEqual(self.ok('proposals','reject',other,'--reason','x')['status'],'rejected')
        self.fails('proposals','show','xyz');self.fails('proposals','show','abc');self.fails('proposals','accept')

    def test_flags_belong_to_one_kind(self):
        note=self.stage();self.fails('proposals','accept',note,'--edited');self.fails('proposals','accept',note,'--expected-revision','x')
        self.put('a.md',b'one');page=self.propose('a.md',b'two')['id']
        self.fails('proposals','accept',page,'--rule');self.fails('proposals','accept',page,'--pin')
        self.assertEqual(self.record(page)['status'],'pending')

    def test_capture_from_proposal_alone_points_to_accept(self):
        note=self.stage();code,text=self.fails('capture','--from-proposal',note);self.assertIn(f'proposals accept {note}',text)
        code,result=self.cli('capture','--from-proposal',note,'--type','decision','--title','Edited','--summary','The whole note beside it.');self.assertEqual(code,0,result)
        self.assertEqual(result['proposal'],{'id':note,'status':'accepted'})

    def test_subagents_and_viewers_read_but_do_not_settle(self):
        note=self.stage()
        for actor in ('subagent','viewer'):
            with self.subTest(actor=actor):
                self.ok('proposals',actor=actor);self.ok('proposals','show',note,actor=actor)
                self.fails('proposals','accept',note,actor=actor);self.fails('proposals','reject',note,'--reason','x',actor=actor)

    def test_a_staged_note_can_be_accepted_inside_a_hivemind_terminal_but_a_page_cannot(self):
        note=self.stage();self.put('a.md',b'one');page=self.propose('a.md',b'two')['id']
        self.ok('proposals','accept',note,env={'HIVEMIND_SESSION':'hm-1'})
        self.fails('proposals','accept',page,env={'HIVEMIND_SESSION':'hm-1'});self.ok('proposals','--kind','page',env={'HIVEMIND_SESSION':'hm-1'})

    def test_proposals_list_filters_notes_by_domain_and_notebook_and_refuses_private_on_note_accept(self):
        note=self.stage()
        self.assertEqual(self.ok('proposals','--domain','other')['proposals'],[])
        self.assertEqual([r['id'] for r in self.ok('proposals','--domain','demo')['proposals']],[note])
        self.assertEqual(self.ok('proposals','--notebook','nb1')['proposals'],[])
        self.fails('proposals','--kind','note','--notebook','nb1')
        self.fails('proposals','accept',note,'--include-private',env={'HIVEMIND_SESSION':'hm-1'})
        self.fails('proposals','accept',note,'--include-private',actor='subagent')


if __name__=='__main__':unittest.main()
