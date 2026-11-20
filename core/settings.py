"""config get and config set: the store-wide settings kept at the top level of bindings.json (FEAT-5).

`set` validates the value, then writes bindings.json through one journaled transaction (kind config_set), so `undo`
reverts it. Domain bindings and aliases are not settings: bind, unbind and remap own them."""
import json
from pathlib import Path
from .common import load_bindings,safe_domain
from .repository import canonical_json,digest,read_json,roll_forward_pending,store_lock,transaction

DEFAULTS={'search_scope':{'global_domains':['_global'],'foreign_domains':[]},'repo_aliases':{},
          'suggest_bind':True,'auto_resolve':True,'auto_rebaseline':True,'cross_project':True}
HELP={'search_scope':'global_domains and foreign_domains: the domains search adds beyond the active, parent and child ones',
      'repo_aliases':'{"REPO_FOLDER_NAME": ["alias", ...]}: other names a prompt may use for a repo',
      'suggest_bind':'false stops the one-line hint to bind an unbound git repo at session start',
      'auto_resolve':'false stops SessionStart resolving finished work notes',
      'auto_rebaseline':'false stops the hourly re-baselining of notes a file change did not touch',
      'cross_project':'false stops the sibling-domain lookup when no local note matches strongly'}

def _key(key):
    if key not in DEFAULTS:raise ValueError(f"unknown_setting: {key!r}; settings are {', '.join(DEFAULTS)}")
    return key

def _names(value,label):
    if not isinstance(value,list) or not all(isinstance(v,str) and v.strip() for v in value):raise ValueError(f'invalid_value: {label} must be a list of names')
    try:return [safe_domain(v) for v in value]
    except ValueError:raise ValueError(f'invalid_value: {label} holds a name that is not a domain name') from None

def validate(key,value):
    """The value as it is stored; ValueError (invalid_value) when it has the wrong shape. None removes the key (the default applies)."""
    if value is None:return None
    if key in ('suggest_bind','auto_resolve','auto_rebaseline','cross_project'):
        if not isinstance(value,bool):raise ValueError(f'invalid_value: {key} is true or false')
        return value
    if key=='search_scope':
        if not isinstance(value,dict) or set(value)-{'global_domains','foreign_domains'}:raise ValueError('invalid_value: search_scope takes only global_domains and foreign_domains')
        scope={k:_names(value.get(k,DEFAULTS['search_scope'][k]),f'search_scope.{k}') for k in ('global_domains','foreign_domains')}
        if not scope['global_domains']:raise ValueError('invalid_value: search_scope.global_domains needs at least one domain')
        return scope
    if not isinstance(value,dict) or not all(isinstance(k,str) and k.strip() for k in value):raise ValueError('invalid_value: repo_aliases maps repo folder names to lists of aliases')
    for repo,aliases in value.items():
        if not isinstance(aliases,list) or not all(isinstance(a,str) and a.strip() for a in aliases):raise ValueError(f'invalid_value: repo_aliases.{repo} must be a list of aliases')
    return {repo:[a.strip() for a in aliases] for repo,aliases in value.items()}

def get(root,key=None):
    """{ok, settings:{key: {value, default}}}: value is what applies now (the stored one, else the default)."""
    config=load_bindings(root);keys=[_key(key)] if key else list(DEFAULTS)
    return {'ok':True,'settings':{k:{'value':config.get(k,DEFAULTS[k]),'default':k not in config} for k in keys}}

def set_(root,key,text):
    """Store one setting. text is JSON (true, false, a list or object, or null to go back to the default)."""
    _key(key)
    try:value=json.loads(text)
    except ValueError:raise ValueError(f'invalid_value: {key} takes JSON, for example true, false or {json.dumps(DEFAULTS[key])}') from None
    value=validate(key,value);root=Path(root).resolve()
    with store_lock(root):
        roll_forward_pending(root)   # an interrupted bind or config change is finished first, so this reads what it wrote
        path=root/'bindings.json';read=path.read_bytes() if path.is_file() else b'';config=read_json(path,None)
        if not isinstance(config,dict):raise ValueError('Invalid bindings preserved')
        before=config.get(key,DEFAULTS[key]);old=canonical_json(config)
        if value is None:config.pop(key,None)
        else:config[key]=value
        new=canonical_json(config);receipt=transaction(root,{'bindings.json':new},'config_set',expected={'bindings.json':digest(read)}) if new!=old else {}   # an unchanged value writes nothing
    return {'ok':True,'setting':key,'value':config.get(key,DEFAULTS[key]),'default':key not in config,'previous':before,'operation_id':receipt.get('operation_id')}
