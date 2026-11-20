"""QUEUE-2: diff-aware staleness (core/rebaseline.py). A note whose file changed outside everything it cites is re-baselined with no model,
its line citations shifted; anything that may touch what it says stays STALE."""
import hashlib,json,os,shutil,subprocess,unittest
from pathlib import Path
from unittest.mock import patch
from test_core import StoreFixture
from core import digests,rebaseline,governance
from core.notes import capture,find_note

TAGS=['subject/project','concern/architecture']
GIT=shutil.which('git')
MOD='''"""A module."""
LIMIT = 3


def alpha(x):
    return x + 1


def beta(y):
    total = y * LIMIT
    return total


def gamma(z):
    return z - 1
'''
def sha(data):return hashlib.sha256(data if isinstance(data,bytes) else data.encode()).hexdigest()

class Assess(unittest.TestCase):
    """The decision on its own: texts in, verdict out."""
    def verdict(self,body,new,old=MOD,tracked=('src/mod.py',)):return rebaseline.assess(body,'src/mod.py',list(tracked),old,new)
    def test_a_change_away_from_every_citation_is_irrelevant_and_shifts_the_lines(self):
        body='gamma subtracts one (mod.py:14-15).'
        v=self.verdict(body,MOD.replace('    return x + 1\n','    y = x + 1\n    return y\n'))
        self.assertFalse(v['relevant'],v['reason'])
        self.assertEqual(rebaseline.shifted(body,v['shifts']),'gamma subtracts one (mod.py:15-16).')
    def test_a_change_inside_a_cited_range_is_relevant(self):
        v=self.verdict('beta multiplies (mod.py:9-11).',MOD.replace('y * LIMIT','y * LIMIT * 2'))
        self.assertTrue(v['relevant']);self.assertIn('near the cited line 9',v['reason'])
    def test_a_change_to_a_symbol_named_without_a_line_is_relevant(self):
        v=self.verdict('`gamma` subtracts one.',MOD.replace('z - 1','z - 2'))
        self.assertTrue(v['relevant']);self.assertIn('gamma',v['reason'])
    def test_a_changed_line_naming_an_identifier_the_note_uses_is_relevant(self):
        v=self.verdict('beta multiplies by `LIMIT` (mod.py:9-11).',MOD.replace('LIMIT = 3','LIMIT = 4'))
        self.assertTrue(v['relevant']);self.assertIn('LIMIT',v['reason'])
    def test_a_note_that_cites_nothing_in_the_file_stays_stale(self):
        v=self.verdict('A module about arithmetic.',MOD.replace('z - 1','z - 2'))
        self.assertTrue(v['relevant']);self.assertIn('cites no line or symbol',v['reason'])
    def test_a_rewrite_of_most_of_the_file_stays_stale(self):
        v=self.verdict('gamma subtracts one (mod.py:14-15).','\n'.join(f'line {i}' for i in range(20))+'\n')
        self.assertTrue(v['relevant']);self.assertIn('lines changed',v['reason'])
    def test_a_bare_citation_follows_the_file_cited_before_it(self):
        body='beta (mod.py:9-11, :14-15) and other.py:3.'
        found=rebaseline.citations(body,['src/mod.py','src/other.py'])
        self.assertEqual([(sorted(c['files']),c['start'],c['end']) for c in found],[(['src/mod.py'],9,11),(['src/mod.py'],14,15),(['src/other.py'],3,3)])
    def test_a_bare_citation_with_two_tracked_files_and_no_file_before_it_is_ambiguous(self):
        v=self.verdict('gamma (:14-15).',MOD.replace('    return x + 1\n','    y = x + 1\n    return y\n'),tracked=('src/mod.py','src/other.py'))
        self.assertTrue(v['relevant']);self.assertIn('another file',v['reason'])
    def test_line_endings_alone_are_irrelevant(self):
        v=rebaseline.assess('gamma (mod.py:14-15).','src/mod.py',['src/mod.py'],MOD,MOD)
        self.assertFalse(v['relevant'])
    def test_paths_and_links_are_not_identifiers(self):
        names=rebaseline.identifiers('See `core/maps.py` and [[Some_note]] in session_start.py; `build_context` calls _open_lines.')
        self.assertEqual(names,{'build_context','_open_lines'})

class Base(StoreFixture):
    def setUp(self):
        super().setUp()
        self.project=self.base/'project';(self.project/'src').mkdir(parents=True,exist_ok=True)
        self.mod=self.project/'src'/'mod.py';self.mod.write_bytes(MOD.encode())
    def cap(self,body,**kw):
        kw.setdefault('files',['src/mod.py'])
        return capture(kw.pop('title','Mod gamma'),body,TAGS,'demo',self.root,allow_similar=True,**kw)
    def get(self,made):return find_note(made['id'],'demo',self.root)[1]
    def stale(self,made):return digests.changed(self.get(made).get('file_hashes') or self.get(made).get('sources'),'demo',self.root)
    def edit_alpha(self):self.mod.write_bytes(MOD.replace('    return x + 1\n','    y = x + 1\n    return y\n').encode())

