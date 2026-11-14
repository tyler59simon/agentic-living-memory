import io,json,os,shlex,subprocess,sys,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from core import launcher,runtime

def fake(folder,python=None,script=None):
    """A launcher file as the installer writes it: the stub's bytes do not matter to active(), only the tail does."""
    path=Path(folder)/launcher.FILE;path.write_bytes(b'MZ'+launcher.tail(python or sys.executable,script or runtime.SCRIPT));return path

class LauncherCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='alm-launcher-test-');self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        launcher._active.cache_clear();self.addCleanup(launcher._active.cache_clear)
    def folder(self,name):
        path=self.base/name;path.mkdir();return path
    def active(self,*folders,script=None):
        with patch.dict(os.environ,{'PATH':os.pathsep.join(str(f) for f in folders)}):
            return launcher.active(script or runtime.SCRIPT)

class Tail(LauncherCase):
    def test_tail_is_deterministic_and_carries_the_marker(self):
        self.assertEqual(launcher.tail('/py','/s.py'),launcher.tail('/py','/s.py'));self.assertNotEqual(launcher.tail('/py','/s.py'),launcher.tail('/py2','/s.py'))
        self.assertIn(launcher.MARK.encode(),launcher.tail('/py','/s.py'))
    @unittest.skipIf(os.name=='nt','sh launcher')
    def test_posix_script_quotes_awkward_paths(self):
        text=launcher.tail("/a b/it's python","/c d/s'x.py").decode().splitlines()
        self.assertEqual(text[0],'#!/bin/sh');self.assertEqual(shlex.split(text[2]),['exec',"/a b/it's python","/c d/s'x.py",'$@'])
    @unittest.skipUnless(os.name=='nt','zip launcher')
    def test_windows_tail_is_a_shebang_and_a_zip_with_main(self):
        data=launcher.tail('C:\\Program Files\\Py\\python.exe','C:\\app\\living_memory.py');line,_,body=data.partition(b'\n')
        self.assertEqual(line,b'#!"C:\\Program Files\\Py\\python.exe"');self.assertTrue(launcher.tail('C:\\Py\\python.exe','C:\\a.py').startswith(b'#!C:\\Py\\python.exe\n'))
        main=zipfile.ZipFile(io.BytesIO(body)).read('__main__.py').decode();self.assertIn(launcher.MARK,main);self.assertIn('C:\\\\app\\\\living_memory.py',main)
    def test_build_is_none_without_a_stub_on_windows(self):
        with patch.object(launcher,'_stub',return_value=None):
            if os.name=='nt':self.assertIsNone(launcher.build('p','s'))
            else:self.assertEqual(launcher.build('p','s'),launcher.tail('p','s'))
    @unittest.skipUnless(os.name=='nt' and launcher._stub(),'needs pip\'s launcher stub')
    def test_build_is_stub_plus_tail(self):
        self.assertEqual(launcher.build('p','s'),launcher._stub()+launcher.tail('p','s'))

class Ours(LauncherCase):
    def test_marker_file_is_ours_foreign_and_symlink_are_not(self):
        mine=fake(self.base);self.assertTrue(launcher.ours(mine))
        other=self.base/'other';other.write_bytes(b'MZ not ours');self.assertFalse(launcher.ours(other));self.assertFalse(launcher.ours(self.base/'missing'))
        link=self.base/'link'
        try:os.symlink(mine,link)
        except (OSError,NotImplementedError):self.skipTest('no symlinks')
        self.assertFalse(launcher.ours(link))

class Active(LauncherCase):
    def test_true_only_for_our_launcher_on_path(self):
        here=self.folder('bin');fake(here)
        self.assertTrue(self.active(here))
        self.assertFalse(self.active(here,script=Path(runtime.SCRIPT).with_name('other.py')),'another script')
        self.assertFalse(self.active(self.folder('empty')),'folder not on PATH')
        self.assertFalse(self.active(),'no PATH')
    def test_other_interpreter_or_script_in_the_launcher_is_false(self):
        for kwargs in ({'python':'/elsewhere/python'},{'script':'/elsewhere/living_memory.py'}):
            with self.subTest(kwargs=kwargs):
                here=self.folder('b'+str(len(kwargs['python' if 'python' in kwargs else 'script'])));fake(here,**kwargs);self.assertFalse(self.active(here))
    def test_foreign_file_ahead_of_ours_is_false(self):
        early,late=self.folder('early'),self.folder('late');fake(late);(early/launcher.FILE).write_bytes(b'foreign')
        self.assertFalse(self.active(early,late));self.assertTrue(self.active(late,early))
    @unittest.skipUnless(os.name=='nt','PATHEXT shadowing')
    def test_cmd_or_ps1_shadows_in_the_same_or_an_earlier_folder(self):
        for name in ('alm.cmd','alm.ps1','alm.bat','alm'):
            with self.subTest(name=name):
                same,early,late=self.folder('same'+name),self.folder('early'+name),self.folder('late'+name);fake(same);fake(late);(same/name).write_text('x');(early/name).write_text('x')
                self.assertFalse(self.active(same));self.assertFalse(self.active(early,late))

@unittest.skipUnless(os.name=='nt' and launcher._stub(),'Windows launcher stub needed')
class RealLauncher(LauncherCase):
    ARGS=['%LOCALAPPDATA%','a!b','c^d','e&f','semi;colon','in"ner','with space','']
    def setUp(self):
        super().setUp();self.script=self.base/'echo.py';self.script.write_text('import sys,json\nsys.stdout.buffer.write(json.dumps(sys.argv[1:]).encode())\nsys.exit(7)\n')
        self.alm=self.base/'alm.exe';self.alm.write_bytes(launcher.build(sys.executable,self.script))
    def test_arguments_and_exit_code_match_a_direct_call(self):
        direct=subprocess.run([sys.executable,str(self.script)]+self.ARGS,capture_output=True);alm=subprocess.run([str(self.alm)]+self.ARGS,capture_output=True)
        self.assertEqual(json.loads(direct.stdout),self.ARGS);self.assertEqual(alm.stdout,direct.stdout);self.assertEqual((alm.returncode,direct.returncode),(7,7))
    def test_powershell_passes_the_same_arguments_as_a_direct_call(self):
        # Windows PowerShell 5.1 mangles an embedded double quote for any native program, so the launcher is compared with python itself.
        quoted=" ".join("'"+a.replace("'","''")+"'" for a in self.ARGS)
        run=lambda exe:subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',f"& {exe} {quoted}; exit $LASTEXITCODE"],capture_output=True,timeout=60)
        direct=run(f"'{sys.executable}' '{self.script}'");alm=run(f"'{self.alm}'")
        self.assertEqual(alm.stdout,direct.stdout,(alm.stderr,direct.stderr));self.assertEqual((alm.returncode,direct.returncode),(7,7))
        self.assertIn('%LOCALAPPDATA%',json.loads(alm.stdout)[0])

if __name__=='__main__':unittest.main()
