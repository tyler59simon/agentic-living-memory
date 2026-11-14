"""MAINT-1F dry-run gate: one real reviewer dry run on a labelled fixture store, or on a copy of the real store. Never writes the source store.

  python scripts/eval_maintenance.py fixture          # build a labelled throwaway store, dry run, score every decision
  python scripts/eval_maintenance.py copy [--root R]  # copy the real store, preview and dry run one domain, print every decision

Each mode makes one paid call through the installed `claude` CLI, bounded by the maintenance limits (Sonnet, 1 USD, 300 s).
"""
import argparse,hashlib,json,shutil,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import maintenance,maintenance_state as ms
from core.common import memory_root

OLD='2020-01-01'
TAGS=['subject/project','concern/architecture']
RETIRE={'archive','supersede','resolve'}
# Copy only what candidate selection reads; operation journals, backups, logs and turn pointers stay behind.
COPY_IGNORE=shutil.ignore_patterns('.operations','backups','logs','.turns','.eval','worker.lock','*.lock')

def hashes(root,patterns):
    root=Path(root)
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for pattern in patterns for p in root.glob(pattern) if p.is_file()}

def note_hashes(root):return hashes(root,('domains/*/notes/*.md','domains/*/archive/*.md','maintenance.json'))

# ---- fixture

# Each case: (label, title, body, capture options, acceptable actions, required). A required case must get one of its acceptable actions;
# every other case must at least not be retired unless retirement is acceptable for it. An update of a case in PATCHED must also apply
# cleanly (each find occurs once) and leave the body with the first text and without the second.
PATCHED={'changed_source':('60 seconds','30 seconds')}
def cases():
    from core.note_format import render
    deploy='Deploys run from the main branch after the test suite passes on the build server and a reviewer approves the change in the tracker.'
    return [
        ('expired_status','Sprint 14 status','Status for sprint 14, which ended on 2020-01-31: the user table migration finished and shipped in release 2.3. Nothing is in progress.',
         {'type':'status','review_by':OLD},{'archive'},True),
        ('finished_plan','User table migration plan',render('Plan for moving the user table to the new schema.',actions=[],done=['Copy the data','Switch reads to the new table','Drop the old table']),
         {'type':'plan'},{'resolve'},True),
        ('changed_source','Request timeout and retries','The request timeout is 30 seconds, set by `TIMEOUT` in src/config.py.\n\nRequests retry through the shared client in src/client.py.',
         {'files':['src/config.py'],'review_by':OLD},{'update'},True),
        ('valuable_decision','Use SQLite FTS5 for the search index','We chose SQLite FTS5 for the search index because it ships with Python and needs no server. Revisit only if the store outgrows one machine.',
         {'type':'decision','review_by':OLD},{'keep','confirm','defer'},False),
        ('caveat_bug','Crash on a cold cache',render('The indexer crashed when the cache folder was empty. Fixed by creating the folder on start. Keep the caveat: a cold cache still makes the first search slow.',actions=[],done=['Create the cache folder on start']),
         {'type':'bug'},{'keep','confirm','update','defer'},False),
        ('pinned','Build image Python version','The build image uses Python 3.8.',{'pinned':True,'review_by':OLD},{'keep','confirm','update','defer'},False),
        ('missing_source','Legacy CSV import','The legacy CSV import lives in src/legacy.py and runs nightly at 02:00.',
         {'files':['src/legacy.py'],'review_by':OLD},{'keep','update','defer'},False),
        ('duplicate_a','Deploy process',deploy,{'review_by':OLD},{'keep','confirm','defer','update','supersede'},False),
        ('duplicate_b','Deploy process',deploy+' Always.',{'review_by':OLD,'idempotency_key':'k2'},{'keep','confirm','defer','update','supersede'},False),
    ]

def build_fixture(base):
    from core.init_domain import init_domain
    from core.notes import capture
    root,project=base/'store',base/'project'
    (project/'src').mkdir(parents=True)
    init_domain('demo',[str(project)],root)
    (root/'tag_taxonomy.json').write_text(json.dumps({'tags':TAGS}))
    (project/'src/config.py').write_text('TIMEOUT = 30\n')
    (project/'src/client.py').write_text('def get(url):\n    return retry(url)\n')
    (project/'src/legacy.py').write_text('def import_csv(path):\n    pass\n')
    labels,bodies={},{}
    for label,title,body,kw,accept,required in cases():
        note=capture(title,body,TAGS,'demo',root,allow_similar=True,**kw)
        labels[f"demo:{note['id']}"]={'label':label,'accept':sorted(accept),'required':required}
        bodies[f"demo:{note['id']}"]=body
    # After capture: the timeout changed and the legacy module was deleted.
    (project/'src/config.py').write_text('TIMEOUT = 60\n')
    (project/'src/legacy.py').unlink()
    ms.set_config(root,ms.validate({'enabled':False,'domains':['demo']}))
    return root,project,labels,bodies

def patched(body,patches):
    """The body after the patches as the executor would apply them, or None when it would refuse them."""
    try:return maintenance._patched(body,patches)
    except ValueError:return None

