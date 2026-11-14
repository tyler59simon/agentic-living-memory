"""C2: `file_hashes` on knowledge-type notes that list files, `checked` on confirm, and check / rewrite-plan leaving both alone."""
import hashlib,os,shutil,subprocess,time,unittest
from pathlib import Path
from test_core import StoreFixture
from unittest.mock import patch
from core import digests,notes,runtime,search
from core.notes import capture,change,check_action,confirm,find_note,rewrite_plan
from core.repository import validate

TAGS=['subject/project','concern/architecture']
A='retry = 3\n';B='timeout = 30\n'
BUG_BODY='Retries stop early.\n\n## Action Items\n\n- [ ] Fix the loop\n'

def sha(text):return hashlib.sha256(text.encode()).hexdigest()
def put(path,text):Path(path).write_bytes(text.encode())

class Base(StoreFixture):
    def setUp(self):
        super().setUp()
        self.project=self.base/'project';(self.project/'src'/'pkg').mkdir(parents=True)
        for name,text in {'a.py':A,'b.py':B,'c.py':'c\n','d.py':'d\n','e.py':'e\n','f.py':'f\n'}.items():put(self.project/'src'/name,text)
    def cap(self,title,files,**kw):
        return capture(title,kw.pop('body','The retry limit is read from the config.'),TAGS,'demo',self.root,files=files,allow_similar=True,**kw)
    def note_of(self,made):return find_note(made['id'],'demo',self.root)[1]
    def hashes(self,made):return {h['path']:h['sha256'] for h in self.note_of(made).get('file_hashes') or []}

