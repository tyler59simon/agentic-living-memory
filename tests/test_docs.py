"""Notebook pages (NS-2A): byte-exact pages, names, moves with link rewriting, attachments as blob effects, proposals,
permissions, private notebooks and undo. Design: docs/notebooks-design.md."""
import base64,contextlib,io,json,os,unittest
from pathlib import Path
from unittest.mock import patch
from test_core import StoreFixture
from core.cli import main
from core.repository import read_json,digest,recover,transaction
from core import docs,governance,repository

PAGE=b'\xef\xbb\xbf# Title\r\n\r\nLine with CRLF\r\nand a tab\there\r\n'


class Fixture(StoreFixture):
    def cli(self,*argv,stdin=None,actor='main',domain='demo',env=None):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err),patch('sys.stdin',io.StringIO(stdin or '')),patch.dict('os.environ',env or {}):
            try:code=main(['--root',str(self.root),'--actor',actor,*(['--domain',domain] if domain else []),'--json',*argv])
            except SystemExit as exc:code=exc.code
        text=out.getvalue() or err.getvalue()
        return code,(json.loads(text) if text.strip()[:1] in '{[' else text)
    def ok(self,*argv,**kw):
        code,result=self.cli(*argv,**kw);self.assertEqual(code,0,result);return result
    def fails(self,*argv,**kw):
        code,result=self.cli(*argv,**kw);self.assertNotEqual(code,0,result);return code,json.dumps(result)
    def journal(self,op):return read_json(self.root/'.operations'/op/'journal.json')
    def notebook(self,nb='nb1',**kw):return docs.create_notebook(self.root,'demo',nb,'Notebook',**kw)
    def page(self,path='a.md',body=PAGE,nb='nb1'):return docs.create_page(self.root,'demo',nb,path,body)
    def file(self,rel,nb='nb1'):return self.root/'domains'/'demo'/'notebooks'/nb/rel
    def body_file(self,data):
        path=self.base/f'body-{digest(data)[:8]}';path.write_bytes(data);return str(path)


