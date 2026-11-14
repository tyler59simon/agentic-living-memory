import json,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from core import maintenance,maintenance_state as ms,notes,digests
from core.note_format import render,action_items
from core.note_types import verified_at
from core.repository import find_secret,inventory
from test_core import StoreFixture

OLD='2020-01-01'
TAGS=['subject/project','concern/architecture']
def put(path,text):Path(path).write_bytes(text.encode())

class Base(StoreFixture):
    def setUp(self):
        super().setUp()
        self.now=datetime.now(timezone.utc)
        self.cfg=ms.validate({'enabled':True,'domains':['demo'],'min_interval_seconds':1})
        ms.set_config(self.root,self.cfg)
        self.project=self.base/'project';(self.project/'src').mkdir(parents=True)
    def tick(self):self.now+=timedelta(hours=1)
    def mk(self,title,body='A fact about the project.',**kw):
        kw.setdefault('allow_similar',True)
        return notes.capture(title,body,TAGS,'demo',self.root,**kw)
    def ref(self,n):return f"demo:{n['id']}"
    def get(self,n):return notes.find_note(n['id'],'demo',self.root)[1]
    def src(self,name,text):put(self.project/'src'/name,text)
    def cands(self,limit=10):return maintenance.candidates(self.root,self.cfg,ms.state(self.root,self.now),self.now,limit)
    # -- reports
    def decisions(self,manifest,*specs):
        out=[]
        for note,action,kw in specs:
            ref=self.ref(note);cand=manifest['candidates'][ref];ev=manifest['evidence']
            def token(t):
                ids=cand['evidence_ids']
                if t=='own':return ids[0]
                kind={'file':'file','successor':'note'}[t]
                return next(i for i in ids[1:] if ev[i]['kind']==kind)
            kw=dict(kw);tokens=kw.pop('evidence',['own'])
            out.append({'ref':ref,'revision':cand['revision'],'action':action,'reason':'Because the evidence says so.','evidence_ids':[token(t) for t in tokens],**kw})
        return out
    def specs_adapter(self,*specs,usage=1000):
        return lambda req:{'ok':True,'report':{'version':1,'run_id':req['run']['id'],'decisions':self.decisions(req['manifest'],*specs)},'usage_micro':usage,'stderr':''}
    def go(self,*specs,dry=False):
        self.tick()
        return maintenance.run(self.root,dry_run=dry,adapter=self.specs_adapter(*specs),now=self.now)
    # -- direct executor access
    def prepare(self):
        self.tick()
        sel=self.cands();run=ms.reserve(self.root,self.cfg,len(sel),self.now,selected=[{'ref':c['ref'],'revision':c['revision']} for c in sel])
        text,manifest=maintenance.packet(self.root,sel,run['id'])
        run=ms.applying(self.root,ms.launch(self.root,run,self.now),self.now)
        return run,manifest,text
    def apply(self,run,manifest,decisions,**kw):
        report={'version':1,'run_id':run['id'],'decisions':decisions}
        return maintenance.apply(self.root,run,report,manifest,self.cfg,self.now,**kw)
    def read_run(self,run_id):return json.loads(next((self.root/'maintenance/runs').glob(f'*/{run_id}.json')).read_text(encoding='utf-8'))
    def one(self,res,i=0):return res['results'][i]
    def d(self,manifest,note,action,**kw):return self.decisions(manifest,(note,action,kw))[0]

class Record(unittest.TestCase):
    def test_retirement_record_carries_the_maintenance_fields_and_existing_callers_are_unchanged(self):
        self.assertEqual(set(notes.retirement_record('archive','agent')),{'version','at','action','actor'})
        rec=notes.retirement_record('archive','maintenance',rule='expired_status',reason='Over.',evidence=[{'kind':'note','ref':'demo:a','revision':'a'*64}],run_id='r',operation_id='m-1')
        self.assertEqual({k:rec[k] for k in ('rule','reason','run_id','operation_id')},{'rule':'expired_status','reason':'Over.','run_id':'r','operation_id':'m-1'})
        from core.repository import check_retirement;check_retirement(rec)

