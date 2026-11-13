"""Note views: a note shown as a self-contained HTML dashboard (registry views.json, derived output under .views/).

A view is a registry entry naming one note. The note stays the only source of truth: the page is rendered from
view_model(note), written to <store>/.views/<domain>/<slug>-<id8>.html and re-rendered by reconcile() (through
refresh) whenever the note's revision differs from the one cached in .cache/views.json. .views/ and that cache are
derived: they are left out of snapshots and relocation, and `views render --all` rebuilds them.

The page needs no network: styles, script, font and logo are inline, and the data is a JSON block that the script
turns into elements with textContent only. Note text is agent-written, so it is never put into markup unescaped."""
from __future__ import annotations
import base64,html,json,re,textwrap
from pathlib import Path
from .common import atomic_write
from .repository import read_json,canonical_json,store_lock,transaction,now

# Bump when the page changes, so views rendered by an older version count as stale.
RENDERER=1
TARGETS=('local','artifact')
RELOAD_SECONDS=15
URL=re.compile(r'https?://[^\s<>"\']+')

def _path(root):return Path(root)/'views.json'
def _cache_path(root):return Path(root)/'.cache'/'views.json'

def load(root):
    """The registry as {'version':1,'views':[...]}; a missing file is an empty registry."""
    data=read_json(_path(root),{'version':1,'views':[]});rows=data.get('views') if isinstance(data,dict) else None
    if not isinstance(rows,list) or any(not isinstance(v,dict) or not isinstance(v.get('note'),str) or not isinstance(v.get('domain'),str) for v in rows) or len({v['note'] for v in rows})!=len(rows):
        raise ValueError(f'Invalid or unreadable configuration preserved: {_path(root)}')
    return data

def _registry(entries):return canonical_json({'version':1,'views':sorted(entries,key=lambda v:(v['domain'],v['note']))})+'\n'

def _cache(root):
    """note ID -> {'revision', 'renderer'} of the local pages as last rendered; {} when missing or damaged (derived data)."""
    try:data=json.loads(_cache_path(root).read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}
    return data if isinstance(data,dict) else {}

def _write_cache(root,data):atomic_write(_cache_path(root),json.dumps(data,sort_keys=True,separators=(',',':')))

def view_path(root,entry,target='local'):
    """<store>/.views/<domain>/<slug>-<id8>.html (the artifact copy: <slug>-<id8>.artifact.html). The slug comes from the view's own title, so it stays put when the note is renamed."""
    from .notes import slug
    name=f"{slug(entry.get('title') or entry['note'])}-{entry['note'][:8]}"
    return Path(root)/'.views'/entry['domain']/(name+('.artifact' if target=='artifact' else '')+'.html')

# ---- the view model ----

_BRACKET=re.compile(r'\[([^\[\]]*)\](?!\()\s*')
_ID=re.compile(r'[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*')
_BOLD_LABEL=re.compile(r'^\s*(?:[-*+]\s+)?\*\*[^*]+?\*\*\s*:?\s*')

def parse_item(text):
    """(groups, id, text) from an action item's text: leading [bracket groups] (each split on commas), then an ID
    such as CUR-1 or HM-P4 (capitals, digits and hyphens, with a hyphen or a digit), then the rest."""
    groups=[];rest=(text or '').strip()
    while True:
        found=_BRACKET.match(rest)
        if not found:break
        tags=[t.strip() for t in found[1].split(',') if t.strip()]
        if tags:groups.append(tags)
        rest=rest[found.end():]
    first=rest.split(None,1);token=first[0].rstrip(':') if first else ''
    if _ID.fullmatch(token) and (('-' in token) or any(c.isdigit() for c in token)):
        return groups,token,(first[1].strip() if len(first)>1 else '')
    return groups,None,rest

def _blocks(text):
    """Lines of a Details section grouped as bullets: a line that starts at the margin, then its indented continuation lines."""
    blocks=[]
    for line in text.split('\n'):
        if blocks and blocks[-1][0].strip() and line.strip() and line[:1] in (' ','\t'):blocks[-1].append(line)
        else:blocks.append([line])
    return blocks

def _detail_text(block):
    first=_BOLD_LABEL.sub('',block[0],count=1).strip()
    rest=textwrap.dedent('\n'.join(block[1:])).strip()
    return (first+'\n'+rest).strip() if rest else first

def _successor(note,root):
    """Title (else a short ref) of the note that replaced this one, when it records one."""
    ref=note.get('superseded_by')
    if not ref:return None
    ref=str(ref)
    try:
        from .notes import find_note
        return find_note(ref,note.get('domain'),root)[1]['title']
    except (ValueError,OSError,TypeError,KeyError):return ref[:8]