class Sweep(Base):
    def test_recording_a_hash_keeps_the_baseline(self):
        made=self.cap('gamma subtracts one (mod.py:14-15).')
        self.assertEqual(rebaseline.load(self.root,self.get(made)['file_hashes'][0]['sha256']),MOD.encode())
    def test_an_irrelevant_change_is_rebaselined_with_shifted_lines_and_the_baseline_dates_kept(self):
        made=self.cap('gamma subtracts one (mod.py:14-15).');before=self.get(made)
        self.edit_alpha();self.assertEqual(self.stale(made),['src/mod.py'])
        res=rebaseline.sweep(self.root)
        self.assertEqual((res['rebaselined'],res['still_stale']),(1,0));self.assertTrue(res['notes'][0]['rebaselined'])
        after=self.get(made)
        self.assertEqual(self.stale(made),[]);self.assertIn('(mod.py:15-16)',after['body'])
        for key in ('updated','checked','review_by','confirmed'):self.assertEqual(after.get(key),before.get(key),key)
        self.assertEqual(rebaseline.load(self.root,sha(self.mod.read_bytes())),self.mod.read_bytes())
        governance.undo(self.root,res['notes'][0]['operation'])
        self.assertEqual(self.stale(made),['src/mod.py']);self.assertIn('(mod.py:14-15)',self.get(made)['body'])
    def test_a_relevant_change_stays_stale_and_nothing_is_written(self):
        made=self.cap('gamma subtracts one (mod.py:14-15).');rev=self.get(made)['revision']
        self.mod.write_bytes(MOD.replace('z - 1','z - 2').encode())
        res=rebaseline.sweep(self.root)
        self.assertEqual((res['rebaselined'],res['still_stale']),(0,1));self.assertEqual(self.get(made)['revision'],rev)
    def test_a_dry_run_writes_nothing(self):
        made=self.cap('gamma subtracts one (mod.py:14-15).');rev=self.get(made)['revision'];self.edit_alpha()
        res=rebaseline.sweep(self.root,dry_run=True)
        self.assertEqual(res['rebaselined'],1);self.assertFalse(res['notes'][0]['rebaselined']);self.assertEqual(self.get(made)['revision'],rev)
    def test_without_a_baseline_or_git_the_note_stays_stale(self):
        made=self.cap('gamma subtracts one (mod.py:14-15).');shutil.rmtree(self.root/'.cache'/'baselines');self.edit_alpha()
        res=rebaseline.sweep(self.root)
        self.assertEqual(res['rebaselined'],0);self.assertIn('no baseline',res['notes'][0]['files'][0]['reason'])
    def test_a_digest_source_is_rebaselined_too(self):
        source=digests.describe(self.root,'demo','src/mod.py')
        made=self.cap('gamma subtracts one (mod.py:14-15).',files=None,sources=[source],title='Mod digest')
        self.edit_alpha();res=rebaseline.sweep(self.root)
        self.assertEqual(res['rebaselined'],1);self.assertEqual(self.get(made)['sources'][0]['sha256'],sha(self.mod.read_bytes()))
    def test_only_the_irrelevant_file_of_two_is_rebaselined(self):
        other=self.project/'src'/'other.py';other.write_bytes(b'value = 1\n')
        made=self.cap('gamma subtracts one (mod.py:14-15); `value` is set in other.py:1.',files=['src/mod.py','src/other.py'])
        self.edit_alpha();other.write_bytes(b'value = 2\n')
        rebaseline.sweep(self.root)
        self.assertEqual(self.stale(made),['src/other.py']);self.assertIn('(mod.py:15-16)',self.get(made)['body'])
    def test_old_unreferenced_baselines_are_pruned(self):
        rebaseline.keep(self.root,self.mod);self.mod.write_bytes(b'x = 1\n')
        self.assertEqual(rebaseline.prune(self.root,now=4_000_000_000),1)

