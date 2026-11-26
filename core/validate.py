"""Validate canonical notes with the shared repository rules."""
from pathlib import Path
from .common import memory_root
from .repository import inventory,validate,problems,reconcile as refresh

def validate_domain(domain,root=None):
    root=Path(root or memory_root()).resolve();issues=[]
    if not (root/'domains'/domain/'notes').is_dir():return ['Domain notes directory is missing: '+domain]
    try:
        for note in inventory(domain,root,retired=True):
            try:
                validate(note,domain,root)
                if note.get('sha256') and note['sha256']!=note['digest']:raise ValueError('Body hash mismatch')
            except ValueError as exc:issues.append(str(exc)+': '+note['path'])
        issues+=[f"{item['problem'].capitalize()}: {item['path']}" for item in problems(domain,root)]
    except ValueError as exc:issues.append(str(exc))
    return issues

def reconcile(domain,root=None):
    """Rebuild a domain's derived indexes and views from every note that parses; notes with issues are indexed too and
    listed as warnings, so one bad note never turns search off (run validate to see them as errors)."""
    root=Path(root or memory_root()).resolve()
    issues=validate_domain(domain,root)
    if not (root/'domains'/domain/'notes').is_dir():return {'ok':False,'domain':domain,'issues':issues,'indexed':0}
    skipped=[];refresh(domain,root,skipped=skipped,full=True)   # `rebuild` rebuilds every row, not only changed ones
    issues+=[f'Left out of the search index ({error}): {path}' for path,error in skipped]
    left_out={path for path,_ in skipped}
    result={'ok':True,'domain':domain,'indexed':sum(1 for n in inventory(domain,root) if n.get('path') not in left_out)}
    if issues:result['warnings']=issues
    return result
