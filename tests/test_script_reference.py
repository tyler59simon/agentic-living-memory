"""Every flag of the argparse parser in core/cli.py appears in the generated table of SCRIPT_REFERENCE.md."""
import importlib.util,unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
SPEC=importlib.util.spec_from_file_location('gen_script_reference',ROOT/'scripts'/'gen_script_reference.py')
gen=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(gen)

class FlagTable(unittest.TestCase):
    def test_the_reference_lists_every_flag_of_the_parser(self):
        table=gen.render_table();text=(ROOT/'SCRIPT_REFERENCE.md').read_text(encoding='utf-8')
        current=gen.block_of(text)
        self.assertIsNotNone(current,f'SCRIPT_REFERENCE.md has no {gen.BEGIN} ... {gen.END} block; run: {gen.COMMAND}')
        missing=sorted(gen.documented(table)-gen.documented(current))
        self.assertFalse(missing,'SCRIPT_REFERENCE.md does not list these flags: '+', '.join(missing)+'\nregenerate with: '+gen.COMMAND)
        self.assertEqual(current,table,'the flag table in SCRIPT_REFERENCE.md is out of date; regenerate with: '+gen.COMMAND)

    def test_the_table_is_stable_and_marks_every_command(self):
        from core.cli import HELP_GROUPS
        table=gen.render_table();self.assertEqual(table,gen.render_table())
        rows={l.split('`')[1].split(' ')[0] for l in table.splitlines()[2:]}
        self.assertFalse({n for _,names in HELP_GROUPS for n in names}-rows,'commands without a row')
        self.assertNotIn('--tags',table);self.assertIn('`project create/update/archive`',table)

    def test_update_keeps_the_text_around_the_markers(self):
        text=f'before\n{gen.BEGIN}\nold\n{gen.END}\nafter\n'
        self.assertEqual(gen.update(text,'new'),f'before\n{gen.BEGIN}\nnew\n{gen.END}\nafter\n')
        self.assertEqual(gen.update(text.replace('\n','\r\n'),'new').count('\r\n'),5)
        with self.assertRaises(ValueError):gen.update('no markers','new')

if __name__=='__main__':unittest.main()
