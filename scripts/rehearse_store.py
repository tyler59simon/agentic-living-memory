"""Rehearse schema migration on an isolated copy, never modify the source store."""
import argparse,hashlib,json,shutil,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.common import memory_root
from core.migration import migration_plan
from core.governance import apply
from core.validate import validate_domain
from core.repository import inventory

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=memory_root());args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='living-memory-rehearsal-') as folder:
        root=Path(folder)/'store';shutil.copytree(args.root,root)
        before={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'domains').rglob('*.md') if p.parent.name in {'notes','archive'}}
        # The copy carries sync-state.json with the real vault's path; never sync it from the rehearsal.
        proposed=migration_plan(root);receipt=apply(root,proposed['plan_id'],proposed['digest'],sync_overlay=False)
        after={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'domains').rglob('*.md') if p.parent.name in {'notes','archive'}}
        domains=sorted(p.name for p in (root/'domains').iterdir() if p.is_dir());issues={d:validate_domain(d,root) for d in domains}
        result={'ok':before==after and not any(issues.values()),'canonical_files_preserved':before==after,'note_count':sum(len(inventory(d,root,True)) for d in domains),'domains':domains,'issues':issues,'migration':proposed['summary'],'state':receipt['state']}
        print(json.dumps(result));return 0 if result['ok'] else 1
if __name__=='__main__':raise SystemExit(main())
