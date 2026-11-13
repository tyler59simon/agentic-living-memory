"""Optional semantic search: the stdlib static-model reader, rank fusion and the injection gate, on a tiny synthetic model."""
import contextlib,hashlib,importlib.util,io,json,struct
from pathlib import Path
from unittest.mock import patch
from core import runtime,semantic
from core.cli import main
from core.search import search_domain,_fresh,_fingerprint,_fuse
from core.repository import inventory
from test_core import StoreFixture

# Five directions: 0 layout, 1 note, 2 weather, 3 filler words, 4 cache.
ROWS={'[PAD]':[0]*5,'[UNK]':[0]*5,'[CLS]':[0]*5,'[SEP]':[0]*5,'[MASK]':[0]*5,
      'layout':[1,0,0,0,0],'laid':[1,0,0,0,0],'out':[1,0,0,0,0],'note':[0,1,0,0,0],
      'weather':[0,0,1,0,0],'tomorrow':[0,0,1,0,0],'how':[0,0,0,.1,0],'should':[0,0,0,.1,0],'be':[0,0,0,.1,0],
      'what':[0,0,0,.1,0],'is':[0,0,0,.1,0],'the':[0,0,0,.1,0],'?':[0,0,0,.1,0],',':[0,0,0,.1,0],'!':[0,0,0,.1,0],
      'cache':[0,0,0,0,1],'rebuild':[0,0,0,0,1],'##able':[0,0,0,0,.5],'cafe':[0,1,0,0,0]}

def write_model(folder,dtype='F32',rows=None):
    """A tiny model in the potion file layout; rows (token -> vector) default to ROWS."""
    rows=rows or ROWS;dim=len(next(iter(rows.values())))
    folder.mkdir(parents=True,exist_ok=True);vocab={token:i for i,token in enumerate(rows)}
    (folder/'tokenizer.json').write_text(json.dumps({'model':{'type':'WordPiece','unk_token':'[UNK]','continuing_subword_prefix':'##','max_input_chars_per_word':100,'vocab':vocab},
        'normalizer':{'type':'BertNormalizer','clean_text':True,'handle_chinese_chars':True,'strip_accents':None,'lowercase':True},
        'pre_tokenizer':{'type':'BertPreTokenizer'}}),encoding='utf-8')
    code,width={'F32':('f',4),'F16':('e',2)}[dtype];data=b''.join(struct.pack(f'<{dim}'+code,*row) for row in rows.values())
    header=json.dumps({'embeddings':{'dtype':dtype,'shape':[len(rows),dim],'data_offsets':[0,len(data)]}}).encode()
    (folder/'model.safetensors').write_bytes(struct.pack('<Q',len(header))+header+data)
    (folder/'config.json').write_text('{"normalize": true}',encoding='utf-8')
    return {name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in ('config.json','tokenizer.json','model.safetensors')}

