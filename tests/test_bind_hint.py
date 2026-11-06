import json,unittest
from test_core import StoreFixture
from core import runtime

class BindHint(StoreFixture):
    def setUp(self):
        super().setUp()
        self.repo=self.base/'elsewhere'/'repo';(self.repo/'.git').mkdir(parents=True);(self.repo/'src').mkdir()
    def start(self,cwd,session='s1'):
        return runtime.process('claude','session-start',{'cwd':str(cwd),'session_id':session},self.root)

    def test_hint_in_an_unbound_repo_once_per_session(self):
        first=self.start(self.repo/'src')
        self.assertIn(f'this repo ({self.repo.resolve().as_posix()}) is not bound',first['context'])
        self.assertIn(f'bind NAME "{self.repo.resolve().as_posix()}"',first['context']);self.assertIn('Ask before binding',first['context'])
        self.assertEqual(self.start(self.repo/'src'),{})   # resume/compact in the same session
        self.assertIn('context',self.start(self.repo,session='s2'))
    def test_worktree_git_file_counts(self):
        (self.repo/'.git').rmdir();(self.repo/'.git').write_text('gitdir: elsewhere')
        self.assertIn('context',self.start(self.repo))
    def test_no_hint_outside_a_repo_or_in_a_bound_one(self):
        plain=self.base/'plain';plain.mkdir()
        self.assertEqual(self.start(plain),{})
        (self.base/'project').mkdir(exist_ok=True);(self.base/'project'/'.git').mkdir()
        self.assertNotIn('is not bound',self.start(self.base/'project')['context'])
    def test_opt_out(self):
        path=self.root/'bindings.json';data=json.loads(path.read_text());data['suggest_bind']=False;path.write_text(json.dumps(data))
        self.assertEqual(self.start(self.repo),{})

if __name__=='__main__':unittest.main()
