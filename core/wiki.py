"""Domain wiki: one domain shown as a structured wiki (projects, sections, a code reference tree and linked articles).

wiki_model() organises the notes with no model calls: a note belongs to the registered project whose project/<slug>
tag it carries (else the project whose repo holds its first file, else the pseudo-project General), sits in a section
chosen by its type, and, when it names files and is knowledge, in a folder tree built from its first file's path.
render_html() turns that model into one self-contained page in Hivemind's shell; Hivemind's Wiki page renders the
same model natively (`wiki --format json`). The page is rendered on demand only: nothing re-renders it on write.

The page needs no network: styles, script, font and logo are inline, and the data is a JSON block that the script
turns into elements with textContent only. Note text is agent-written, so it is never put into markup unescaped."""
from __future__ import annotations
import hashlib,html,json,os
from datetime import datetime,timezone
from pathlib import Path
from .common import atomic_write
from .note_types import NOTE_TYPES

# Bump when the model or the page changes shape.
WIKI_RENDERER=1
SECTIONS=(('overview','Overview','Project briefs, charters and the context a newcomer reads first.'),
          ('architecture','Architecture','Codebase maps and notes on how the system is put together.'),
          ('decisions','Decisions','Decisions, policies and preferences, newest first, each with its reasons.'),
          ('guides','Guides','Runbooks: step-by-step procedures with when to use them and how to check the result.'),
          ('issues','Known issues','Bugs and risks, with their cause, fix or mitigation.'),
          ('plans','Plans and status','Plans, roadmaps and status notes.'),
          ('research','Research','Investigations, evaluations and their findings.'),
          ('reference','Reference','Facts and conventions that are not tied to one file.'),
          ('code','Code reference','Notes about specific files and folders, arranged as the repository tree.'))
CHRONOLOGICAL=('decisions','plans','issues','research')
GENERAL={'slug':'general','name':'General','description':'Notes that belong to no registered project: preferences, tools and knowledge shared across projects.',
         'aliases':[],'repos':[],'status':'active','charter':None}
OTHER='other files'

def section_of(note,files):
    """The section a note is listed in, from its type (knowledge: Code reference when it names files)."""
    kind=note.get('type')
    if kind in ('brief','context'):return 'overview'
    if kind=='map':return 'architecture'
    if kind in ('decision','policy','preference'):return 'decisions'
    if kind=='runbook':return 'guides'
    if kind in ('bug','risk'):return 'issues'
    if kind in ('plan','status'):return 'plans'
    if kind=='research':return 'research'
    if files:return 'code'
    return 'architecture' if 'concern/architecture' in (note.get('tags') or []) else 'reference'

def _locate(value,prefixes,repos):
    """(display path, owning repo or None, is_dir) for one stored file path. Relative paths resolve against the domain's
    bound folders, as capture stored them. The owner is the registered repo (longest first) whose full path holds the file,
    so two repos with the same folder name stay apart; the display path is <repo folder>/<path inside it>. A path that
    exists says whether it is a folder; otherwise a last segment with no dot is taken as a folder. A path the wiki may not
    probe (a PermissionError, say) counts as missing."""
    def probe(path,test):
        try:return test(path)
        except (OSError,ValueError):return False
    text=str(value).strip().replace('\\','/')
    while text.startswith('./'):text=text[2:]
    text=text.rstrip('/') or '.'
    raw=Path(text);candidates=[raw] if raw.is_absolute() else [prefix/text for prefix in prefixes]
    path=next((c for c in candidates if probe(c,Path.exists)),candidates[0] if candidates else None)
    # '.' is the bound folder itself: shown by its folder name (the repo's, when a registered repo owns it).
    shown,owner=((prefixes[0].name if prefixes else '') if text=='.' else text),None
    if path is not None:
        full=Path(os.path.normpath(path)).as_posix().rstrip('/');folded=full.casefold()
        for repo in repos:
            base=repo.replace('\\','/').rstrip('/')
            if folded==base.casefold() or folded.startswith(base.casefold()+'/'):
                shown,owner=(base.rsplit('/',1)[-1]+full[len(base):]).strip('/'),repo;break
    last=shown.rsplit('/',1)[-1]
    exists=path is not None and probe(path,Path.exists)
    return shown,owner,(probe(path,Path.is_dir) if exists else '.' not in last)

def _split_lead(body,summary):
    """The body without its leading summary paragraph, which the page shows as the lead."""
    lead=(summary or '').strip();text=(body or '').lstrip()
    return text[len(lead):].lstrip('\n') if lead and text.startswith(lead) else (body or '')

def wiki_model(domain,root,archived=False):
    """The JSON-safe wiki of one domain: notes (as articles), projects with their sections, the code folder tree,
    the section list, and the targets of links into other domains."""
    from .repository import inventory
    from .note_format import parts,plain
    from .links import note_links
    from . import projects as registry
    root=Path(root)
    raw=[dict(n,domain=domain) for n in inventory(domain,root,retired=archived)]
    graph=note_links(root,[domain],'any' if archived else 'active')
    nodes={n['ref']:n for n in graph['nodes']};out={};back={};external={}
    for edge in graph['edges']:
        a,b=nodes[edge['from']],nodes[edge['to']]
        if edge['kind']!='link' or a['domain']!=domain:continue
        if b['domain']==domain:out.setdefault(a['id'],[]).append(b['id']);back.setdefault(b['id'],[]).append(a['id'])
        else:external[(b['title'] or '').strip().casefold()]={'title':b['title'],'domain':b['domain']}

    projects=[]
    for p in registry.load(root)['projects']:
        charter=str(p.get('charter') or '')
        projects.append({'slug':p['slug'],'name':p.get('name') or p['slug'],'description':p.get('description') or '','aliases':list(p.get('aliases') or []),
                         'repos':[Path(r).name for r in p.get('repos') or []],'repo_paths':list(p.get('repos') or []),'status':p.get('status') or 'active',
                         'tag':registry.tag(p['slug']),'charter':charter.split(':',1)[1] if charter.startswith(domain+':') else None})
    by_tag={p['tag']:p for p in projects};repo_project={r:p['slug'] for p in projects for r in p['repo_paths']}
    repos=sorted(repo_project,key=len,reverse=True)
    from .common import domain_prefixes,load_bindings
    prefixes=domain_prefixes(domain,load_bindings(root))
    # The pseudo-project for unassigned notes takes the first free slug, so a registered project named general keeps its own.
    registered={p['slug'] for p in projects};general=next(s for s in ('general',*(f'general-{i}' for i in range(2,100))) if s not in registered)

    notes=[];where={}
    for n in raw:
        summary=next((text for key,_,text in parts(n.get('body') or '') if key=='summary'),'')
        located=[l for l in (_locate(v,prefixes,repos) for v in n.get('files') or []) if l[0]]
        files=[l[0] for l in located];owner=repo_project.get(located[0][1]) if located else None
        if located:where[n['id']]=located[0]
        tagged=[by_tag[t]['slug'] for t in n.get('tags') or [] if t in by_tag]
        if len(tagged)>1 and owner in tagged:project=owner
        elif tagged:project=tagged[0]
        else:project=owner or general
        status=n.get('status') or 'active'
        notes.append({'id':n['id'],'ref':n['id'][:8],'title':n.get('title') or '','type':n.get('type') or '','status':status,'retired':status!='active',
                      'tags':list(n.get('tags') or []),'files':files,'updated':str(n.get('updated') or '')[:10],'created':str(n.get('created') or '')[:10],
                      'lead':' '.join(plain(summary).split()),'body':_split_lead(n.get('body') or '',summary),
                      'out':out.get(n['id'],[]),'backlinks':back.get(n['id'],[]),
                      'project':project,'section':section_of(n,files),'folder':None,'filename':''})
    by_id={n['id']:n for n in notes};used={n['project'] for n in notes}
    projects=[p for p in projects if p['slug'] in used]+([dict(GENERAL,slug=general)] if general in used else [])

    # The code tree: one folder per path prefix and project; a note sits in the folder of its first file.
    folders=[];index={}
    def folder(project,segs):
        key=(project,tuple(segs))
        if key not in index:
            node={'id':f'c-{len(folders)+1}','project':project,'name':segs[-1],'path':'/'.join(segs),'parent':None,'folders':[],'notes':[]}
            folders.append(node);index[key]=node
            if len(segs)>1:parent=folder(project,segs[:-1]);node['parent']=parent['id'];parent['folders'].append(node['id'])
        return index[key]
    for n in notes:
        if n['id'] not in where:continue
        shown,owner,is_dir=where[n['id']];segs=[s for s in shown.split('/') if s]
        if not segs:continue
        if owner is None:segs=[OTHER]+segs[-1:]
        is_file=not is_dir and len(segs)>1
        node=folder(n['project'],segs[:-1] if is_file else segs)
        node['notes'].append(n['id']);n['folder']=node['id'];n['filename']=segs[-1] if is_file else ''
    live={f['id']:f for f in folders}
    # Collapse chains of folders that hold one subfolder and no notes (src-tauri/src is one entry).
    for f in folders:
        if f['id'] not in live:continue
        while len(f['folders'])==1 and not f['notes']:
            child=live.pop(f['folders'][0])
            f['name']+='/'+child['name'];f['path']=child['path'];f['folders']=child['folders'];f['notes']=child['notes']
            for c in f['folders']:live[c]['parent']=f['id']
            for i in f['notes']:by_id[i]['folder']=f['id']
    folders=[f for f in folders if f['id'] in live]
    # Final ids hash the project and the shown path, so a folder keeps its address when other notes are added or a chain
    # stops collapsing (Hivemind keeps the route across refreshes).
    rename={f['id']:'c-'+hashlib.sha1(f"{f['project']}/{f['path']}".encode()).hexdigest()[:10] for f in folders}
    for f in folders:
        f['id']=rename[f['id']];f['parent']=rename.get(f['parent']);f['folders']=[rename[c] for c in f['folders']]
    for n in notes:
        if n['folder']:n['folder']=rename[n['folder']]
    live={f['id']:f for f in folders}
    def total(f):return len(f['notes'])+sum(total(live[c]) for c in f['folders'])
    for f in folders:
        f['count']=total(f)
        f['folders'].sort(key=lambda c:live[c]['name'].lower())
        f['notes'].sort(key=lambda i:(by_id[i]['filename'] or by_id[i]['title']).lower())

    for p in projects:
        mine=[n for n in notes if n['project']==p['slug']];sections={}
        for key,_,_ in SECTIONS:
            if key=='code':
                roots=sorted((f['id'] for f in folders if f['project']==p['slug'] and not f['parent']),key=lambda i:live[i]['name'].lower())
                if roots:sections['code']={'folders':roots,'count':sum(live[r]['count'] for r in roots)}
                continue
            ids=[n['id'] for n in mine if n['section']==key]
            if not ids:continue
            if key in CHRONOLOGICAL:ids.sort(key=lambda i:by_id[i]['updated'],reverse=True)
            else:ids.sort(key=lambda i:(by_id[i]['type'] not in ('brief','map'),by_id[i]['title'].lower()))
            sections[key]={'notes':ids,'count':len(ids)}
        p.pop('repo_paths',None);p.pop('tag',None)
        p.update(sections=sections,count=len(mine),updated=max((n['updated'] for n in mine),default=''),
                 briefs=[n['id'] for n in mine if n['type']=='brief'],maps=[n['id'] for n in mine if n['type']=='map'])
        if p['charter'] not in by_id:p['charter']=None
    projects.sort(key=lambda p:(p['slug']==general,-p['count']))
    return {'domain':domain,'generated':datetime.now(timezone.utc).strftime('%Y-%m-%d'),'renderer':WIKI_RENDERER,'archived':bool(archived),
            'notes':notes,'projects':projects,'folders':folders,'sections':[{'key':k,'label':l,'blurb':b} for k,l,b in SECTIONS],'external':external}