class FileCurrency(Base):
    def test_a_knowledge_note_hashes_the_regular_files_it_lists(self):
        made=self.cap('Retry limit',['src/a.py','src/b.py'])
        self.assertEqual(self.hashes(made),{'src/a.py':sha(A),'src/b.py':sha(B)})

    def test_only_the_types_a_changed_file_can_falsify_get_hashes(self):
        for kind in ('knowledge','runbook','policy','bug','risk','research'):
            made=self.cap(f'Retry {kind}',['src/a.py'],type=kind);self.assertEqual(self.hashes(made),{'src/a.py':sha(A)},kind)
        for kind in ('decision','plan','status','preference','context'):
            made=self.cap(f'Retry {kind}',['src/a.py'],type=kind);self.assertNotIn('file_hashes',self.note_of(made),kind)

    def test_at_most_five_files_and_never_a_folder_a_glob_a_missing_file_or_a_digested_one(self):
        files=['src/pkg','src/*.py','src/missing.py','src/a.py','src/b.py','src/c.py','src/d.py','src/e.py','src/f.py']
        made=self.cap('Many files',files);self.assertEqual(list(self.hashes(made)),['src/a.py','src/b.py','src/c.py','src/d.py','src/e.py'])
        digested=self.cap('Digested file',['src/a.py','src/b.py'],sources=[{'path':'src/a.py','sha256':sha(A)}])
        self.assertEqual(list(self.hashes(digested)),['src/b.py'])

    def test_an_absolute_file_outside_the_project_is_hashed(self):
        outside=self.base/'elsewhere.txt';put(outside,'x\n')
        made=self.cap('Outside file',[str(outside).replace(os.sep,'/')]);self.assertEqual(list(self.hashes(made).values()),[sha('x\n')])
        self.assertEqual(digests.changed(self.note_of(made)['file_hashes'],'demo',self.root),[])

    def test_a_changed_file_is_stale_and_confirm_rehashes_and_sets_checked(self):
        made=self.cap('Retry limit',['src/a.py'])
        self.assertNotIn('checked',self.note_of(made));self.assertEqual(digests.stale_note(self.note_of(made),self.root),'')
        put(self.project/'src'/'a.py','retry = 5\n')
        self.assertEqual(digests.stale_note(self.note_of(made),self.root),'STALE: src/a.py changed since this note')
        before=self.note_of(made);confirm(made['id'],'demo',self.root);after=self.note_of(made)
        self.assertEqual(after['file_hashes'],[{'path':'src/a.py','sha256':sha('retry = 5\n')}]);self.assertEqual(digests.stale_note(after,self.root),'')
        validate(after,'demo',self.root);self.assertEqual(after['updated'],before['updated']);self.assertGreater(after['checked'],before['updated'])

    def test_confirm_starts_hashes_for_a_note_written_before_this_field(self):
        made=self.cap('Old note',['src/a.py']);path,note=find_note(made['id'],'demo',self.root)
        note.pop('file_hashes');Path(path).write_text(notes.serialize(note),encoding='utf-8')
        confirm(made['id'],'demo',self.root);self.assertEqual(self.hashes(made),{'src/a.py':sha(A)})

    def test_a_second_confirm_the_same_day_runs_again_when_the_file_changed(self):
        made=self.cap('Retry limit',['src/a.py']);confirm(made['id'],'demo',self.root)
        self.assertEqual(confirm(made['id'],'demo',self.root)['status'],'unchanged')
        put(self.project/'src'/'a.py','retry = 6\n');self.assertEqual(confirm(made['id'],'demo',self.root)['status'],'confirm')
        self.assertEqual(self.hashes(made),{'src/a.py':sha('retry = 6\n')})

    def test_decision_notes_get_no_hashes_but_confirm_still_sets_checked(self):
        made=self.cap('Retry decision',['src/a.py'],type='decision');confirm(made['id'],'demo',self.root);note=self.note_of(made)
        self.assertNotIn('file_hashes',note);self.assertIn('checked',note)

    def test_a_body_update_rehashes_and_a_files_only_update_keeps_the_hashes_of_files_still_listed(self):
        made=self.cap('Retry limit',['src/a.py','src/b.py']);put(self.project/'src'/'a.py','retry = 7\n')
        change(made['id'],'demo','update',self.root,files=['src/a.py','src/c.py'])
        self.assertEqual(self.hashes(made),{'src/a.py':sha(A),'src/c.py':sha('c\n')})   # a.py stays stale: only the list changed
        change(made['id'],'demo','update',self.root,body='The retry limit is 7 now.')
        self.assertEqual(self.hashes(made)['src/a.py'],sha('retry = 7\n'))

    def test_a_files_only_update_does_not_bump_updated(self):
        made=self.cap('Retry limit',['src/a.py']);before=self.note_of(made)['updated']
        change(made['id'],'demo','update',self.root,files=['src/a.py','src/b.py']);self.assertEqual(self.note_of(made)['updated'],before)

    def test_a_type_change_drops_or_starts_hashes(self):
        made=self.cap('Retry limit',['src/a.py'])
        change(made['id'],'demo','update',self.root,type='decision');self.assertNotIn('file_hashes',self.note_of(made))
        change(made['id'],'demo','update',self.root,type='runbook');self.assertEqual(self.hashes(made),{'src/a.py':sha(A)})

    def test_check_and_rewrite_plan_do_not_rehash(self):
        bug=self.cap('Retry bug',['src/a.py'],type='bug',body=BUG_BODY);put(self.project/'src'/'a.py','retry = 8\n')
        check_action(bug['id'],'demo',self.root,item='Fix the loop');self.assertEqual(self.hashes(bug),{'src/a.py':sha(A)})   # still stale
        plan=rewrite_plan([{'ref':f"demo:{bug['id']}",'revision':self.note_of(bug)['revision'],'body':'Retries stop after one attempt.'}],'demo',self.root)
        self.assertTrue(plan.get('notes'),plan)
        self.assertEqual(self.hashes(bug),{'src/a.py':sha(A)})   # planned, not applied: the note is untouched

    def test_rehash_false_leaves_both_hash_lists_in_the_planned_text(self):
        from core.repository import parse_text
        bug=self.cap('Retry bug',['src/a.py'],type='bug',body=BUG_BODY,sources=[{'path':'src/b.py','sha256':sha(B)}])
        put(self.project/'src'/'a.py','retry = 9'+chr(10));put(self.project/'src'/'b.py','timeout = 9'+chr(10))
        for rehash,hashed,digested in ((False,sha(A),sha(B)),(True,sha('retry = 9'+chr(10)),sha('timeout = 9'+chr(10)))):
            changes,_,target=notes._change_set(bug['id'],'demo','update',self.root,rehash=rehash,body='Retries stop after one attempt.')
            planned=parse_text(changes[target.relative_to(self.root).as_posix()],target)
            self.assertEqual((planned['file_hashes'][0]['sha256'],planned['sources'][0]['sha256']),(hashed,digested),rehash)

    def test_check_does_not_rehash_a_digests_sources_either(self):
        bug=self.cap('Digest with actions',['src/a.py'],type='bug',body=BUG_BODY,sources=[{'path':'src/b.py','sha256':sha(B)}])
        put(self.project/'src'/'b.py','timeout = 99\n');check_action(bug['id'],'demo',self.root,item='Fix the loop')
        self.assertEqual(self.note_of(bug)['sources'],[{'path':'src/b.py','sha256':sha(B)}])

    def test_validation_rejects_a_bad_file_hashes_or_checked(self):
        note=dict(self.note_of(self.cap('Retry limit',['src/a.py'])))
        for bad in ({'file_hashes':'x'},{'file_hashes':[{'path':'a','sha256':'zz'}]},{'file_hashes':[{'path':'a','sha256':'0'*64}]*31},{'checked':'yesterday'},{'checked':5}):
            with self.assertRaises(ValueError,msg=bad):validate({**note,**bad},'demo',self.root)

    def test_the_index_carries_file_hashes_and_search_reports_them(self):
        self.cap('Retry limit',['src/a.py']);search.rebuild_index('demo',self.root)
        self.assertEqual(search.file_notes('src/a.py',['demo'],self.root,ensure_fresh=False)[0]['file_hashes'],[{'path':'src/a.py','sha256':sha(A)}])
        self.assertEqual(search.search_domain('retry limit config','demo',root=self.root)[0]['file_hashes'][0]['path'],'src/a.py')