class Pages(Fixture):
    def test_pages_are_byte_exact_and_revisioned(self):
        self.notebook();created=self.page()
        self.assertEqual(self.file('a.md').read_bytes(),PAGE)
        self.assertEqual(created['revision'],digest(PAGE));self.assertTrue(created['operation_id'].startswith('doc-'))
        read=self.ok('doc','read','demo','nb1','a.md')
        self.assertEqual(read['revision'],digest(PAGE));self.assertEqual(read['text'].encode('utf-8'),PAGE)
        new=b'# Title\r\n\r\nEdited\r\n'
        written=self.ok('doc','write','demo','nb1','a.md','--expected-revision',read['revision'],'--body-file',self.body_file(new))
        self.assertEqual(self.file('a.md').read_bytes(),new);self.assertEqual(written['kind'],'doc_write')

    def test_stale_revision_is_exit_6(self):
        self.notebook();self.page()
        code,text=self.fails('doc','write','demo','nb1','a.md','--expected-revision','0'*64,'--body-file',self.body_file(b'x'))
        self.assertEqual(code,6,text);self.assertEqual(self.file('a.md').read_bytes(),PAGE)

    def test_body_must_be_utf8_and_capped(self):
        self.notebook()
        with self.assertRaisesRegex(ValueError,'UTF-8'):self.page(body=b'\xff\xfe bad')
        with patch('core.docs.MAX_PAGE_BYTES',10),self.assertRaisesRegex(ValueError,'at most'):self.page(body=b'x'*11)
        self.assertFalse(self.file('a.md').exists())
        self.assertFalse(list((self.root/'.operations').glob('*/pending')))

    def test_body_file_over_1_mib(self):
        self.notebook();big=(b'line of text\r\n'*100000)[:2*1024*1024]
        self.ok('doc','create','demo','nb1','big.md','--body-file',self.body_file(big))
        self.assertEqual(self.file('big.md').read_bytes(),big)

    def test_deleting_a_page_keeps_its_attachment(self):
        self.notebook();source=self.base/'pic.bin';source.write_bytes(b'\x00pic')
        link=docs.attach(self.root,'demo','nb1',source)['path'];self.page('p.md',f'![x]({link})'.encode())
        docs.delete_page(self.root,'demo','nb1','p.md',digest(f'![x]({link})'.encode()))
        self.assertEqual(self.file(link).read_bytes(),b'\x00pic')

    def test_undo_restores_bytes_and_reconciles_nothing(self):
        self.notebook();self.page()
        read=docs.read_page(self.root,'demo','nb1','a.md')
        written=docs.write_page(self.root,'demo','nb1','a.md',b'new\n',read['revision'])
        with patch('core.governance.reconcile') as reconcile:self.ok('doc','undo',written['operation_id'])
        reconcile.assert_not_called()
        self.assertEqual(self.file('a.md').read_bytes(),PAGE)

    def test_doc_undo_refuses_other_operations(self):
        n=self.note()
        ops=[p.parent.name for p in (self.root/'.operations').glob('*/journal.json')]
        code,text=self.fails('doc','undo',ops[0]);self.assertIn('not a notebook operation',text)

    def test_names(self):
        self.notebook()
        for bad in ('notebook.json','assets/x.md','.proposals/x.md','a:b.md','con.md','a/ b.md','a./b.md','no-extension','a\\b.md','/'.join(['d']*9)+'.md'):
            with self.subTest(bad=bad),self.assertRaises(ValueError):self.page(bad)
        self.page('Folder/Page.md')
        with self.assertRaisesRegex(ValueError,'letter case'):self.page('folder/Other.md')
        with self.assertRaisesRegex(ValueError,'letter case'):self.page('Folder/page.md')
        with self.assertRaisesRegex(ValueError,'already exists'):self.page('Folder/Page.md')

    def test_junction_inside_a_notebook_is_refused(self):
        self.notebook();outside=self.base/'outside';outside.mkdir();(outside/'x.md').write_text('secret')
        link=self.file('linked')
        try:os.symlink(outside,link,target_is_directory=True)
        except OSError:
            import subprocess
            if subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],capture_output=True).returncode:self.skipTest('no symlink or junction')
        with self.assertRaisesRegex(ValueError,'Links and junctions'):docs.read_page(self.root,'demo','nb1','linked/x.md')
        with self.assertRaisesRegex(ValueError,'Links and junctions'):self.page('linked/y.md')
        self.assertNotIn('linked/x.md',[e['path'] for e in docs.list_pages(self.root,'demo','nb1')['entries']])

    def test_pages_are_not_notes(self):
        self.notebook();self.page('Cache.md',b'The local cache is rebuildable.\n')
        self.assertEqual(self.ok('list')['notes'],[])
        self.assertEqual(self.ok('validate','demo').get('issues',[]),[])


class Moves(Fixture):
    def test_move_page_rewrites_links_and_undoes(self):
        self.notebook()
        self.page('Old.md',b'target\n')
        self.page('Section/Other.md',b'See [[Old]] and [[Old|label]] and [[Old.md#Head]] and [[Unrelated]].\r\n')
        moved=self.ok('doc','move','demo','nb1','Old.md','Section/New.md','--rewrite-links')
        self.assertEqual(moved['summary']['rewritten'],['Section/Other.md'])
        self.assertEqual(self.file('Section/Other.md').read_bytes(),b'See [[New]] and [[New|label]] and [[New#Head]] and [[Unrelated]].\r\n')
        self.assertFalse(self.file('Old.md').exists());self.assertEqual(self.file('Section/New.md').read_bytes(),b'target\n')
        self.ok('doc','undo',moved['operation_id'])
        self.assertTrue(self.file('Old.md').exists());self.assertIn(b'[[Old]]',self.file('Section/Other.md').read_bytes())

    def test_ambiguous_new_name_gets_a_path(self):
        self.notebook();self.page('Old.md',b'x');self.page('Elsewhere/New.md',b'y');self.page('Ref.md',b'[[Old]]')
        docs.move(self.root,'demo','nb1','Old.md','Here/New.md',rewrite_links=True)
        self.assertEqual(self.file('Ref.md').read_bytes(),b'[[Here/New]]')

    def test_move_section_and_case_rename(self):
        self.notebook();self.page('A/one.md',b'1');self.page('A/sub/two.md',b'2')
        with self.assertRaisesRegex(ValueError,'into itself'):docs.move(self.root,'demo','nb1','A','A/sub/A')
        docs.move(self.root,'demo','nb1','A','B')
        self.assertEqual(self.file('B/sub/two.md').read_bytes(),b'2');self.assertFalse(self.file('A').exists())
        with self.assertRaisesRegex(ValueError,'two steps'):docs.move(self.root,'demo','nb1','B/one.md','B/One.md')   # R1

    def test_pending_proposal_follows_its_page(self):
        self.notebook();self.page('p.md',b'x')
        pid=docs.propose(self.root,'demo','nb1','p.md',b'y',digest(b'x'))['id']
        docs.move(self.root,'demo','nb1','p.md','q.md')
        accepted=docs.accept(self.root,pid);self.assertEqual(self.file('q.md').read_bytes(),b'y');self.assertEqual(accepted['kind'],'doc_accept')