# ---- the page ----

def _tokens(spec):return ''.join(f'--{k}:{v};' for k,v in (p.split('=') for p in spec.split()))
# Hivemind's Emerald Enterprise and Emerald Enterprise Light themes (agentic-hivemind-app/src/App.css), plus the note-type
# colours of its .type-* classes as --t-<type>. Both sets carry the same tokens.
DARK=_tokens('bg=#002d2a bg-alt=#0c3835 bg-hover=#194441 fg=#f5f5f5 muted=#a7b9b7 border=#1b4340 accent=#7ecf1c accent-soft=#23462b '
             'error=#ff8f8f mark=#5a4a14 bg-sidebar=#002420 bg-topbar=#002926 bg-input=#002522 bg-code=#001f1c bg-active=#214846 '
             'border-soft=#103532 border-strong=#426461 warn-fg=#f7d65e warn-bg=#3a3218 warn-border=#a88b2a fg-soft=#c8d2d2 fg-meta=#c8d2d2 '
             'switcher-bg=#0a3431 switcher-border=#214846 nav-on-end=#214846 nav-on-marker=#7ecf1c row-on=#173f3c badge-bg=#123d3a '
             'chip-bg=#0c3835 chip-ok-bg=#1b3a1c chip-ok-border=#4f8a1a chip-ok-fg=#b1ee2c selected=#7ecf1c scrollbar=#426461 '
             'overlay=rgba(0,18,16,.65) '
             't-knowledge=#94a3b8 t-decision=#7fb0ff t-preference=#c58bff t-context=#94a3b8 t-status=#14b8a6 t-bug=#ff7a7a t-plan=#4ade80 '
             't-risk=#f59e0b t-research=#22d3ee t-policy=#eab308 t-map=#fb923c t-runbook=#f472b6 t-brief=#62d6a2')+'color-scheme:dark;'
LIGHT=_tokens('bg=#f5f5f5 bg-alt=#ffffff bg-hover=#e9ecec fg=#002d2a muted=#426461 border=#c8d2d2 accent=#3b6a12 accent-soft=#e3f2cf '
              'error=#b3261e mark=#fbe7a1 bg-sidebar=#002d2a bg-topbar=#ffffff bg-input=#ffffff bg-code=#e9ecec bg-active=#dfe8e6 '
              'border-soft=#dde4e4 border-strong=#5e7f7c warn-fg=#6b4a00 warn-bg=#fdf3d6 warn-border=#d9a92e fg-soft=#2b4a47 fg-meta=#426461 '
              'switcher-bg=#ffffff switcher-border=#c8d2d2 nav-on-end=#dfe8e6 nav-on-marker=#3b6a12 row-on=#e7efed badge-bg=#e9ecec '
              'chip-bg=#f0f3f3 chip-ok-bg=#e8f6d6 chip-ok-border=#7ecf1c chip-ok-fg=#2f5a0c selected=#3b6a12 scrollbar=#a7b9b7 '
              'overlay=rgba(0,45,42,.35) '
              't-knowledge=#475569 t-decision=#1d4ed8 t-preference=#7e22ce t-context=#475569 t-status=#0f766e t-bug=#b91c1c t-plan=#166534 '
              't-risk=#92400e t-research=#155e75 t-policy=#854d0e t-map=#9a3412 t-runbook=#be185d t-brief=#16794c')+'color-scheme:light;'
# The page tree stays dark in the light theme, as Hivemind's sidebar does.
SIDEBAR_LIGHT='--bg-sidebar:#002d2a;'
SIDEBAR_DARK='--bg-sidebar:#002420;'

