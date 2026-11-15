"""UX-C9: the `distinct_from` field, `update --distinct/--not-distinct`, and the duplicate scan skipping distinct pairs."""
import contextlib,io,json,unittest,unittest.mock
from test_core import StoreFixture
from core.cli import main
from core.governance import apply,undo
from core.notes import capture,find_note,distinct
from core.repository import validate
from core.worklist import worklist

TAGS=['subject/project','concern/architecture']

class Distinct(StoreFixture):
    def cap(self,title,body='A short fact worth keeping.',**kw):return capture(title,body,TAGS,'demo',self.root,allow_similar=True,**kw)
    def cli(self,*argv,ok=True):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=main(['--root',str(self.root),'--domain','demo',*argv])
        if ok:self.assertEqual(code,0,out.getvalue()+err.getvalue())
        return json.loads(out.getvalue() or '{}') if ok else (code,out.getvalue()+err.getvalue())
    def get(self,made):return find_note(made['id'],'demo',self.root)[1]
    def pair(self):
        return self.cap('Retry limit setting','The retry limit is three attempts.'),self.cap('Retry limit setting value','The retry limit is three attempts per request.')

    def entry(self,other,**kw):return {'ref':'demo:'+other['id'],'why':'Different retry scopes.','by':'user','at':'2026-11-15T10:00:00+00:00',**kw}

    def test_validation(self):
        a,b=self.pair();note=self.get(b)
        validate({**note,'distinct_from':[self.entry(a)]},'demo',self.root)
        for bad in ('x',[self.entry(a,why='short')],[self.entry(a,why='x'*201)],[self.entry(a,by='agent')],[self.entry(a,at='yesterday')],[self.entry(a,ref='nocolon')],
                    [{**self.entry(a),'extra':1}],[self.entry(a)]*21,['demo:abc'],[self.entry(a,why='two\nlines of text')]):
            with self.assertRaises(ValueError,msg=repr(bad)[:60]):validate({**note,'distinct_from':bad},'demo',self.root)

    def test_distinct_helper_works_in_either_direction_by_id(self):
        a,b=self.pair();na,nb=self.get(a),self.get(b)
        self.assertFalse(distinct(na,nb))
        nb['distinct_from']=[self.entry(a)]
        self.assertTrue(distinct(na,nb));self.assertTrue(distinct(nb,na))
        self.assertFalse(distinct(na,{**nb,'domain':'other','id':'zzz','distinct_from':[]}))

    def test_update_distinct_is_journaled_undoable_and_keeps_updated(self):
        a,b=self.pair();before=self.get(b)
        result=self.cli('update','demo:'+b['id'],'--distinct','demo:'+a['id'],'--why','Different retry scopes.')
        after=self.get(b);record=after['distinct_from'][0]
        self.assertEqual((record['ref'],record['why'],record['by']),('demo:'+a['id'],'Different retry scopes.','user'))
        self.assertEqual(after['updated'],before['updated']);self.assertTrue(result.get('operation_id'))
        reverse=undo(self.root,result['operation_id']);apply(self.root,reverse['plan_id'],reverse['digest'])
        self.assertNotIn('distinct_from',self.get(b))

    def test_update_distinct_needs_why_and_a_note_in_the_same_domain(self):
        a,b=self.pair()
        code,text=self.cli('update','demo:'+b['id'],'--distinct','demo:'+a['id'],ok=False);self.assertNotEqual(code,0);self.assertIn('--why',text)
        code,text=self.cli('update','demo:'+b['id'],'--distinct','demo:'+b['id'],'--why','Not itself at all.',ok=False);self.assertNotEqual(code,0)

    def test_not_distinct_removes_the_record_from_either_note(self):
        a,b=self.pair()
        self.cli('update','demo:'+b['id'],'--distinct','demo:'+a['id'],'--why','Different retry scopes.')
        self.cli('update','demo:'+a['id'],'--not-distinct','demo:'+b['id']);self.assertNotIn('distinct_from',self.get(b))
        self.assertFalse(distinct(self.get(a),self.get(b)))
        code,text=self.cli('update','demo:'+a['id'],'--not-distinct','demo:'+b['id'],ok=False);self.assertNotEqual(code,0);self.assertIn('not marked distinct',text)

    def test_not_distinct_on_the_note_that_carries_it_and_the_updated_stamp(self):
        a,b=self.pair();self.cli('update','demo:'+b['id'],'--distinct','demo:'+a['id'],'--why','Different retry scopes.')
        stamp=self.get(b)['updated'];self.cli('update','demo:'+b['id'],'--not-distinct','demo:'+a['id'])
        self.assertEqual((self.get(b)['updated'],'distinct_from' in self.get(b)),(stamp,False))

    def test_the_duplicate_scan_skips_a_distinct_pair(self):
        a,b=self.pair()
        self.assertEqual([r['ref'] for r in worklist(self.root,['demo'],kinds=['duplicate'])['items']],['demo:'+b['id']])
        self.cli('update','demo:'+a['id'],'--distinct','demo:'+b['id'],'--why','Different retry scopes.')
        self.assertEqual(worklist(self.root,['demo'],kinds=['duplicate'])['items'],[])
        self.cli('update','demo:'+b['id'],'--not-distinct','demo:'+a['id'])
        self.assertEqual(len(worklist(self.root,['demo'],kinds=['duplicate'])['items']),1)

    def test_a_credential_in_why_is_redacted_or_refused(self):
        a,b=self.pair();secret='password: Hunter2Hunter2'
        code,text=self.cli('update','demo:'+b['id'],'--distinct','demo:'+a['id'],'--why','Different scope '+secret,ok=False)
        self.assertNotEqual(code,0);self.assertNotIn('distinct_from',self.get(b))
        result=self.cli('update','demo:'+b['id'],'--distinct','demo:'+a['id'],'--why','Different scope '+secret,'--redact')
        why=self.get(b)['distinct_from'][0]['why'];self.assertNotIn('Hunter2',why);self.assertTrue(result.get('redacted'))
        with self.assertRaises(ValueError):validate({**self.get(b),'distinct_from':[self.entry(a,why='Different scope '+secret)]},'demo',self.root)

    def test_a_private_span_in_why_is_refused_with_a_session(self):
        from core.cli import _session_state
        a,b=self.pair();phrase='the moon cheese ledger of seventeen hidden vaults today'
        from core.private import remember
        state={};remember(state,'<private>'+phrase+'</private>')
        with unittest.mock.patch('core.cli._session_state',return_value=state):
            code,text=self.cli('update','demo:'+b['id'],'--distinct','demo:'+a['id'],'--why',phrase,'--session','s1',ok=False)
        self.assertNotEqual(code,0);self.assertIn('private',text);self.assertNotIn('distinct_from',self.get(b))

    def test_not_distinct_is_one_transaction_and_checks_the_expected_revision_first(self):
        a,b=self.pair();self.cli('update','demo:'+b['id'],'--distinct','demo:'+a['id'],'--why','Different retry scopes.')
        revisions=(self.get(a)['revision'],self.get(b)['revision'])
        code,text=self.cli('update','demo:'+a['id'],'--not-distinct','demo:'+b['id'],'--expected-revision','0'*8,ok=False)
        self.assertNotEqual(code,0);self.assertEqual((self.get(a)['revision'],self.get(b)['revision']),revisions);self.assertTrue(self.get(b).get('distinct_from'))
        code,text=self.cli('update','demo:'+b['id'],'--not-distinct','demo:'+a['id'],'--review-by','not-a-date',ok=False);self.assertNotEqual(code,0)
        self.assertEqual((self.get(a)['revision'],self.get(b)['revision']),revisions)
        a2,b2=self.pair()
        self.cli('update','demo:'+a2['id'],'--distinct','demo:'+b2['id'],'--why','Different retry scopes.')
        result=self.cli('update','demo:'+b2['id'],'--not-distinct','demo:'+a2['id'],'--context','together')
        self.assertEqual((self.get(b2).get('context'),'distinct_from' in self.get(a2)),('together',False))
        reverse=undo(self.root,result['operation_id']);apply(self.root,reverse['plan_id'],reverse['digest'])
        self.assertEqual((self.get(b2).get('context'),'distinct_from' in self.get(a2)),('',True))

if __name__=='__main__':unittest.main()
