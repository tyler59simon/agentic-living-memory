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
    issues=validate_domain(domain,root)
    if not issues:refresh(domain,root)
    return {'ok':not issues,'domain':domain,'issues':issues,'indexed':len(inventory(domain,root)) if not issues else 0}