class Attachments(Fixture):
    def test_attach_is_a_blob_effect_and_undoes(self):
        self.notebook();data=bytes(range(256))*64;source=self.base/'pic.png';source.write_bytes(data)
        attached=self.ok('doc','attach','demo','nb1','--from-file',str(source))
        self.assertEqual(attached['path'],f'assets/{digest(data)[:8]}-pic.png')
        asset=self.file(attached['path']);self.assertEqual(asset.read_bytes(),data)
        journal=self.journal(attached['operation_id']);effect=journal['effects'][0]
        self.assertTrue(effect['blob']);self.assertIsNone(effect['after']);self.assertEqual(effect['after_hash'],digest(data))
        self.assertNotIn(base64.b64encode(data[:300]).decode()[:40],json.dumps(journal))
        self.assertEqual(journal['meta']['notebook'],'nb1')
        again=self.ok('doc','attach','demo','nb1','--from-file',str(source));self.assertFalse(again['created'])
        undone=self.ok('doc','undo',attached['operation_id']);self.assertFalse(asset.exists())
        self.ok('doc','undo',undone['operation_id']);self.assertEqual(asset.read_bytes(),data)

    ASSET='domains/demo/notebooks/nb1/assets/abcd1234-x.bin'
    def crash(self,changes,op,expected=None):
        """Run a transaction that stops after the first effect, like a crash mid-apply (effects run in path order)."""
        real=repository._write
        def failing(path,data):raise OSError('crash')
        with patch('core.repository._write',failing),self.assertRaises(OSError):transaction(self.root,changes,'doc_attach',expected,op)
        self.assertEqual(self.journal(op)['state'],'applying')

    def test_crash_mid_apply_rolls_back_or_forward(self):
        self.notebook();data=b'\x00\x01binary'*10;page='domains/demo/notebooks/nb1/z.md'
        self.crash({self.ASSET:data,page:'text'},'d-crash1')
        asset=self.root/self.ASSET;self.assertEqual(asset.read_bytes(),data)
        recover(self.root,'d-crash1','backward');self.assertFalse(asset.exists())
        self.assertEqual((self.root/'.operations'/'d-crash1'/'blobs'/digest(data)).read_bytes(),data)
        self.crash({self.ASSET:data,page:'text'},'d-crash2')
        recover(self.root,'d-crash2','forward');self.assertEqual(asset.read_bytes(),data);self.assertEqual((self.root/page).read_text(),'text')

    def test_missing_blob_bytes_are_reported(self):
        self.notebook();data=b'bytes'
        with patch('core.repository.recover',side_effect=OSError('crash')),self.assertRaises(OSError):transaction(self.root,{self.ASSET:data},'doc_attach',None,'d-gone')
        (self.root/'.operations'/'d-gone'/'blobs'/digest(data)).unlink()
        with self.assertRaisesRegex(ValueError,'Attachment bytes are missing'):recover(self.root,'d-gone','forward')
        self.assertFalse((self.root/self.ASSET).exists())

    def test_overwritten_asset_bytes_come_back(self):
        self.notebook()
        transaction(self.root,{self.ASSET:b'first'},'doc_attach',None,'d-one')
        transaction(self.root,{self.ASSET:b'second'},'doc_attach',None,'d-two')
        self.assertEqual((self.root/self.ASSET).read_bytes(),b'second')
        governance.undo(self.root,'d-two');self.assertEqual((self.root/self.ASSET).read_bytes(),b'first')

    def test_identical_bytes_under_two_names(self):
        self.notebook();other=self.ASSET.replace('-x.bin','-y.bin')
        transaction(self.root,{self.ASSET:b'same',other:b'same'},'doc_attach',None,'d-pair')
        transaction(self.root,{self.ASSET:None,other:None},'doc_delete',None,'d-gone2')
        self.assertFalse((self.root/other).exists())
        governance.undo(self.root,'d-gone2')
        self.assertEqual(((self.root/self.ASSET).read_bytes(),(self.root/other).read_bytes()),(b'same',b'same'))

    def test_size_cap(self):
        self.notebook();source=self.base/'big.bin';source.write_bytes(b'x'*11)
        with patch('core.docs.MAX_ASSET_BYTES',10),self.assertRaisesRegex(ValueError,'at most'):docs.attach(self.root,'demo','nb1',source)

    def test_notebook_delete_with_pages_is_a_plan_that_keeps_attachment_bytes(self):
        self.notebook();self.page();source=self.base/'pic.bin';source.write_bytes(b'\x00pic');docs.attach(self.root,'demo','nb1',source)
        code,text=self.fails('doc','notebook','delete','demo','nb1');self.assertIn('--with-pages',text)
        planned=self.ok('doc','notebook','delete','demo','nb1','--with-pages');self.assertIn('plan_id',planned)
        shown=self.ok('inspect-plan',planned['plan_id']);self.assertTrue(any(e.get('binary') for e in shown['effects']))
        applied=self.ok('apply',planned['plan_id'],'--expected-plan-sha256',planned['digest'])
        self.assertFalse(self.file('a.md').exists())
        self.ok('undo',applied['operation_id'])
        self.assertEqual(self.file('a.md').read_bytes(),PAGE)
        sha=digest(b'\x00pic')   # outside the f-string: Python before 3.12 refuses a backslash there
        self.assertEqual(self.file(f'assets/{sha[:8]}-pic.bin').read_bytes(),b'\x00pic')


