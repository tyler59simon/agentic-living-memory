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

    # -- Codex review of 7980f44
    def test_a_change_to_a_member_of_a_cited_class_is_relevant(self):
        old='class RetryPolicy:\n    """Retries."""\n\n    def max_tries(self):\n        return 3\n\n\ndef other():\n    return 1\n\n\ndef more():\n    return 2\n'
        v=rebaseline.assess('RetryPolicy retries three times.','src/r.py',['src/r.py'],old,old.replace('return 3','return 5'))
        self.assertTrue(v['relevant']);self.assertIn('RetryPolicy',v['reason'])
        v=rebaseline.assess('RetryPolicy retries three times.','src/r.py',['src/r.py'],old,old.replace('    return 2\n','    return 2\n\n\ndef extra():\n    return 4\n'))
        self.assertFalse(v['relevant'],v['reason'])   # a change outside the class still clears
    def test_a_short_name_in_backticks_counts(self):
        old=MOD.replace('LIMIT = 3\n','LIMIT = 3\nID = 1\n')
        v=self.verdict('`beta` returns `ID` (mod.py:10-12).',old.replace('ID = 1','ID = 2'),old=old)
        self.assertTrue(v['relevant']);self.assertIn('ID',v['reason'])
    def test_a_line_citation_belongs_to_the_file_named_before_it(self):
        found=rebaseline.citations('See a.txt:10 and b.txt (line 30).',['a.txt','b.txt'])
        self.assertEqual([(sorted(c['files']),c['start']) for c in found],[(['a.txt'],10),(['b.txt'],30)])
        found=rebaseline.citations('See mod.py:10 and notes.md (line 30).',['src/mod.py'])
        self.assertEqual([(sorted(c['files']),c['start']) for c in found],[(['src/mod.py'],10),([],30)])
        found=rebaseline.citations('The limit is set e.g. at line 3.',['src/mod.py'])
        self.assertEqual([(sorted(c['files']),c['start']) for c in found],[(['src/mod.py'],3)])
    def test_a_file_of_repeated_lines_is_compared_quickly(self):
        import time
        a=['a']+['x']*40000+['b'];b=['c']+['x']*40000+['d'];start=time.perf_counter()
        ops=rebaseline._opcodes(a,b)
        self.assertLess(time.perf_counter()-start,2.0)
        self.assertEqual(sum(i2-i1 for t,i1,i2,_,_ in ops),len(a));self.assertEqual(sum(j2-j1 for t,_,_,j1,j2 in ops),len(b))

    # -- Codex re-review of c1ec616
    def test_a_file_named_between_without_qualifying_the_line_leaves_it_ambiguous(self):
        found=rebaseline.citations('See r.txt:3. Unlike defaults.toml, this file sets the limit at line 20.',['r.txt'])
        self.assertEqual([(c['start'],c['ambiguous']) for c in found],[(3,False),(20,True)])
        found=rebaseline.citations('See r.txt:3 and defaults.toml, line 20.',['r.txt','defaults.toml'])
        self.assertEqual([(sorted(c['files']),c['ambiguous']) for c in found],[(['r.txt'],False),(['defaults.toml'],False)])
    def test_a_citation_whose_lines_now_occur_twice_stays_stale(self):
        old=MOD+''.join(f'pad{i} = {i}\n' for i in range(40))
        lines=old.split('\n');new='\n'.join(lines[:5]+['# copy above']+lines)
        v=rebaseline.assess('The limit is set at mod.py:2.','src/mod.py',['src/mod.py'],old,new)
        self.assertTrue(v['relevant']);self.assertIn('more than once',v['reason'])
    def test_members_are_indexed_once(self):
        import time
        old=''.join(f'def f{i}(x):\n    return x + {i}\n\n\n' for i in range(3000))
        new=old+'def extra():\n    return 0\n';start=time.perf_counter()
        v=rebaseline.assess('These functions add (m.py:1-11000).','src/m.py',['src/m.py'],old,new)
        self.assertLess(time.perf_counter()-start,0.5);self.assertFalse(v['relevant'],v['reason'])   # 1.3 s with a scan per cited symbol

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

    def test_a_file_holding_a_credential_is_not_kept_as_a_baseline(self):
        key='sk-ant-'+'Ab1'*12
        self.mod.write_bytes((MOD+f'TOKEN = "{key}"\n').encode())
        made=self.cap('gamma subtracts one (mod.py:14-15).')
        self.assertIsNone(rebaseline.load(self.root,self.get(made)['file_hashes'][0]['sha256']))
        self.assertIsNone(rebaseline.keep(self.root,self.mod))
    def test_a_missing_file_counts_as_still_stale(self):
        made=self.cap('gamma subtracts one (mod.py:14-15).');self.mod.unlink()
        res=rebaseline.sweep(self.root)
        self.assertEqual((res['rebaselined'],res['still_stale']),(0,1));self.assertEqual(res['notes'][0]['files'][0]['reason'],'the file is missing')
    def test_git_never_fetches_from_a_remote(self):
        with patch('core.rebaseline.subprocess.run') as run:
            run.return_value=subprocess.CompletedProcess([],0,b'',b'')
            rebaseline._git(self.project,'log')
        self.assertEqual(run.call_args.kwargs['env']['GIT_NO_LAZY_FETCH'],'1')
    def test_the_setting_turns_off_the_run_sweep(self):
        from core import settings
        self.assertIs(settings.validate('auto_rebaseline',False),False)
        bindings=json.loads((self.root/'bindings.json').read_text(encoding='utf-8'));bindings['auto_rebaseline']=False
        (self.root/'bindings.json').write_text(json.dumps(bindings),encoding='utf-8')
        made=self.cap('gamma subtracts one (mod.py:14-15).');self.edit_alpha()
        self.assertEqual(rebaseline.before_review(self.root,[{'note':self.get(made),'domain':'demo'}],5),0)
        self.assertEqual(self.stale(made),['src/mod.py'])

    def test_a_quoted_credential_key_or_a_long_line_is_not_cached(self):
        from core.repository import find_secret
        self.assertTrue(find_secret('{"password": "hunter2hunter2"}'))
        self.mod.write_bytes((MOD+'CONFIG = {"password": "hunter2hunter2"}\n').encode());self.assertIsNone(rebaseline.keep(self.root,self.mod))
        self.mod.write_bytes((MOD+'NAME = "'+'a_'*1500+'"\n').encode());self.assertIsNone(rebaseline.keep(self.root,self.mod))
    def test_the_pre_review_pass_rebaselines_only_the_picked_notes_within_its_time(self):
        made=self.cap('gamma subtracts one (mod.py:14-15).');other=self.cap('gamma again (mod.py:14-15).',title='Mod gamma two');self.edit_alpha()
        self.assertEqual(rebaseline.before_review(self.root,[{'note':self.get(made),'domain':'demo'}],0),0)
        self.assertEqual(rebaseline.before_review(self.root,[{'note':self.get(made),'domain':'demo'}],5),1)
        self.assertEqual(self.stale(made),[]);self.assertEqual(self.stale(other),['src/mod.py'])

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

    def test_a_dry_run_keeps_nothing_it_finds_in_git(self):
        self.git('init','-q');self.git('add','-A');self.git('commit','-q','-m','one')
        made=self.cap('gamma subtracts one (mod.py:14-15).');recorded=self.get(made)['file_hashes'][0]['sha256']
        shutil.rmtree(self.root/'.cache'/'baselines');self.edit_alpha()
        res=rebaseline.sweep(self.root,dry_run=True)
        self.assertEqual(res['rebaselined'],1,res);self.assertIsNone(rebaseline.load(self.root,recorded))

    def test_a_partial_clone_is_not_read(self):
        self.git('init','-q');self.git('add','-A');self.git('commit','-q','-m','one');self.git('config','remote.origin.promisor','true')
        made=self.cap('gamma subtracts one (mod.py:14-15).');shutil.rmtree(self.root/'.cache'/'baselines');self.edit_alpha()
        rebaseline._PARTIAL.clear()
        res=rebaseline.sweep(self.root,dry_run=True)
        self.assertEqual(res['rebaselined'],0,res);self.assertIn('no baseline',res['notes'][0]['files'][0]['reason'])

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

