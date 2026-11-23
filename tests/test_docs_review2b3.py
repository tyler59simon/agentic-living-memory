"""Regression tests for the third (final) Codex review of NS-2B (fix commit 2412053): one or more per finding, named by its ID.
Record: docs/reviews/ns2b-codex-review.md."""
import base64,json,os,shutil,sqlite3,time,types
from pathlib import Path
from unittest.mock import patch
import test_docs,test_docs_sync,test_sources_notebooks
from test_docs_review2b import own,junction
from test_docs_sync import PAGE
from core import digests,docs,docs_index,folder_policy,governance,notebook_sync,runtime,safe_put,sources
from core.repository import read_json
from core.sync import teardown_plan


def posix(**override):
    """The os module as notebook_sync sees it on POSIX: rename replaces, as it does there."""
    fake=types.SimpleNamespace(**{k:getattr(os,k) for k in dir(os) if not k.startswith('__')})
    fake.name='posix';fake.rename=os.replace;fake.path=os.path
    for k,v in override.items():setattr(fake,k,v)
    return fake


@own
class Reach(test_sources_notebooks.Notebooks):
    def registry(self,*entries):(self.root/folder_policy.REGISTRY).write_text(json.dumps({'version':1,'folders':list(entries)}))

    def test_F01_a_deleted_page_under_any_bound_folder_is_judged(self):
        sha=digests.describe(self.root,'demo',str(self.nb/'Runbook.md'))['sha256']
        self.set(self.nb,'searchable','--private');(self.nb/'Runbook.md').unlink()
        hit={'domain':'demo','id':'x','title':'Runbook digest','snippet':'standby','sources':[{'path':'kb/notes/Runbook.md','sha256':sha}]}
        with patch('core.digests.domain_prefixes',lambda *a:[self.base/'longer-root',self.project]):digests.mark([hit],self.root)
        self.assertTrue(hit.get('withheld'),hit)

    def test_F02_the_prompt_fallback_shows_no_withheld_title(self):
        described=digests.describe(self.root,'demo',str(self.nb/'Runbook.md'));self.set(self.nb,'searchable','--private')
        note={'domain':'demo','id':'abc12345','title':'Secret standby wombat runbook','body':'x','sources':[described]}
        with patch('core.search.recent_notes',lambda *a,**k:[dict(note)]):
            text,refs,overview=runtime.context(str(self.project),'zyxwv qutrop',self.root,'demo')
        self.assertTrue(text and 'Withheld note' in text,text);self.assertNotIn('wombat',text)

    def test_F03_a_retargeted_link_with_matching_file_stamps_is_read_again(self):
        first=self.base/'first';first.mkdir();second=self.base/'second';second.mkdir()
        (first/'Page.md').write_text('alpha zeppelin\n');(second/'Page.md').write_text('bravo marmalad\n')
        stamp=os.stat(first/'Page.md').st_mtime_ns;os.utime(second/'Page.md',ns=(stamp,stamp))
        alias=self.base/'alias';junction(alias,first,self);self.register(name='alias',kind='folder',path=str(alias));self.sync()
        os.rmdir(alias);junction(alias,second,self);self.sync()
        self.assertNotIn('Page.md',self.plain('alpha zeppelin'));self.assertIn('Page.md',self.plain('bravo marmalad'))

    def test_F12_an_ordinary_source_named_like_a_notebook_source_reads_as_missing(self):
        outside=self.base/'older';outside.mkdir();(outside/'Old.md').write_text('# Old\n\nThe zeppelin marmalade guide.\n')
        self.register(name='nbold',kind='folder',path=str(outside))
        for p in self.root.rglob('sources.json'):
            text=p.read_text(encoding='utf-8')
            if '"nbold"' in text:p.write_text(text.replace('"nbold"','"nb-0123456789"'),encoding='utf-8')
        self.sync();described=digests.describe(self.root,'demo',str(outside/'Old.md'))
        if not described['path'].startswith('nb-0123456789:'):self.skipTest('the file was not named by its source')
        (outside/'Old.md').unlink()
        self.assertEqual(digests.states([described],'demo',self.root),{described['path']:'missing'})

    def test_F14_counts_and_ingest_scan_leave_out_rows_of_a_retargeted_link(self):
        first=self.base/'first';first.mkdir();(first/'A.md').write_text('# A\n\nzeppelin\n');empty=self.base/'empty';empty.mkdir()
        alias=self.base/'alias';junction(alias,first,self);self.register(name='alias',kind='folder',path=str(alias));self.sync()
        os.rmdir(alias);junction(alias,empty,self)
        row=next(r for r in sources.listing(self.root,'demo')['sources'] if r['name']=='alias')
        self.assertEqual(row['files'],0);self.assertIn('folder moved',row['state'])
        self.assertFalse([i for i in digests.scan(self.root,'demo','alias')['items'] if i['path'].endswith('A.md')])

    def test_F15_a_nested_source_with_globs_leaves_the_notebook_its_other_pages(self):
        book=self.base/'book';sub=book/'sub';sub.mkdir(parents=True)
        (sub/'only.md').write_text('# Only\n\nonly page\n');(sub/'Other.md').write_text('# Other\n\nzeppelin marmalade other\n')
        self.register(name='inner',kind='folder',path=str(sub),include=['only.md']);self.set(book,'searchable');self.sync()
        self.assertIn('Other.md',[h['path'].rsplit('/',1)[-1] for h in sources.search(self.root,'demo','zeppelin marmalade',10,pages=True)])

    def test_F16_a_file_two_sources_index_never_takes_two_result_slots(self):
        lib=self.base/'lib';lib.mkdir()
        (lib/'A.md').write_text('# Quokka\n\nquokka quokka quokka quokka quokka\n');(lib/'B.md').write_text('# B\n\nA page with one quokka in it among other words.\n')
        self.register(name='one',kind='folder',path=str(lib));self.register(name='two',kind='folder',path=str(lib));self.sync()
        self.assertEqual(sorted(h['path'] for h in sources.search(self.root,'demo','quokka',2)),['A.md','B.md'])

    def test_F17_a_notebook_moved_to_another_domain_leaves_the_old_one_at_once(self):
        outside=self.base/'journal';outside.mkdir();(outside/'Day.md').write_text('# Day\n\nzeppelin marmalade walk\n')
        self.set(outside,'searchable');self.sync();self.assertIn('Day.md',self.pages('zeppelin marmalade'))
        docs.init_notebook_domain('other',self.root);spawned=[]
        with patch('core.folder_policy._spawn',lambda root,domain:spawned.append(domain)):folder_policy.set_folder(self.root,str(outside),'other','searchable')
        self.assertNotIn('Day.md',self.pages('zeppelin marmalade'));self.assertEqual(sorted(spawned),['demo','other'])

    def test_F18_ordinary_source_queries_all_run_before_page_queries(self):
        docs.create_notebook(self.root,'demo','inj','Inj',visibility='injectable')
        docs.create_page(self.root,'demo','inj','Z.md',b'# Z\n\nzeppelin\n');docs_index.refresh(self.root,'demo');self.sync()
        calls=[];real=sources.search
        def record(*a,**k):calls.append(bool(k.get('pages')));return real(*a,**k)
        prompt=('Please read the wombat cluster sizing guide.\n\n'+'Some unrelated filler words about nothing in particular. '*6+'\n\n'
                +'Then compare the failover runbook with the standby notes.\n\n'+'More filler words that say nothing at all here. '*6)
        with patch('core.sources.search',record):runtime.prompt_sources(prompt,self.root,'demo',{},{})
        self.assertGreaterEqual(calls.count(False),2,calls);self.assertEqual(calls,sorted(calls))

    def test_F23_set_and_unset_fold_duplicate_rows_even_when_nothing_else_changes(self):
        path=self.nb.resolve().as_posix()
        loose={'path':path,'domain':'demo','visibility':'injectable','private':False};strict={'path':path,'domain':'demo','visibility':'searchable','private':True}
        self.registry(loose,strict);self.set(self.nb,'searchable','--private')
        self.assertEqual(len(folder_policy.load(self.root)['folders']),1)
        self.registry(dict(strict,removed=True),loose);self.cli('--json','doc','folder','unset',str(self.nb))
        self.assertEqual(len(folder_policy.load(self.root)['folders']),1)