class Proposals(Fixture):
    def test_subagent_proposes_and_main_accepts(self):
        self.notebook();self.page('p.md',b'one\n')
        code,text=self.fails('doc','write','demo','nb1','p.md','--expected-revision',digest(b'one\n'),'--body-file',self.body_file(b'x'),actor='subagent')
        self.assertIn('doc propose',text)
        staged=self.ok('doc','propose','demo','nb1','p.md','--base-revision',digest(b'one\n'),'--note','tidy',stdin='two\n',actor='subagent')
        self.assertEqual(self.file('p.md').read_bytes(),b'one\n')
        listed=self.ok('doc','proposals');self.assertEqual([p['id'] for p in listed['proposals']],[staged['id']])
        self.fails('doc','accept',staged['id'],actor='subagent')
        self.ok('doc','accept',staged['id']);self.assertEqual(self.file('p.md').read_bytes(),b'two\n')
        self.assertEqual(self.ok('doc','proposals')['proposals'],[])

    def test_accept_refuses_a_page_changed_since(self):
        self.notebook();self.page('p.md',b'one')
        pid=docs.propose(self.root,'demo','nb1','p.md',b'two',digest(b'one'))['id']
        docs.write_page(self.root,'demo','nb1','p.md',b'three',digest(b'one'))
        code,text=self.fails('doc','accept',pid);self.assertEqual(code,6,text)

    def test_accept_edited_writes_the_merge_and_settles_it_in_one_operation(self):
        # NS-2C review, D3: Hivemind's merge of a stale proposal is written and settled together, against the page as merged.
        self.notebook();self.page('p.md',b'one\r\n')
        pid=docs.propose(self.root,'demo','nb1','p.md',b'two\r\n',digest(b'one\r\n'))['id']
        docs.write_page(self.root,'demo','nb1','p.md',b'three\r\n',digest(b'one\r\n'))
        code,text=self.fails('doc','accept',pid,'--edited','--expected-revision',digest(b'one\r\n'),stdin='merged\r\n');self.assertEqual(code,6,text)
        self.assertEqual(self.file('p.md').read_bytes(),b'three\r\n')
        done=self.ok('doc','accept',pid,'--edited','--expected-revision',digest(b'three\r\n'),'--body-file',self.body_file(b'two and three\r\n'))
        self.assertEqual(self.file('p.md').read_bytes(),b'two and three\r\n');self.assertEqual(done['revision'],digest(b'two and three\r\n'))
        settled=read_json(self.file(f'.proposals/{pid}.json'));self.assertEqual((settled['status'],settled.get('edited')),('accepted',True))
        self.assertEqual([p['edited'] for p in docs.proposals(self.root,['demo'],status='all')['proposals']],[True])
        self.ok('undo',done['operation_id'])
        self.assertEqual(self.file('p.md').read_bytes(),b'three\r\n');self.assertEqual(read_json(self.file(f'.proposals/{pid}.json'))['status'],'pending')

    def test_accept_edited_refuses_a_settled_proposal_and_a_new_page_that_exists_now(self):
        self.notebook()
        pid=docs.propose(self.root,'demo','nb1','n.md',b'agent')['id']
        self.page('n.md',b'someone else')
        code,text=self.fails('doc','accept',pid,'--edited',stdin='mine');self.assertEqual(code,6,text)
        self.assertEqual(self.file('n.md').read_bytes(),b'someone else')
        docs.reject(self.root,pid,'no')
        self.fails('doc','accept',pid,'--edited','--expected-revision',digest(b'someone else'),stdin='mine')
        self.assertEqual(self.file('n.md').read_bytes(),b'someone else')
        fresh=docs.propose(self.root,'demo','nb1','m.md',b'agent')['id']
        self.ok('doc','accept',fresh,'--edited',stdin='mine');self.assertEqual(self.file('m.md').read_bytes(),b'mine')
        self.fails('doc','accept',fresh,'--body-file',self.body_file(b'x'))

    def test_accept_edited_needs_a_body_given_on_purpose(self):
        # NS-2C re-review, R2-D5: a terminal or a serve request without stdin is not an empty merge.
        self.notebook();self.page('p.md',b'one')
        pid=docs.propose(self.root,'demo','nb1','p.md',b'two',digest(b'one'))['id']
        from core.cli import _RequestStdin
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err),patch('sys.stdin',_RequestStdin('',False)):
            code=main(['--root',str(self.root),'--actor','main','--domain','demo','--json','doc','accept',pid,'--edited','--expected-revision',digest(b'one')])
        self.assertNotEqual(code,0);self.assertIn('--body-file',out.getvalue()+err.getvalue())
        self.assertEqual(self.file('p.md').read_bytes(),b'one')
        with contextlib.redirect_stdout(io.StringIO()),patch('sys.stdin',_RequestStdin('',True)):
            code=main(['--root',str(self.root),'--actor','main','--domain','demo','--json','doc','accept',pid,'--edited','--expected-revision',digest(b'one')])
        self.assertEqual(code,0);self.assertEqual(self.file('p.md').read_bytes(),b'')

    def test_undo_restores_a_page_that_held_bytes_that_are_not_utf8(self):
        # NS-2C re-review, R2-D6: an editor left the page as Latin-1; replacing it, then undoing, brings those bytes back.
        self.notebook();self.page('p.md',b'ok\n')
        bad=b'caf\xe9\n';self.file('p.md').write_bytes(bad)
        done=docs.write_page(self.root,'demo','nb1','p.md',b'fixed\n',digest(bad))
        self.ok('undo',done['operation_id']);self.assertEqual(self.file('p.md').read_bytes(),bad)
        pid=docs.propose(self.root,'demo','nb1','p.md',b'agent\n',digest(bad))['id']
        done=self.ok('doc','accept',pid,'--edited','--expected-revision',digest(bad),stdin='merged\n')
        self.ok('undo',done['operation_id']);self.assertEqual(self.file('p.md').read_bytes(),bad)
        self.assertEqual(read_json(self.file(f'.proposals/{pid}.json'))['status'],'pending')

    def test_propose_a_new_page_and_reject(self):
        self.notebook()
        pid=docs.propose(self.root,'demo','nb1','new.md',b'hello')['id']
        self.page('exists.md',b'z')
        with self.assertRaisesRegex(ValueError,'base-revision'):docs.propose(self.root,'demo','nb1','exists.md',b'x')
        rejected=self.ok('doc','reject',pid,'--reason','no');self.assertEqual(read_json(self.file(f'.proposals/{pid}.json'))['status'],'rejected')
        self.fails('doc','accept',pid)

    def test_viewer_cannot_propose(self):
        self.notebook();self.fails('doc','propose','demo','nb1','v.md',stdin='x',actor='viewer')
        self.ok('doc','list','demo','nb1',actor='viewer')

    def test_hivemind_terminal(self):
        self.notebook();self.page('p.md',b'one')
        env={'HIVEMIND_SESSION':'1'}
        code,text=self.fails('doc','write','demo','nb1','p.md','--expected-revision',digest(b'one'),'--body-file',self.body_file(b'x'),env=env)
        self.assertIn('hivemind',text)
        self.fails('domain','create','nbonly','--notebook-only',env=env)
        self.ok('doc','propose','demo','nb1','p.md','--base-revision',digest(b'one'),stdin='two',env=env)
        self.fails('doc','notebooks','--include-private',env=env)