def view_model(note,root=None):
    """The JSON-safe model of any note. Items are the Action items, numbered as `check --index` numbers them;
    sections are everything else, with the Details bullets that belong to an item moved onto that item."""
    from .note_format import action_items,detail_label,parts,plain
    body=note.get('body') or '';found=parts(body)
    summary=next((text for key,_,text in found if key=='summary'),'')
    important=next((text for key,_,text in found if key=='important' and text.strip()),None)
    details=next((text for key,_,text in found if key=='details'),'')
    blocks=_blocks(details) if details.strip() else []
    items=[]
    for number,(done,raw) in enumerate(action_items(body),1):
        groups,ident,text=parse_item(raw)
        items.append({'index':number,'done':bool(done),'text':text,'tags':[t for g in groups for t in g],'id':ident,'detail':None})
    for item in items:
        if not item['id']:continue
        label=item['id'].casefold()
        for block in blocks:
            if block[0][:1] not in (' ','\t') and detail_label(block[0])==label:
                item['detail']=_detail_text(block);blocks.remove(block);break
    sections=[]
    for key,heading,text in found:
        if key in ('summary','important','actions'):continue
        if key=='details':text='\n'.join(line for block in blocks for line in block).strip();heading='Details'
        elif key=='links':heading='Links'
        if heading and text.strip():sections.append({'heading':heading,'text':text.strip()})
    done=sum(1 for i in items if i['done'])
    return {'id':str(note.get('id') or ''),'domain':str(note.get('domain') or ''),'title':str(note.get('title') or ''),'type':str(note.get('type') or ''),
            'status':str(note.get('status') or 'active'),'updated':str(note.get('updated') or ''),'revision':str(note.get('revision') or ''),
            'summary':' '.join(plain(summary).split()),'important':important.strip() if important else None,'items':items,'sections':sections,
            'counts':{'open':len(items)-done,'done':done,'total':len(items)},'superseded_by':_successor(note,root) if note.get('superseded_by') else None}

def missing_model(entry):
    """The model of a view whose note is gone."""
    return {'id':entry['note'],'domain':entry['domain'],'title':entry.get('title') or 'Note not found','type':'','status':'missing','updated':'','revision':'',
            'summary':'','important':None,'items':[],'sections':[],'counts':{'open':0,'done':0,'total':0},'superseded_by':None}

# ---- the page ----

DARK='''color-scheme:dark;--bg:#1b0626;--bg-alt:#251332;--bg-hover:#341848;--fg:#f6f1fa;--muted:#b7a6c6;--border:#3a224c;--accent:#c884f4;--accent-soft:#442558;--error:#ff7a90;--mark:#5c4712;--bg-sidebar:#190621;--bg-topbar:#1b0727;--bg-input:#200c2c;--bg-code:#170d24;--bg-active:#4c2d64;--border-soft:#291738;--border-strong:#6d5188;--border-lilac:#d2aee7;--accent-strong:#8048b0;--primary:#019871;--teal:#01dca4;--success:#04cf8a;--success-soft:#03473e;--warn:#f7ae3a;--warn-fg:#fde329;--warn-bg:#44281c;--warn-border:#b99a2a;--sub:#e9def2;--chip-bg:#391e48;--p3-bg:#2b2136;--ok-bg:#192b30;--ok-border:#165848;--ok-fg:#7df3c6;--next-bg:#3a1f55;--next-border:#8a5bc0;--next-fg:#e0c2fb;'''
# Light counterpart for artifacts: the Hivemind accents (purple #8048b0, teal #018a68) on pale lilac steps, text at 4.5:1 or better.
LIGHT='''color-scheme:light;--bg:#faf7fd;--bg-alt:#ffffff;--bg-hover:#f1e9f8;--fg:#241530;--muted:#675777;--border:#e3d8ec;--accent:#8048b0;--accent-soft:#eadcf5;--error:#bf2447;--mark:#fbe9a8;--bg-sidebar:#f3edf8;--bg-topbar:#faf7fd;--bg-input:#ffffff;--bg-code:#f3edf8;--bg-active:#e2d0f0;--border-soft:#eee6f4;--border-strong:#a68dbb;--border-lilac:#8048b0;--accent-strong:#8048b0;--primary:#018a68;--teal:#018a68;--success:#018a68;--success-soft:#d8f1e9;--warn:#a86a00;--warn-fg:#6b4700;--warn-bg:#fdf1d6;--warn-border:#d8a733;--sub:#3d2c4d;--chip-bg:#efe4f7;--p3-bg:#f0eaf4;--ok-bg:#dff3ec;--ok-border:#7cc9b0;--ok-fg:#0a5d46;--next-bg:#eadcf5;--next-border:#a982cf;--next-fg:#5a2d85;'''

