"""Crash-recoverable package installer retaining the lifecycle receipt contract."""
import json,os,uuid
from pathlib import Path
from . import installer_lifecycle as old
from .repository import transaction,recover,read_json,store_lock,canonical_json

def apply_package(plan,expected_digest):
    if not isinstance(plan,dict) or plan.get('schema')!=old.PLAN_SCHEMA:raise old.InstallError('Invalid install plan')
    unsigned={k:v for k,v in plan.items() if k!='planSha256'}
    if old._digest(unsigned)!=plan.get('planSha256') or expected_digest!=plan['planSha256']:raise old.StalePlanError('Install plan digest mismatch')
    if plan.get('blockers'):raise old.ConflictError('Unresolved ownership conflicts')
    target=Path(plan['target']);source=Path(plan['source']);identity=plan['operationId'];operation=old._operation_root(target,identity)
    receipt_path=operation/'receipt.json';intent_path=operation/'operation.json';journal=target.parent/'.operations'/identity/'journal.json'
    with store_lock(target.parent):
        if receipt_path.exists():
            receipt=old._load_receipt(receipt_path)
            return {'operationId':identity,'state':receipt['state'],'receipt':str(receipt_path),'receiptDigest':receipt['receiptDigest'],'idempotent':True}
        intent=read_json(intent_path)
        if intent:
            if intent['planSha256']!=expected_digest:raise old.StalePlanError('Recovery intent differs from approved plan')
        else:
            fresh=old.build_plan(target,source,plan.get('operation')=='uninstall')
            if fresh['sourceInventory']!=plan['sourceInventory'] or [(a['path'],a['expected'],a['action']) for a in fresh['actions']]!=[(a['path'],a['expected'],a['action']) for a in plan['actions']]:raise old.StalePlanError('Install plan became stale')
            changes={};effects=[];entries={}
            for item in plan['actions']:
                rel=item['path'];action=item['action'];path=old._path(target,rel)
                if action in {'create','replace','remove'}:
                    changes[rel]=None if action=='remove' else old._read_source(source,rel,item['sourceSha256']).decode('utf-8')
                if plan['operation']=='install' and action in {'create','replace','adopt','keep'}:
                    entries[rel]={'state':'owned','sourceSha256':item['sourceSha256'],'currentSha256':item['sourceSha256'],'mode':item['expected'].get('mode') or (0o666 if os.name=='nt' else 0o600),'operationId':identity}
            if plan['operation']=='install':
                ownership={'schema':old.OWNERSHIP_SCHEMA,'installationId':(plan.get('ownership') or {}).get('installationId',uuid.uuid4().hex),'installedRelease':old.CORE_RELEASE,'source':str(source),'entries':entries,'updatedAt':old._timestamp()}
                ownership['recordDigest']=old._digest(ownership);changes['.core-ownership-v1.json']=canonical_json(ownership)+'\n'
            else:changes['.core-ownership-v1.json']=None
            for rel,content in changes.items():
                path=old._path(target,rel);before=old._snapshot(path);backup=None
                if before['exists']:
                    backup='backups/'+rel;data,_=old._read_regular(path);old._atomic_write(old._path(operation,backup),data)
                effects.append({'path':rel,'before':before,'after':None,'backup':backup,'action':'remove' if content is None else 'write'})
            intent={'operationId':identity,'target':str(target),'state':'prepared','planSha256':expected_digest,'changes':changes,'effects':effects}
            old._atomic_write(intent_path,canonical_json(intent).encode())
        if journal.exists():recover(target.parent,identity)
        else:
            changes={(Path(target.name)/rel).as_posix():value for rel,value in intent['changes'].items()}
            expected={(Path(target.name)/e['path']).as_posix():e['before'].get('sha256') for e in intent['effects']}
            transaction(target.parent,changes,'package_'+plan['operation'],expected,identity)
        effects=intent['effects']
        for effect in effects:effect['after']=old._snapshot(old._path(target,effect['path']))
        receipt={'schema':old.RECEIPT_SCHEMA,'protocolVersion':old.PROTOCOL_VERSION,'operationId':identity,'operation':plan['operation'],'target':str(target),'source':str(source),'effects':effects,'state':'committed','createdAt':old._timestamp()}
        receipt['receiptDigest']=old._digest(receipt);old._atomic_write(receipt_path,canonical_json(receipt).encode())
        intent['state']='committed';old._atomic_write(intent_path,canonical_json(intent).encode())
        return {'operationId':identity,'state':'committed','receipt':str(receipt_path),'receiptDigest':receipt['receiptDigest'],'effects':len(effects)}