class Privacy(Fixture):
    def test_private_notebook_is_hidden_and_redacted(self):
        made=self.notebook('secret',private=True);self.notebook('open')
        written=docs.create_page(self.root,'demo','secret','plan.md',b'private words\n',include_private=True)
        names=[n['notebook'] for n in self.ok('doc','notebooks')['notebooks']];self.assertEqual(names,['open'])
        self.assertIn('secret',[n['notebook'] for n in self.ok('doc','notebooks','--include-private')['notebooks']])
        code,text=self.fails('doc','read','demo','secret','plan.md');self.assertIn('not_found',text)
        self.assertEqual(self.ok('doc','read','demo','secret','plan.md','--include-private')['text'],'private words\n')
        rows=self.ok('history','--limit','50');row=next(r for r in rows if r['operation_id']==written['operation_id'])
        self.assertEqual(row['files'],[]);self.assertIsNone(row['summary'])
        diff=self.ok('diff',written['operation_id']);self.assertNotIn('private words',json.dumps(diff))
        self.assertIn('private words',json.dumps(self.ok('diff',written['operation_id'],'--include-private')))
        registry=read_json(self.root/'notebook-privacy.json');self.assertEqual(registry['notebooks'][0]['uid'],read_json(self.file('notebook.json','secret'))['uid'])

    def test_made_public_stays_redacted_for_old_operations(self):
        self.notebook('was')
        docs.update_notebook(self.root,'demo','was',private=True)
        written=docs.create_page(self.root,'demo','was','p.md',b'while private\n',include_private=True)
        docs.update_notebook(self.root,'demo','was',private=False,include_private=True)
        self.assertNotIn('while private',json.dumps(self.ok('diff',written['operation_id'])))

    def test_hidden_notebooks_need_include_hidden(self):
        self.notebook('quiet',visibility='hidden')
        self.assertEqual(self.ok('doc','notebooks')['notebooks'],[])
        self.assertEqual([n['notebook'] for n in self.ok('doc','notebooks','--include-hidden')['notebooks']],['quiet'])