CSS='''
:root{--font-sans:"Plus Jakarta Sans Variable","Segoe UI",system-ui,-apple-system,sans-serif;--font-mono:"JetBrains Mono Variable",ui-monospace,"Cascadia Code",Consolas,monospace;font-family:var(--font-sans);font-size:clamp(14px,1.2vw,18px);line-height:1.5;color:var(--fg);background:var(--bg)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg)}
button,input,select{font:inherit;color:inherit}
button{cursor:pointer;background:none;border:1px solid var(--border-strong);border-radius:.45rem;padding:.25rem .7rem;display:inline-flex;align-items:center;justify-content:center;gap:.4rem}
button:hover{background:var(--bg-hover)}
button:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
input,select{background:var(--bg-input);border:1px solid var(--border-strong);border-radius:.45rem;padding:.35rem .55rem}
input:focus,select:focus{outline:2px solid var(--accent);outline-offset:-1px}
h1,h2{line-height:1.2}
code{font-family:var(--font-mono);font-size:.9em;background:var(--bg-code);border:1px solid var(--border-soft);border-radius:.3rem;padding:0 .3rem;overflow-wrap:anywhere}
pre{font-family:var(--font-mono);font-size:.86rem;background:var(--bg-code);border:1px solid var(--border-soft);border-radius:.45rem;padding:.5rem .7rem;overflow-x:auto;margin:.4rem 0}
pre code{background:none;border:none;padding:0}
.muted{color:var(--muted)}
.small{font-size:.86rem}
.page{max-width:62rem;margin:0 auto;padding:1.5rem 16px 3rem;display:flex;flex-direction:column;gap:1.3rem;min-width:0}
.page>*{flex-shrink:0;min-width:0}
.brand{display:flex;align-items:center;gap:.7rem;font-size:1.45rem;font-weight:700;letter-spacing:-.01em}
.brand-logo{display:block;width:2.6rem;height:2.6rem;background:url("data:image/png;base64,@@LOGO@@") center/contain no-repeat}
.page-head{display:flex;align-items:flex-start;justify-content:space-between;gap:1.5rem}
.page-title{min-width:0}
.page-head h1{margin:0;font-size:2.55rem;font-weight:700;letter-spacing:-.015em;line-height:1.1;overflow-wrap:anywhere}
.page-sub{margin:.4rem 0 0;font-size:1.1rem;color:var(--sub);overflow-wrap:anywhere}
.page-date{white-space:nowrap;padding-top:.5rem}
.eyebrow{text-transform:uppercase;letter-spacing:.06em;color:var(--accent);font-size:.95rem;font-weight:600;overflow-wrap:anywhere}
.banner,.callout{border:1px solid var(--warn-border);background:var(--warn-bg);color:var(--warn-fg);border-radius:.5rem;padding:.5rem .8rem;overflow-wrap:anywhere}
.callout{border-color:var(--border-strong);background:var(--bg-alt);color:var(--fg);border-left:.22rem solid var(--accent)}
.callout p{margin:0}
.progress-line{display:flex;align-items:center;gap:.7rem}
.progress{display:block;flex:1;min-width:4rem;height:.4rem;border-radius:999px;background:var(--bg-hover);overflow:hidden}
.progress>span{display:block;height:100%;background:var(--teal);border-radius:999px}
.lib-bar{display:flex;align-items:flex-end;flex-wrap:wrap;gap:.4rem 1rem;border-bottom:1px solid var(--border)}
.lib-bar .line-tabs{border-bottom:none;flex:1}
.line-tabs{display:flex;gap:.3rem;border-bottom:1px solid var(--border)}
.line-tabs button{border:none;border-bottom:.18rem solid transparent;border-radius:0;padding:.55rem 1rem;font-size:1.05rem;margin-bottom:-1px}
.line-tabs button:hover{background:none;border-bottom-color:var(--border-strong)}
.line-tabs .on{border-bottom-color:var(--accent);font-weight:700}
.tab-count{margin-left:.5rem;font-weight:700}
.lib-search{display:flex;align-items:center;gap:.5rem;padding-bottom:.4rem}
.lib-search input{width:14rem;max-width:100%}
.lib-bar select{margin-bottom:.4rem}
.chips{display:flex;flex-wrap:wrap;gap:.4rem}
.chip{font-size:.86rem;padding:1px .55rem;border-radius:999px}
.chip.on{background:var(--accent-soft);border-color:var(--accent)}
.chip-gap{width:1px;background:var(--border);margin:0 .2rem}
span.chip{display:inline-block;border:1px solid var(--border-strong);vertical-align:baseline}
.work-list{display:flex;flex-direction:column;gap:.6rem}
.work-group{border:1px solid var(--border);border-left-width:1px;border-radius:.5rem;padding:.5rem .8rem;background:var(--bg-alt);display:flex;flex-direction:column;gap:.25rem}
.work-group.has-detail:hover{background:var(--bg-hover)}
.work-group.expanded{border-left:.22rem solid var(--teal)}
.work-head{display:flex;align-items:flex-start;gap:.7rem;width:100%;text-align:left;justify-content:flex-start}
button.work-head{border:none;border-radius:.3rem;padding:0;background:none}
button.work-head:hover{background:none}
.work-group.done .work-main{opacity:.6}
.work-mark{flex:none;display:inline-flex;margin-top:.15rem;color:var(--muted)}
.work-group.done .work-mark{color:var(--teal)}
.work-main{flex:1;min-width:0;display:flex;flex-direction:column}
.work-text{font-weight:600;overflow-wrap:anywhere}
.work-tags{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:.35rem;max-width:45%}
.work-more{flex:none;color:var(--muted);margin-top:.15rem;display:inline-flex;transition:transform .12s}
.work-group.expanded .work-more{transform:rotate(180deg)}
.work-detail{padding:.4rem 0 .3rem 2rem;overflow-wrap:anywhere}
.work-detail p,.section p{margin:0 0 .5rem}
.work-detail ul,.section ul{margin:0 0 .5rem;padding-left:1.3rem}
.status-chip{display:inline-flex;align-items:center;gap:.35rem;padding:.08rem .6rem;border-radius:.4rem;border:1px solid var(--border-strong);background:var(--chip-bg);font-size:.86rem;white-space:nowrap}
.status-chip.p3{background:var(--p3-bg)}
.status-chip.ok{background:var(--ok-bg);border-color:var(--ok-border);color:var(--ok-fg)}
.status-chip.warn{background:var(--warn-bg);border-color:var(--warn-border);color:var(--warn-fg)}
.status-chip.next{background:var(--next-bg);border-color:var(--next-border);color:var(--next-fg)}
.status-chip.bad{border-color:var(--error);color:var(--error)}
.section{display:flex;flex-direction:column;gap:.7rem;min-width:0;border-top:1px solid var(--border);padding-top:1.3rem}
.section h2{margin:0;font-size:1.45rem;font-weight:700}
.section h3{margin:0;font-size:1.1rem;font-weight:700}
.section-body{display:flex;flex-direction:column;gap:.4rem;overflow-wrap:anywhere}
.empty-state{padding:1rem 0}
@media (max-width:600px){
.page-head{flex-direction:column;gap:.3rem}
.page-head h1{font-size:1.9rem}
.page-date{padding-top:0}
.lib-search{flex:1 1 100%}
.lib-search input{width:100%}
.line-tabs{flex:1 1 100%}
.line-tabs button{padding:.55rem .6rem}
.work-head{flex-wrap:wrap}
.work-tags{flex-basis:100%;justify-content:flex-start;max-width:none;padding-left:1.7rem;order:3}
.work-detail{padding-left:0}
}
'''

