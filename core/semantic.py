"""Optional semantic search with a model2vec static model, read with the standard library only.

A static model is a WordPiece vocabulary plus one embedding row per word piece, distilled from a larger
sentence model. A text's vector is the normalized mean of its pieces' rows, so nothing runs but lookups and
arithmetic: no pip dependency and no network after the one explicit `semantic install`. The model lives in
`<root>/.models/<name>/`; it is a download, not canonical data, and backups skip it.
"""
from __future__ import annotations
import array,hashlib,json,math,mmap,os,shutil,struct,tempfile,unicodedata,urllib.request
from pathlib import Path

# The eval flips this to compare orderings; with no model installed it changes nothing.
USE_SEMANTIC=True
FOLDER='.models'
MARKER='verified.json'
PARTIAL='.partial-'
MAX_TOKENS=512
# Pinned to a Hugging Face commit and the bytes it served. min_similarity: the cosine at which a note found only by
# meaning is shown on a prompt. Chosen 2026-11-06 by an eval bake-off against potion-base-8M: both reached hit@3 1.0
# (lexical 0.909), but unrelated prompts scored at most 0.30 here and 0.46 with the smaller model.
MODELS={
    'potion-retrieval-32M':{'repo':'minishlab/potion-retrieval-32M','revision':'6fc8051fab2a1e0ee76689cf08c853792ac285e7','min_similarity':0.45,
        'files':{'config.json':'63c00d90824c832c04ec1d02b6a983fb90489bf049f29fbff15ba481b8a432ee',
                 'tokenizer.json':'7d75cbc54318138807c401b0f0c9721117c628b39de8e8e0edb6cb17e0ee7d18',
                 'model.safetensors':'07609e5bd33aad37900b3fd62f4ec96f6daec88ca4d46b9d8b928bfababf6ea0'}},
}
DEFAULT_MODEL='potion-retrieval-32M'
_DTYPES={'F32':('f',4),'F16':('e',2)}

def _punctuation(ch):
    code=ord(ch)
    return 33<=code<=47 or 58<=code<=64 or 91<=code<=96 or 123<=code<=126 or unicodedata.category(ch).startswith('P')

def _cjk(code):
    return any(a<=code<=b for a,b in ((0x4E00,0x9FFF),(0x3400,0x4DBF),(0x20000,0x2A6DF),(0x2A700,0x2B73F),(0x2B740,0x2B81F),(0x2B820,0x2CEAF),(0xF900,0xFAFF),(0x2F800,0x2FA1F)))