class Retention(Fixture):
    def edits(self,count):
        self.made=self.notebook()['operation_id'];self.page('p.md',b'0');ops=[]
        for i in range(1,count+1):
            ops.append(docs.write_page(self.root,'demo','nb1','p.md',str(i).encode(),digest(str(i-1).encode()))['operation_id'])
        return ops
    def kept(self):return sorted(p.name for p in (self.root/'.operations').glob('doc-*'))

    def test_newest_operations_per_page_are_kept(self):
        ops=self.edits(4);other=docs.create_page(self.root,'demo','nb1','q.md',b'q')['operation_id']
        self.assertEqual(docs.prune_journals(self.root,keep=2),3)
        self.assertEqual(self.kept(),sorted(ops[-2:]+[other,self.made]))   # notebook.json has one operation: kept
        self.ok('doc','undo',ops[-1]);self.assertEqual(self.file('p.md').read_bytes(),b'3')

    def test_old_operations_go_even_when_few(self):
        ops=self.edits(2)
        self.assertEqual(docs.prune_journals(self.root,keep=100,days=-1),4)   # every operation is older than a cutoff in the future
        self.assertEqual(self.kept(),[])

    def test_unfinished_operations_and_missing_index_files(self):
        ops=self.edits(3)
        (self.root/'.operations'/ops[0]/'doc.json').unlink()   # read from the journal instead
        (self.root/'.operations'/ops[1]/'pending').write_text('pending\n')
        docs.prune_journals(self.root,keep=1)
        self.assertIn(ops[1],self.kept());self.assertNotIn(ops[0],self.kept());self.assertIn(ops[2],self.kept())


