"""Every CLI error reaches the agent as JSON with a plain message, and a next step where there is one (CLI-1)."""
import contextlib,io,json
from unittest.mock import patch
from test_core import StoreFixture
from core import cli

class CliErrors(StoreFixture):
    def fail_with(self,*argv):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=cli.main(['--root',str(self.root),*argv])
        self.assertEqual(out.getvalue(),'')
        return code,json.loads(err.getvalue())['error']

    def test_a_bad_flag_is_a_json_error_naming_the_help_command(self):
        for argv in (('search','cache','--no-such-flag'),('capture','--type','not-a-type'),('--domain','demo','frobnicate')):
            with self.subTest(argv=argv):
                code,error=self.fail_with(*argv)
                self.assertEqual((code,error['code']),(2,'invalid'))
                self.assertTrue(error['message'].startswith('bad_arguments: '),error)
                self.assertIn('--help',error['next'])
        self.assertIn('alm capture --help',self.fail_with('capture','--type','not-a-type')[1]['next'])
        self.assertIn('alm search --help',self.fail_with('--domain','demo','search','cache','--no-such-flag')[1]['next'])

    def test_a_filter_without_a_value_explains_the_form(self):
        code,error=self.fail_with('--domain','demo','search','cache','--filter','decision')
        self.assertEqual(code,2);self.assertIn('KEY=VALUE',error['message']);self.assertNotIn('dictionary',error['message'])

    def test_an_unbound_folder_says_what_to_do(self):
        code,error=self.fail_with('--cwd',str(self.base/'elsewhere'),'search','cache')
        self.assertEqual(code,2);self.assertTrue(error['message'].startswith('unbound_project: '))
        self.assertIn('--domain',error['next'])

    def fail_with_root(self,root,*argv):
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()) as err:cli.main(['--root',str(root),*argv])
        return json.loads(err.getvalue())['error']

    def test_a_root_that_is_not_a_store_fails_and_creates_nothing(self):   # STO-2
        missing,empty=self.base/'stroe',self.base/'empty';empty.mkdir()
        for argv in (('status',),('--domain','demo','search','cache'),('--domain','demo','capture','--type','knowledge','--title','Typo root','--summary','Nothing.')):
            for root in (missing,empty):
                with self.subTest(argv=argv,root=root.name):
                    out,err=io.StringIO(),io.StringIO()
                    with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=cli.main(['--root',str(root),*argv])
                    error=json.loads(err.getvalue())['error']
                    self.assertEqual(code,2);self.assertTrue(error['message'].startswith('no_store: '),error);self.assertIn('bind',error['next'])
        self.assertFalse(missing.exists());self.assertEqual(list(empty.iterdir()),[])
        journal_root=self.base/'harness-home';(journal_root/'.operations').mkdir(parents=True)   # RECOVERY.md: history and recover run on journal roots
        with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(cli.main(['--root',str(journal_root),'history']),0)
        self.assertEqual(self.fail_with_root(missing,'history')['message'][:9],'no_store:')
        with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(cli.main(['--root',str(empty),'bind','fresh',str(self.base/'fresh')]),0)   # bind starts a store in an empty folder
        self.assertTrue((empty/'bindings.json').is_file())

    def test_diff_of_an_unknown_operation_is_not_found(self):
        code,error=self.fail_with('diff','0123456789abcdef0123456789abcdef')
        self.assertEqual(code,2);self.assertTrue(error['message'].startswith('not_found: no operation'),error);self.assertIn('history',error['next'])

    def test_an_unknown_note_points_at_search(self):
        code,error=self.fail_with('--domain','demo','read','nothing-here')
        self.assertEqual(code,2);self.assertIn('search',error['next'])

    def test_a_lock_timeout_is_busy(self):
        self.note()
        with patch('core.repository.file_lock',side_effect=TimeoutError('timed out locking store/.store.lock')):
            code,error=self.fail_with('--domain','demo','capture','--title','Another','--summary','Body text about the lock.','--tag','subject/project','--tag','concern/architecture')
        self.assertEqual((code,error['code']),(2,'busy'));self.assertIn('retry',error['next'])

    def test_errors_without_a_next_step_keep_the_old_shape(self):
        code,error=self.fail_with('--domain','demo','search','cache','--filter','colour=blue')
        self.assertEqual(set(error),{'code','message'})
