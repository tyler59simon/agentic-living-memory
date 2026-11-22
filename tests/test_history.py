"""history: readable per-note rows (event, summary, actor) from the activity cache, and `history --markdown`."""
import contextlib,io,json,unittest
from unittest.mock import patch
from core import activity,notes
from core.cli import main
from core.note_format import render
from core.repository import canonical_json
from test_maintenance import Base,OLD,TAGS

BODY=render(summary='Rotate the signing keys. Then tell the team.',actions=['Run the migration','Notify support'],named=[('Notes','Keys live in the vault.')],note_type='plan')

class History(Base):
    def setUp(self):
        names=patch('getpass.getuser',return_value='tester');names.start();self.addCleanup(names.stop);super().setUp()   # REG-32: journals record the OS user
    def cli(self,*argv):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=main(['--root',str(self.root),'--domain','demo',*argv])
        return code,json.loads(out.getvalue() or err.getvalue())
    def ok(self,*argv):
        code,result=self.cli(*argv);self.assertEqual(code,0,result);return result
    def story(self):
        """One plan note captured by an agent, edited, confirmed, archived by maintenance and restored; and a second note that points at it."""
        a=self.mk('Key rotation',BODY,type='plan',review_by=OLD,origin='claude:abc123');ref='demo:'+a['id']
        self.ok('update',ref,'--title','Signing key rotation')
        self.ok('update',ref,'--tag','subject/project','--tag','concern/architecture','--tag','concern/decision')
        self.ok('check',ref,'Run the migration')
        self.ok('update',ref,'--section','Notes','Keys live in the vault.\nRotate yearly.\nOwner is security.')
        self.ok('confirm',ref)
        b=self.mk('Pointer note',f"See [Key rotation](./PLAN%20-%20key-rotation.md).")
        self.ok('update',ref,'--review-by',OLD)
        res=self.go((a,'archive',{'rule':'obsolete_content','reason':'The rotation finished long ago.'}))
        self.assertEqual(res.get('archived'),1,res)
        self.ok('restore',ref)
        return a,b

    def test_a_note_history_lists_its_own_operations_newest_first_with_summaries_and_actors(self):
        a,b=self.story();rows=self.ok('history','--note','demo:'+a['id'])
        self.assertEqual([r['event'] for r in rows],['restored','archived','confirmed','updated','updated','updated','updated','created'])
        by=lambda e,i=0:[r for r in rows if r['event']==e][i]
        self.assertEqual(rows[-1]['actor'],'agent:claude');self.assertIn('Created as plan: Rotate the signing keys.',rows[-1]['summary']);self.assertNotIn('Then tell',rows[-1]['summary'])
        self.assertEqual(by('archived')['actor'],'maintenance');self.assertIn('obsolete_content: The rotation finished',by('archived')['summary']);self.assertTrue(by('archived')['summary'].startswith('obsolete_content: '))
        self.assertEqual(by('restored')['summary'],'restored and pinned');self.assertEqual(by('confirmed')['summary'],'confirmed')
        summaries=[r['summary'] for r in rows if r['event']=='updated'];self.assertEqual(len(summaries),4)
        self.assertIn('Notes +2/-0 lines',summaries[0]);self.assertIn('ticked "Run the migration"',summaries[1]);self.assertIn('tags +concern/decision',summaries[2]);self.assertIn('title "Key rotation" -> "Signing key rotation"',summaries[3])
        self.assertTrue(all(r['actor']=='command' for r in rows if r['event'] in ('updated','confirmed','restored')))
        self.assertTrue(all(len(r['summary'])<=200 for r in rows))
        for r in rows:
            for key in ('operation_id','kind','state','created','files'):self.assertIn(key,r)
            self.assertEqual(r['state'],'committed');self.assertTrue(r['files']);self.assertEqual(r['title'] in ('Key rotation','Signing key rotation'),True)
        self.assertEqual([r['created'] for r in rows],sorted((r['created'] for r in rows),reverse=True))
        self.assertEqual(len(self.ok('history','--note','demo:'+a['id'],'--limit','3')),3)

    def test_a_link_rewrite_in_another_note_is_not_in_either_history(self):
        a,b=self.story()
        events=lambda n:[r['event'] for r in self.ok('history','--note','demo:'+n['id'])]
        self.assertEqual(events(b),['created']);self.assertNotIn('created',events(a)[:-1])
        ops_b={r['operation_id'] for r in self.ok('history','--note','demo:'+b['id'])};ops_a={r['operation_id'] for r in self.ok('history','--note','demo:'+a['id'])}
        self.assertFalse(ops_a&ops_b)

    def test_log_note_rows_carry_the_same_summaries_and_actors(self):
        a,_=self.story();history=self.ok('history','--note','demo:'+a['id']);log=self.ok('log','--note','demo:'+a['id'])['rows']
        self.assertEqual({r['op']:(r['summary'],r['actor']) for r in log},{r['operation_id']:(r['summary'],r['actor']) for r in history})

    def test_markdown_wraps_the_rows_and_the_plain_output_stays_a_list(self):
        a,_=self.story();ref='demo:'+a['id']
        out=self.ok('history','--note',ref,'--markdown');self.assertEqual(sorted(out),['markdown','ok','rows']);self.assertTrue(out['ok'])
        self.assertEqual(out['rows'],self.ok('history','--note',ref));self.assertIsInstance(self.ok('history'),list)
        lines=out['markdown'].split('\n');self.assertEqual(len(lines),len(out['rows']))
        self.assertRegex(lines[1],r'^- \d{4}-\d\d-\d\d \d\d:\d\d archived \(maintenance, tester\): obsolete_content: The rotation finished')
        plain=self.ok('history','--markdown','--limit','2');self.assertEqual(len(plain['rows']),2);self.assertEqual(len(plain['markdown'].split('\n')),2);self.assertIn(plain['rows'][0]['kind'],plain['markdown'])

    def test_superseded_names_the_successor_and_long_summaries_are_cut(self):
        a=self.mk('Old way','Body one.');b=self.mk('New way','Body two.',supersedes='demo:'+a['id'])
        old=self.ok('history','--note','demo:'+a['id'])[0];self.assertEqual((old['event'],old['summary']),('superseded','superseded by '+b['id'][:8]))
        self.assertEqual(activity._summary('capture','created',None,('demo',{'type':'knowledge','body':'x'*500}))[-3:],'...');self.assertEqual(len(activity._summary('capture','created',None,('demo',{'type':'knowledge','body':'x'*500}))),200)

    def test_an_edit_of_unheaded_text_after_a_callout_is_summarized(self):
        # parts() returns (None, None, text) for a paragraph after the Important callout; it needs a string label.
        o={'body':'Summary.\n\n> **Important:** careful.\n\nLoose paragraph.'};n={'body':'Summary.\n\n> **Important:** careful.\n\nLoose paragraph, edited.'}
        self.assertEqual(activity._summary('update','updated',('demo',o),('demo',n)),'text +1/-1 lines')

    def test_carried_content_in_a_lifecycle_operation_stays_in_the_other_note_history(self):
        import base64
        from pathlib import Path
        from core.repository import parse_text,serialize
        a=self.mk('Retired one','Body one.');b=self.mk('Successor','See [Retired one](./KNOWLEDGE%20-%20retired-one.md).')
        enc=lambda note:base64.b64encode(serialize(note).encode('utf-8')).decode('ascii')
        read=lambda n:parse_text(Path(n['path']).read_text(encoding='utf-8'),n['path'])
        old_a,old_b=read(a),read(b);new_a={**old_a,'status':'superseded','superseded_by':'demo:'+b['id']}
        def rows(body):
            effects=[{'path':'domains/demo/notes/a.md','before':enc(old_a),'after':enc(new_a)},{'path':'domains/demo/notes/b.md','before':enc(old_b),'after':enc({**old_b,'body':body})}]
            return {r['ref'].rpartition(':')[2]:r['event'] for r in activity._note_rows('op1','supersede','2026-11-14T00:00:00+00:00',effects)}
        self.assertEqual(rows(old_b['body'].replace('./KNOWLEDGE%20-%20retired-one.md','../archive/KNOWLEDGE%20-%20retired-one.md')),{a['id']:'superseded'})   # a path rewrite only
        self.assertEqual(rows(old_b['body']+'\n\n## Links\n\n- [Carried](https://example.com)'),{a['id']:'superseded',b['id']:'updated'})

    def test_a_retirement_names_the_actor_it_recorded(self):
        before=('demo',{'status':'active'});retired=lambda who:('demo',{'status':'archived','last_retirement':{'actor':who}})
        for who,want in (('plan','plan'),('vault','obsidian'),('agent','command'),('auto_resolve','auto_resolve')):
            self.assertEqual(activity._actor('op1','archive',before,retired(who)),want,who)
        self.assertEqual(activity._actor('m-1','archive',before,retired('maintenance')),'maintenance')
        self.assertEqual(activity._actor('op1','undo:abc',retired('plan'),before),'undo')
        self.assertEqual(activity._actor('op1','update',('demo',{'status':'archived','last_retirement':{'actor':'plan'}}),retired('plan')),'command')   # no transition: an ordinary edit

    def test_version_advertises_history_3(self):
        self.assertEqual(self.ok('version')['capabilities']['history'],3)

    def test_bare_history_rows_carry_a_summary_and_an_actor(self):
        a,_=self.story();rows=self.ok('history')
        self.assertTrue(rows and all(isinstance(r['summary'],str) and r['summary'] and isinstance(r['actor'],str) for r in rows))
        by_op={r['operation_id']:r for r in rows};note=self.ok('history','--note','demo:'+a['id'])
        for r in note:self.assertEqual((by_op[r['operation_id']]['summary'],by_op[r['operation_id']]['actor']),(r['summary'],r['actor']))
        self.assertIn('maintenance',{r['actor'] for r in rows})
        self.assertEqual([k for k in rows[0]],['operation_id','kind','state','created','files','summary','actor','user'])

    def test_history_and_log_rows_name_the_os_user_and_leave_it_out_when_it_is_unknown(self):
        a,_=self.story();ref='demo:'+a['id']
        for rows in (self.ok('history'),self.ok('history','--note',ref),self.ok('log','--note',ref)['rows']):self.assertTrue(rows and {r['user'] for r in rows}=={'tester'})
        with patch('getpass.getuser',side_effect=OSError('no user')):
            self.ok('update',ref,'--title','Rotated signing keys');newest=self.ok('history','--note',ref)[0]
        self.assertEqual((newest['event'],'user' in newest),('updated',False))
        self.assertNotIn(', tester)',self.ok('history','--note',ref,'--markdown')['markdown'].split(chr(10))[0])

    def test_a_version_2_cache_is_replaced(self):
        a=self.mk('Old cache note','Body one.');cache=self.root/'.cache'/'activity.json';cache.parent.mkdir(exist_ok=True)
        cache.write_text(canonical_json({'version':2,'ops':{'stale':{'created':'2020-01-01T00:00:00+00:00','kind':'capture','rows':[],'sig':[0,0]}}}),encoding='utf-8')
        rows=self.ok('history','--note','demo:'+a['id']);self.assertEqual([r['event'] for r in rows],['created'])
        value=json.loads(cache.read_text(encoding='utf-8'));self.assertEqual(value['version'],activity.CACHE_VERSION);self.assertNotIn('stale',value['ops'])
        self.assertTrue(all('files' in e for e in value['ops'].values()))

if __name__=='__main__':unittest.main()