CSS='''
:root{--font-sans:"Plus Jakarta Sans Variable","Segoe UI",system-ui,-apple-system,sans-serif;--font-mono:"JetBrains Mono Variable",ui-monospace,"Cascadia Code",Consolas,monospace;font-family:var(--font-sans);font-size:clamp(15px,1.15vw,18px);line-height:1.5}
*{box-sizing:border-box;scrollbar-width:thin;scrollbar-color:var(--scrollbar) transparent}
body{margin:0;background:var(--bg);color:var(--fg)}
body::before{content:"";position:fixed;inset:0 0 auto;height:env(safe-area-inset-top,0px);z-index:10;background:var(--bg-topbar)}
a{color:inherit;text-decoration:none}
button,input{font:inherit;color:inherit}
button{cursor:pointer;background:none;border:1px solid var(--border-strong);border-radius:.45rem;padding:.25rem .7rem;display:inline-flex;align-items:center;justify-content:center;gap:.4rem}
button:hover{background:var(--bg-hover)}
button:focus-visible,a:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
svg{flex:none}
h1,h2,h3{line-height:1.2}
.muted{color:var(--muted)}
.mono{font-family:var(--font-mono);font-size:.92em}
.icon-button{border:none;padding:.4rem;border-radius:.4rem}

.shell{display:grid;grid-template-columns:16.5rem minmax(0,1fr);min-height:100vh}
.sidebar{position:sticky;top:0;height:100vh;background:var(--bg-sidebar);color:var(--fg);border-right:1px solid var(--border-soft);display:flex;flex-direction:column;gap:.4rem;padding:1rem .6rem .9rem;overflow-y:auto;z-index:30}
.brand-block{display:flex;align-items:center;gap:.7rem;padding:0 .6rem 1rem;font-size:1.45rem;font-weight:700;letter-spacing:-.01em}
.brand-block .close{display:none;margin-left:auto}
.logo{width:2.9rem;height:2.9rem;flex:none;background:url("data:image/png;base64,@@LOGO@@") center/contain no-repeat}
.switcher{display:flex;align-items:center;gap:.75rem;padding:0 .8rem;height:2.75rem;border:1px solid var(--switcher-border);border-radius:.45rem;background:var(--switcher-bg);font-weight:600;font-size:1.05rem;margin:0 .1rem .6rem}
.switcher .muted{font-weight:500;margin-left:auto;font-size:.9rem}
.tree{display:flex;flex-direction:column;gap:.1rem}
.tree-label{text-transform:uppercase;letter-spacing:.06em;font-size:.72rem;font-weight:700;color:var(--muted);padding:.9rem .8rem .3rem}
.tree-row{display:flex;align-items:center;border-radius:.5rem;min-height:2.3rem}
.tree-row:hover{background:var(--bg-hover)}
.tree-row.on{background:linear-gradient(90deg,var(--bg-active),var(--nav-on-end));box-shadow:inset 3px 0 0 var(--nav-on-marker)}
.tree-row.on>a{font-weight:650}
.tree-row>a{flex:1;min-width:0;display:flex;align-items:center;gap:.65rem;padding:.3rem .6rem .3rem 0;color:var(--fg)}
.tree-row>a .grow{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tree-row .n{color:var(--muted);font-size:.82rem;font-variant-numeric:tabular-nums}
.tree-toggle{border:none;padding:0;width:1.7rem;height:2.3rem;flex:none;color:var(--muted);border-radius:.4rem}
.tree-toggle:hover{background:none;color:var(--fg)}
.tree-toggle svg{transition:transform .12s}
.tree-toggle[aria-expanded="true"] svg{transform:rotate(90deg)}
.tree-spacer{width:1.7rem;flex:none}
.tree-kids{display:flex;flex-direction:column;gap:.1rem;margin-left:1.05rem;padding-left:.35rem;border-left:1px solid var(--border)}
.tree-kids .tree-row{min-height:2rem;font-size:.94rem}
.tree-top>a{padding-left:.6rem}
.sidebar-foot{margin-top:auto;border-top:1px solid var(--border);padding:.8rem .6rem 0;font-size:.82rem;color:var(--muted)}
.nowrap{white-space:nowrap}
.scrim{display:none}

.main{min-width:0;display:flex;flex-direction:column}
.topbar{position:sticky;top:env(safe-area-inset-top,0px);z-index:20;display:flex;align-items:center;gap:1rem;min-height:3.55rem;padding:0 1rem 0 1.6rem;background:var(--bg-topbar);border-bottom:1px solid var(--border)}
.menu{display:none}
.crumbs{display:flex;align-items:center;gap:.5rem;font-size:1rem;min-width:0;white-space:nowrap;overflow:hidden;flex:1}
.crumb{color:var(--fg-soft);flex:none;max-width:16rem;overflow:hidden;text-overflow:ellipsis}
.crumb:hover{color:var(--fg);text-decoration:underline}
.crumb.here{color:var(--fg);font-weight:700;flex:0 1 auto;max-width:none}
.crumb-sep{color:var(--muted);display:inline-flex;flex:none}
.top-search{display:flex;align-items:center;gap:.6rem;width:min(21.5rem,36%);height:2.35rem;padding:0 .5rem 0 .8rem;border:1px solid var(--border-strong);border-radius:.45rem;background:var(--bg-input);flex:none;color:var(--muted)}
.top-search:focus-within{border-color:var(--accent)}
.top-search input{border:none;background:none;flex:1;min-width:0;padding:0;outline:none;color:var(--fg)}
.top-search input::-webkit-search-cancel-button{display:none}

.page{padding:1.6rem 1.8rem 4rem;display:flex;flex-direction:column;gap:1.4rem;min-width:0;max-width:82rem;width:100%}
.page-head{display:flex;align-items:flex-start;justify-content:space-between;gap:1.5rem}
.page-title{min-width:0}
.page-head h1{margin:0;font-size:2.55rem;font-weight:700;letter-spacing:-.015em;line-height:1.1;text-wrap:balance;overflow-wrap:anywhere}
.page-sub{margin:.5rem 0 0;font-size:1.12rem;color:var(--fg-meta);max-width:46rem}
.page-date{white-space:nowrap;padding-top:.5rem;color:var(--muted);font-size:.86rem}
.eyebrow{text-transform:uppercase;letter-spacing:.06em;color:var(--accent);font-size:.86rem;font-weight:650}
.path-line{display:inline-block;margin-top:.5rem;font-family:var(--font-mono);font-size:.86rem;background:var(--bg-code);border:1px solid var(--border-soft);border-radius:.35rem;padding:.05rem .45rem;color:var(--fg-soft);overflow-wrap:anywhere}
.banner{border:1px solid var(--warn-border);background:var(--warn-bg);color:var(--warn-fg);border-radius:.5rem;padding:.5rem .8rem}

.article{display:grid;grid-template-columns:minmax(0,1fr) 19rem;gap:1rem 2.4rem;align-items:start}
.article-body{grid-column:1;grid-row:1;min-width:0;display:flex;flex-direction:column;gap:1.6rem}
.article-rail{grid-column:2;grid-row:1;position:sticky;top:calc(3.55rem + 1.2rem);display:flex;flex-direction:column;gap:1rem;max-height:calc(100vh - 6rem);overflow-y:auto}
.lead{font-size:1.14rem;line-height:1.6;margin:0;max-width:46rem}
.infobox{border:1px solid var(--border-strong);border-radius:.6rem;background:var(--bg-alt);overflow:hidden}
.infobox-head{padding:.6rem .9rem;border-bottom:1px solid var(--border);font-weight:700;display:flex;align-items:center;gap:.5rem}
.infobox dl{margin:0;padding:.6rem .9rem .8rem;display:grid;gap:.45rem}
.infobox dl>div{display:grid;grid-template-columns:5.6rem minmax(0,1fr);gap:.6rem;font-size:.92rem}
.infobox dt{color:var(--muted)}
.infobox dd{margin:0;min-width:0;overflow-wrap:anywhere;display:flex;flex-direction:column;align-items:flex-start;gap:.15rem}
.infobox dd a{color:var(--accent)}
.infobox dd a:hover{text-decoration:underline}
.infobox .link-list{padding:.3rem .9rem .5rem}
.toc{border:1px solid var(--border);border-radius:.6rem;padding:.6rem .9rem .7rem}
.toc summary{font-weight:700;cursor:pointer;list-style:none;display:flex;align-items:center;justify-content:space-between}
.toc summary::-webkit-details-marker{display:none}
.toc ol{list-style:none;margin:.5rem 0 0;padding:0;display:flex;flex-direction:column;gap:.1rem}
.toc li a{display:flex;gap:.55rem;padding:.18rem .3rem;border-radius:.3rem;color:var(--fg-soft);font-size:.92rem}
.toc li a:hover{background:var(--bg-hover);color:var(--fg)}
.toc li.sub a{padding-left:1.6rem;font-size:.88rem}
.toc .num{color:var(--muted);font-variant-numeric:tabular-nums;min-width:1rem}
.toc .n{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums}
.wsec{display:flex;flex-direction:column;gap:.6rem;scroll-margin-top:5rem}
.wsec>h2{margin:0;font-size:1.5rem;padding-bottom:.4rem;border-bottom:1px solid var(--border);display:flex;align-items:baseline;gap:.6rem}
.wsec>h2 .n{font-size:1rem;color:var(--muted);font-weight:600}
.wsec>p{margin:0;color:var(--fg-meta)}
.arrow-link{display:inline-flex;align-items:center;gap:.4rem;color:var(--accent);font-weight:600;align-self:flex-start}
.arrow-link:hover{text-decoration:underline}

.portals{display:grid;grid-template-columns:repeat(auto-fill,minmax(19rem,1fr));gap:1rem}
.portal{display:flex;flex-direction:column;gap:.55rem;border:1px solid var(--border-strong);border-radius:.6rem;background:var(--bg-alt);padding:1rem 1.1rem}
.portal h3{margin:0;font-size:1.3rem}
.portal h3 a:hover{text-decoration:underline}
.portal p{margin:0;color:var(--fg-soft)}
.portal .meta{color:var(--muted);font-size:.86rem}
.portal-links{display:flex;flex-wrap:wrap;gap:.35rem;margin-top:.2rem}
.chip-link{font-size:.84rem;padding:.05rem .55rem;border-radius:999px;border:1px solid var(--border);color:var(--fg-soft);white-space:nowrap}
.chip-link:hover{border-color:var(--accent);color:var(--fg)}
.chip-link .n{color:var(--muted);margin-left:.3rem}

.lib-list{list-style:none;margin:0;padding:0;border-top:1px solid var(--border)}
.lib-row{display:flex;align-items:flex-start;gap:.9rem;border-bottom:1px solid var(--border);border-left:.22rem solid transparent;padding:.75rem .9rem .75rem .8rem;min-width:0}
.lib-row:hover{background:var(--row-on);border-left-color:var(--selected)}
.lib-row>svg{margin-top:.15rem;color:var(--muted)}
.lib-row-main{display:flex;flex-direction:column;gap:.15rem;min-width:0;flex:1}
.lib-row-title{font-size:1.06rem;font-weight:700;overflow-wrap:anywhere}
.lib-row-title .mono{font-weight:600}
.lib-row-meta{display:flex;flex-wrap:wrap;align-items:center;gap:.2rem .6rem;color:var(--fg-meta);font-size:.84rem}
.lib-row-meta>*+*::before{content:"\\2022";margin-right:.6rem;font-size:.7em;vertical-align:middle;color:var(--muted)}
.lib-row-text{color:var(--fg-soft);font-size:.95rem;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.arrow-row{display:flex;align-items:center;gap:.7rem;padding:.55rem .2rem;border-bottom:1px solid var(--border);min-width:0}
.arrow-row:last-child{border-bottom:none}
.arrow-row:hover .row-label{text-decoration:underline}
.row-label{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.arrow-row>svg{color:var(--muted)}
.arrow-row .n{color:var(--muted);font-size:.86rem;font-variant-numeric:tabular-nums}
.link-list{display:flex;flex-direction:column}
.badge{display:inline-block;font-size:.72rem;padding:0 .42rem;border-radius:.4rem;border:1px solid var(--border-strong);background:var(--badge-bg);text-transform:uppercase;letter-spacing:.03em;white-space:nowrap;font-weight:650;line-height:1.5}
@@TYPES@@
.status-chip{display:inline-flex;align-items:center;padding:0 .5rem;border-radius:.4rem;border:1px solid var(--chip-ok-border);background:var(--chip-ok-bg);color:var(--chip-ok-fg);font-size:.8rem;white-space:nowrap}
.status-chip.retired{border-color:var(--warn-border);background:var(--warn-bg);color:var(--warn-fg)}
.tags{display:flex;flex-wrap:wrap;gap:.3rem}
.tag{font-size:.8rem;padding:0 .45rem;border-radius:.3rem;border:1px solid var(--border);white-space:nowrap;color:var(--fg-soft)}
.letters{display:flex;flex-wrap:wrap;gap:.3rem}
.letters button{min-width:2.1rem;padding:.2rem .4rem;font-weight:650}

.markdown{font-size:1.03rem;line-height:1.68;overflow-wrap:anywhere;max-width:48rem}
.markdown>:first-child{margin-top:0}
.markdown h2{font-size:1.4rem;margin:1.8rem 0 .55rem;padding-bottom:.35rem;border-bottom:1px solid var(--border);scroll-margin-top:5rem}
.markdown h3{font-size:1.15rem;margin:1.4rem 0 .4rem;scroll-margin-top:5rem}
.markdown p{margin:0 0 .8rem}
.markdown ul,.markdown ol{margin:0 0 .8rem;padding-left:1.4rem}
.markdown li{margin:.25rem 0}
.markdown li>ul,.markdown li>ol{margin:.25rem 0}
.markdown li.task{list-style:none;margin-left:-1.4rem}
.markdown li.task input{margin:0 .45rem 0 0;vertical-align:-.1em;accent-color:var(--accent)}
.markdown li.task.done>span{color:var(--muted)}
.markdown code{background:var(--bg-code);padding:1px .25rem;border-radius:.3rem;font-family:var(--font-mono);font-size:.88em}
.markdown pre{background:var(--bg-code);border:1px solid var(--border);padding:.8rem;border-radius:.5rem;overflow:auto;font-size:.88rem;line-height:1.5}
.markdown pre code{background:none;padding:0}
.markdown blockquote{margin:.2rem 0 1rem;padding:.55rem .9rem;border:1px solid var(--warn-border);border-left:3px solid var(--warn-fg);background:var(--warn-bg);color:var(--warn-fg);border-radius:.45rem}
.markdown blockquote p{margin:0}
.markdown a{color:var(--accent)}
.markdown .tbl{overflow-x:auto;margin:0 0 .8rem}
.markdown table{border-collapse:collapse;font-size:.92rem}
.markdown th,.markdown td{border:1px solid var(--border);padding:.25rem .5rem;text-align:left;vertical-align:top}
.markdown th{background:var(--bg-alt)}
.wikilink{color:var(--accent);border-bottom:1px dashed var(--accent)}
.wikilink:hover{border-bottom-style:solid}
.wikilink.external{color:var(--fg-soft);border-bottom-color:var(--muted)}
.wikilink.external small{color:var(--muted);margin-left:.3rem;font-size:.75em;text-transform:uppercase;letter-spacing:.04em}
.wikilink.unresolved{color:var(--muted);border-bottom:1px dotted var(--muted)}
.see-also{display:grid;grid-template-columns:repeat(auto-fit,minmax(16rem,1fr));gap:1rem 2rem}
.see-also h3{margin:0 0 .3rem;font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.pager{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
.pager a{display:flex;flex-direction:column;gap:.15rem;border:1px solid var(--border);border-radius:.55rem;padding:.65rem .9rem;min-width:0}
.pager a:hover{border-color:var(--accent)}
.pager a.next{text-align:right;grid-column:2}
.pager small{color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-size:.72rem;font-weight:700}
.pager span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.empty{color:var(--muted)}

@media (max-width:1100px){
/* One column: the lead, then the infobox and contents, then the rest of the body. */
.article{display:flex;flex-direction:column;align-items:stretch;gap:1.4rem}
.article-body{display:contents}
.article-body>*{order:2;min-width:0}
.article-body>.lead-block{order:0}
.article-rail{order:1;position:static;max-height:none;overflow:visible;min-width:0}
}
@media (max-width:760px){
.shell{grid-template-columns:minmax(0,1fr)}
.sidebar{position:fixed;inset:0 auto 0 0;height:auto;width:min(19rem,88vw);transform:translateX(-105%);transition:transform .18s ease;padding-top:calc(1rem + env(safe-area-inset-top,0px));padding-left:max(.6rem,env(safe-area-inset-left,0px));box-shadow:0 0 2rem rgba(0,0,0,.35)}
.shell.open .sidebar{transform:none}
.shell.open .scrim{display:block;position:fixed;inset:0;background:var(--overlay);z-index:25}
.brand-block .close{display:inline-flex}
.topbar{flex-wrap:wrap;gap:.45rem .6rem;padding:.5rem max(16px,env(safe-area-inset-right,0px)) .6rem max(16px,env(safe-area-inset-left,0px))}
.menu{display:inline-flex;margin-left:-.4rem}
.crumbs{font-size:.92rem}
.crumbs .crumb:not(.here):not(.parent),.crumbs .crumb-sep:not(.last){display:none}
.crumb.parent{max-width:9rem}
.top-search{order:3;width:100%;flex-basis:100%}
.page{padding:1.1rem max(16px,env(safe-area-inset-right,0px)) 3rem max(16px,env(safe-area-inset-left,0px));gap:1.15rem}
.page-head{flex-direction:column;gap:.3rem}
.page-head h1{font-size:1.8rem}
.page-sub,.lead{font-size:1.02rem}
.page-date{padding-top:0}
.wsec>h2{font-size:1.25rem}
.lib-row{padding:.7rem .4rem .7rem .5rem;gap:.6rem}
.lib-row>svg{display:none}
.lib-row-title{font-size:1rem}
.markdown{font-size:.99rem}
.markdown pre{font-size:.8rem}
.pager{grid-template-columns:1fr}
.pager a.next{grid-column:1}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
'''

