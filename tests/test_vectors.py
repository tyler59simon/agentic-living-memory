"""The unit index (core/vectors.py): incremental updates, backend selection and fallback, native/Python parity.

Tests marked "native" load the real sqlite-vec extension and are skipped when it is not installed; every other test
runs the Python backend or forces a fallback, so they pass on machines without the extension."""
import os,sqlite3,struct,unittest
from unittest.mock import patch
from core import runtime,search,semantic,vectors
from core.repository import inventory
from test_core import StoreFixture
from test_semantic import write_model

# Six directions: 0 auth, 1 retry, 2 messaging, 3 filler, 4 weather, 5 release.
D=lambda i,w=1.0:[w if k==i else 0.0 for k in range(6)]
ROWS={'[PAD]':[0.0]*6,'[UNK]':[0.0]*6,
      'auth':D(0),'refresh':D(0),'token':D(0),'login':D(0),'service':D(0,.5),'centralized':D(0,.5),
      'retry':D(1),'duplicated':D(1),'logic':D(1,.5),'client':D(1,.5),'backoff':D(1),
      'kafka':D(2),'queue':D(2),'broker':D(2),'messaging':D(2),'rabbitmq':D(2),'rejected':D(3,.2),
      'the':D(3,.1),'is':D(3,.1),'in':D(3,.1),'a':D(3,.1),'every':D(3,.1),'we':D(3,.1),'why':D(3,.1),'.':D(3,.1),':':D(3,.1),'-':D(3,.1),
      'weather':D(4),'forecast':D(4),'rain':D(4),'umbrella':D(4),
      'release':D(5),'deploy':D(5),'ship':D(5)}

def native_available():
    db=sqlite3.connect(':memory:')
    try:return vectors.load(db)[0]
    finally:db.close()

NATIVE=native_available() and os.environ.get(vectors.ENV,'').strip().lower() not in ('0','off','false','no')

DECISION='''Refresh is centralized in the auth service.

## Why
Every client duplicated retry logic with its own backoff.

## Alternatives considered
- Kafka queue broker: rejected.
'''

class UnitIndexCase(StoreFixture):
    def setUp(self):
        super().setUp()
        self.source=self.base/'tiny6';hashes=write_model(self.source,rows=ROWS)
        models=patch.dict(semantic.MODELS,{'tiny6':{'repo':'none/tiny6','revision':'0','min_similarity':0.45,'files':hashes}},clear=True)
        models.start();self.addCleanup(models.stop);self.addCleanup(semantic._unload)

    def install(self):return semantic.install(self.root,'tiny6',self.source)
    def path(self):return self.root/'domains/demo'/vectors.FILE
    def meta(self):
        db=sqlite3.connect(self.path())
        try:return dict(db.execute('SELECT key,value FROM meta'))
        finally:db.close()
    def open(self):
        db=sqlite3.connect((self.root/'domains/demo/search.sqlite3').as_uri()+'?mode=ro',uri=True)
        try:fingerprint=db.execute('SELECT fingerprint FROM metadata').fetchone()[0]
        finally:db.close()
        model=semantic.load(self.root)
        return vectors.open_index('demo',self.root,fingerprint,search._vector_identity(self.root),model.dim if model else 0)
    def query(self,text):return semantic.load(self.root).embed(text)