JS='''
(function(){
'use strict';
var D=JSON.parse(document.getElementById('view-data').textContent);
var items=D.items||[];
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function icon(inner){var w=document.createElement('span');w.innerHTML='<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+inner+'</svg>';return w.firstChild}
// Fixed markup only (Lucide shapes); no note text is ever put through innerHTML.
var ICON_OPEN='<circle cx="12" cy="12" r="10"/>';
var ICON_DONE='<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>';
var ICON_MORE='<path d="m6 9 6 6 6-6"/>';

// Minimal Markdown for notes: `code`, **bold**, [text](url) shown as its text, [[Note]] as a plain chip, "- " bullets, fenced code. Text nodes only.
function inline(parent,text){
  text.split(/(`[^`]+`|\\*\\*[^*]+\\*\\*|\\[\\[[^\\]]+\\]\\]|\\[[^\\]]+\\]\\([^)]*\\))/).forEach(function(s,i){
    if(!s)return;
    if(i%2===0){parent.appendChild(document.createTextNode(s));return}
    if(s.charAt(0)==='`')parent.appendChild(el('code',null,s.slice(1,-1)));
    else if(s.slice(0,2)==='**')parent.appendChild(el('strong',null,s.slice(2,-2)));
    else if(s.slice(0,2)==='[[')parent.appendChild(el('span','chip',s.slice(2,-2).split('|')[0]));
    else parent.appendChild(document.createTextNode(/^\\[([^\\]]+)\\]/.exec(s)[1]));
  });
}
function markdown(text){
  var box=document.createDocumentFragment(),para=null,list=null,fence=null;
  String(text||'').split('\\n').forEach(function(line){
    if(fence){if(/^\\s*```/.test(line)){var pre=el('pre');pre.appendChild(el('code',null,fence.join('\\n')));box.appendChild(pre);fence=null}else fence.push(line);return}
    if(/^\\s*```/.test(line)){para=list=null;fence=[];return}
    var bullet=/^\\s*(?:[-*+]|\\d+\\.)\\s+(.*)$/.exec(line);
    if(bullet){para=null;if(!list){list=el('ul');box.appendChild(list)}var li=el('li');inline(li,bullet[1]);list.appendChild(li);return}
    if(!line.trim()){para=list=null;return}
    list=null;var head=/^#{1,6}\\s+(.*)$/.exec(line);
    if(head){para=null;var p=el('p');var b=el('strong');inline(b,head[1]);p.appendChild(b);box.appendChild(p);return}
    if(!para){para=el('p');box.appendChild(para)}else para.appendChild(document.createTextNode(' '));
    inline(para,line.trim());
  });
  if(fence){var last=el('pre');last.appendChild(el('code',null,fence.join('\\n')));box.appendChild(last)}
  return box;
}

// State lives in location.hash (tab, tags, search, sort, expanded items), so the local page's reload keeps it.
var defaultTab=D.counts.open?'open':'all';
var st=(function(){var p=new URLSearchParams(location.hash.slice(1));
  return {tab:p.get('tab')||defaultTab,tags:new Set(p.getAll('t')),q:p.get('q')||'',sort:p.get('s')||'written',open:new Set((p.get('x')||'').split(',').filter(Boolean).map(Number))}})();
function save(){
  var p=new URLSearchParams();
  if(st.tab!==defaultTab)p.set('tab',st.tab);
  st.tags.forEach(function(t){p.append('t',t)});
  if(st.q)p.set('q',st.q);
  if(st.sort!=='written')p.set('s',st.sort);
  if(st.open.size)p.set('x',Array.from(st.open).join(','));
  try{history.replaceState(null,'',location.href.split('#')[0]+(p.toString()?'#'+p.toString():''))}catch(e){}
}

// Chips: every tag belongs to one of three facets, by what it is: priority (P1, P2, ...), status (approved, next, failed, ...) or
// anything else (an area such as ALM). An item passes when, for every facet with chips selected, it has at least one of that
// facet's selected tags: OR within a facet, AND across facets. (Bracket position is not used: it shifts when a group is left out.)
var STATUS={approved:1,active:1,succeeded:1,next:1,proposed:1,busy:1,running:1,waiting:1,paused:1,deprecated:1,failed:1,blocked:1,done:1};
function kind(t){return /^P\\d$/i.test(t)?0:(STATUS.hasOwnProperty(t.toLowerCase())?1:2)}
var facet=new Map();
items.forEach(function(it){it.tags.forEach(function(t){facet.set(t,kind(t))})});
function passes(it){
  if(st.tab==='open'&&it.done)return false;
  if(st.tab==='done'&&!it.done)return false;
  var byFacet=new Map();
  st.tags.forEach(function(t){if(!facet.has(t))return;var f=facet.get(t);if(!byFacet.has(f))byFacet.set(f,[]);byFacet.get(f).push(t)});
  var ok=true;
  byFacet.forEach(function(want){if(!want.some(function(t){return it.tags.indexOf(t)>=0}))ok=false});
  if(!ok)return false;
  if(st.q){var hay=[it.text,it.id||'',it.tags.join(' '),it.detail||''].join(' ').toLowerCase();if(hay.indexOf(st.q.toLowerCase())<0)return false}
  return true;
}
function priority(it){for(var i=0;i<it.tags.length;i++){var m=/^P(\\d)$/i.exec(it.tags[i]);if(m)return +m[1]}return 99}
function ordered(list){
  var out=list.slice();
  if(st.sort==='priority')out.sort(function(a,b){return priority(a)-priority(b)||a.index-b.index});
  else if(st.sort==='id')out.sort(function(a,b){if(!a.id!==!b.id)return a.id?-1:1;return (a.id||'').localeCompare(b.id||'',undefined,{numeric:true})||a.index-b.index});
  return out;
}
function badge(tag){
  var k=tag.toLowerCase(),c='';
  if(k==='p1'||k==='failed'||k==='blocked')c='bad';
  else if(k==='p2'||k==='waiting'||k==='paused'||k==='deprecated')c='warn';
  else if(k==='p3')c='p3';
  else if(k==='approved'||k==='active'||k==='succeeded')c='ok';
  else if(k==='next'||k==='proposed'||k==='busy'||k==='running')c='next';
  return el('span','status-chip'+(c?' '+c:''),tag);
}
function card(it){
  var has=!!it.detail,open=has&&st.open.has(it.index);
  var box=el('article','work-group'+(it.done?' done':'')+(has?' has-detail':'')+(open?' expanded':''));
  var head=el(has?'button':'div','work-head');
  if(has){head.type='button';head.setAttribute('aria-expanded',open?'true':'false')}
  var mark=el('span','work-mark');mark.appendChild(icon(it.done?ICON_DONE:ICON_OPEN));mark.setAttribute('title',it.done?'Done':'Open');head.appendChild(mark);
  var main=el('span','work-main');
  if(it.id)main.appendChild(el('span','eyebrow',it.id));
  var text=el('span','work-text');inline(text,it.text||it.id||'(no text)');main.appendChild(text);
  head.appendChild(main);
  if(it.tags.length){var tags=el('span','work-tags');it.tags.forEach(function(t){tags.appendChild(badge(t))});head.appendChild(tags)}
  if(has){var more=el('span','work-more');more.appendChild(icon(ICON_MORE));head.appendChild(more)}
  box.appendChild(head);
  if(has){
    var detail=el('div','work-detail');detail.appendChild(markdown(it.detail));detail.hidden=!open;box.appendChild(detail);
    head.addEventListener('click',function(){
      var now=detail.hidden;detail.hidden=!now;box.classList.toggle('expanded',now);head.setAttribute('aria-expanded',now?'true':'false');
      if(now)st.open.add(it.index);else st.open['delete'](it.index);save();
    });
  }
  return box;
}
function tabs(){
  var box=document.getElementById('tabs');box.textContent='';
  [['open','Open',D.counts.open],['done','Done',D.counts.done],['all','All',D.counts.total]].forEach(function(t){
    var b=el('button',st.tab===t[0]?'on':'');b.type='button';b.setAttribute('role','tab');b.setAttribute('aria-selected',st.tab===t[0]?'true':'false');
    b.appendChild(document.createTextNode(t[1]));b.appendChild(el('span','tab-count',String(t[2])));
    b.addEventListener('click',function(){st.tab=t[0];save();draw()});box.appendChild(b);
  });
}
function chips(){
  var box=document.getElementById('chips');box.textContent='';
  var tags=Array.from(facet.keys()).sort(function(a,b){return facet.get(a)-facet.get(b)||a.localeCompare(b)}),prev=null;
  tags.forEach(function(t){
    if(prev!==null&&facet.get(t)!==prev)box.appendChild(el('span','chip-gap'));prev=facet.get(t);
    var on=st.tags.has(t),b=el('button','chip'+(on?' on':''),t);b.type='button';b.setAttribute('aria-pressed',on?'true':'false');
    b.addEventListener('click',function(){if(on)st.tags['delete'](t);else st.tags.add(t);save();draw()});box.appendChild(b);
  });
}
function draw(){
  tabs();chips();
  var list=document.getElementById('list');list.textContent='';
  var shown=ordered(items.filter(passes));
  shown.forEach(function(it){list.appendChild(card(it))});
  if(!shown.length)list.appendChild(el('p','empty-state muted',items.length?'No items match the filters.':'No items.'));
}
function build(){
  if(D.important){var c=document.getElementById('important');c.hidden=false;c.appendChild(markdown(D.important))}
  var notes=document.getElementById('notes');
  if((D.sections||[]).length)notes.appendChild(el('div','eyebrow','Notes'));
  (D.sections||[]).forEach(function(s){var sec=el('section','section');sec.appendChild(el('h2',null,s.heading));var body=el('div','section-body');body.appendChild(markdown(s.text));sec.appendChild(body);notes.appendChild(sec)});
  if(items.length){
    document.getElementById('work').hidden=false;
    var q=document.getElementById('q'),sort=document.getElementById('sort');q.value=st.q;sort.value=st.sort;
    q.addEventListener('input',function(){st.q=q.value;save();draw()});
    sort.addEventListener('change',function(){st.sort=sort.value;save();draw()});
    draw();
  }
}
build();
if(document.documentElement.getAttribute('data-target')==='local')setInterval(function(){if(document.visibilityState==='visible')location.reload()},@@RELOAD@@);
})();
'''

