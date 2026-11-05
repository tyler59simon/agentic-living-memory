"""Validated revision-checked canonical update script."""
from pathlib import Path
from .common import memory_root
from .repository import parse,parse_text,validate,serialize,confined,store_lock
from .notes import find_note,_commit

def safe_write(domain,relative_path,content,root=None,expected_revision=None):
    root=Path(root or memory_root()).resolve()
    with store_lock(root):
        target=confined(root,Path('domains')/domain/relative_path);directory=root/'domains'/domain
        if not target.is_relative_to(directory/'notes'):raise ValueError('Updates must target the canonical notes tree')
        note=parse_text(content,target)
        if not note:raise ValueError('Invalid note frontmatter')
        validate(note,domain,root,new=not target.exists());expected={}
        if target.exists():
            old=parse(target)
            if not old or old['id']!=note['id']:raise ValueError('Immutable note ID cannot change')
            if not expected_revision:raise ValueError('expected_revision is required')
            if old['revision']!=expected_revision:raise ValueError('stale_revision')
            expected[target.relative_to(root).as_posix()]=expected_revision
        else:raise ValueError('Use capture for new notes')
        from .governance import plan
        return plan(root,{target.relative_to(root).as_posix():serialize(note)},'safe_write',[domain],{'id':note['id'],'action':'update'})

def main(argv=None):
    import argparse
    p=argparse.ArgumentParser();p.add_argument('domain');p.add_argument('path');p.add_argument('--root',type=Path);p.add_argument('--content-file',type=Path,required=True);p.add_argument('--expected-revision',required=True);a=p.parse_args(argv)
    import json
    print(json.dumps(safe_write(a.domain,a.path,a.content_file.read_text(encoding='utf-8'),a.root,a.expected_revision),separators=(',',':')));return 0
if __name__=='__main__':raise SystemExit(main())