from test_maintenance import Base as MaintenanceBase
from core import maintenance
class BeforeReview(MaintenanceBase):
    def test_a_maintenance_run_rebaselines_before_it_picks_notes(self):
        text=lambda v:'retry = 3\n'+''.join(f'x{i} = {i}\n' for i in range(12))+f'tail = {v}\n'
        self.src('r.py',text(1));n=self.mk('Retry limit','The retry limit is 3 (r.py:1).',files=['src/r.py']);self.src('r.py',text(2))
        self.assertEqual([c['ref'] for c in self.cands()],[self.ref(n)])
        seen=[]
        def adapter(req):seen.append(req);return {'ok':True,'report':{'version':1,'run_id':req['run']['id'],'decisions':[]},'usage_micro':1,'stderr':''}
        self.tick();res=maintenance.run(self.root,adapter=adapter,now=self.now)
        self.assertEqual((seen,res.get('skip_reason')),([],'no_candidates'));self.assertEqual(self.cands(),[])
    def test_a_dry_run_does_not_sweep(self):
        self.src('r.py','retry = 3\n'+'x = 1\n'*12+'tail = 1\n');n=self.mk('Retry limit','The retry limit is 3 (r.py:1).',files=['src/r.py'])
        self.src('r.py','retry = 3\n'+'x = 1\n'*12+'tail = 2\n');rev=self.get(n)['revision']
        self.go(dry=True);self.assertEqual(self.get(n)['revision'],rev)