_assets={}
def _asset(name):
    """The text of a vendored asset (assets/*.b64 hold binary files as base64 so the installer, which copies text, can carry them)."""
    if name not in _assets:_assets[name]=(Path(__file__).resolve().parent.parent/'assets'/name).read_text(encoding='ascii').strip()
    return _assets[name]

def _css(target):
    face=('@font-face{font-family:"Plus Jakarta Sans Variable";font-style:normal;font-display:swap;font-weight:200 800;'
          'src:url(data:font/woff2;base64,'+_asset('plus-jakarta-sans-latin-wght-normal.woff2.b64')+') format("woff2")}')
    if target=='local':theme=':root{'+DARK+'}'
    else:theme=(':root{'+LIGHT+'}@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){'+DARK+'}}'
                ':root[data-theme="dark"]{'+DARK+'}')
    return face+theme+CSS.replace('@@LOGO@@',_asset('hivemind-logo.png.b64'))

def _banner(model):
    status=model['status'];name=model.get('superseded_by')
    if status=='missing':return 'This note was not found. It may have been deleted or moved to another store. Remove this view with views remove.'
    if status=='superseded':return 'This note was superseded'+(f' by {name}.' if name else '.')
    if status in ('resolved','archived'):return f'This note is {status}.'+(f' It was replaced by {name}.' if name else '')
    return ''