ICONS={
    'menu':'<line x1="4" x2="20" y1="12" y2="12"/><line x1="4" x2="20" y1="6" y2="6"/><line x1="4" x2="20" y1="18" y2="18"/>',
    'search':'<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    'home':'<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-5.999a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    'chev':'<path d="m9 18 6-6-6-6"/>',
    'sun':'<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
    'moon':'<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    'book':'<path d="M12 7v14"/><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"/>',
    'file':'<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
    'folder':'<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    'box':'<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
    'list':'<path d="M3 12h.01"/><path d="M3 18h.01"/><path d="M3 6h.01"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M8 6h13"/>',
    'clock':'<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    'x':'<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    'arrow':'<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
}
def _svg(name,size=20):return f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS[name]}</svg>'

JS=r'''
(function(){
'use strict';
var D=JSON.parse(document.getElementById('wiki-data').textContent);
var ICONS=@@ICONS@@;
var N={},R={},T={},P={},F={},S={},X={};
// External targets by the model's Python casefold key (Straße is strasse) and by lowercased title, which is how links are looked up here.
Object.keys(D.external).forEach(function(k){var x=D.external[k];X[k]=x;X[x.title.trim().toLowerCase()]=x});
D.notes.forEach(function(n){N[n.id]=n;R[n.ref]=n;var k=n.title.trim().toLowerCase();if(!T[k]||T[k].retired&&!n.retired)T[k]=n});
D.projects.forEach(function(p){P[p.slug]=p});
D.folders.forEach(function(f){F[f.id]=f});
D.sections.forEach(function(s){S[s.key]=s});
var CHRONO={decisions:1,plans:1,issues:1,research:1};
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function link(href,cls,text){var a=el('a',cls,text);a.href=href;return a}
// Fixed markup only (Lucide shapes); note text never goes through innerHTML.
function icon(name,size){var w=document.createElement('span');w.innerHTML='<svg viewBox="0 0 24 24" width="'+(size||20)+'" height="'+(size||20)+'" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+ICONS[name]+'</svg>';return w.firstChild}
function badge(t){return el('span','badge type-'+t,t)}
function plural(n,w){return n+' '+w+(n===1?'':'s')}
function cap(s){return s.charAt(0).toUpperCase()+s.slice(1)}
function nh(n){return '#n-'+n.ref}
function sh(p,k){return '#s-'+p+'-'+k}
var MONTHS=['January','February','March','April','May','June','July','August','September','October','November','December'];
function month(d){var m=/^(\d{4})-(\d{2})/.exec(d||'');return m?MONTHS[+m[2]-1]+' '+m[1]:'Undated'}
// A file note is titled "path: what it is"; its page shows the description as the title and the path above it.
function pathPrefix(n){var i=n.title.indexOf(': ');return n.files.length&&i>0&&n.title.slice(0,i).indexOf('/')>=0?n.title.slice(0,i):''}
function shortTitle(n){var p=pathPrefix(n);return p?n.title.slice(p.length+2):n.title}

// ---- Markdown: headings, paragraphs, nested lists with task boxes, blockquotes, fenced code, pipe tables ----
var INLINE=/(`[^`]+`|\*\*[^*]+\*\*|\*[^*\s][^*]*\*|\[\[[^\]]+\]\]|\[[^\]]+\]\([^)\s]*\))/;
function inline(parent,text){
  String(text).split(INLINE).forEach(function(s,i){
    if(!s)return;
    if(i%2===0){parent.appendChild(document.createTextNode(s));return}
    if(s.charAt(0)==='`')parent.appendChild(el('code',null,s.slice(1,-1)));
    else if(s.slice(0,2)==='**'){var b=el('strong');inline(b,s.slice(2,-2));parent.appendChild(b)}
    else if(s.slice(0,2)==='[[')parent.appendChild(wikilink(s.slice(2,-2)));
    else if(s.charAt(0)==='*'){var em=el('em');inline(em,s.slice(1,-1));parent.appendChild(em)}
    else{var m=/^\[([^\]]+)\]\(([^)\s]*)\)$/.exec(s);
      if(/^https?:\/\//i.test(m[2])){var a=link(m[2],null,m[1]);a.target='_blank';a.rel='noopener noreferrer';parent.appendChild(a)}
      else parent.appendChild(document.createTextNode(m[1]))}
  });
}
function wikilink(raw){
  var bar=raw.indexOf('|'),target=bar>=0?raw.slice(0,bar):raw,alias=bar>=0?raw.slice(bar+1):'';
  var hash=target.indexOf('#'),title=(hash>=0?target.slice(0,hash):target).trim(),label=alias||(hash>=0?title+' \u203a '+target.slice(hash+1):title);
  var n=T[title.toLowerCase()];
  if(n){var a=link(nh(n),'wikilink',label);a.title=n.type+': '+n.title;return a}
  var x=X[title.toLowerCase()];
  if(x){var s=el('span','wikilink external',label);s.appendChild(el('small',null,x.domain));s.title='In the '+x.domain+' domain, not part of this wiki';return s}
  var u=el('span','wikilink unresolved',label);u.title='No page has this title yet';return u;
}
function markdown(text,heads){
  var box=document.createDocumentFragment(),lines=String(text||'').replace(/\r/g,'').split('\n'),i=0;
  function isList(l){return /^(\s*)([-*+]|\d+\.)\s+/.test(l)}
  while(i<lines.length){
    var line=lines[i];
    if(!line.trim()){i++;continue}
    if(/^\s*```/.test(line)){var buf=[];i++;while(i<lines.length&&!/^\s*```/.test(lines[i]))buf.push(lines[i++]);i++;var pre=el('pre');pre.appendChild(el('code',null,buf.join('\n')));box.appendChild(pre);continue}
    var h=/^(#{1,6})\s+(.*)$/.exec(line);
    if(h){var lvl=h[1].length<=2?2:3,hx=el('h'+lvl);inline(hx,h[2]);if(heads){hx.id='h-'+heads.length;heads.push({level:lvl,text:hx.textContent,id:hx.id})}box.appendChild(hx);i++;continue}
    if(/^\s*>/.test(line)){var q=[];while(i<lines.length&&/^\s*>/.test(lines[i]))q.push(lines[i++].replace(/^\s*>\s?/,''));var bq=el('blockquote');bq.appendChild(markdown(q.join('\n')));box.appendChild(bq);continue}
    if(/^\s*\|/.test(line)&&i+1<lines.length&&/^\s*\|?\s*:?-{2,}/.test(lines[i+1])){
      var rows=[];while(i<lines.length&&/^\s*\|/.test(lines[i]))rows.push(lines[i++]);
      var cells=function(r){return r.trim().replace(/^\||\|$/g,'').split('|').map(function(c){return c.trim()})};
      var wrap=el('div','tbl'),tb=el('table'),thead=el('thead'),tr=el('tr');
      cells(rows[0]).forEach(function(c){var th=el('th');inline(th,c);tr.appendChild(th)});thead.appendChild(tr);tb.appendChild(thead);
      var tbody=el('tbody');rows.slice(2).forEach(function(r){var tr2=el('tr');cells(r).forEach(function(c){var td=el('td');inline(td,c);tr2.appendChild(td)});tbody.appendChild(tr2)});
      tb.appendChild(tbody);wrap.appendChild(tb);box.appendChild(wrap);continue}
    if(isList(line)){
      var stack=[];
      while(i<lines.length){
        var l=lines[i];
        if(!l.trim()){if(i+1<lines.length&&(isList(lines[i+1])||/^\s{2,}\S/.test(lines[i+1]))){i++;continue}break}
        var m=/^(\s*)([-*+]|\d+\.)\s+(.*)$/.exec(l);
        if(m){
          var ind=m[1].replace(/\t/g,'    ').length,ordered=/\d/.test(m[2]);
          while(stack.length&&stack[stack.length-1].ind>ind)stack.pop();
          if(!stack.length||stack[stack.length-1].ind<ind){var lst=el(ordered?'ol':'ul');if(stack.length)stack[stack.length-1].list.lastChild.appendChild(lst);else box.appendChild(lst);stack.push({ind:ind,list:lst})}
          var li=el('li'),t=/^\[([ xX])\]\s+(.*)$/.exec(m[3]);
          if(t){li.className='task'+(t[1]!==' '?' done':'');var cb=el('input');cb.type='checkbox';cb.disabled=true;cb.checked=t[1]!==' ';cb.setAttribute('aria-label',t[1]!==' '?'Done':'Open');li.appendChild(cb);var sp=el('span');inline(sp,t[2]);li.appendChild(sp)}
          else inline(li,m[3]);
          stack[stack.length-1].list.appendChild(li);i++;continue}
        if(/^\s+\S/.test(l)&&stack.length){var last=stack[stack.length-1].list.lastChild,tgt=last.querySelector(':scope>span')||last;tgt.appendChild(document.createTextNode(' '));inline(tgt,l.trim());i++;continue}
        break}
      continue}
    var p=el('p'),first=true;
    while(i<lines.length&&lines[i].trim()&&(first||!/^\s*(```|#{1,6}\s|>|\|)/.test(lines[i])&&!isList(lines[i]))){if(!first)p.appendChild(document.createTextNode(' '));inline(p,lines[i].trim());first=false;i++}
    box.appendChild(p);
  }
  return box;
}

// ---- building blocks ----
var page=document.getElementById('page'),q=document.getElementById('q'),crumbs=document.getElementById('crumbs');
function setCrumbs(parts){
  crumbs.textContent='';
  parts.forEach(function(p,i){
    var last=i===parts.length-1;
    if(i){var sep=el('span','crumb-sep'+(last?' last':''));sep.appendChild(icon('chev',15));crumbs.appendChild(sep)}
    var c=last?el('span','crumb here',p[0]):link(p[1],'crumb'+(i===parts.length-2?' parent':''),p[0]);
    if(last)c.setAttribute('aria-current','page');crumbs.appendChild(c);
  });
}
function chainOf(f){var chain=[];while(f){chain.unshift(f);f=F[f.parent]}return chain}
function isCode(n){return n.section==='code'&&n.folder&&F[n.folder]}
function noteCrumbs(n){
  var p=P[n.project],c=[['Wiki','#'],[p.name,'#p-'+p.slug],[S[n.section].label,sh(p.slug,n.section)]];
  if(isCode(n))chainOf(F[n.folder]).forEach(function(x){c.push([x.name,'#'+x.id])});
  c.push([isCode(n)&&n.filename?n.filename:shortTitle(n),nh(n)]);return c;
}
function head(eyebrow,title,sub,date){
  var h=el('header','page-head'),d=el('div','page-title');
  if(eyebrow)d.appendChild(el('div','eyebrow',eyebrow));d.appendChild(el('h1',null,title));if(sub){var s=el('p','page-sub');inline(s,sub);d.appendChild(s)}h.appendChild(d);
  if(date)h.appendChild(el('div','page-date',date));return h;
}
function row(n,opts){
  opts=opts||{};
  var li=el('li'),a=link(nh(n),'lib-row');a.appendChild(icon(n.filename?'file':'book',19));
  var m=el('div','lib-row-main'),t=el('span','lib-row-title');
  if(opts.file&&n.filename)t.appendChild(el('span','mono',n.filename));else t.textContent=shortTitle(n);
  m.appendChild(t);
  var meta=el('div','lib-row-meta');meta.appendChild(badge(n.type));
  if(n.retired)meta.appendChild(el('span',null,n.status));
  if(opts.where)meta.appendChild(el('span',null,P[n.project].name+' \u203a '+S[n.section].label));
  if(opts.file&&n.filename)meta.appendChild(el('span',null,shortTitle(n)));
  meta.appendChild(el('span',null,n.updated));m.appendChild(meta);
  if(n.lead&&!opts.noLead)m.appendChild(el('span','lib-row-text',n.lead));
  a.appendChild(m);li.appendChild(a);return li;
}
function rows(ns,opts){var ol=el('ol','lib-list');ns.forEach(function(n){ol.appendChild(row(n,opts))});if(!ns.length)ol.appendChild(el('li','empty','Nothing here yet.'));return ol}
function arrowRow(href,label,iconName,count,badgeType){
  var a=link(href,'arrow-row');if(badgeType)a.appendChild(badge(badgeType));else if(iconName)a.appendChild(icon(iconName,18));
  a.appendChild(el('span','row-label',label));if(count!=null)a.appendChild(el('span','n',String(count)));a.appendChild(icon('chev',17));return a;
}
function wsec(id,title,count,blurb){
  var s=el('section','wsec');s.id=id;var h2=el('h2');h2.appendChild(document.createTextNode(title));if(count!=null)h2.appendChild(el('span','n',String(count)));s.appendChild(h2);
  if(blurb)s.appendChild(el('p',null,blurb));return s;
}
function more(href,text){var a=link(href,'arrow-link',text);a.appendChild(icon('arrow',16));return a}
function infobox(title,iconName,pairs){
  var box=el('div','infobox'),h=el('div','infobox-head');h.appendChild(icon(iconName,18));h.appendChild(document.createTextNode(title));box.appendChild(h);
  var dl=el('dl');pairs.forEach(function(p){var v=p[1];if(v==null||v===''||(Array.isArray(v)&&!v.length))return;var d=el('div');d.appendChild(el('dt',null,p[0]));var dd=el('dd');
    (Array.isArray(v)?v:[v]).forEach(function(x){dd.appendChild(typeof x==='string'?el('span',null,x):x)});d.appendChild(dd);dl.appendChild(d)});
  if(dl.childNodes.length)box.appendChild(dl);return box;
}
function toc(items,open){
  var d=el('details','toc');d.open=open;d.appendChild(el('summary',null,'On this page'));var ol=el('ol');
  items.forEach(function(it){var li=el('li',it.sub?'sub':''),a=el('a');a.href='#';
    if(it.num!=null)a.appendChild(el('span','num',String(it.num)));a.appendChild(el('span',null,it.text));if(it.count!=null)a.appendChild(el('span','n',String(it.count)));
    a.addEventListener('click',function(e){e.preventDefault();var t=document.getElementById(it.id);if(t)t.scrollIntoView({behavior:'smooth',block:'start'})});li.appendChild(a);ol.appendChild(li)});
  d.appendChild(ol);return d;
}
function wide(){return window.innerWidth>1100}
function article(rail,body){var g=el('div','article'),r=el('aside','article-rail'),b=el('div','article-body');rail.forEach(function(x){r.appendChild(x)});body.forEach(function(x){b.appendChild(x)});g.appendChild(r);g.appendChild(b);return g}
function sectionNotes(p,key){var s=p.sections[key];return s&&s.notes?s.notes.map(function(id){return N[id]}):[]}
function byUpdated(a,b){return a.updated<b.updated?1:a.updated>b.updated?-1:0}

// ---- pages ----
function mainPage(){
  setCrumbs([['Wiki','#']]);
  page.appendChild(head(D.domain+' \u00b7 wiki',cap(D.domain)+' wiki','The working memory of the '+D.domain+' domain, organised by project. Open a project for its overview, decisions, guides and code reference, or search every page.','Generated '+D.generated));
  var sp=wsec('projects','Projects',D.projects.length),grid=el('div','portals');
  D.projects.forEach(function(p){
    var c=el('div','portal'),h=el('h3');h.appendChild(link('#p-'+p.slug,null,p.name));c.appendChild(h);
    if(p.description)c.appendChild(el('p',null,p.description));c.appendChild(el('span','meta',plural(p.count,'page')+(p.updated?' \u00b7 updated '+p.updated:'')));
    var ls=el('div','portal-links');D.sections.forEach(function(s){var x=p.sections[s.key];if(!x)return;var a=link(sh(p.slug,s.key),'chip-link',s.label);a.appendChild(el('span','n',String(x.count)));ls.appendChild(a)});
    c.appendChild(ls);grid.appendChild(c);
  });
  if(!D.projects.length)grid.appendChild(el('p','empty','This domain has no notes yet.'));
  sp.appendChild(grid);page.appendChild(sp);
  var rs=wsec('recent-pages','Recently updated',null,'The ten pages changed most recently, across every project.');
  rs.appendChild(rows(D.notes.slice().sort(byUpdated).slice(0,10),{where:true}));rs.appendChild(more('#recent','All recent changes'));
  var start=[];D.projects.forEach(function(p){p.briefs.concat(p.maps).forEach(function(id){start.push(N[id])})});
  var rail=[];
  if(start.length){var st=infobox('Start here','book',[]),ll=el('div','link-list');start.forEach(function(n){ll.appendChild(arrowRow(nh(n),n.title,null,null,n.type))});st.appendChild(ll);rail.push(st)}
  var br=infobox('Browse','list',[]),bl=el('div','link-list');
  bl.appendChild(arrowRow('#az','A to Z index','list',D.notes.length));bl.appendChild(arrowRow('#recent','Recent changes','clock'));br.appendChild(bl);rail.push(br);
  page.appendChild(article(rail,[rs]));
}
function projectPage(p){
  setCrumbs([['Wiki','#'],[p.name,'#p-'+p.slug]]);
  page.appendChild(head('Project',p.name,p.description,null));
  var body=[],items=[],num=0,leadNote=p.charter?N[p.charter]:(p.briefs[0]?N[p.briefs[0]]:null);
  if(leadNote&&leadNote.lead){var w=el('div','lead-block'),lead=el('p','lead');inline(lead,leadNote.lead);w.appendChild(lead);var m=more(nh(leadNote),'Read the '+(leadNote.type==='brief'?'project brief':'charter'));m.style.marginTop='.6rem';w.appendChild(m);body.push(w)}
  D.sections.forEach(function(s){
    var x=p.sections[s.key];if(!x)return;num++;
    var sec=wsec('sec-'+s.key,s.label,x.count,s.blurb);items.push({num:num,text:s.label,id:'sec-'+s.key,count:x.count});
    if(s.key==='code'){var ll=el('div','link-list');x.folders.forEach(function(id){ll.appendChild(arrowRow('#'+id,F[id].name,'folder',F[id].count))});sec.appendChild(ll)}
    else{var ns=sectionNotes(p,s.key),limit=s.key==='overview'||s.key==='architecture'?8:5;sec.appendChild(rows(ns.slice(0,limit)));
      if(ns.length>limit)sec.appendChild(more(sh(p.slug,s.key),'All '+ns.length+' '+s.label.toLowerCase()))}
    body.push(sec);
  });
  var to=function(ids){return ids.filter(function(id){return N[id]}).map(function(id){return link(nh(N[id]),null,N[id].title)})};
  var rail=[infobox(p.name,'box',[['Aliases',p.aliases.join(', ')],['Status',p.status],['Repositories',p.repos.map(function(r){return el('span','mono',r)})],
    ['Pages',String(p.count)],['Updated',p.updated],['Charter',p.charter?to([p.charter]):null],['Brief',to(p.briefs)],['Code maps',to(p.maps)]])];
  if(items.length)rail.push(toc(items,wide()));
  page.appendChild(article(rail,body));
}
function sectionPage(p,key){
  var s=S[key],x=p.sections[key];setCrumbs([['Wiki','#'],[p.name,'#p-'+p.slug],[s.label,sh(p.slug,key)]]);
  page.appendChild(head(p.name,s.label,s.blurb,null));
  if(key==='code'){
    x.folders.forEach(function(id){var f=F[id],sec=wsec(id+'-s',f.name,f.count),ll=el('div','link-list');
      f.folders.forEach(function(c){ll.appendChild(arrowRow('#'+c,F[c].name,'folder',F[c].count))});
      if(f.notes.length)ll.appendChild(arrowRow('#'+id,'Pages in '+f.name,'file',f.notes.length));sec.appendChild(ll);page.appendChild(sec)});
    return;
  }
  var ns=sectionNotes(p,key);
  if(CHRONO[key]){var groups=[],cur=null;ns.forEach(function(n){var m=month(n.updated);if(!cur||cur.m!==m){cur={m:m,ns:[]};groups.push(cur)}cur.ns.push(n)});
    groups.forEach(function(g,i){var sec=wsec('m-'+i,g.m,g.ns.length);sec.appendChild(rows(g.ns));page.appendChild(sec)})}
  else page.appendChild(rows(ns));
}
function folderPage(f){
  var p=P[f.project],c=[['Wiki','#'],[p.name,'#p-'+p.slug],[S.code.label,sh(p.slug,'code')]];
  chainOf(f).forEach(function(y){c.push([y.name,'#'+y.id])});setCrumbs(c);
  var hd=head(p.name+' \u00b7 code reference',f.name,null,null),pl=el('div');pl.appendChild(el('span','path-line',f.path+'/'));hd.firstChild.appendChild(pl);page.appendChild(hd);
  var all=f.notes.map(function(id){return N[id]}),docs=all.filter(function(n){return !n.filename}),files=all.filter(function(n){return n.filename});
  if(docs.length){var a=wsec('about','About this folder',null);a.appendChild(rows(docs));page.appendChild(a)}
  if(f.folders.length){var fs=wsec('folders','Folders',f.folders.length),ll=el('div','link-list');f.folders.forEach(function(id){ll.appendChild(arrowRow('#'+id,F[id].name,'folder',F[id].count))});fs.appendChild(ll);page.appendChild(fs)}
  if(files.length){var fl=wsec('files','Files',files.length);fl.appendChild(rows(files,{file:true}));page.appendChild(fl)}
}
function siblings(n){
  if(isCode(n))return F[n.folder].notes.map(function(id){return N[id]}).filter(function(m){return m.section==='code'});
  return sectionNotes(P[n.project],n.section);
}
function notePage(n){
  setCrumbs(noteCrumbs(n));
  var p=P[n.project],hd=el('header','page-head'),d=el('div','page-title');
  d.appendChild(el('div','eyebrow',p.name+' \u00b7 '+S[n.section].label));d.appendChild(el('h1',null,shortTitle(n)));
  var path=pathPrefix(n)||(n.section==='code'?n.files[0]||'':'');if(path){var pl=el('div');pl.appendChild(el('span','path-line',path));d.appendChild(pl)}
  hd.appendChild(d);page.appendChild(hd);
  if(n.retired)page.appendChild(el('div','banner','This page is '+n.status+'. It is kept for history and may be out of date.'));
  var heads=[],md=el('div','markdown');md.appendChild(markdown(n.body,heads));
  var body=[];
  if(n.lead){var lead=el('p','lead lead-block');inline(lead,n.lead);body.push(lead)}
  body.push(md);
  var sa=el('section','wsec');sa.id='see-also';sa.appendChild(el('h2',null,'See also'));var grid=el('div','see-also');
  [['Links to',n.out],['Linked from',n.backlinks]].forEach(function(pair){var b=el('div'),ll=el('div','link-list');b.appendChild(el('h3',null,pair[0]+' ('+pair[1].length+')'));
    pair[1].forEach(function(id){var m=N[id];if(m)ll.appendChild(arrowRow(nh(m),shortTitle(m),null,null,m.type))});if(!pair[1].length)ll.appendChild(el('span','empty','None'));b.appendChild(ll);grid.appendChild(b)});
  sa.appendChild(grid);body.push(sa);
  var sib=siblings(n),i=sib.indexOf(n);
  if(sib.length>1&&i>=0){var pg=el('nav','pager');pg.setAttribute('aria-label','Previous and next page');
    if(i>0){var pv=link(nh(sib[i-1]),'prev');pv.appendChild(el('small',null,'Previous'));pv.appendChild(el('span',null,shortTitle(sib[i-1])));pg.appendChild(pv)}
    if(i<sib.length-1){var nx=link(nh(sib[i+1]),'next');nx.appendChild(el('small',null,'Next'));nx.appendChild(el('span',null,shortTitle(sib[i+1])));pg.appendChild(nx)}
    body.push(pg)}
  var tags=null;if(n.tags.length){tags=el('div','tags');n.tags.forEach(function(t){tags.appendChild(el('span','tag',t))})}
  var rail=[infobox('Page details','file',[['Type',badge(n.type)],['Status',el('span','status-chip'+(n.retired?' retired':''),n.status)],['Project',link('#p-'+p.slug,null,p.name)],
    ['Section',link(sh(p.slug,n.section),null,S[n.section].label)],['Updated',n.updated],['Created',n.created],['Files',n.files.map(function(f){return el('span','mono',f)})],
    ['Tags',tags],['Ref',el('span','mono',D.domain+':'+n.ref)]])];
  if(heads.length){var items=heads.map(function(h){return {text:h.text,id:h.id,sub:h.level===3}});items.push({text:'See also',id:'see-also'});rail.push(toc(items,wide()))}
  page.appendChild(article(rail,body));
  document.title=shortTitle(n)+' \u00b7 '+cap(D.domain)+' wiki';
}
function azPage(){
  setCrumbs([['Wiki','#'],['A to Z index','#az']]);
  page.appendChild(head('Browse','A to Z index',plural(D.notes.length,'page')+' in alphabetical order.'));
  var groups={},order=[];
  D.notes.slice().sort(function(a,b){return shortTitle(a).localeCompare(shortTitle(b),undefined,{sensitivity:'base'})}).forEach(function(n){var c=shortTitle(n).charAt(0).toUpperCase();if(!/[A-Z]/.test(c))c='#';if(!groups[c]){groups[c]=[];order.push(c)}groups[c].push(n)});
  var bar=el('div','letters');order.forEach(function(c,i){var b=el('button',null,c);b.type='button';b.addEventListener('click',function(){document.getElementById('az-'+i).scrollIntoView({behavior:'smooth'})});bar.appendChild(b)});page.appendChild(bar);
  order.forEach(function(c,i){var s=wsec('az-'+i,c,groups[c].length);s.appendChild(rows(groups[c],{where:true,noLead:true}));page.appendChild(s)});
}
function recentPage(){
  setCrumbs([['Wiki','#'],['Recent changes','#recent']]);
  page.appendChild(head('Browse','Recent changes','The 80 most recently updated pages, by day.'));
  var cur=null,list=null;D.notes.slice().sort(byUpdated).slice(0,80).forEach(function(n){if(n.updated!==cur||!list){cur=n.updated;var sec=wsec('d-'+(cur||'none'),cur||'Undated',null);list=el('ol','lib-list');sec.appendChild(list);page.appendChild(sec)}list.appendChild(row(n,{where:true,noLead:true}))});
}
function searchPage(s){
  var k=s.toLowerCase(),hits=[];
  D.notes.forEach(function(n){var sc=n.title.toLowerCase().indexOf(k)>=0?4:n.tags.join(' ').toLowerCase().indexOf(k)>=0?3:n.lead.toLowerCase().indexOf(k)>=0?2:n.body.toLowerCase().indexOf(k)>=0?1:0;if(sc)hits.push([sc,n])});
  hits.sort(function(a,b){return b[0]-a[0]||byUpdated(a[1],b[1])});
  setCrumbs([['Wiki','#'],['Search','#']]);
  page.appendChild(head('Search','\u201c'+s+'\u201d',plural(hits.length,'page')+' match. Title matches come first.'));page.appendChild(rows(hits.map(function(h){return h[1]}),{where:true}));
}

// ---- the page tree ----
var open={},shell=document.getElementById('shell'),treeBox=document.getElementById('tree');
function treeRow(href,label,iconName,count,kids,key,top){
  var wrap=el('div'),r=el('div','tree-row'+(top?' tree-top':''));r.dataset.h=href.slice(1);
  if(kids){var b=el('button','tree-toggle');b.type='button';b.setAttribute('aria-expanded',open[key]?'true':'false');b.setAttribute('aria-label',(open[key]?'Collapse ':'Expand ')+label);b.appendChild(icon('chev',15));
    b.addEventListener('click',function(){open[key]=!open[key];drawTree()});r.appendChild(b)}else if(!top)r.appendChild(el('span','tree-spacer'));
  var a=link(href);if(iconName)a.appendChild(icon(iconName,18));a.appendChild(el('span','grow',label));if(count!=null)a.appendChild(el('span','n',String(count)));r.appendChild(a);wrap.appendChild(r);
  if(kids&&open[key]){var k=el('div','tree-kids');kids().forEach(function(x){k.appendChild(x)});wrap.appendChild(k)}
  return wrap;
}
function drawTree(){
  treeBox.textContent='';
  treeBox.appendChild(treeRow('#','Main page','home',null,null,null,true));
  if(D.projects.length)treeBox.appendChild(el('div','tree-label','Projects'));
  D.projects.forEach(function(p){
    treeBox.appendChild(treeRow('#p-'+p.slug,p.name,'box',null,function(){
      return D.sections.filter(function(s){return p.sections[s.key]}).map(function(s){
        var x=p.sections[s.key];
        if(s.key==='code')return treeRow(sh(p.slug,'code'),s.label,null,x.count,function(){return x.folders.map(function(id){return treeRow('#'+id,F[id].name,null,F[id].count)})},'code-'+p.slug);
        return treeRow(sh(p.slug,s.key),s.label,null,x.count);
      });
    },'p-'+p.slug));
  });
  treeBox.appendChild(el('div','tree-label','Browse'));
  treeBox.appendChild(treeRow('#az','A to Z index','list',null,null,null,true));
  treeBox.appendChild(treeRow('#recent','Recent changes','clock',null,null,null,true));
  mark();
}
function rootOf(f){while(f.parent&&F[f.parent])f=F[f.parent];return f}
// The page itself when the tree lists it, else its nearest listed ancestor.
function mark(){
  var h=location.hash.slice(1),n=h.indexOf('n-')===0?R[h.slice(2)]:null,keys=[h];
  if(n)keys.push(isCode(n)?rootOf(F[n.folder]).id:'s-'+n.project+'-'+n.section,'s-'+n.project+'-'+n.section,'p-'+n.project);
  if(F[h])keys.push(rootOf(F[h]).id,'s-'+F[h].project+'-code');
  treeBox.querySelectorAll('.tree-row.on').forEach(function(r){r.classList.remove('on')});
  for(var i=0;i<keys.length;i++){var r=treeBox.querySelector('.tree-row[data-h="'+CSS.escape(keys[i])+'"]');if(r){r.classList.add('on');break}}
}
function expand(){
  var h=location.hash.slice(1),proj=null,code=false,n=h.indexOf('n-')===0?R[h.slice(2)]:null;
  if(h.indexOf('p-')===0)proj=h.slice(2);
  else if(h.indexOf('s-')===0)D.projects.forEach(function(p){if(h.indexOf('s-'+p.slug+'-')===0){proj=p.slug;code=h==='s-'+p.slug+'-code'}});
  else if(n){proj=n.project;code=!!isCode(n)}
  else if(F[h]){proj=F[h].project;code=true}
  if(proj){open['p-'+proj]=true;if(code)open['code-'+proj]=true}
}

// ---- routing ----
function closeNav(){shell.classList.remove('open')}
function sectionRoute(h){var hit=null;D.projects.forEach(function(p){var pre='s-'+p.slug+'-';if(h.indexOf(pre)===0&&p.sections[h.slice(pre.length)])hit=[p,h.slice(pre.length)]});return hit}
function route(){
  var h=location.hash.slice(1),sec;page.textContent='';document.title=cap(D.domain)+' wiki';
  if(q.value.trim())searchPage(q.value.trim());
  else if(h.indexOf('n-')===0&&R[h.slice(2)])notePage(R[h.slice(2)]);
  else if(h.indexOf('p-')===0&&P[h.slice(2)])projectPage(P[h.slice(2)]);
  else if(h.indexOf('s-')===0&&(sec=sectionRoute(h)))sectionPage(sec[0],sec[1]);
  else if(F[h])folderPage(F[h]);
  else if(h==='az')azPage();
  else if(h==='recent')recentPage();
  else mainPage();
  expand();drawTree();closeNav();window.scrollTo(0,0);
}
var timer;q.addEventListener('input',function(){clearTimeout(timer);timer=setTimeout(route,150)});
document.addEventListener('click',function(e){
  var a=e.target.closest('a[href^="#"]');if(!a||a.closest('.toc'))return;
  var target=a.getAttribute('href').slice(1),searching=q.value;q.value='';
  if(target===location.hash.slice(1)||searching){e.preventDefault();if(target===location.hash.slice(1))route();else location.hash=target}
});
document.getElementById('menu').addEventListener('click',function(){shell.classList.add('open')});
document.getElementById('close').addEventListener('click',closeNav);
document.getElementById('scrim').addEventListener('click',closeNav);
document.addEventListener('keydown',function(e){if(e.key==='Escape')closeNav();if(e.key==='/'&&document.activeElement!==q){e.preventDefault();q.focus()}});
var themeBtn=document.getElementById('theme');
function current(){return document.documentElement.getAttribute('data-theme')||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light')}
function paint(){var t=current();themeBtn.textContent='';themeBtn.appendChild(icon(t==='dark'?'sun':'moon',20));themeBtn.setAttribute('aria-label',t==='dark'?'Use the light theme':'Use the dark theme');themeBtn.title=themeBtn.getAttribute('aria-label')}
themeBtn.addEventListener('click',function(){var t=current()==='dark'?'light':'dark';document.documentElement.setAttribute('data-theme',t);try{localStorage.setItem('wiki-theme',t)}catch(e){}paint()});
try{var saved=localStorage.getItem('wiki-theme');if(saved==='dark'||saved==='light')document.documentElement.setAttribute('data-theme',saved)}catch(e){}
paint();window.addEventListener('hashchange',function(){q.value='';route()});route();
})();
'''