class NotebookOnlyDomains(Fixture):
    def test_create_refuse_notes_and_convert(self):
        made=self.ok('domain','create','journal','--notebook-only');self.assertEqual(made['kind'],'notebook')
        self.assertFalse((self.root/'domains'/'journal'/'notes').exists())
        kinds={d['name']:d['kind'] for d in self.ok('domains')['domains']};self.assertEqual((kinds['journal'],kinds['demo']),('notebook','memory'))
        code,text=self.fails('capture','Title','Body','--tag','subject/project',domain='journal');self.assertIn('notebooks only',text)
        self.assertTrue(self.ok('doctor',domain='journal')['domain_data']=='present')
        self.assertEqual(self.ok('validate','journal').get('issues',[]),[])
        docs.create_notebook(self.root,'journal','days','Days');docs.create_page(self.root,'journal','days','today.md',b'x')
        code,text=self.fails('unbind','journal');self.assertIn('notebook pages',text)
        self.ok('list','--all-domains')
        self.ok('bind','journal',str(self.base/'journal-folder'))
        entry=read_json(self.root/'bindings.json')['domains']['journal'];self.assertNotIn('notebook_only',entry)
        self.assertTrue((self.root/'domains'/'journal'/'notes').is_dir())
        self.assertTrue((self.root/'domains'/'journal'/'notebooks'/'days'/'today.md').exists())

    def test_every_all_domains_path_skips_notebooks(self):
        self.note();self.ok('domain','create','journal','--notebook-only')
        docs.create_notebook(self.root,'journal','days','Days');docs.create_page(self.root,'journal','days','Cache.md',b'The local cache is rebuildable. PAGE-ONLY-WORD\n')
        docs.create_notebook(self.root,'demo','nb1','Notebook');docs.create_page(self.root,'demo','nb1','Cache.md',b'PAGE-ONLY-WORD\n')
        for argv in (('list','--all-domains'),('links','--all-domains'),('review','--all-domains'),('open','--all-domains'),('recap','--all-domains'),('search','PAGE-ONLY-WORD')):
            # NS-2B: search lists pages in their own `pages` array, never among notes
            with self.subTest(argv=argv):self.assertNotIn('PAGE-ONLY-WORD',json.dumps({k:v for k,v in self.ok(*argv).items() if k!='pages'}))

    def test_version_reports_docs(self):
        self.assertEqual(self.ok('version')['capabilities']['docs']['version'],1)
        self.assertTrue(self.ok('version')['capabilities']['docs']['accept_edited'])


if __name__=='__main__':unittest.main()
