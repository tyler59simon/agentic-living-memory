"""Memory units: deterministic section extraction from canonical notes (core/units.py)."""
import tempfile,unittest
from pathlib import Path
from core import units
from core.repository import parse_text

BODY='''Refresh is centralized in the auth service.

> **Important:** tokens must never be logged anywhere.

## Why
Per-client refresh duplicated retry logic in every client.

## Alternatives considered
- Per-client refresh: rejected because each client retried on its own.
- A sidecar proxy: rejected as one more process to run.

## Steps
```bash
## not a heading inside a fence
auth-cli rotate --all
```
Then restart the gateway.

## Notes
First notes section with enough words.

## Notes
Second notes section with other words.

### Deeper detail
A subsection that belongs under the second notes heading.

## Links
- [[Some other note]]
'''

FILE='''---
title: "Authentication design"
type: "decision"
status: "active"
id: "abc123"
---

# Authentication design

'''+BODY

def note(body=BODY,path=None):
    return {'id':'abc123','title':'Authentication design','body':body.strip(),'path':path}

class Extraction(unittest.TestCase):
    def test_overview_first_then_sections_with_roles(self):
        found=units.extract(note())
        self.assertEqual(found[0]['role'],'overview')
        self.assertEqual(found[0]['text'],'Authentication design\nRefresh is centralized in the auth service.')   # the note-level embedding text
        roles={u['heading']:u['role'] for u in found[1:]}
        self.assertEqual(roles['Important'],'constraint')
        self.assertEqual(roles['Why'],'rationale')
        self.assertEqual(roles['Alternatives considered'],'alternative')
        self.assertEqual(roles['Steps'],'procedure')
        self.assertNotIn('Links',roles)   # layout section with no fact of its own

    def test_a_heading_inside_a_fence_is_text_and_the_command_stays_whole(self):
        steps=[u for u in units.extract(note()) if u['heading']=='Steps']
        self.assertEqual(len(steps),1)
        self.assertIn('auth-cli rotate --all',steps[0]['excerpt'])
        self.assertIn('not a heading inside a fence',steps[0]['excerpt'])
        self.assertIn('restart the gateway',steps[0]['excerpt'])

    def test_duplicate_headings_get_distinct_stable_ids_and_subheadings_a_path(self):
        found=units.extract(note())
        notes=[u for u in found if u['heading']=='Notes']
        self.assertEqual(len(notes),2)
        self.assertNotEqual(notes[0]['unit_id'],notes[1]['unit_id'])
        self.assertIn('Notes > Deeper detail',[u['heading'] for u in found])
        self.assertEqual(len({u['unit_id'] for u in found}),len(found))

    def test_ids_survive_reordering_and_hashes_follow_the_text(self):
        before={u['heading']:u for u in units.extract(note())}
        swapped=BODY.replace('## Why\nPer-client refresh duplicated retry logic in every client.\n\n','')
        swapped=swapped.replace('## Links','## Why\nPer-client refresh duplicated retry logic in every client.\n\n## Links')
        after={u['heading']:u for u in units.extract(note(swapped))}
        self.assertEqual(before['Why']['unit_id'],after['Why']['unit_id'])
        self.assertEqual(before['Why']['sha'],after['Why']['sha'])   # same text: the stored embedding is reused
        edited={u['heading']:u for u in units.extract(note(BODY.replace('every client','every mobile client')))}
        self.assertEqual(before['Why']['unit_id'],edited['Why']['unit_id'])
        self.assertNotEqual(before['Why']['sha'],edited['Why']['sha'])   # changed text: re-embedded

    def test_line_spans_point_into_the_note_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'note.md';path.write_text(FILE,encoding='utf-8')
            parsed=parse_text(FILE,path)
            lines=FILE.split('\n')
            for unit in units.extract(parsed):
                if unit['role']=='overview':continue
                first,last=unit['lines']
                if unit['heading'] and unit['heading']!='Important':
                    self.assertTrue(lines[first-1].lstrip('#').strip()==unit['heading'].split(' > ')[-1],(unit['heading'],lines[first-1]))
                self.assertTrue(lines[last-1].strip(),unit['heading'])

    def test_unreadable_file_gives_body_relative_lines(self):
        found=units.extract(note(path='does/not/exist.md'))
        why=next(u for u in found if u['heading']=='Why')
        self.assertEqual(BODY.strip().split('\n')[why['lines'][0]-1],'## Why')

    def test_an_oversized_section_splits_between_paragraphs_never_inside_a_fence(self):
        paragraph=' '.join(['word']*150)
        fence='```\n'+'\n'.join(f'command {i}' for i in range(200))+'\n```'
        body='Summary line.\n\n## Big\n'+'\n\n'.join([paragraph,paragraph,fence,paragraph])
        big=[u for u in units.extract(note(body)) if u['heading']=='Big']
        self.assertGreater(len(big),1)
        self.assertEqual(len({u['unit_id'] for u in big}),len(big))
        whole=[u for u in big if 'command 0' in u['excerpt']]
        self.assertEqual(len(whole),1);self.assertIn('command 199',whole[0]['text'])

    def test_empty_and_repeated_sections_are_skipped(self):
        body='Summary here.\n\n## Empty\n\n## A\nSame words in two places.\n\n## B\nSame words in two places.\n'
        headings=[u['heading'] for u in units.extract(note(body))]
        self.assertNotIn('Empty',headings);self.assertEqual(headings.count('A')+headings.count('B'),1)

    def test_role_names_come_from_existing_type_sections(self):
        self.assertEqual(units.role_of('Roll back'),'procedure')
        self.assertEqual(units.role_of('When not to use'),'constraint')
        self.assertEqual(units.role_of('Cause'),'cause')
        self.assertEqual(units.role_of('Auth service internals'),'section')

if __name__=='__main__':unittest.main()