@unittest.skipUnless(GIT,'git is not installed')
class Backfill(Base):
    def git(self,*args):
        env={**os.environ,'GIT_AUTHOR_NAME':'t','GIT_AUTHOR_EMAIL':'t@example.org','GIT_COMMITTER_NAME':'t','GIT_COMMITTER_EMAIL':'t@example.org'}
        return subprocess.run(['git','-C',str(self.project),*args],check=True,capture_output=True,env=env)
    def test_a_hash_recorded_before_baselines_is_found_in_git_history(self):
        self.git('init','-q');self.git('add','-A');self.git('commit','-q','-m','one')
        made=self.cap('gamma subtracts one (mod.py:14-15).');shutil.rmtree(self.root/'.cache'/'baselines')
        self.edit_alpha();res=rebaseline.sweep(self.root)
        self.assertEqual(res['rebaselined'],1,res);self.assertEqual(self.stale(made),[])
    def test_a_crlf_checkout_of_an_lf_blob_is_found(self):
        self.git('init','-q');self.git('config','core.autocrlf','true')
        self.mod.write_bytes(MOD.replace('\n','\r\n').encode());self.git('add','-A');self.git('commit','-q','-m','one')
        made=self.cap('gamma subtracts one (mod.py:14-15).');shutil.rmtree(self.root/'.cache'/'baselines')
        self.mod.write_bytes(MOD.replace('    return x + 1\n','    y = x + 1\n    return y\n').replace('\n','\r\n').encode())
        res=rebaseline.sweep(self.root)
        self.assertEqual(res['rebaselined'],1,res);self.assertIn('(mod.py:15-16)',self.get(made)['body'])

class Schedule(Base):
    def go(self,bindings=None,**kw):
        with patch.dict('os.environ',{'LIVING_MEMORY_NO_BACKGROUND':''}),patch('core.rebaseline._spawn') as spawn:
            return rebaseline.schedule(self.root,bindings or {},**kw),spawn.call_count
    def test_spawns_at_most_once_an_hour(self):
        self.assertEqual(self.go(),('spawned',1));self.assertEqual(self.go(),('cooldown',0))
        marker=self.root/'hooks'/'last-rebaseline.json';marker.write_text(json.dumps({'at':0}))
        self.assertEqual(self.go(),('spawned',1))
    def test_off_switch_subagents_and_compaction(self):
        self.assertEqual(self.go({'auto_rebaseline':False}),('disabled',0))
        self.assertEqual(self.go(subagent=True),('subagent',0));self.assertEqual(self.go(compact=True),('compact',0))
        self.assertEqual(self.go(plan=True),('plan_mode',0))
    def test_the_scheduled_sweep_skips_while_another_runs(self):
        from core.common import file_lock
        with file_lock(self.root/'hooks'/'rebaseline.lock'):
            self.assertEqual(rebaseline.scheduled(self.root)['skipped'],'already_running')

class Command(Base):
    def cli(self,*argv,actor=None):
        import contextlib,io
        from core.cli import main
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            code=main(['--root',str(self.root),*(['--actor',actor] if actor else []),*argv])
        return code,json.loads(out.getvalue() or err.getvalue())
    def test_dry_run_lists_and_the_main_agent_applies(self):
        made=self.cap('gamma subtracts one (mod.py:14-15).');self.edit_alpha()
        code,res=self.cli('rebaseline','--dry-run');self.assertEqual((code,res['rebaselined'],res['dry_run']),(0,1,True))
        self.assertEqual(self.stale(made),['src/mod.py'])
        code,res=self.cli('rebaseline');self.assertEqual((code,res['rebaselined']),(0,1));self.assertEqual(self.stale(made),[])
    def test_subagents_and_viewers_may_only_dry_run(self):
        self.cap('gamma subtracts one (mod.py:14-15).');self.edit_alpha()
        for actor in ('subagent','viewer'):
            self.assertEqual(self.cli('rebaseline','--dry-run',actor=actor)[0],0,actor)
            code,res=self.cli('rebaseline',actor=actor);self.assertNotEqual(code,0,actor);self.assertIn('rebaseline --dry-run is allowed',json.dumps(res))
    def test_the_hook_allows_it_and_denies_a_subagent_the_write(self):
        import sys
        from core import runtime
        base=f'"{sys.executable}" "{runtime.SCRIPT}" --root "{self.root}" --domain demo rebaseline'
        self.assertIn('rebaseline',runtime.ALLOWED_SUBCOMMANDS)
        self.assertTrue(runtime.rebaseline_write(base,'posix'));self.assertFalse(runtime.rebaseline_write(base+' --dry-run','posix'))
        for command,write in ((base,True),(base+' --dry-run',False)):
            payload={'cwd':str(self.project),'session_id':'h','tool_name':'Bash','tool_input':{'command':command}}
            self.assertIn('allow',runtime.process('claude','pre-tool-use',payload,self.root),command)
            reply=runtime.process('claude','pre-tool-use',{**payload,'agent_id':'sub-1'},self.root)
            self.assertEqual('deny' in reply,write,command)
    def test_an_operation_reads_as_rebaseline_in_the_activity_log(self):
        from core import activity
        self.cap('gamma subtracts one (mod.py:14-15).');self.edit_alpha();res=rebaseline.sweep(self.root)
        rows=[r for r in activity.journal_rows(self.root) if r.get('op')==res['notes'][0]['operation']]
        self.assertTrue(rows);self.assertEqual({r.get('actor') for r in rows},{'rebaseline'})

if __name__=='__main__':unittest.main()