def render_html(model,target='local'):
    """One self-contained page for the model. target 'local' is dark and reloads itself; 'artifact' follows the viewer's theme and never reloads."""
    if target not in TARGETS:raise ValueError('target must be local or artifact')
    esc=lambda v:html.escape(str(v),quote=True)
    short=model.get('view_title') or model['title'] or 'Note view'
    data=json.dumps(model,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    eyebrow=' · '.join(p for p in (model['domain'],model['type'] if model['status']!='missing' else '') if p)
    banner=_banner(model);updated=str(model['updated'])[:10]
    counts=model['counts'];pct=round(100*counts['done']/counts['total']) if counts['total'] else 0
    progress=(f'<div class="progress-line"><span class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="{counts["total"]}" aria-valuenow="{counts["done"]}"><span style="width:{pct}%"></span></span>'
              f'<span class="muted small">{counts["done"]} of {counts["total"]} done</span></div>') if counts['total'] else ''
    bar=('<div id="work" hidden><div class="lib-bar"><div class="line-tabs" id="tabs" role="tablist"></div>'
         '<label class="lib-search"><input id="q" type="text" placeholder="Search items" aria-label="Search items" autocomplete="off"></label>'
         '<select id="sort" aria-label="Sort items"><option value="written">Written order</option><option value="priority">Priority</option><option value="id">ID</option></select></div>'
         '<div class="chips" id="chips" style="margin-top:.8rem"></div><div class="work-list" id="list" style="margin-top:.8rem"></div></div>')
    script=JS.replace('@@RELOAD@@',str(RELOAD_SECONDS*1000))
    return (f'<!doctype html>\n<html lang="en" data-target="{target}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(short)}</title><style>{_css(target)}</style></head><body><main class="page">'
            f'<div class="brand"><span class="brand-logo" aria-hidden="true"></span><span>Hivemind</span></div>'
            f'<header class="page-head"><div class="page-title"><div class="eyebrow">{esc(eyebrow)}</div><h1>{esc(model["title"] or short)}</h1>'
            +(f'<p class="page-sub">{esc(model["summary"])}</p>' if model['summary'] else '')+'</div>'
            +(f'<div class="page-date muted small">Updated {esc(updated)}</div>' if updated else '')+'</header>'
            +(f'<div class="banner" role="status">{esc(banner)}</div>' if banner else '')
            +'<div class="callout" id="important" hidden></div>'+progress+bar+'<div id="notes" class="notes"></div>'
            f'<noscript><p class="muted">This page needs JavaScript to list the note.</p></noscript></main>'
            f'<script type="application/json" id="view-data">{data}</script><script>{script}</script></body></html>\n')

# ---- rendering and the registry ----

def _find(entry,root,notes=None):
    """The note an entry names (its ID is unique store-wide, so a promoted note is found in its new domain), or None."""
    from .notes import find_note,domain_folders
    if notes is not None and entry['note'] in notes:return notes[entry['note']]
    for domain in [entry['domain']]+[d for d in domain_folders(root) if d!=entry['domain']]:
        try:return dict(find_note(entry['note'],domain,root)[1],domain=domain)
        except (ValueError,OSError):continue
    return None

def _model(entry,note,root):
    model=view_model(note,root) if note else missing_model(entry)
    model['view_title']=entry.get('title') or model['title'];return model

def _revision(note):return str(note.get('revision') or '') if note else 'missing'

def _render_local(root,entry,note,cache):
    """Write the local page for one entry and record its revision in `cache` (the caller saves the cache)."""
    path=view_path(root,entry);atomic_write(path,render_html(_model(entry,note,root),'local'))
    cache[entry['note']]={'revision':_revision(note),'renderer':RENDERER}
    return path

def _lives_in(root,domain,note_id):
    """True when a note file of this ID (named ...-<id8>.md) is in the domain's folder."""
    folder=Path(root)/'domains'/domain
    return folder.is_dir() and any(True for _ in folder.rglob(f'*-{note_id[:8]}.md'))

def _out_path(root,out):
    """An --out destination: anywhere outside the store, or under its .views folder. Inside the store it would overwrite
    canonical files (notes, registries) with no revision check or journal."""
    path=Path(out).resolve()
    if path.is_relative_to(root) and not path.is_relative_to(root/'.views'):raise ValueError('--out must be outside the memory store (or under its .views folder)')
    return path

def refresh(domain,root):
    """Re-render the local pages of this domain's views whose note changed. Called at the end of reconcile(), on every
    write: with no registry or no view in the domain it returns after one stat. It never raises."""
    try:
        if not _path(root).is_file():return
        # A promoted or remapped note keeps its registry domain, so also take views whose note file now lives here.
        entries=[v for v in load(root)['views'] if v['domain']==domain or _lives_in(root,domain,v['note'])]
        if not entries:return
        from .repository import inventory
        notes={n['id']:dict(n,domain=domain) for n in inventory(domain,root,retired=True) if n.get('id')}
        cache=_cache(root);changed=False
        for entry in entries:
            note=_find(entry,root,notes);seen=cache.get(entry['note']) or {}
            if seen.get('revision')==_revision(note) and seen.get('renderer')==RENDERER and view_path(root,entry).is_file():continue
            try:_render_local(root,entry,note,cache);changed=True
            except Exception as exc:
                from .logs import swallowed
                swallowed('views.render',exc,root)
        if changed:_write_cache(root,cache)
    except Exception as exc:
        from .logs import swallowed
        swallowed('views.refresh',exc,root)

def listing(root,domain=None):
    """Registry rows with their derived state (domain None: every domain)."""
    rows=[];cache=_cache(root)
    for entry in load(root)['views']:
        if domain and entry['domain']!=domain:continue
        note=_find(entry,root);revision=_revision(note);seen=cache.get(entry['note']) or {};path=view_path(root,entry)
        rows.append({'note':entry['note'],'domain':entry['domain'],'title':entry.get('title'),'note_title':note['title'] if note else None,'path':str(path),
                     'fresh':bool(seen.get('revision')==revision and seen.get('renderer')==RENDERER),'artifact_url':entry.get('artifact_url'),
                     'artifact_current':bool(entry.get('artifact_url') and entry.get('published_revision')==revision),'exists':path.is_file()})
    return {'ok':True,'views':rows}

def warnings(root):
    """Doctor lines for views whose note no longer resolves."""
    return [f"view {v['title'] or v['note'][:8]}: note {v['note']} not found; remove it with views remove" for v in listing(root)['views'] if v['note_title'] is None]

def add(root,domain,note_id,title=None):
    """Register a view (kind views_add) and render it at once."""
    root=Path(root).resolve()
    with store_lock(root):
        note=_find({'note':note_id,'domain':domain},root)
        if not note:raise ValueError('No such note')
        entries=load(root)['views'];found=next((v for v in entries if v['note']==note_id),None)
        title=(title or '').strip() or (found or {}).get('title') or note['title']
        if len(title)>120 or '\n' in title:raise ValueError('title must be one line of at most 120 characters')
        entry=dict(found or {'note':note_id,'domain':note['domain'],'added':now(),'artifact_url':None,'published_revision':None},title=title)
        receipt=transaction(root,{'views.json':_registry([v for v in entries if v['note']!=note_id]+[entry])},'views_add') if entry!=found else {}
        cache=_cache(root);path=_render_local(root,entry,note,cache);_write_cache(root,cache)
    return {'ok':True,'note':note_id,'domain':entry['domain'],'title':title,'path':str(path),'status':'exists' if not receipt else 'added','operation_id':receipt.get('operation_id')}

def remove(root,note_id):
    """Drop a view (kind views_remove) and delete its pages."""
    root=Path(root).resolve()
    with store_lock(root):
        entries=load(root)['views'];entry=next((v for v in entries if v['note']==note_id),None)
        if not entry:raise ValueError('That note has no view')
        rest=[v for v in entries if v is not entry]
        receipt=transaction(root,{'views.json':_registry(rest) if rest else None},'views_remove')
        for target in TARGETS:
            try:view_path(root,entry,target).unlink()
            except FileNotFoundError:pass
        cache=_cache(root)
        if cache.pop(note_id,None) is not None:_write_cache(root,cache)
    return {'ok':True,'note':note_id,'status':'removed','operation_id':receipt.get('operation_id')}

def published(root,note_id,url):
    """Record where the artifact page lives and which revision of the note it shows (kind views_published)."""
    if not isinstance(url,str) or not re.fullmatch(r'https?://[^\s<>"\']+',url):raise ValueError('--url must be an http(s) address')
    root=Path(root).resolve()
    with store_lock(root):
        entries=load(root)['views'];entry=next((v for v in entries if v['note']==note_id),None)
        if not entry:raise ValueError('That note has no view; add one with views add REF')
        note=_find(entry,root)
        if not note:raise ValueError('The note of this view was not found')
        new=dict(entry,artifact_url=url,published_revision=_revision(note))
        receipt=transaction(root,{'views.json':_registry([v for v in entries if v is not entry]+[new])},'views_published') if new!=entry else {}
    return {'ok':True,'note':note_id,'artifact_url':url,'published_revision':new['published_revision'],'operation_id':receipt.get('operation_id')}

def render(root,entries,target='local',out=None,fmt='html'):
    """Render registered entries: html to their standard path (artifact: beside it, <name>.artifact.html) or to `out`
    (one entry only), or json models. Local pages update the cache like refresh does."""
    root=Path(root).resolve();rows=[]
    if out is not None and len(entries)!=1:raise ValueError('--out needs exactly one view')
    if out is not None:out=_out_path(root,out)
    with store_lock(root):
        cache=_cache(root);changed=False
        for entry in entries:
            note=_find(entry,root);model=_model(entry,note,root)
            if fmt=='json':
                if out is not None:atomic_write(Path(out),json.dumps(model,ensure_ascii=False,indent=1)+'\n')
                rows.append({'note':entry['note'],'model':model,**({'path':str(out)} if out is not None else {})});continue
            if out is None and target=='local':path=_render_local(root,entry,note,cache);changed=True
            else:
                path=Path(out) if out is not None else view_path(root,entry,target)
                atomic_write(path,render_html(model,target))
                if out is None and target=='local':changed=True
            rows.append({'note':entry['note'],'target':target,'path':str(path)})
        if changed:_write_cache(root,cache)
    return rows

def transient(note):
    """A one-off entry for a note that has no view (render to --out or json only)."""
    return {'note':note['id'],'domain':note['domain'],'title':note['title']}

# ---- the artifact hint ----

HINT_MAX=3
def behind(root,domains,cmd='CMD'):   # domains: names, or a function returning them (called only when the store has views)
    """[(key, line)] for views with a published artifact that shows an older revision than the note. Reads only
    views.json and .cache/views.json (no note is parsed), so the prompt path stays cheap. The line names the repair."""
    try:
        if not _path(root).is_file():return []
        wanted=set(domains() if callable(domains) else domains);rows=[v for v in load(root)['views'] if v.get('artifact_url') and v['domain'] in wanted]
        if not rows:return []
        from .notes import short_ref
        cache=_cache(root);found=[]
        for entry in rows:
            revision=(cache.get(entry['note']) or {}).get('revision')
            if not revision or revision=='missing' or entry.get('published_revision')==revision:continue
            ref=short_ref(entry['domain'],entry['note']);url=entry['artifact_url'];title=' '.join(str(entry.get('title') or ref).split())[:80].replace('"',"'")
            found.append((f"{entry['note']}@{revision}",f'Dashboard "{title}" is behind its note: run {cmd} views render {ref} --target artifact, publish the printed file to {url} with the Artifact tool, then {cmd} views published {ref} --url {url}'))
        return found[:HINT_MAX]
    except Exception as exc:
        from .logs import swallowed
        swallowed('views.behind',exc,root);return []