class Preview(Base):
    def test_candidates_and_packet_are_a_free_read_only_preview(self):
        self.mk('Sprint status','Done.',type='status',review_by=OLD)
        usage=self.root/'ledger';before=sorted(p.name for p in self.root.rglob('*') if p.is_file() and 'usage' in p.name)
        sel=self.cands();text,manifest=maintenance.packet(self.root,sel,'preview')
        self.assertEqual(len(sel),1);self.assertEqual(list(manifest['candidates']),[sel[0]['ref']])
        self.assertEqual(sorted(p.name for p in self.root.rglob('*') if p.is_file() and 'usage' in p.name),before)
        self.assertFalse((self.root/'maintenance/runs').exists());st=ms.state(self.root,self.now);self.assertEqual((st['reviewed'],st['reserved_notes'],st['charged_micro']),(0,0,0))
        self.assertEqual(maintenance.candidates(self.root,self.cfg,st,self.now,10)[0]['ref'],sel[0]['ref'])
    def test_candidates_come_in_priority_order_with_round_robin_domains(self):
        from core.init_domain import init_domain
        init_domain('other',[str(self.base/'project2')],self.root)
        cfg=ms.validate({'enabled':True,'domains':['demo','other'],'min_interval_seconds':1})
        a=self.mk('Warned status','Done.',type='status',review_by=OLD)
        self.src('s.py','v = 1\n');s=self.mk('Source note','Defined in s.py.',files=['src/s.py']);self.src('s.py','v = 2\n')
        o=notes.capture('Other status','Done.',TAGS,'other',self.root,type='status',review_by=OLD)
        st=ms.state(self.root,self.now)
        order=[c['ref'] for c in maintenance.candidates(self.root,cfg,st,self.now,10)]
        self.assertEqual(order,[self.ref(s),f"other:{o['id']}",self.ref(a)])
        order=[c['ref'] for c in maintenance.candidates(self.root,cfg,dict(st,domain_cursor='other'),self.now,10)]
        self.assertEqual(order[0],f"other:{o['id']}")
        self.assertEqual(maintenance.next_cursor(cfg,[{'domain':'demo'}]),'other');self.assertEqual(maintenance.next_cursor(cfg,[{'domain':'other'}]),'demo')

class PacketBounds(Base):
    def test_total_size_body_size_excerpts_and_redaction(self):
        key='sk-ant-'+'Ab1'*12;self.assertTrue(find_secret(key))
        for i in range(5):self.src(f'f{i}.py',f'import os\n# note about retry limit {i}\nTOKEN_FILE = "{key}"\n'+'filler = 1\n'*400)
        self.mk('Many sources','The retry limit is read from the files.',files=[f'src/f{i}.py' for i in range(5)])
        for i in range(5):self.src(f'f{i}.py',f'import os\n# note about retry limit {i}\nTOKEN_FILE = "{key}"\n'+'filler = 2\n'*400)
        for i in range(8):self.mk(f'Large note {i}',f'Topic {i}. '+('Sentence number %d is here and stays. '%i)*500,review_by=OLD)
        sel=self.cands(10);text,manifest=maintenance.packet(self.root,sel,'r1')
        self.assertLessEqual(len(text.encode('utf-8')),maintenance.PACKET_MAX);self.assertTrue(manifest['dropped'])
        self.assertNotIn(key,text);self.assertIn('redacted',text)
        packed=json.loads(text)['candidates']
        for block in packed:
            files=[e for e in block['evidence'] if e['kind']=='file']
            self.assertLessEqual(len(files),maintenance.EXCERPTS)
            for e in files:self.assertLessEqual(len(e.get('excerpt','')),maintenance.EXCERPT_CHARS+200)   # redaction markers can lengthen text
        evidence=[e for e in manifest['evidence'].values() if e['kind']=='file'];self.assertTrue(evidence);self.assertTrue(all(e['sha256'] and e['lines'] for e in evidence))
        self.assertEqual(sorted(manifest['evidence'],key=lambda i:int(i[1:]))[0],'E1')

if __name__=='__main__':unittest.main()