class Freshness(Base):
    """MAINT-1A: only a body rewrite, an explicit confirm or a files change speaks for the files; a missing file keeps its hash; `checked` is the baseline."""
    def legacy(self,made):
        """Strip `checked` like a note written before the field: its baseline is `updated`."""
        path,note=find_note(made['id'],'demo',self.root);note.pop('checked',None);Path(path).write_text(notes.serialize(note),encoding='utf-8');return self.note_of(made)

    def test_f1_a_rename_of_a_stale_note_stays_stale_and_keeps_its_hash(self):
        made=self.cap('Retry limit',['src/a.py'],body='The retry limit is 3.');put(self.project/'src'/'a.py','retry = 9\n')
        change(made['id'],'demo','rename',self.root,title='Retry cap')
        note=self.note_of(made);self.assertEqual(self.hashes(made),{'src/a.py':sha(A)})
        self.assertEqual(digests.stale_note(note,self.root),'STALE: src/a.py changed since this note')

    def test_f1_title_and_type_only_edits_keep_every_hash(self):
        made=self.cap('Retry limit',['src/a.py']);put(self.project/'src'/'a.py','retry = 9\n')
        change(made['id'],'demo','update',self.root,type='runbook');self.assertEqual(self.hashes(made),{'src/a.py':sha(A)})
        change(made['id'],'demo','update',self.root,title='Retry cap',context='why');self.assertEqual(self.hashes(made),{'src/a.py':sha(A)})
        change(made['id'],'demo','update',self.root,commands=['make test']);self.assertEqual(self.hashes(made),{'src/a.py':sha(A)})

    def test_a_type_change_starts_hashes_only_for_files_without_one(self):
        made=self.cap('Retry limit',['src/a.py'],type='decision');change(made['id'],'demo','update',self.root,files=['src/a.py','src/b.py'])
        put(self.project/'src'/'a.py','retry = 9\n')
        change(made['id'],'demo','update',self.root,type='knowledge');self.assertEqual(self.hashes(made),{'src/a.py':sha('retry = 9\n'),'src/b.py':sha(B)})
        made=self.cap('Retry cap',['src/a.py','src/b.py']);put(self.project/'src'/'b.py','timeout = 1\n')
        change(made['id'],'demo','update',self.root,type='runbook')
        self.assertEqual(self.hashes(made)['src/b.py'],sha(B))   # still the old hash: a type change does not vouch

    def test_a_cosmetic_edit_to_a_legacy_note_keeps_its_baseline_in_checked(self):
        made=self.cap('Retry limit',['src/a.py']);before=self.legacy(made)['updated']
        change(made['id'],'demo','rename',self.root,title='Retry cap');note=self.note_of(made)
        self.assertEqual(note['checked'],before);self.assertGreater(note['updated'],before)
        change(made['id'],'demo','update',self.root,body='The retry limit is 4 now.');note=self.note_of(made)
        self.assertEqual(note['checked'],note['updated'])   # a body rewrite is the author's word for the facts

    def test_verified_at_is_checked_else_updated_else_created(self):
        from core.note_types import verified_at
        self.assertEqual(verified_at({'checked':'2026-01-02T00:00:00+00:00','updated':'2026-02-01T00:00:00+00:00'}),'2026-01-02T00:00:00+00:00')
        self.assertEqual(verified_at({'updated':'2026-02-01T00:00:00+00:00','created':'2026-01-01T00:00:00+00:00'}),'2026-02-01T00:00:00+00:00')
        self.assertEqual(verified_at({'created':'2026-01-01T00:00:00+00:00'}),'2026-01-01T00:00:00+00:00');self.assertEqual(verified_at({}),'')

    def test_f2_confirm_after_the_file_was_deleted_keeps_the_hash_and_reports_it_missing(self):
        made=self.cap('Retry limit',['src/a.py']);(self.project/'src'/'a.py').unlink()
        confirm(made['id'],'demo',self.root);note=self.note_of(made)
        self.assertEqual(self.hashes(made),{'src/a.py':sha(A)});self.assertEqual(digests.stale_note(note,self.root),'STALE: src/a.py is missing')
        self.assertEqual(digests.states(note['file_hashes'],'demo',self.root),{'src/a.py':'missing'})

    def test_a_body_update_after_a_deletion_keeps_the_hash_and_new_files_get_one(self):
        made=self.cap('Retry limit',['src/a.py','src/b.py']);(self.project/'src'/'a.py').unlink();put(self.project/'src'/'b.py','timeout = 1\n')
        change(made['id'],'demo','update',self.root,body='The timeout moved.',files=['src/a.py','src/b.py','src/c.py'])
        self.assertEqual(self.hashes(made),{'src/a.py':sha(A),'src/b.py':sha('timeout = 1\n'),'src/c.py':sha('c\n')})

    def test_removing_a_file_from_the_list_drops_only_its_hash(self):
        made=self.cap('Retry limit',['src/a.py','src/b.py']);(self.project/'src'/'a.py').unlink()
        change(made['id'],'demo','update',self.root,files=['src/b.py']);self.assertEqual(self.hashes(made),{'src/b.py':sha(B)})

    def test_source_states_current_changed_missing_unavailable_unchecked(self):
        outside=self.base/'outside.txt';put(outside,'x\n');path=str(outside).replace(os.sep,'/')
        listed=[{'path':'src/a.py','sha256':sha(A)},{'path':'src/b.py','sha256':sha('other')},{'path':'src/gone.py','sha256':sha('g')},{'path':path,'sha256':sha('x\n')},{'path':'src/c.py','sha256':''}]
        outside.unlink()
        self.assertEqual(digests.states(listed,'demo',self.root),{'src/a.py':'current','src/b.py':'changed','src/gone.py':'missing',path:'unavailable','src/c.py':'unchecked'})
        self.assertEqual(digests.changed(listed[:3],'demo',self.root),['src/b.py','src/gone.py'])   # `changed` keeps its meaning: a missing file counts

    def test_a_missing_file_has_its_own_wording_and_notes_carry_a_currency_field(self):
        from core.worklist import currency_of
        made=self.cap('Retry limit',['src/a.py','src/b.py']);put(self.project/'src'/'a.py','retry = 5\n');(self.project/'src'/'b.py').unlink()
        note=self.note_of(made);self.assertEqual(digests.stale_note(note,self.root),'STALE: src/a.py changed since this note; src/b.py is missing')
        info=currency_of(note,self.root);self.assertEqual(info['state'],'changed');self.assertEqual(info['files'],[{'path':'src/a.py','state':'changed'},{'path':'src/b.py','state':'missing'}])

    def test_the_cli_read_has_a_currency_field(self):
        import contextlib,io,json
        from core.cli import main
        made=self.cap('Retry limit',['src/a.py']);out=io.StringIO()
        with contextlib.redirect_stdout(out):main(['--root',str(self.root),'--domain','demo','read',made['id']])
        self.assertEqual(json.loads(out.getvalue())['currency']['state'],'current')

    def test_a_checkbox_does_not_write_checked(self):
        bug=self.cap('Retry bug',['src/a.py'],type='bug',body=BUG_BODY);self.legacy(bug)
        check_action(bug['id'],'demo',self.root,item='Fix the loop');self.assertNotIn('checked',self.note_of(bug))