@own
class History(test_sources_notebooks.Notebooks):
    def test_F08_recovering_a_registry_change_backward_into_a_looser_one_needs_include_private(self):
        open_=self.set(self.nb,'searchable');closed=self.set(self.nb,'searchable','--private')
        journal=read_json(self.root/'.operations'/closed['operation_id']/'journal.json')
        pending=self.root/'.operations'/'interrupted01';pending.mkdir()
        (pending/'journal.json').write_text(json.dumps(dict(journal,id='interrupted01',state='prepared')))
        code,result=self.cli('--json','recover','interrupted01','--direction','backward')
        self.assertNotEqual(code,0,result);self.assertIn('--include-private',json.dumps(result))
        self.assertTrue(folder_policy.load(self.root)['folders'][0]['private'])


@own
class Index(test_docs.Fixture):
    def setUp(self):
        super().setUp();self.notebook();self.page('Good.md',b'# Good\n\nquokka good\n')

    def test_F19_an_index_from_before_the_vector_rule_is_embedded_once(self):
        docs_index.refresh(self.root,'demo')
        db=sqlite3.connect(docs_index.db_path(self.root,'demo'))
        try:sources._set_meta(db,'vectors_pending','0');db.execute("DELETE FROM meta WHERE key='vectors_checked'");db.commit()
        finally:db.close()
        calls=[]
        with patch('core.sources._embed',lambda *a:calls.append(1) or {'embedded':0,'pending':0}):docs_index.refresh(self.root,'demo',embed=True)
        self.assertEqual(calls,[1])

    def test_F21_a_domain_whose_refresh_fails_still_passes_the_allowance_on(self):
        (self.root/'domains'/'bad'/'notebooks').mkdir(parents=True);seen=[];real=docs_index.refresh
        def refresh(root,domain,*a,**k):
            seen.append((domain,k.get('progress')))
            return {'stale':True,'indexed':0,'removed':0,'error':'cannot list'} if domain=='bad' else real(root,domain,*a,**k)
        with patch('core.docs_index.refresh',refresh):
            for _ in range(2):docs_index.refresh_all(self.root,['bad','demo'])
        self.assertIn(('demo',True),seen)


