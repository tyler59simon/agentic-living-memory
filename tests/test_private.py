import contextlib,io,json,unittest
from test_core import StoreFixture
from core import runtime
from core.cli import main
from core import private

SECRET_PLAN='the acquisition of northwind closes on march third pending board approval'

class Private(StoreFixture):
    def prompt(self,text,session='s1'):
        payload={'cwd':str(self.base/'project'),'session_id':session,'prompt':text}
        return runtime.process('claude','user-prompt',payload,self.root)
    def cli(self,*argv):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=main(['--root',str(self.root),'--domain','demo',*argv])
        return code,json.loads(out.getvalue() or err.getvalue())
    def turns_text(self):return ''.join(p.read_text(encoding='utf-8') for p in (self.root/'.turns').glob('*'))

    def test_notice_and_no_plaintext_in_state(self):
        result=self.prompt(f'Use this for the memo: <private>{SECRET_PLAN}</private> and draft it.')
        self.assertIn(private.NOTICE,result['context'])
        text=self.turns_text().casefold()
        for word in ('northwind','acquisition','march'):self.assertNotIn(word,text)
        self.assertNotIn(private.NOTICE,self.prompt('next question')['context'])
    def test_capture_copying_a_private_span_is_refused(self):
        self.prompt(f'<private>{SECRET_PLAN}</private> plan the comms')
        code,result=self.cli('capture','--session','s1','--concern','decision','--title','Comms plan','--summary','Announce after the deal.',
                             '--detail',f'**Timing:** The Acquisition of Northwind closes on March third, pending board approval.')
        self.assertEqual(code,5,result);self.assertIn('private_content conflict: 📝 Key Details',result['error']['message'])
        code,result=self.cli('capture','--session','s1','--concern','decision','--title','Comms plan','--summary','Announce after the deal closes.')
        self.assertEqual(code,0,result)
    def test_update_is_checked_too(self):
        n=self.note()
        self.prompt('<private>project falcon</private> rename it')
        code,result=self.cli('update','demo:'+n['id'],'--session','s1','--title','Project Falcon cache')
        self.assertEqual(code,5);self.assertIn('title',result['error']['message'])
    def test_short_span_and_later_turns(self):
        self.prompt('call it <private>blue harbor</private>')
        self.prompt('ok now save the decision')
        code,result=self.cli('capture','--session','s1','--concern','decision','--title','Codename chosen','--summary','We picked Blue Harbor.')
        self.assertEqual(code,5,result)
    def test_unclosed_tag_runs_to_the_end(self):
        result=self.prompt('<private>this never closes')
        self.assertIn(private.NOTICE,result['context'])
    def test_without_session_there_is_no_check(self):
        self.prompt('<private>blue harbor</private>')
        self.assertEqual(self.cli('capture','--concern','decision','--title','Codename chosen','--summary','We picked Blue Harbor.')[0],0)
    def test_sessions_are_independent(self):
        self.prompt('<private>blue harbor</private>',session='other')
        self.assertEqual(self.cli('capture','--session','s1','--turn','t','--concern','decision','--title','Codename chosen','--summary','Blue Harbor.')[0],0)

if __name__=='__main__':unittest.main()
