"""The package supports Python 3.10, but the suite runs on newer versions. Python 3.12 (PEP 701) lets an f-string reuse its own
quote inside a replacement field, which is a SyntaxError on 3.10 and 3.11. This test finds such f-strings with the tokenizer."""
import sys,tokenize,unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def _quote(text):
    """The quote of a string or f-string start token: ' " ''' or \"\"\"."""
    body=text.lstrip('rRbBfFuU')
    return body[:3] if body[:3] in ("'''",'"""') else body[:1]

def reused_quotes(path):
    """(line, text) of each string inside an f-string replacement field that uses the enclosing f-string's quote."""
    found=[]
    stack=[]
    with open(path,'rb') as f:
        for tok in tokenize.tokenize(f.readline):
            if tok.type==tokenize.FSTRING_START:
                if stack and _quote(tok.string)==stack[-1]:found.append((tok.start[0],tok.line.strip()[:120]))
                stack.append(_quote(tok.string))
            elif tok.type==tokenize.FSTRING_END:stack.pop()
            elif tok.type==tokenize.STRING and stack and _quote(tok.string)==stack[-1]:found.append((tok.start[0],tok.line.strip()[:120]))
    return found

@unittest.skipIf(sys.version_info<(3,12),'the tokenizer reports f-string parts from Python 3.12')
class MinPythonSyntax(unittest.TestCase):
    def test_no_fstring_reuses_its_quote(self):
        files=[p for p in ROOT.rglob('*.py') if not any(part in {'.git','node_modules','.venv','__pycache__'} for part in p.parts)]
        bad=[f'{p.relative_to(ROOT)}:{line}: {text}' for p in files for line,text in reused_quotes(p)]
        self.assertEqual(bad,[],'f-strings that need Python 3.12:\n'+'\n'.join(bad))

    def test_the_check_finds_one(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'x.py';path.write_text("d={}\ny=f'{d.get('a')}'\nz=f'{d.get(\"a\")}'\n",encoding='utf-8')
            self.assertEqual([line for line,_ in reused_quotes(path)],[2])