class SemanticCase(StoreFixture):
    def setUp(self):
        super().setUp()
        self.source=self.base/'tiny';hashes=write_model(self.source)
        models=patch.dict(semantic.MODELS,{'tiny':{'repo':'none/tiny','revision':'0','min_similarity':0.45,'files':hashes}},clear=True)
        models.start();self.addCleanup(models.stop);self.addCleanup(semantic._unload)
        self.cwd=str(self.base/'project')

    def install(self):return semantic.install(self.root,'tiny',self.source)

    def test_tokenizer_splits_punctuation_strips_accents_and_continues_words(self):
        model=semantic.Model(self.source);self.addCleanup(model.close);vocab=model.vocab
        self.assertEqual(model.tokenize('Rebuildable, CAFÉ!'),[vocab['rebuild'],vocab['##able'],vocab[','],vocab['cafe'],vocab['!']])
        self.assertEqual(model.tokenize('zzz layout qqq'),[vocab['layout']])   # unknown words are dropped
        self.assertIsNone(model.embed('zzz qqq'))

    def test_the_parsed_tokenizer_is_cached_beside_the_source_and_rebuilt_when_it_changes(self):
        cache=self.source/'tokenizer.marshal'
        first=semantic.Model(self.source);self.addCleanup(first.close)
        self.assertTrue(cache.is_file())
        real=json.loads
        def header_only(text,*a,**k):   # the safetensors header is read as bytes; the tokenizer file as text
            if isinstance(text,str):raise AssertionError('tokenizer.json parsed again')
            return real(text,*a,**k)
        with patch.object(semantic.json,'loads',header_only):second=semantic.Model(self.source);self.addCleanup(second.close)
        self.assertEqual(second.vocab,first.vocab);self.assertEqual(second.tokenize('Rebuildable, CAFÉ!'),first.tokenize('Rebuildable, CAFÉ!'))
        # A changed source (size and mtime) is parsed again and the cache follows it.
        spec=json.loads((self.source/'tokenizer.json').read_text(encoding='utf-8'));spec['model']['vocab']['extra']=spec['model']['vocab']['layout']
        (self.source/'tokenizer.json').write_text(json.dumps(spec),encoding='utf-8')
        third=semantic.Model(self.source);self.addCleanup(third.close)
        self.assertIn('extra',third.vocab)
        with patch.object(semantic.json,'loads',header_only):
            fourth=semantic.Model(self.source);self.addCleanup(fourth.close)
        self.assertIn('extra',fourth.vocab)

    def test_an_unreadable_or_unwritable_tokenizer_cache_is_ignored(self):
        (self.source/'tokenizer.marshal').write_bytes(b'not marshal data')
        model=semantic.Model(self.source);self.addCleanup(model.close)
        self.assertIn('layout',model.vocab)
        import marshal
        self.assertEqual(marshal.loads((self.source/'tokenizer.marshal').read_bytes())['spec']['model']['vocab'],model.vocab)   # rebuilt
        (self.source/'tokenizer.marshal').write_bytes(marshal.dumps({'source':[0,0],'spec':{}}))   # stale
        self.assertIn('layout',semantic._tokenizer_spec(self.source)['model']['vocab'])
        (self.source/'tokenizer.marshal').unlink()
        with patch.object(semantic.tempfile,'mkstemp',side_effect=PermissionError('read-only')):   # a model folder that cannot be written
            self.assertIn('layout',semantic._tokenizer_spec(self.source)['model']['vocab'])
        self.assertFalse((self.source/'tokenizer.marshal').exists())

    def test_f16_rows_match_f32(self):
        wide=semantic.Model(self.source);write_model(self.base/'half','F16');half=semantic.Model(self.base/'half')
        self.addCleanup(wide.close);self.addCleanup(half.close)
        self.assertAlmostEqual(semantic.cosine(wide.embed('how should the cache be laid out'),half.embed('how should the cache be laid out')),1.0,places=3)

    def test_install_rejects_a_hash_mismatch_and_leaves_nothing(self):
        with (self.source/'model.safetensors').open('ab') as handle:handle.write(b'x')
        with self.assertRaisesRegex(ValueError,'hash_mismatch'):self.install()
        self.assertIsNone(semantic.installed(self.root))
        self.assertEqual([p.name for p in (self.root/'.models').iterdir()],[])

    def test_meaning_only_match_is_injected_and_unrelated_prompt_is_not(self):
        layout=self.note('Layout rules','Sections follow a fixed order.');self.note()
        prompt='how should entries be laid out?'
        _,before,_=runtime.context(self.cwd,prompt,self.root)
        self.assertEqual(before,[])
        result=self.install();self.assertEqual(result['indexes'],{'demo':'vectors'})
        text,ids,_=runtime.context(self.cwd,prompt,self.root)
        self.assertEqual(ids,[f"demo:{layout['id']}"]);self.assertIn('Layout rules',text)
        self.assertEqual(runtime.context(self.cwd,'what is the weather tomorrow?',self.root)[1],[])
        hit=search_domain(prompt,'demo',5,self.root)[0]
        self.assertEqual((hit['id'],hit['engine'],hit['coverage']),(layout['id'],'fts5+semantic',0.0));self.assertGreater(hit['similarity'],0.9)

    def test_fusion_ties_keep_the_lexical_order(self):
        a,b={'id':'a'},{'id':'b'}
        # Lexical ranks b, a and semantic ranks a, b give equal reciprocal scores.
        self.assertEqual([n['id'] for n,_ in _fuse([(b,2.0),(a,1.0)],{'a':0.9,'b':0.8},{'a':a,'b':b})],['b','a'])

    def test_without_a_model_the_fingerprint_is_the_schema_version_and_the_notes(self):
        self.note();notes=inventory('demo',self.root,retired=True)
        earlier=hashlib.sha256(json.dumps([4,[(n.get('id'),n['revision']) for n in notes]],sort_keys=True).encode()).hexdigest()
        self.assertEqual(_fingerprint(notes),earlier);self.assertNotEqual(_fingerprint(notes,'tiny'),earlier)

    def test_a_model_another_process_holds_open_is_left_inert(self):
        self.note();self.install()
        with patch.object(semantic.shutil,'rmtree'):   # as on Windows, where a mapped file cannot be deleted
            result=semantic.remove(self.root)
            self.assertEqual((result['enabled'],result['leftover']),(False,['tiny']));self.assertIsNone(semantic.installed(self.root))
            with self.assertRaisesRegex(ValueError,'semantic_model_in_use'):self.install()
        self.assertIsNone(semantic.installed(self.root))
        self.assertTrue(self.install()['enabled'])

    def test_eval_semantic_mode_restores_the_indexes_and_refuses_synthetic_fixtures(self):
        spec=importlib.util.spec_from_file_location('eval_retrieval',Path(__file__).resolve().parents[1]/'scripts'/'eval_retrieval.py')
        evaluation=importlib.util.module_from_spec(spec);spec.loader.exec_module(evaluation)
        layout=self.note('Layout rules','Sections follow a fixed order.');self.install()
        fixture=self.base/'fixture.json'
        fixture.write_text(json.dumps({'domain':'demo','cwd':self.cwd,'retrieval':[{'prompt':'how should entries be laid out?','expect':['demo:'+layout['id']]}]}),encoding='utf-8')
        self.assertEqual(evaluation.run(fixture,self.root,'off')['retrieval']['hit@3'],0.0)
        self.assertEqual(semantic.status(self.root)['indexes'],{'demo':'vectors'})
        self.assertTrue(_fresh('demo',self.root,inventory('demo',self.root,retired=True)))
        self.assertEqual(evaluation.run(fixture,self.root,'on')['retrieval']['hit@3'],1.0)
        with self.assertRaisesRegex(ValueError,'needs a real store'):
            evaluation.run(Path(__file__).resolve().parent/'fixtures'/'eval_synthetic.json',None,'on')

    def test_lexical_leader_stays_first_and_no_model_means_no_semantic_fields(self):
        cache=self.note();self.note('Layout rules','Sections follow a fixed order.')
        plain=search_domain('cache','demo',5,self.root)
        self.assertEqual(plain[0]['engine'],'fts5');self.assertNotIn('similarity',plain[0])
        self.install()
        fused=search_domain('cache','demo',5,self.root)
        self.assertEqual(fused[0]['id'],cache['id']);self.assertEqual(fused[0]['engine'],'fts5+semantic')

    def test_install_and_remove_change_the_fingerprint(self):
        self.note();notes=inventory('demo',self.root,retired=True)
        search_domain('cache','demo',5,self.root,rebuild=True);self.assertTrue(_fresh('demo',self.root,notes))
        self.install();self.assertTrue(_fresh('demo',self.root,notes))
        with patch.object(semantic,'USE_SEMANTIC',False):self.assertFalse(_fresh('demo',self.root,notes))
        result=semantic.remove(self.root)
        self.assertEqual((result['removed'],result['enabled'],result['indexes']),(True,False,{'demo':'lexical'}))
        self.assertTrue(_fresh('demo',self.root,notes))

    def test_unreadable_model_falls_back_to_lexical_and_is_reported(self):
        self.note('Layout rules','Sections follow a fixed order.');self.install();semantic._unload()
        (self.root/'.models'/'tiny'/'model.safetensors').write_bytes(b'\0'*4)
        self.assertEqual(runtime.context(self.cwd,'how should entries be laid out?',self.root)[1],[])
        self.assertEqual(search_domain('layout rules','demo',5,self.root)[0]['engine'],'fts5')
        status=semantic.status(self.root);self.assertFalse(status['enabled']);self.assertIn('error',status)

    def test_cli_status_install_and_remove(self):
        def run(*argv):
            out=io.StringIO()
            with contextlib.redirect_stdout(out):code=main(['--root',str(self.root),'--domain','demo',*argv])
            return code,json.loads(out.getvalue() or 'null')
        self.assertEqual(run('semantic','status')[1]['enabled'],False)
        code,result=run('semantic','install','--model','tiny','--from',str(self.source))
        self.assertEqual((code,result['enabled'],result['model']),(0,True,'tiny'))
        doctor=run('doctor')[1]['semantic'];self.assertEqual({k:doctor[k] for k in ('enabled','model')},{'enabled':True,'model':'tiny'});self.assertIn(doctor['backend'],('native','python'))
        self.assertEqual(run('semantic','remove')[1]['enabled'],False)