@own
class Vault(test_docs_sync.Mirror):
    def tick(self):
        status=self.vault/'Dashboards'/'Sync Status - demo.md';status.write_text(self.status().replace('- [ ] Delete page','- [x] Delete page'),encoding='utf-8')

    def test_F04_a_file_that_cannot_be_judged_is_put_back(self):
        self.build();copy=self.copy('Plan.md');real=safe_put._bytes
        def bytes_(path):
            if str(path).endswith('.aside'):raise PermissionError('locked')
            return real(path)
        with patch('core.safe_put._bytes',bytes_):
            try:notebook_sync._put(self.vault,'Notebooks/demo/nb1/Plan.md',b'new\n',notebook_sync._sha(PAGE))
            except OSError:pass
        self.assertEqual(copy.read_bytes(),PAGE);self.assertFalse(list(copy.parent.glob('*.aside')))

    def test_F04_on_posix_a_second_save_is_never_replaced_when_a_file_is_put_back(self):
        self.build();copy=self.copy('Plan.md');real=safe_put._bytes
        def bytes_(path):
            if str(path).endswith('.aside'):copy.write_bytes(b'second save\n');return b'changed'   # saved twice around the rename
            return real(path)
        with patch('core.safe_put._bytes',bytes_),patch.object(safe_put,'os',posix()):
            self.assertFalse(notebook_sync._put(self.vault,'Notebooks/demo/nb1/Plan.md',b'new\n',notebook_sync._sha(PAGE)))
        self.assertEqual(copy.read_bytes(),b'second save\n')
        self.assertEqual([p.read_bytes() for p in copy.parent.glob('Plan (kept*')],[PAGE])

    def test_F11_a_filesystem_without_hard_links_still_gets_new_copies(self):
        self.build()
        def no_link(*a):raise PermissionError('hard links not supported')
        with patch.object(safe_put,'os',posix(link=no_link)):
            self.assertTrue(notebook_sync._put(self.vault,'Notebooks/demo/nb1/New.md',b'new page\n',None))
        self.assertEqual(self.copy('New.md').read_bytes(),b'new page\n')

    def test_F06_a_stale_teardown_removes_nothing_and_a_kept_copy_is_reported(self):
        self.build();made=teardown_plan(self.vault,self.root)
        home=self.vault/'Dashboards'/'Home.md';original=home.read_bytes();home.write_bytes(original+b'\nmine\n')
        with self.assertRaisesRegex(ValueError,'Stale revision'):governance.apply(self.vault,made['plan_id'],made['digest'])
        self.assertTrue(self.copy('Plan.md').exists())
        home.write_bytes(original);made=teardown_plan(self.vault,self.root)
        with patch('core.notebook_sync._put',lambda *a:(_ for _ in ()).throw(PermissionError('locked'))):
            receipt=governance.apply(self.vault,made['plan_id'],made['digest'])
        self.assertIn('left in the vault',json.dumps(receipt.get('warnings')))

    def test_F07_a_closing_notebook_keeps_the_bytes_it_judged(self):
        loose=self.base/'loose.txt';loose.write_bytes(b'saved later\n')
        folder=self.root/'domains'/'demo'/'notebooks'/'nb1'
        self.assertEqual(notebook_sync._keep_in_store(self.root,'demo','nb1',folder,'notes.txt',b'judged bytes\n',loose),'attached')
        kept=[p.read_bytes() for p in (folder/'assets').iterdir() if p.name.endswith('from-obsidian.txt')]
        self.assertEqual(kept,[b'judged bytes\n'])

    def test_F09_alm_sync_keeps_the_status_page_owned_for_teardown(self):
        self.build();self.copy('Plan.md').unlink();self.ok('sync',str(self.vault))
        made=teardown_plan(self.vault,self.root);self.assertTrue(made['ok'])

    def test_F10_inspect_plan_lists_the_mirror_copies_a_teardown_removes(self):
        self.build();made=teardown_plan(self.vault,self.root)
        effects=governance.inspect_plan(self.vault,made['plan_id'])['effects']
        self.assertIn(('Notebooks/demo/nb1/Plan.md','delete'),[(e['path'],e['action']) for e in effects])

    def test_F13_a_fresh_copy_clears_its_waiting_deletion(self):
        self.build();self.copy('Plan.md').unlink();self.build();self.tick()
        self.copy('Plan.md').parent.mkdir(parents=True,exist_ok=True);self.copy('Plan.md').write_bytes(b'rewritten just now\n')
        with patch('core.sync.QUIET_SECONDS',30):self.assertEqual(self.build()['deferred'],1)   # too fresh to read back
        self.assertEqual(read_json(self.root/'domains'/'demo'/'notebook-sync.json').get('pending'),{});self.assertNotIn('lm-page:',self.status())

    def test_F20_a_closing_notebook_keeps_tracking_a_folder_it_cannot_list(self):
        docs.create_page(self.root,'demo','nb1','sub/Deep.md',b'# Deep\n\ndeep\n');self.build()
        docs.update_notebook(self.root,'demo','nb1',private=True);real=os.scandir;sub=self.copy('sub')
        def scandir(path='.'):
            if Path(path)==sub:raise PermissionError('denied')
            return real(path)
        with patch('core.notebook_sync.os.scandir',scandir):self.build()
        self.assertIn('nb1/sub/Deep.md',read_json(self.root/'domains'/'demo'/'notebook-sync.json')['pages'])

    def test_F22_a_plan_whose_digest_does_not_match_is_made_again(self):
        self.build();self.copy('Plan.md').unlink();self.build()
        first=read_json(self.root/'domains'/'demo'/'notebook-sync.json')['pending']['nb1/Plan.md']['plan_id']
        path=self.root/'.plans'/(first+'.json');value=read_json(path);value['digest']='bad';path.write_text(json.dumps(value))
        self.tick();self.build()
        again=read_json(self.root/'domains'/'demo'/'notebook-sync.json')['pending']['nb1/Plan.md']['plan_id']
        self.assertNotEqual(again,first)


if __name__=='__main__':
    import unittest;unittest.main()
