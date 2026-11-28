"""config get and config set: the store-wide settings kept at the top level of bindings.json (FEAT-5).

`set` validates the value, then writes bindings.json through one journaled transaction (kind config_set), so `undo`
reverts it. Domain bindings and aliases are not settings: bind, unbind and remap own them."""
import json,re
from pathlib import Path
from .common import load_bindings,safe_domain
from .repository import canonical_json,digest,read_json,roll_forward_pending,store_lock,transaction

DEFAULTS={'search_scope':{'global_domains':['_global'],'foreign_domains':[]},'repo_aliases':{},
          'suggest_bind':True,'auto_resolve':True,'auto_rebaseline':True,'cross_project':True,'digest_refresh':None,'brand':None}
HELP={'search_scope':'global_domains and foreign_domains: the domains search adds beyond the active, parent and child ones',
      'repo_aliases':'{"REPO_FOLDER_NAME": ["alias", ...]}: other names a prompt may use for a repo',
      'suggest_bind':'false stops the one-line hint to bind an unbound git repo at session start',
      'auto_resolve':'false stops SessionStart resolving finished work notes',
      'auto_rebaseline':'false stops the hourly re-baselining of notes a file change did not touch',
      'cross_project':'false stops the sibling-domain lookup when no local note matches strongly',
      'digest_refresh':'{"daily_budget_usd": "1.00", "model": "haiku"} rewrites stale digests with a headless agent once a day (spends money; off by default)',
      'brand':'{"name": "Acme Docs", "logo": "C:/path/logo.svg"}: the name and logo of wiki and dashboard pages not scoped to one branded project (default: Living Memory); the logo is copied into the store'}

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
    if key=='brand':return value   # checked and its logo read in set_, which has the store
    if key in ('suggest_bind','auto_resolve','auto_rebaseline','cross_project'):
        if not isinstance(value,bool):raise ValueError(f'invalid_value: {key} is true or false')
        return value
    if key=='search_scope':
        if not isinstance(value,dict) or set(value)-{'global_domains','foreign_domains'}:raise ValueError('invalid_value: search_scope takes only global_domains and foreign_domains')
        scope={k:_names(value.get(k,DEFAULTS['search_scope'][k]),f'search_scope.{k}') for k in ('global_domains','foreign_domains')}
        if not scope['global_domains']:raise ValueError('invalid_value: search_scope.global_domains needs at least one domain')
        return scope
    if key=='digest_refresh':
        if not isinstance(value,dict) or set(value)-{'daily_budget_usd','model'} or 'daily_budget_usd' not in value:
            raise ValueError('invalid_value: digest_refresh takes daily_budget_usd and an optional model, for example {"daily_budget_usd": "1.00", "model": "haiku"}')
        try:budget=float(str(value['daily_budget_usd']))
        except ValueError:budget=-1.0
        if isinstance(value['daily_budget_usd'],bool) or not 0<budget<=50:raise ValueError('invalid_value: digest_refresh.daily_budget_usd is a dollar amount above 0 and at most 50')
        model=value.get('model','haiku')
        if not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',model):raise ValueError('invalid_value: digest_refresh.model is a model alias such as haiku or sonnet')
        return {'daily_budget_usd':f'{budget:.2f}','model':model}
    if not isinstance(value,dict) or not all(isinstance(k,str) and k.strip() for k in value):raise ValueError('invalid_value: repo_aliases maps repo folder names to lists of aliases')
    for repo,aliases in value.items():
        if not isinstance(aliases,list) or not all(isinstance(a,str) and a.strip() for a in aliases):raise ValueError(f'invalid_value: repo_aliases.{repo} must be a list of aliases')
    return {repo:[a.strip() for a in aliases] for repo,aliases in value.items()}

def get(root,key=None):
    """{ok, settings:{key: {value, default}}}: value is what applies now (the stored one, else the default)."""
    config=load_bindings(root)
    keys=[_key(key)] if key else list(DEFAULTS)
    return {'ok':True,'settings':{k:{'value':config.get(k,DEFAULTS[k]),'default':k not in config} for k in keys}}

def set_(root,key,text):
    """Store one setting. text is JSON (true, false, a list or object, or null to go back to the default)."""
    _key(key)
    try:value=json.loads(text)
    except ValueError:raise ValueError(f'invalid_value: {key} takes JSON, for example true, false or {json.dumps(DEFAULTS[key])}') from None
    value=validate(key,value)
    root=Path(root).resolve()
    with store_lock(root):
        roll_forward_pending(root)   # an interrupted bind or config change is finished first, so this reads what it wrote
        path=root/'bindings.json'
        read=path.read_bytes() if path.is_file() else b''
        config=read_json(path,None)
        if not isinstance(config,dict):raise ValueError('Invalid bindings preserved')
        before=config.get(key,DEFAULTS[key])
        old=canonical_json(config)
        files={}
        if key=='brand':
            from .brand import clean
            value,files=clean(value,before if isinstance(before,dict) else None,'default',True)
        if value is None:config.pop(key,None)
        else:config[key]=value
        new=canonical_json(config)  # an unchanged value writes nothing
        expected={'bindings.json':digest(read)}
        for rel,text in list(files.items()):
            path=root/rel
            current=path.read_bytes() if path.is_file() else None
            if current==(text.encode('utf-8') if text is not None else None):del files[rel]   # the same logo again writes nothing
            else:expected[rel]=digest(current)
        receipt=transaction(root,{'bindings.json':new,**files},'config_set',expected=expected) if new!=old or files else {}
        if key=='brand' and receipt:
            from .brand import refresh_views
            refresh_views(root)
    return {'ok':True,'setting':key,'value':config.get(key,DEFAULTS[key]),'default':key not in config,'previous':before,'operation_id':receipt.get('operation_id')}