class Model:
    """A loaded static model: BERT normalization and pre-tokenization, greedy WordPiece, rows read on demand."""
    def __init__(self,folder):
        folder=Path(folder);spec=json.loads((folder/'tokenizer.json').read_text(encoding='utf-8'))
        model,norm,pre=spec.get('model') or {},spec.get('normalizer') or {},spec.get('pre_tokenizer') or {}
        if model.get('type')!='WordPiece' or norm.get('type')!='BertNormalizer' or pre.get('type')!='BertPreTokenizer':
            raise ValueError('semantic_model_unsupported: only WordPiece tokenizers with BERT normalization are read')
        self.name=folder.name;self.vocab=model['vocab'];self.unk=self.vocab.get(model.get('unk_token','[UNK]'))
        self.prefix=model.get('continuing_subword_prefix','##');self.max_chars=model.get('max_input_chars_per_word',100)
        self.lower=norm.get('lowercase',True);accents=norm.get('strip_accents')
        self.strip=self.lower if accents is None else accents;self.clean=norm.get('clean_text',True);self.chinese=norm.get('handle_chinese_chars',True)
        with open(folder/'model.safetensors','rb') as handle:self._map=mmap.mmap(handle.fileno(),0,access=mmap.ACCESS_READ)
        size=struct.unpack_from('<Q',self._map,0)[0];header=json.loads(self._map[8:8+size])
        tensors=[v for k,v in header.items() if k!='__metadata__']
        if len(tensors)!=1 or tensors[0].get('dtype') not in _DTYPES or len(tensors[0].get('shape',[]))!=2:
            raise ValueError('semantic_model_unsupported: expected one F32 or F16 embedding matrix')
        tensor=tensors[0];self.rows,self.dim=tensor['shape'];self._code,width=_DTYPES[tensor['dtype']]
        self._row_bytes=self.dim*width;self._base=8+size+tensor['data_offsets'][0]
        if tensor['data_offsets'][1]-tensor['data_offsets'][0]!=self.rows*self._row_bytes or 8+size+tensor['data_offsets'][1]>len(self._map):
            raise ValueError('semantic_model_corrupt: embedding matrix size does not match its header')
        if max(self.vocab.values(),default=-1)>=self.rows:raise ValueError('semantic_model_corrupt: vocabulary is larger than the embedding matrix')
        self._format='<'+str(self.dim)+self._code;self._cache={}

    def close(self):self._map.close()

    def _normalize(self,text):
        out=[]
        for ch in text:
            code=ord(ch)
            if self.clean:
                if code in (0,0xFFFD) or (unicodedata.category(ch) in ('Cc','Cf') and ch not in '\t\n\r'):continue
                if ch.isspace():ch=' '
            if self.chinese and _cjk(code):ch=' '+ch+' '
            out.append(ch)
        text=''.join(out)
        if self.lower:text=text.lower()
        if self.strip:text=''.join(c for c in unicodedata.normalize('NFD',text) if unicodedata.category(c)!='Mn')
        return text

    def _words(self,text):
        words=[]
        for chunk in text.split():
            current=''
            for ch in chunk:
                if _punctuation(ch):
                    if current:words.append(current);current=''
                    words.append(ch)
                else:current+=ch
            if current:words.append(current)
        return words

    def tokenize(self,text):
        """Vocabulary ids, as `tokenizers` gives them without special tokens, truncated, unknown pieces dropped."""
        ids=[]
        for word in self._words(self._normalize(text)):
            if len(ids)>=MAX_TOKENS:break
            if len(word)>self.max_chars:ids.append(self.unk);continue
            pieces=[];start=0
            while start<len(word):
                end=len(word);found=None
                while start<end:
                    piece=word[start:end] if not start else self.prefix+word[start:end]
                    if piece in self.vocab:found=self.vocab[piece];break
                    end-=1
                if found is None:pieces=[self.unk];break
                pieces.append(found);start=end
            ids.extend(pieces)
        return [i for i in ids[:MAX_TOKENS] if i is not None and i!=self.unk]

    def row(self,index):
        if index not in self._cache:self._cache[index]=struct.unpack_from(self._format,self._map,self._base+index*self._row_bytes)
        return self._cache[index]

    def embed(self,text):
        """The unit-length mean of the text's piece rows, or None when no piece is known."""
        ids=self.tokenize(text)
        if not ids:return None
        total=[0.0]*self.dim
        for index in ids:
            for k,value in enumerate(self.row(index)):total[k]+=value
        norm=math.sqrt(sum(v*v for v in total))
        return array.array('f',(v/norm for v in total)) if norm else None

def to_blob(vector):return vector.tobytes()
def from_blob(blob):
    vector=array.array('f');vector.frombytes(blob);return vector
def cosine(a,b):return sum(x*y for x,y in zip(a,b))

def models_dir(root):return Path(root)/FOLDER

def installed(root):
    """The name of the verified model in this store, or None. Cheap: reads one marker, loads nothing."""
    if not USE_SEMANTIC:return None
    folder=models_dir(root)
    for name in MODELS:
        if (folder/name/MARKER).is_file():return name
    return None

_loaded={};_errors={}
def load(root):
    """The installed model, loaded once per process; None when absent or unreadable (the error is kept for status)."""
    name=installed(root)
    if not name:return None
    folder=str((models_dir(root)/name).resolve())
    if folder not in _loaded:
        try:_loaded[folder]=Model(folder);_errors.pop(folder,None)
        except (OSError,ValueError,KeyError,TypeError,struct.error) as error:_loaded[folder]=None;_errors[folder]=str(error)
    return _loaded[folder]

def _unload():
    """Close every mapped model file (Windows cannot delete a mapped file) and forget it."""
    for model in _loaded.values():
        if model:model.close()
    _loaded.clear();_errors.clear()

