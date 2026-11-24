"""Shared test helpers for the folder notebook tests: a store fixture that runs the CLI in process, and a link or junction maker."""
import contextlib,io,json,os,subprocess
from unittest.mock import patch
from test_core import StoreFixture
from core.cli import main
from core.repository import read_json,digest

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
    def body_file(self,data):
        path=self.base/f'body-{digest(data)[:8]}';path.write_bytes(data);return str(path)


def junction(link,target,test):
    """A Windows junction, or a directory symlink elsewhere (the code refuses both as links)."""
    if os.name!='nt':os.symlink(target,link,target_is_directory=True);return
    if subprocess.run(['cmd','/c','mklink','/J',str(link),str(target)],capture_output=True).returncode:test.skipTest('no junction')
