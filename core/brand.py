"""The brand a generated page shows in its sidebar header: a name and a logo, in place of "Living Memory" and its emblem.

A project may carry a brand (the `brand` field of its projects.json entry, set with `project update`), and the store may
carry a default brand (the `brand` setting in bindings.json, set with `config set brand`). A page scoped to one project
shows that project's brand; any other page, or a project without a brand, shows the default brand, else Living Memory.
A project brand without a name shows the project's name, and one without a logo falls back to the default logo, else
the emblem.

The logo is copied into the store when the brand is set, as a text file `brand/<owner>.logo` that holds the image as a
data URI, so it is written, journaled and undone like any other store file and the page inlines it with no network."""
from __future__ import annotations
import base64,hashlib,re
from pathlib import Path

MAX_LOGO=256*1024
NAME_MAX=60
DEFAULT_NAME='Living Memory'
_SVG=re.compile(r'<svg[\s>]',re.I)

def _sniff(data):
    """The image type of the bytes (png, jpeg, gif, webp or svg+xml), else None."""
    if data.startswith(b'\x89PNG\r\n\x1a\n'):return 'png'
    if data.startswith(b'\xff\xd8\xff'):return 'jpeg'
    if data[:6] in (b'GIF87a',b'GIF89a'):return 'gif'
    if data[:4]==b'RIFF' and data[8:12]==b'WEBP':return 'webp'
    try:text=data.decode('utf-8-sig').lstrip()
    except UnicodeDecodeError:return None
    return 'svg+xml' if text.startswith('<') and _SVG.search(text[:4096]) else None

def logo_uri(source):
    """The logo file at `source` as a data URI; ValueError (invalid_logo) when it is missing, too large or not an image."""
    path=Path(str(source)).expanduser()
    if not path.is_file():raise ValueError(f'invalid_logo: {source} is not a file')
    if path.stat().st_size>MAX_LOGO:raise ValueError(f'invalid_logo: {source} is larger than {MAX_LOGO//1024} KB')
    data=path.read_bytes()
    kind=_sniff(data)
    if not kind:raise ValueError(f'invalid_logo: {source} is not a PNG, JPEG, GIF, WebP or SVG image')
    return f'data:image/{kind};base64,'+base64.b64encode(data).decode('ascii')

def logo_path(owner):return f'brand/{owner}.logo'

def clean(value,old,owner,name_required):
    """(record, file changes) for a brand value from a payload or setting. value None removes the brand; {"name": N,
    "logo": PATH} sets it, where a missing logo keeps the stored one and "logo": null removes it. old is the stored record."""
    rel=logo_path(owner)
    had=bool(old and old.get('logo'))
    if value is None:return None,({rel:None} if had else {})
    if not isinstance(value,dict) or set(value)-{'name','logo'}:raise ValueError('invalid_value: brand takes name and logo, for example {"name": "Acme Docs", "logo": "C:/path/logo.svg"}')
    record={}
    name=value.get('name')
    if name is not None:
        if not isinstance(name,str) or not 1<=len(name.strip())<=NAME_MAX:raise ValueError(f'invalid_value: brand.name must be 1..{NAME_MAX} characters')
        from .repository import find_secrets
        if find_secrets(name):raise ValueError('Potential credential detected in brand.name; remove it')
        record['name']=name.strip()
    elif name_required:raise ValueError('invalid_value: the default brand needs a name')
    changes={}
    if 'logo' not in value:
        if had:record['logo']=rel
    elif value['logo'] is None:
        if had:changes[rel]=None
    else:
        if not isinstance(value['logo'],str) or not value['logo'].strip():raise ValueError('invalid_value: brand.logo is the path of an image file, or null')
        changes[rel]=logo_uri(value['logo'].strip())+'\n'
        record['logo']=rel
    return record,changes

def _stored_logo(root,record):
    if not record or not record.get('logo'):return None
    try:text=(Path(root)/record['logo']).read_text(encoding='utf-8').strip()
    except OSError:return None
    return text if text.startswith('data:image/') and ';base64,' in text else None

def state(root):
    """The stored brand records: {'default': record or None, 'projects': {slug: (project name, record or None)}}."""
    from .common import load_bindings
    from . import projects
    default=load_bindings(root).get('brand')
    try:entries=projects.load(root)['projects']
    except ValueError:entries=[]
    return {'default':default if isinstance(default,dict) else None,
            'projects':{p['slug']:(p.get('name') or p['slug'],p.get('brand') if isinstance(p.get('brand'),dict) else None) for p in entries}}

def resolve(root,project=None,known=None):
    """{name, logo, key} for a page: project is the slug the page is scoped to (None: several or none). logo is a data
    URI, or None for the Living Memory emblem; key changes whenever what the page shows changes. known: a state() result."""
    known=known or state(root)
    default=known['default']
    name,logo=DEFAULT_NAME,None
    if default:
        name,logo=default.get('name') or DEFAULT_NAME,_stored_logo(root,default)
    if project in known['projects']:
        project_name,record=known['projects'][project]
        if record is not None:
            name=record.get('name') or project_name
            logo=_stored_logo(root,record) or logo
    key=hashlib.sha256(f'{name}\n{logo or ""}'.encode('utf-8')).hexdigest()[:16]
    return {'name':name,'logo':logo,'key':key}

def note_project(note,known):
    """The registered project a note belongs to when it carries exactly one registered project/<slug> tag, else None."""
    slugs=[t[8:] for t in note.get('tags') or [] if isinstance(t,str) and t.startswith('project/') and t[8:] in known['projects']]
    return slugs[0] if len(slugs)==1 else None

def refresh_views(root):
    """Re-render the local dashboard pages after a brand change (their cache records the brand they show). It never raises."""
    from . import views
    try:domains=sorted({v['domain'] for v in views.note_rows(root)})
    except Exception:return
    for domain in domains:views.refresh(domain,root)