def threshold(root):
    name=installed(root)
    return MODELS[name]['min_similarity'] if name else None

def _sha256(path):
    digest=hashlib.sha256()
    with open(path,'rb') as handle:
        for block in iter(lambda:handle.read(1<<20),b''):digest.update(block)
    return digest.hexdigest()

def _fetch(url,target):
    with urllib.request.urlopen(url,timeout=60) as response,open(target,'wb') as out:shutil.copyfileobj(response,out,1<<20)

def rebuild_all(root):
    from .search import rebuild_index
    rebuilt=[]
    for directory in sorted((Path(root)/'domains').iterdir()) if (Path(root)/'domains').is_dir() else []:
        if (directory/'notes').is_dir():rebuild_index(directory.name,root);rebuilt.append(directory.name)
    return rebuilt

def install(root,name=DEFAULT_MODEL,source=None):
    """Download (or copy from a local folder) the pinned model files, verify every SHA-256, then rebuild indexes."""
    if name not in MODELS:raise ValueError(f'unknown_model: choose one of {", ".join(MODELS)}')
    spec=MODELS[name];folder=models_dir(root);folder.mkdir(parents=True,exist_ok=True)
    partial=Path(tempfile.mkdtemp(prefix=PARTIAL,dir=folder))   # one per installer, so two cannot share it
    try:
        for filename,expected in spec['files'].items():
            target=partial/filename
            if source:shutil.copyfile(Path(source)/filename,target)
            else:_fetch(f"https://huggingface.co/{spec['repo']}/resolve/{spec['revision']}/{filename}",target)
            if _sha256(target)!=expected:raise ValueError(f'semantic_model_hash_mismatch: {filename} does not match the pinned SHA-256')
        Model(partial).close()   # refuse a model this reader cannot use before it replaces anything
        _unload()
        for other in folder.iterdir():
            if other.is_dir() and not other.name.startswith(PARTIAL):_retire(other)
        if (folder/name).exists():
            raise ValueError('semantic_model_in_use: another Living Memory process still has the old model open; search is lexical until install is retried')
        (partial/MARKER).write_text(json.dumps({'model':name,'revision':spec['revision'],'files':spec['files']},indent=1),encoding='utf-8')
        os.replace(partial,folder/name)
    finally:shutil.rmtree(partial,ignore_errors=True)
    rebuilt=rebuild_all(root)
    return {**status(root),'rebuilt':rebuilt}

def _retire(folder):
    """Make a model inert first (readers need its marker), then delete what can be deleted. On Windows a file another
    process has mapped cannot be deleted; that leftover has no marker, so nothing uses it, and a later remove clears it."""
    (folder/MARKER).unlink(missing_ok=True)
    shutil.rmtree(folder,ignore_errors=True)

def remove(root):
    """Delete the downloaded model; indexes are rebuilt lexical-only."""
    folder=models_dir(root);existed=folder.is_dir()
    _unload()
    if existed:
        for other in folder.iterdir():
            if other.is_dir():_retire(other)
        try:folder.rmdir()
        except OSError:pass
    rebuilt=rebuild_all(root)
    result={**status(root),'removed':existed,'rebuilt':rebuilt}
    if folder.is_dir() and any(folder.iterdir()):result['leftover']=sorted(p.name for p in folder.iterdir())
    return result

def status(root):
    import sqlite3
    name=installed(root);model=load(root) if name else None
    folder=models_dir(root)/name if name else None
    indexes={}
    for directory in sorted((Path(root)/'domains').iterdir()) if (Path(root)/'domains').is_dir() else []:
        db_path=directory/'search.sqlite3'
        if not db_path.is_file():continue
        try:
            db=sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True)
            try:indexes[directory.name]='vectors' if db.execute("SELECT 1 FROM sqlite_master WHERE name='vectors'").fetchone() else 'lexical'
            finally:db.close()
        except sqlite3.Error:indexes[directory.name]='unreadable'
    result={'enabled':bool(model),'model':name,'available':list(MODELS),'indexes':indexes}
    if folder:result['size_bytes']=sum(p.stat().st_size for p in folder.iterdir() if p.is_file())
    if name and not model:result['error']=_errors.get(str(folder.resolve()),'unreadable')
    return result