class BatchedFollow(Base):
    """C5: one batch lookup for files no hint looked up and for files a shell or git changed; the pull guard; one sentence for many notes."""
    def state(self,**extra):return {'mutating':True,'shell':True,**extra}
    def follow(self,state,files=(),pulled=False):
        return runtime.follow_pending(self.root,state,runtime.load_bindings(self.root),'demo',lambda:({f:self.project/f for f in files},pulled,set()))

    def test_files_notes_matches_files_sources_hashes_and_bug_bodies_in_one_scan(self):
        a=self.cap('Retry limit',['src/a.py']);b=self.cap('Timeout digest',['src/b.py'],sources=[{'path':'src/b.py','sha256':sha(B)}])
        c=self.cap('Config bug',['src/other.py'],type='bug',body='Setting src/c.py to zero hangs.')
        found=search.files_notes(['src/a.py','src/b.py','src/c.py','src/none.py'],['demo'],self.root,per_file=1)
        ids={p:[h['id'] for h in hits] for p,hits in found.items()}
        self.assertEqual(ids,{'src/a.py':[a['id']],'src/b.py':[b['id']],'src/c.py':[c['id']],'src/none.py':[]})
        self.assertEqual(found['src/a.py'][0]['file_hashes'][0]['path'],'src/a.py');self.assertEqual(found['src/b.py'][0]['sources'][0]['path'],'src/b.py')
        self.assertEqual(found['src/c.py'][0]['match'],'body')

    def test_files_changed_by_a_shell_command_are_followed_and_a_pull_is_not(self):
        a=self.cap('Retry limit',['src/a.py'])
        self.assertEqual([d['ref'] for k,d in self.follow(self.state(),['src/a.py'])],[f"demo:{a['id']}"])
        self.assertEqual(self.follow(self.state(),['src/a.py'],pulled=True),[])
        edited=self.state(touched=['src/a.py'],hinted={'src/a.py':[f"demo:{a['id']}"]})   # the agent's own edit still counts in a pull
        self.assertEqual(len(self.follow(edited,['src/a.py'],pulled=True)),1)

    def test_a_note_that_only_names_the_file_in_its_body_is_followed_only_when_it_is_a_bug_or_decision(self):
        self.cap('Mentions',['src/b.py'],body='Also see src/c.py for the default.');bug=self.cap('Bug',['src/b.py'],type='bug',body='src/c.py is read twice.')
        refs=[d['ref'] for k,d in self.follow(self.state(),['src/c.py'])];self.assertEqual(refs,[f"demo:{bug['id']}"])

    def test_files_past_the_hint_cap_count_and_already_hinted_ones_are_not_looked_up_again(self):
        a=self.cap('Retry limit',['src/a.py'])
        self.assertEqual([d['path'] for k,d in self.follow(self.state(unlooked=['src/a.py']))],['src/a.py'])
        hinted=self.state(unlooked=['src/a.py'],hinted={'src/a.py':[]})
        with patch.object(search,'files_notes',wraps=search.files_notes) as batch:
            self.assertEqual(self.follow(hinted),[]);batch.assert_not_called()

    def test_turn_changed_is_asked_once_per_stop_for_both_reminders(self):
        self.cap('Retry limit',['src/a.py']);calls=[]
        def fake(*args):calls.append(1);return {'src/a.py':self.project/'src'/'a.py'},False,set()
        with patch.object(runtime,'turn_changed',fake):
            pending=runtime.stop_pending(self.root,runtime.load_bindings(self.root),self.state(),'s1',['t1'],'demo',str(self.project))
        self.assertEqual(len(calls),1);self.assertEqual([k for k,_ in pending if k=='follow'],['follow'])

    @unittest.skipUnless(shutil.which('git'),'git is not installed')
    def test_turn_changed_guards_a_pull_by_commits_and_by_files(self):
        from core.common import head_oid
        env={**os.environ,'GIT_AUTHOR_NAME':'t','GIT_AUTHOR_EMAIL':'t@example.org','GIT_COMMITTER_NAME':'t','GIT_COMMITTER_EMAIL':'t@example.org'}
        def git(*args):subprocess.run(['git','-C',str(self.project),*args],check=True,capture_output=True,env=env)
        serial=iter(range(1000))
        def commit(files,message):
            message=f'{message}-{next(serial)}'
            for name in files:put(self.project/name,message+name+chr(10))
            git('add','-A');git('commit','-q','-m',message)
        (self.project/'vendor').mkdir();git('init','-q');commit(['src/a.py'],'initial')
        def run(count,names):
            start=head_oid(self.project);state=self.state(turn_started=time.time(),heads={self.project.resolve().as_posix():start,self.project.as_posix():start})
            for i in range(count):commit(names[i::count] or names[:1],f'c{i}')
            return runtime.turn_changed(self.root,runtime.load_bindings(self.root),state,'demo',str(self.project))
        files,pulled,edited=run(2,['src/a.py','src/b.py']);self.assertEqual((sorted(files),pulled,edited),(['src/a.py','src/b.py'],False,set()))
        files,pulled,edited=run(runtime.CHANGE_GUARD_COMMITS+1,['src/a.py']);self.assertTrue(pulled);self.assertIn('src/a.py',files);self.assertEqual(edited,{'src/a.py'})   # still reported; edited by the pull, so the document reminder leaves it out
        files,pulled,edited=run(1,[f'vendor/f{i}.txt' for i in range(runtime.CHANGE_GUARD_FILES+1)]);self.assertTrue(pulled);self.assertEqual(edited,set())   # all added
        self.assertEqual(runtime.turn_changed(self.root,runtime.load_bindings(self.root),{'mutating':True},'demo',str(self.project)),({},False,set()))   # no shell command, no look

    def test_many_notes_are_named_in_one_sentence_up_to_the_limit(self):
        items=[('follow',{'ref':f'demo:{i}','short':f'demo:{i:08d}','title':'t','type':'bug' if i==1 else 'knowledge','path':f'src/m{i}.py','digest':False}) for i in range(runtime.STOP_FOLLOW+1)]
        used=[];text=runtime.stop_text(items,'s1',used)
        self.assertEqual(text.count('You edited'),1);self.assertEqual(len(used),runtime.STOP_FOLLOW);self.assertEqual(text.count('CMD resolve'),1)
        self.assertIn('files that notes cover',text);self.assertNotIn(f'src/m{runtime.STOP_FOLLOW}.py',text);self.assertLessEqual(len(text),runtime.STOP_TEXT_CAP)
        used=[];one=runtime.stop_text(items[:1],'s1',used);self.assertIn('You edited src/m0.py, which note demo:00000000 t covers',one)

if __name__=='__main__':unittest.main()
