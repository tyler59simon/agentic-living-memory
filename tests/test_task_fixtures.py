"""The task line fixture (tests/fixtures/task_lines.json): one parsing rule for page tasks and action items. Hivemind keeps a byte
copy and runs the same cases through its Rust and TypeScript parsers, so a case is `{"line", "task"}` with task null for a line
that is not a task, else {done, text, task, due, owner, priority}: text follows the box (cut at 500 characters), task is the text
people read, and the fields are read from the cut text."""
import json,unittest
from pathlib import Path
from core import docs_index
from core.note_format import item_fields

FIXTURE=Path(__file__).resolve().parent/'fixtures'/'task_lines.json'
CASES=json.loads(FIXTURE.read_text(encoding='utf-8'))


class TaskFixtures(unittest.TestCase):
    def test_every_case_is_self_describing(self):
        self.assertGreaterEqual(len(CASES),30)
        for case in CASES:
            self.assertEqual(set(case),{'line','task'},case)
            if case['task'] is not None:
                self.assertEqual(set(case['task']),{'done','text','task','due','owner','priority'},case['line'])
                self.assertIsInstance(case['task']['done'],bool)
        self.assertTrue(any(c['task'] is None for c in CASES));self.assertTrue(any(c['task'] for c in CASES))

    def test_tasks_reads_every_case(self):
        for case in CASES:
            with self.subTest(line=case['line'][:80]):
                found=docs_index.tasks(case['line']);want=case['task']
                if want is None:self.assertEqual(found,[]);continue
                self.assertEqual(len(found),1);number,done,text,due,owner,priority=found[0]
                self.assertEqual((number,done,text,due,owner,priority),(1,want['done'],want['text'],want['due'],want['owner'],want['priority']))

    def test_item_fields_reads_the_text_of_every_case(self):
        for case in CASES:
            want=case['task']
            if want is None:continue
            with self.subTest(line=case['line'][:80]):
                task,fields,_=item_fields(want['text'])
                self.assertEqual((task,fields),(want['task'],{'due':want['due'],'owner':want['owner'],'priority':want['priority']}))


if __name__=='__main__':unittest.main()
