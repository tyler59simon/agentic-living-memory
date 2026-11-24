"""MACOS-1: path comparison on case-insensitive disks (Windows, macOS APFS) and macOS Unicode spelling.

The fold tests force sys.platform so they run on any host; the disk tests need a case-insensitive disk and skip on Linux."""
import posixpath,sys,tempfile,unicodedata,unittest
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
        for p in [patch.object(paths.sys,'platform',platform),patch.object(paths.os.path,'normcase',posixpath.normcase)]:
            p.start();self.addCleanup(p.stop)

    def test_macos_folds_case_and_unicode_spelling(self):
        self.under('darwin')
        self.assertEqual(paths.fold('/Users/A/DOMAINS/'+NFD),paths.fold('/users/a/domains/'+NFC))

    def test_linux_keeps_case(self):
        self.under('linux')
        self.assertNotEqual(paths.fold('/srv/Store'),paths.fold('/srv/store'))

    def test_md_suffix_matches_any_case_on_macos(self):
        self.under('darwin')
        self.assertTrue(paths.fold('Note.MD').endswith('.md'))

    def test_name_key_joins_nfc_and_nfd(self):
        self.assertEqual(paths.name_key(NFC),paths.name_key(NFD.upper()))

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
        with patch('core.repository.os.path.samefile',return_value=True):
            left,_,renames=repository._fold_case_renames(Path('/store'),changes,None)
        self.assertEqual(list(left),[Path('notes')/NFD]);self.assertEqual(renames,[(Path('notes')/NFC,Path('notes')/NFD)])


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

    def test_nested_store_lock_with_other_casing_does_not_wait(self):
        root=self.root.resolve()
        with repository.store_lock(root):
            with repository.store_lock(self.flip(root),timeout=1):pass


if __name__=='__main__':unittest.main()