class Building(UnitIndexCase):
    def test_without_a_model_units_are_indexed_without_vectors(self):
        self.note('Authentication design',DECISION)
        meta=self.meta();self.assertEqual(meta['identity'],'');self.assertEqual(meta['vectors'],'0')
        status=vectors.status('demo',self.root)
        self.assertEqual(status['notes'],1);self.assertGreaterEqual(status['units'],3);self.assertEqual(status['native_table'],'absent')

    def test_install_embeds_every_unit_and_records_the_model(self):
        self.note('Authentication design',DECISION);self.install()
        meta=self.meta();status=vectors.status('demo',self.root)
        self.assertTrue(meta['identity'].endswith('tiny6@0'));self.assertEqual(meta['dim'],'6')
        self.assertEqual(status['vectors'],status['units'])

    def test_an_edit_re_embeds_only_the_changed_section(self):
        n=self.note('Authentication design',DECISION);self.note('Weather',"Rain forecast means an umbrella.",allow_similar=True);self.install()
        model=semantic.load(self.root)
        path=next((self.root/'domains/demo/notes').glob('*authentication*'))
        path.write_text(path.read_text(encoding='utf-8').replace('its own backoff','its own exponential backoff'),encoding='utf-8')
        with patch.object(model,'embed',wraps=model.embed) as embed:
            self.assertTrue(search.refresh_stale('demo',self.root))
        self.assertEqual(embed.call_count,1)   # the Why section; the overview and the other note are reused

    def test_moving_a_section_embeds_nothing(self):
        self.note('Authentication design',DECISION);self.install()
        model=semantic.load(self.root)
        path=next((self.root/'domains/demo/notes').glob('*authentication*'));text=path.read_text(encoding='utf-8')
        why='## Why\nEvery client duplicated retry logic with its own backoff.\n\n'
        self.assertIn(why,text)
        path.write_text(text.replace(why,'')+'\n'+why,encoding='utf-8')
        with patch.object(model,'embed',wraps=model.embed) as embed:search.refresh_stale('demo',self.root)
        self.assertEqual(embed.call_count,0)

    def test_a_removed_note_leaves_no_units_or_vectors(self):
        self.note('Authentication design',DECISION);self.install()
        for path in (self.root/'domains/demo/notes').glob('*.md'):path.unlink()
        search.rebuild_index('demo',self.root)
        status=vectors.status('demo',self.root);self.assertEqual((status['units'],status['vectors']),(0,0))

    def test_a_damaged_index_is_never_trusted_and_the_next_write_rebuilds_it(self):
        self.note('Authentication design',DECISION);self.install()
        self.path().write_bytes(b'not a database at all'*100)
        index,reason=self.open();self.assertIsNone(index);self.assertEqual(reason,'unit_index_unreadable')
        hits=search.search_domain('why centralize refresh','demo',5,self.root)
        self.assertEqual(hits[0]['backend'],'notes')   # served from note-level vectors meanwhile
        search.rebuild_index('demo',self.root)
        self.assertEqual(vectors.status('demo',self.root)['notes'],1)

    def test_a_stale_index_is_refused_until_rebuilt(self):
        self.note('Authentication design',DECISION);self.install()
        path=next((self.root/'domains/demo/notes').glob('*authentication*'))
        db=sqlite3.connect(self.path());db.execute("UPDATE meta SET value='other' WHERE key='notes_fingerprint'");db.commit();db.close()
        index,reason=self.open();self.assertIsNone(index);self.assertEqual(reason,'unit_index_stale')
        self.assertEqual(search.search_domain('auth refresh','demo',5,self.root)[0]['backendReason'],'unit_index_stale')

    def test_a_failure_while_updating_never_fails_the_write(self):
        with patch.object(vectors,'update',side_effect=RuntimeError('boom')):
            n=self.note('Authentication design',DECISION)
        self.assertTrue(n['id'])

    def test_blobs_are_little_endian_float32(self):
        import array
        self.assertEqual(vectors._blob(array.array('f',[1.0,-2.5])),struct.pack('<2f',1.0,-2.5))
        self.assertEqual(list(vectors._vector(struct.pack('<2f',1.0,-2.5))),[1.0,-2.5])

