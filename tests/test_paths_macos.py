"""MACOS-1: path comparison on case-insensitive disks (Windows, macOS APFS) and macOS Unicode spelling.

The fold tests force sys.platform so they run on any host; the disk tests need a case-insensitive disk and skip on Linux."""
import os,posixpath,sys,tempfile,unicodedata,unittest
from pathlib import Path
from unittest.mock import patch
from core import paths,repository
from core.common import resolve_domain
from core.runtime import protected_rule
from test_core import StoreFixture

NFC=unicodedata.normalize('NFC','Café.md');NFD=unicodedata.normalize('NFD','Café.md')
CASE_INSENSITIVE=sys.platform in ('win32','darwin')


class FoldTest(unittest.TestCase):
    def under(self,platform):
        for p in [patch.object(paths.sys,'platform',platform),patch.object(paths.os.path,'normcase',posixpath.normcase),
                  patch.object(paths.os,'name','posix')]:
            p.start();self.addCleanup(p.stop)

    def test_macos_folds_case_and_unicode_spelling(self):
        self.under('darwin')
        self.assertEqual(paths.fold('/Users/A/DOMAINS/'+NFD),paths.fold('/users/a/domains/'+NFC))

    def test_linux_keeps_case(self):
        self.under('linux')
        self.assertNotEqual(paths.fold('/srv/Store'),paths.fold('/srv/store'))

    def test_same_name_joins_nfc_and_nfd(self):
        self.assertTrue(paths.same_name(NFC,NFD.upper()))

    def test_same_name_keeps_plain_casefold_matches(self):
        """Codex F11: canonical caseless matching alone misses this pair, which casefold() matched before."""
        self.assertTrue(paths.same_name('Άͅ.md','αί.md'))

    @unittest.skipUnless(sys.platform=='win32','Windows comparison')
    def test_windows_folds_as_pathlib_does(self):
        """Codex F5: pathlib treats the Kelvin sign as k; the guard must not match less than it did."""
        self.assertEqual(paths.fold('C:/Store/'+chr(0x212a)),paths.fold('c:/store/k'))

    @unittest.skipUnless(sys.platform=='win32','Windows paths')
    def test_display_path_keeps_a_stream_name(self):
        """Codex F12: rebuilding the path from its parts read 'a:b' as a drive and dropped the folder."""
        from core.common import display_path
        self.assertEqual(display_path(Path('C:/project/sub/a:b'),[Path('C:/project')]),'sub/a:b')

    def test_is_within_needs_a_separator(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            self.assertTrue(paths.is_within(base/'a'/'b',base/'a'))
            self.assertTrue(paths.is_within(base/'a',base/'a'))
            self.assertFalse(paths.is_within(base/'ab',base/'a'))

    def test_os_junk(self):
        self.assertTrue(paths.is_os_junk('.DS_Store'));self.assertTrue(paths.is_os_junk('/x/._Note.md'))
        self.assertFalse(paths.is_os_junk('Note.md'))


class RenameTest(unittest.TestCase):
    def test_nfc_to_nfd_rename_is_one_file(self):
        """On APFS the two spellings name one file: deleting the old name after writing the new one would lose the note."""
        changes={Path('notes')/NFC:None,Path('notes')/NFD:'text'}
        with patch('core.repository.os.path.samefile',return_value=True),patch('core.repository.os.listdir',return_value=[NFC]):
            left,_,renames=repository._fold_case_renames(Path('/store'),changes,None)
        self.assertEqual(list(left),[Path('notes')/NFD]);self.assertEqual(renames,[(Path('notes')/NFC,Path('notes')/NFD)])

    def test_two_hard_links_are_two_names(self):
        """Codex F6: on Linux both spellings can be hard links to one file; renaming one onto the other does nothing."""
        changes={Path('notes')/NFC:None,Path('notes')/NFD:'text'}
        with patch('core.repository.os.path.samefile',return_value=True),patch('core.repository.os.listdir',return_value=[NFC,NFD]):
            left,_,renames=repository._fold_case_renames(Path('/store'),changes,None)
        self.assertEqual((len(left),renames),(2,[]))


@unittest.skipUnless(CASE_INSENSITIVE,'needs a case-insensitive disk')
class CaseInsensitiveDiskTest(StoreFixture):
    def flip(self,path):
        return Path(str(path).swapcase())

    def test_protected_rule_denies_other_casing(self):
        root=self.root.resolve()
        self.assertEqual(protected_rule(str(self.flip(root/'domains'/'demo'/'x.md')),str(root),root),'domains')
        self.assertEqual(protected_rule(str(self.flip(root/'bindings.json')),str(root),root),'config')

    def test_resolve_domain_ignores_cwd_casing(self):
        project=(self.base/'project').resolve();project.mkdir(exist_ok=True)
        from core.common import load_bindings
        self.assertEqual(resolve_domain(self.flip(project),load_bindings(self.root)),'demo')

    def test_folder_notebook_inside_the_store_is_refused_in_any_case(self):
        """Codex F7: the registration guard compared paths exactly."""
        from core.folder_policy import _canonical_folder
        inside=self.root.resolve()/'domains'/'demo'
        with self.assertRaises(ValueError):_canonical_folder(self.root,str(self.flip(inside)))

    def test_nested_store_lock_with_other_casing_does_not_wait(self):
        root=self.root.resolve()
        with repository.store_lock(root):
            with repository.store_lock(self.flip(root),timeout=1):pass


class MacRuntimeTest(StoreFixture):
    def test_vault_sync_skips_appledouble_files(self):
        """A ._ file on an exFAT or SMB vault is binary: sync crashed on it, then would have tried to capture it."""
        from core.sync import sync
        a=self.note();vault=self.base/'vault';folder=vault/'Domains'/'demo';folder.mkdir(parents=True)
        (folder/Path(a['path']).name).write_text(Path(a['path']).read_text(encoding='utf-8'),encoding='utf-8')
        (folder/('._'+Path(a['path']).name)).write_bytes(b'\x00\x05\x16\x07\xff\xfe\x80 Mac OS X')
        (folder/'.DS_Store').write_bytes(b'\x00\x00\x00\x01Bud1\xff')
        result=sync('demo',vault,self.root)
        self.assertEqual((result['staged'],result['copied']),(0,0),result)

    def test_vault_sync_reports_a_note_that_is_not_utf8(self):
        from core.sync import sync
        self.note();vault=self.base/'vault';folder=vault/'Domains'/'demo';folder.mkdir(parents=True)
        (folder/'Latin1.md').write_bytes('---\ntitle: Caf\xe9\n---\n'.encode('latin-1'))
        with patch('core.sync._recent',return_value=False):result=sync('demo',vault,self.root)
        self.assertEqual(result['staged'],1,result)

    def test_rebaseline_sweep_accepts_an_unresolved_root(self):
        """macOS reaches temp stores through /var, a link to /private/var; a relative root shows the same mismatch anywhere."""
        from core import rebaseline
        from core.notes import capture
        project=self.base/'project';(project/'src').mkdir(parents=True,exist_ok=True)
        mod=project/'src'/'mod.py';mod.write_text('def alpha(x):\n    return x + 1\n\n\ndef gamma(x):\n    return x - 1\n',encoding='utf-8')
        capture('Mod gamma','gamma subtracts one (mod.py:5-6).',['subject/project','concern/architecture'],'demo',self.root,files=['src/mod.py'])
        mod.write_text('def alpha(x):\n    y = x + 1\n    return y\n\n\ndef gamma(x):\n    return x - 1\n',encoding='utf-8')
        try:root=os.path.relpath(self.root)
        except ValueError:self.skipTest('store and working folder are on different drives')
        res=rebaseline.sweep(root)
        self.assertEqual((res['rebaselined'],res['still_stale']),(1,0),res)

    def test_note_scan_skips_appledouble_files(self):
        a=self.note();folder=Path(a['path']).parent
        (folder/('._'+Path(a['path']).name)).write_bytes(b'\x00\x05\x16\x07\xff\xfe')
        self.assertEqual([n['id'] for n in repository.inventory('demo',self.root)],[a['id']])
        from core.storage import validate_store
        validate_store(self.root)   # raised "Unreadable frontmatter" for the ._ file

    def test_a_folder_holding_only_finder_files_counts_as_empty(self):
        folder=self.base/'section';folder.mkdir();(folder/'.DS_Store').write_bytes(b'\x00')
        self.assertTrue(paths.remove_empty_dir(folder));self.assertFalse(folder.exists())
        kept=self.base/'kept';kept.mkdir();(kept/'page.md').write_text('x')
        self.assertFalse(paths.remove_empty_dir(kept));self.assertTrue((kept/'page.md').exists())

    def test_maintenance_provider_reads_LIVING_MEMORY_CLAUDE(self):
        from core import maintenance_agent
        with patch.dict(os.environ,{'LIVING_MEMORY_CLAUDE':str(self.base/'missing'/'claude')}):
            found=maintenance_agent.provider()
        self.assertFalse(found['ok']);self.assertIn('LIVING_MEMORY_CLAUDE',found.get('detail',''))


class InstallerTest(unittest.TestCase):
    def test_a_crlf_profile_is_not_stale(self):
        """The plan hashed decoded text and the apply check hashed raw bytes, so a CRLF profile was stale forever."""
        import install
        with tempfile.TemporaryDirectory() as tmp:
            profile=Path(tmp)/'.zprofile';profile.write_bytes(b'export A=1\r\nexport B=2\r\n')
            with patch.object(install,'_on_path',return_value=False),patch.object(install,'_profile',return_value=profile),\
                 patch.object(install.os,'name','posix'):
                change=install._path_plan()
            self.assertFalse(install._path_stale(change))
            install._add_to_path_posix(change)
            self.assertTrue(profile.read_bytes().startswith(b'export A=1\r\nexport B=2\r\n'));self.assertTrue(install._path_done(change))

    def test_install_target_under_a_linked_folder(self):
        """macOS /tmp and /var are links into /private; the installer refused any linked ancestor."""
        from core import installer_lifecycle
        with tempfile.TemporaryDirectory() as tmp:
            real=Path(tmp).resolve()/'real';real.mkdir();link=Path(tmp)/'link'
            try:os.symlink(real,link,target_is_directory=True)
            except (OSError,NotImplementedError):self.skipTest('cannot create a directory symlink here')
            plan=installer_lifecycle.build_plan(link/'app')
            self.assertEqual(Path(plan['target']),real/'app')


if __name__=='__main__':unittest.main()