def score(labels,result,permitted,status,bodies=None):
    """The gate checks of handoff section 4.7 over one dry-run result. Returns (checks, rows)."""
    decisions={d['ref']:d for d in result.get('decisions') or []}
    rows,checks=[],{}
    for ref,info in labels.items():
        d=decisions.get(ref)
        action=d['action'] if d else None
        row={'ref':ref,**info,'action':action,'rule':d.get('rule') if d else None,'evidence_ids':d.get('evidence_ids') if d else None,
             'successor':d.get('successor') if d else None,'patches':d.get('patches') if d else None,'reason':d.get('reason') if d else None,
             'permitted':permitted.get(ref)}
        row['acceptable']=action in info['accept'] if action else not info['required']
        if action=='update' and info['label'] in PATCHED:
            want,gone=PATCHED[info['label']];after=patched((bodies or {}).get(ref,''),row['patches']) if row['patches'] else None
            row['acceptable']=after is not None and want in after and gone not in after
        rows.append(row)
    retired=[r for r in rows if r['action'] in RETIRE]
    checks['status_dry_run']=result.get('status')=='dry_run'
    checks['no_protected_retired']=not [r for r in retired if r['action'] not in r['accept']]
    checks['retirements_permitted']=all(r['permitted'] is not None and r['action'] in r['permitted'] for r in retired)
    checks['retirements_cite_evidence']=all(r['evidence_ids'] or r['action']=='resolve' for r in retired)
    pair=[r for r in rows if r['label'].startswith('duplicate_')]
    checks['duplicates_not_both_retired']=sum(r['action'] in RETIRE for r in pair)<2
    checks['required_cases_useful']=all(r['acceptable'] for r in rows if r['required'])
    checks['every_case_acceptable']=all(r['acceptable'] for r in rows)
    acc=status.get('accounting') or {}
    usage=result.get('usage_micro')
    checks['accounting_matches']=acc.get('reviewed')==result.get('reviewed') and (usage is None or acc.get('charged_usd')==ms.format_usd(usage))
    return checks,rows

def fixture_gate(base,adapter=None):
    """Build the fixture under `base`, dry run it once (the real reviewer unless `adapter` is given) and score it."""
    root,project,labels,bodies=build_fixture(Path(base))
    preview=maintenance.preview(root,domains=['demo'])
    permitted={c['ref']:c['permitted'] for c in preview['candidates']}
    before,src_before=note_hashes(root),hashes(project,('src/*',))
    result=maintenance.run(root,dry_run=True,adapter=adapter,allow_disabled=True,domains=['demo'])
    checks,rows=score(labels,result,permitted,maintenance.status(root),bodies)
    checks['notes_and_config_unchanged']=note_hashes(root)==before
    checks['sources_unchanged']=hashes(project,('src/*',))==src_before
    checks['every_case_selected']=set(labels)<=set(permitted)
    return {'ok':all(checks.values()),'mode':'fixture','checks':checks,'run':{k:result.get(k) for k in ('id','status','reason_code','detail','reviewed','usage_micro','proposed')},
            'cost_usd':ms.format_usd(result['usage_micro']) if result.get('usage_micro') is not None else None,'cases':rows}

def run_fixture(args):
    with tempfile.TemporaryDirectory(prefix='living-memory-maint-gate-') as folder:
        out=fixture_gate(folder)
        print(json.dumps(out,indent=2));return 0 if out['ok'] else 1

# ---- copy

def run_copy(args):
    source=Path(args.root).resolve()
    with tempfile.TemporaryDirectory(prefix='living-memory-maint-copy-') as folder:
        root=Path(folder)/'store';shutil.copytree(source,root,ignore=COPY_IGNORE)
        # The copy carries each domain's sync-state.json with the real vault's path; never sync it from the copy.
        for state in root.glob('domains/*/sync-state.json'):state.unlink()
        # Review exactly the requested domain, whatever the real store has configured (a configured list would win over the fallback).
        ms.set_config(root,ms.validate(dict(ms.config(root),enabled=False,domains=[args.domain]),root,allow_global=True),allow_global=True)
        source_maint=hashes(source,('maintenance.json','maintenance/**/*'))
        before=note_hashes(root)
        preview=maintenance.preview(root,domains=[args.domain])
        result=maintenance.run(root,dry_run=True,allow_disabled=True,domains=[args.domain])
        titles={c['ref']:c for c in preview['candidates']}
        decisions=[{**d,'title':titles.get(d['ref'],{}).get('title'),'type':titles.get(d['ref'],{}).get('type'),'kinds':titles.get(d['ref'],{}).get('kinds'),
                    'permitted':titles.get(d['ref'],{}).get('permitted')} for d in result.get('decisions') or []]
        status=maintenance.status(root)
        checks={'status_dry_run':result.get('status')=='dry_run','copy_notes_unchanged':note_hashes(root)==before,
                'source_maintenance_untouched':hashes(source,('maintenance.json','maintenance/**/*'))==source_maint,
                'retirements_permitted':all(d['action'] in (d['permitted'] or []) for d in decisions if d['action'] in RETIRE),
                'accounting_matches':(status.get('accounting') or {}).get('reviewed')==result.get('reviewed')}
        out={'ok':all(checks.values()),'mode':'copy','domain':args.domain,'checks':checks,
             'run':{k:result.get(k) for k in ('id','status','reason_code','detail','reviewed','dropped','usage_micro','proposed')},
             'cost_usd':ms.format_usd(result['usage_micro']) if result.get('usage_micro') is not None else None,
             'preview':[{k:c[k] for k in ('ref','title','type','level','kinds','permitted')} for c in preview['candidates']],'decisions':decisions}
        print(json.dumps(out,indent=2));return 0 if out['ok'] else 1

def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    sub=parser.add_subparsers(dest='mode',required=True)
    sub.add_parser('fixture',help='labelled throwaway store')
    copy=sub.add_parser('copy',help='copy of the real store');copy.add_argument('--root',type=Path,default=None);copy.add_argument('--domain',default='sandbox')
    args=parser.parse_args()
    if args.mode=='copy':
        args.root=args.root or memory_root()
        return run_copy(args)
    return run_fixture(args)
if __name__=='__main__':raise SystemExit(main())