def _css():
    from .views import _asset
    face=('@font-face{font-family:"Plus Jakarta Sans Variable";font-style:normal;font-display:swap;font-weight:200 800;'
          'src:url(data:font/woff2;base64,'+_asset('plus-jakarta-sans-latin-wght-normal.woff2.b64')+') format("woff2")}')
    theme=(':root{'+LIGHT+'}.sidebar{'+DARK+SIDEBAR_LIGHT+'}'
           '@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){'+DARK+'}:root:not([data-theme="light"]) .sidebar{'+SIDEBAR_DARK+'}}'
           ':root[data-theme="dark"]{'+DARK+'}:root[data-theme="dark"] .sidebar{'+SIDEBAR_DARK+'}')
    types=''.join(f'.type-{t}{{color:var(--t-{t})}}' for t in NOTE_TYPES)
    return face+theme+CSS.replace('@@LOGO@@',_asset('hivemind-logo.png.b64')).replace('@@TYPES@@',types)

def render_html(model,document=True):
    """One self-contained page for the model. document=False leaves out the doctype, html, head and body wrapper (a
    claude.ai artifact adds its own); the content is otherwise the same."""
    esc=lambda v:html.escape(str(v),quote=True)
    data=json.dumps(model,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    domain=model['domain'];title=f'{domain[:1].upper()}{domain[1:]} memory wiki'
    noscript=''.join(f'<li>{esc(n["title"])}: {esc(n["lead"])}</li>' for n in model['notes'])
    content=(f'<title>{esc(title)}</title><meta name="claude:viewport-fit-sides" content="cover"><style>{_css()}</style>'
             '<div class="shell" id="shell"><aside class="sidebar" id="sidebar" aria-label="Wiki pages">'
             f'<div class="brand-block"><span class="logo" aria-hidden="true"></span><span>Hivemind</span><button type="button" class="icon-button close" id="close" aria-label="Close navigation">{_svg("x")}</button></div>'
             f'<div class="switcher">{_svg("book")}<span>{esc(domain)}</span><span class="muted">wiki</span></div>'
             '<nav class="tree" id="tree"></nav>'
             f'<div class="sidebar-foot">{len(model["notes"])} pages. Generated <span class="nowrap">{esc(model["generated"])}</span> from Living Memory.</div></aside>'
             '<div class="scrim" id="scrim"></div><div class="main"><header class="topbar">'
             f'<button type="button" class="icon-button menu" id="menu" aria-label="Open navigation">{_svg("menu")}</button>'
             '<nav class="crumbs" id="crumbs" aria-label="Breadcrumb"></nav>'
             f'<label class="top-search">{_svg("search",18)}<input id="q" type="search" placeholder="Search the wiki" aria-label="Search the wiki" autocomplete="off"></label>'
             '<button type="button" class="icon-button" id="theme" aria-label="Switch theme"></button></header>'
             '<main class="page" id="page"></main></div></div>'
             f'<noscript><ul>{noscript}</ul></noscript>'
             f'<script type="application/json" id="wiki-data">{data}</script><script>{JS.replace("@@ICONS@@",json.dumps(ICONS))}</script>')
    if not document:return content
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '</head><body>'+content+'</body></html>\n')

def default_path(root,domain):return Path(root)/'.views'/domain/'wiki.html'

def export(domain,root,archived=False,out=None,fmt='html'):
    """Write the wiki of one domain: html to `out` or <store>/.views/<domain>/wiki.html, or the json model to `out`.
    --out inside the store is refused unless it is under .views (it would overwrite canonical files unjournaled)."""
    from .views import _out_path
    root=Path(root).resolve();model=wiki_model(domain,root,archived)
    path=_out_path(root,out) if out is not None else default_path(root,domain)
    text=json.dumps(model,ensure_ascii=False,indent=1)+'\n' if fmt=='json' else render_html(model)
    atomic_write(path,text)
    return {'ok':True,'domain':domain,'path':str(path),'pages':len(model['notes']),'format':fmt}