class Retrieval(UnitIndexCase):
    def test_a_fact_only_in_a_section_is_found_by_meaning(self):
        self.note('Authentication design',DECISION);self.note('Weather',"Rain forecast means an umbrella.",allow_similar=True);self.install()
        with patch.object(vectors,'NATIVE',False):
            hits=search.search_domain('backoff duplicated','demo',5,self.root)
        self.assertEqual(hits[0]['title'],'Authentication design')
        self.assertEqual(hits[0]['backend'],'python');self.assertEqual(hits[0]['backendReason'],'disabled')
        self.assertEqual(hits[0]['units'][0]['heading'],'Why')
        with patch.object(search,'SEMANTIC_SOURCE','notes'):   # the note-level vector holds only the summary
            note_level=search.search_domain('backoff duplicated','demo',5,self.root)
        self.assertLess(note_level[0]['similarity'],hits[0]['similarity'])

    def test_a_long_note_gains_nothing_from_its_length(self):
        many='Weather is fine.\n\n'+'\n\n'.join(f'## Part {i}\nThe kafka word and rain {i} again.' for i in range(8))
        self.note('Long weather notes',many);self.note('Messaging',"Kafka broker queue messaging.",allow_similar=True);self.install()
        index,_=self.open()
        try:top=index.top_notes(self.query('kafka broker'),{n['id'] for n in inventory('demo',self.root)},5)
        finally:index.close()
        titles={n['id']:n['title'] for n in inventory('demo',self.root)}
        self.assertEqual(titles[top[0][0]],'Messaging')

    def test_retired_notes_cannot_crowd_out_eligible_ones(self):
        self.note('Messaging',"Kafka broker queue messaging.")
        for i in range(6):self.note(f'Old messaging {i}',f"Kafka broker queue {i}.",allow_similar=True)
        self.install()
        notes=inventory('demo',self.root);eligible={n['id'] for n in notes if n['title']=='Messaging'}
        index,_=self.open()
        try:
            with patch.object(vectors,'K_START',2):top=index.top_notes(self.query('kafka'),eligible,3)
        finally:index.close()
        self.assertEqual([i for i,_ in top],list(eligible))

    def test_without_the_extension_the_reason_is_reported(self):
        self.note('Authentication design',DECISION);self.install()
        with patch.object(vectors,'_library_path',return_value=None),patch.dict(os.environ,{vectors.ENV:''}):
            index,_=self.open()
            # An index written without the extension has no native table, which is reported first.
            expected='native_absent' if vectors.status('demo',self.root)['native_table']=='absent' else 'not_installed'
            try:self.assertEqual((index.backend,index.reason),('python',expected))
            finally:index.close()
        with patch.dict(os.environ,{vectors.ENV:'0'}):
            index,_=self.open()
            try:self.assertEqual(index.reason,'disabled')
            finally:index.close()

    def test_a_library_that_will_not_load_falls_back(self):
        db=sqlite3.connect(':memory:')
        try:
            with patch.object(vectors,'_library_path',return_value=str(self.base/'missing'/'vec0')):
                self.assertEqual(vectors.load(db),(False,'load_blocked'))
        finally:db.close()

    def test_lexical_only_store_still_answers(self):
        self.note('Authentication design',DECISION)
        hits=search.search_domain('auth refresh','demo',5,self.root)
        self.assertEqual(hits[0]['backend'],'lexical');self.assertNotIn('similarity',hits[0])
        self.assertTrue(any(u['heading']=='Why' for u in hits[0]['units']))   # units still give evidence by heading

    def test_the_semantic_off_switch_still_works(self):
        self.note('Authentication design',DECISION);self.install()
        with patch.object(semantic,'USE_SEMANTIC',False):search.rebuild_index('demo',self.root);hits=search.search_domain('auth refresh','demo',5,self.root)
        self.assertEqual(hits[0]['backend'],'lexical')

@unittest.skipUnless(NATIVE,'native: sqlite-vec is not installed or LIVING_MEMORY_NATIVE switches it off')
class Native(UnitIndexCase):
    def test_native_table_is_written_with_the_units_and_used(self):
        self.note('Authentication design',DECISION);self.install()
        self.assertEqual(vectors.status('demo',self.root)['native_table'],'fresh')
        index,_=self.open()
        try:self.assertEqual((index.backend,index.reason),('native',None))
        finally:index.close()

    def test_native_and_python_rank_the_same(self):
        for i,text in enumerate(['Auth refresh token login.','Retry backoff logic.','Kafka broker queue.','Rain forecast umbrella.','Deploy release ship.',DECISION]):
            self.note(f'Note {i}',text,allow_similar=True)
        self.install();ids={n['id'] for n in inventory('demo',self.root)}
        for text in ('auth','retry backoff','kafka rain','deploy the release','why refresh'):
            index,_=self.open()
            try:native=index.top_notes(self.query(text),ids,10);backend=index.backend
            finally:index.close()
            with patch.object(vectors,'NATIVE',False):
                index,_=self.open()
                try:python=index.top_notes(self.query(text),ids,10)
                finally:index.close()
            self.assertEqual(backend,'native')
            self.assertEqual([i for i,_ in native],[i for i,_ in python],text)
            for (_,a),(_,b) in zip(native,python):self.assertAlmostEqual(a,b,places=5)

    def test_a_write_without_the_extension_marks_the_native_table_stale_until_resynced(self):
        self.note('Authentication design',DECISION);self.install()
        with patch.object(vectors,'NATIVE',False):self.note('Messaging',"Kafka broker queue messaging.",allow_similar=True)
        self.assertEqual(vectors.status('demo',self.root)['native_table'],'stale')
        index,_=self.open()
        try:self.assertEqual((index.backend,index.reason),('python','native_stale'))
        finally:index.close()
        search.rebuild_index('demo',self.root)
        self.assertEqual(vectors.status('demo',self.root)['native_table'],'fresh')

    def test_a_reader_open_during_an_update_sees_a_whole_generation(self):
        self.note('Authentication design',DECISION);self.install()
        index,_=self.open()
        try:
            self.note('Messaging',"Kafka broker queue messaging.",allow_similar=True)
            generation=dict(index.db.execute('SELECT key,value FROM meta'))['generation']
            units=index.db.execute('SELECT count(*) FROM unit').fetchone()[0]
            vectors_=index.db.execute('SELECT count(*) FROM unit_knn').fetchone()[0]
        finally:index.close()
        self.assertEqual(units,vectors_);self.assertEqual(generation,'2')

if __name__=='__main__':unittest.main()
