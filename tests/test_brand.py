"""WIKI-BRAND: project and default brands (name and logo) on wiki and dashboard pages, and wiki --project."""
import base64,json
from pathlib import Path
from test_wiki import Fixture
from core.runtime import protected_rule

SVG=b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 8 8'><rect width='8' height='8' fill='#c00'/></svg>"
PNG=b'\x89PNG\r\n\x1a\n'+b'\x00'*32

class Brand(Fixture):
    def file(self,name,data):
        path=self.base/'images'/name;path.parent.mkdir(exist_ok=True);path.write_bytes(data);return str(path)
    def update(self,slug,brand):
        payload=self.base/'payload.json';payload.write_text(json.dumps({'slug':slug,'brand':brand}))
        return self.cli('project','update','--payload-file',str(payload))
    def brand(self,slug,brand):
        code,result=self.update(slug,brand);self.assertEqual(code,0,result);return result
    def default(self,value):return self.ok('config','set','brand',json.dumps(value))
    def entry(self,slug):return next(p for p in json.loads((self.root/'projects.json').read_text(encoding='utf-8'))['projects'] if p['slug']==slug)
    def page(self,*argv):return Path(self.ok('wiki',*argv)['path']).read_text(encoding='utf-8')
    def uri(self,kind,data):return f'data:image/{kind};base64,'+base64.b64encode(data).decode()

    def test_project_brand_names_its_project_page_and_undo_removes_it(self):
        self.sample()
        result=self.brand('hive',{'name':'Hive Docs','logo':self.file('logo.svg',SVG)})
        self.assertEqual(self.entry('hive')['brand'],{'name':'Hive Docs','logo':'brand/project-hive.logo'})
        stored=self.root/'brand'/'project-hive.logo'
        self.assertEqual(stored.read_text(encoding='utf-8').strip(),self.uri('svg+xml',SVG))
        r=self.ok('wiki','--project','hive');self.assertEqual(Path(r['path']),(self.root/'.views'/'demo'/'wiki-hive.html').resolve())
        page=Path(r['path']).read_text(encoding='utf-8')
        self.assertIn('<span>Hive Docs</span>',page);self.assertIn(self.uri('svg+xml',SVG),page);self.assertIn('<title>Hive memory wiki</title>',page)
        page=self.page()   # the domain wiki covers several projects: no project brand
        self.assertIn('<span>Living Memory</span>',page);self.assertNotIn(self.uri('svg+xml',SVG),page)
        self.ok('undo',result['operation_id'])
        self.assertNotIn('brand',self.entry('hive'));self.assertFalse(stored.exists())

    def test_fallbacks_project_name_default_brand_and_emblem(self):
        self.sample()
        self.default({'name':'Acme','logo':self.file('logo.png',PNG)})
        self.assertEqual(self.ok('config','get','brand')['settings']['brand']['value'],{'name':'Acme','logo':'brand/default.logo'})
        self.brand('hive',{})   # no name: the project's own name; no logo: the default logo
        page=self.page('--project','hive');self.assertIn('<span>Hive</span>',page);self.assertIn(self.uri('png',PNG),page)
        page=self.page('--project','lm');self.assertIn('<span>Acme</span>',page)   # a project without a brand shows the default
        self.assertIn('<span>Acme</span>',self.page())
        result=self.default(None);self.assertTrue(result['default']);self.assertFalse((self.root/'brand'/'default.logo').exists())
        self.assertIn('<span>Living Memory</span>',self.page())
        page=self.page('--project','hive');self.assertIn('<span>Hive</span>',page);self.assertNotIn('data:image/png',page)

    def test_logo_null_keeps_the_name_and_a_missing_logo_keeps_the_file(self):
        self.brand('hive',{'name':'Hive Docs','logo':self.file('logo.svg',SVG)})
        self.brand('hive',{'name':'Hive Book'})
        self.assertEqual(self.entry('hive')['brand'],{'name':'Hive Book','logo':'brand/project-hive.logo'})
        self.brand('hive',{'name':'Hive Book','logo':None})
        self.assertEqual(self.entry('hive')['brand'],{'name':'Hive Book'});self.assertFalse((self.root/'brand'/'project-hive.logo').exists())
        self.brand('hive',None);self.assertNotIn('brand',self.entry('hive'))

    def test_bad_brands_are_refused_and_write_nothing(self):
        before=(self.root/'projects.json').read_bytes()
        for brand,text in (({'logo':self.file('fake.png',b'just text')},'not a PNG, JPEG, GIF, WebP or SVG'),
                           ({'logo':self.file('big.svg',b'<svg>'+b' '*(256*1024))},'larger than 256 KB'),
                           ({'logo':str(self.base/'nowhere.png')},'is not a file'),({'name':''},'brand.name must be'),
                           ({'name':'x'*61},'brand.name must be'),({'colour':'red'},'brand takes name and logo'),('Acme','brand takes name and logo')):
            with self.subTest(brand=brand):
                code,err=self.update('hive',brand);self.assertNotEqual(code,0);self.assertIn(text,err['error']['message'])
        self.assertEqual((self.root/'projects.json').read_bytes(),before);self.assertFalse((self.root/'brand').exists())
        code,err=self.cli('config','set','brand',json.dumps({'logo':self.file('logo.svg',SVG)}))
        self.assertNotEqual(code,0);self.assertIn('the default brand needs a name',err['error']['message'])
        code,err=self.cli('config','set','brand',json.dumps({'name':'Acme'}),actor='subagent');self.assertNotEqual(code,0)

    def test_the_same_default_brand_again_writes_nothing(self):
        logo=self.file('logo.svg',SVG)
        self.assertTrue(self.default({'name':'Acme','logo':logo})['operation_id'])
        self.assertIsNone(self.default({'name':'Acme','logo':logo})['operation_id'])

    def test_wiki_project_keeps_one_project(self):
        ids=self.sample()
        model=self.ok('wiki','--project','hive','--format','json')['model']
        self.assertEqual(model['project'],'hive');self.assertEqual([p['slug'] for p in model['projects']],['hive'])
        kept={n['id'] for n in model['notes']};self.assertIn(ids['decision'],kept);self.assertNotIn(ids['both'],kept);self.assertNotIn(ids['general'],kept)
        self.assertTrue(all(n['project']=='hive' and set(n['out'])<=kept and set(n['backlinks'])<=kept for n in model['notes']))
        self.assertTrue(all(f['project']=='hive' for f in model['folders']))
        code,err=self.cli('wiki','--project','nosuch');self.assertNotEqual(code,0);self.assertIn('no notes for project nosuch',err['error']['message'])
        code,err=self.cli('wiki','--project','Bad Slug');self.assertNotEqual(code,0);self.assertIn('--project takes a project slug',err['error']['message'])

    def test_dashboards_follow_the_note_project(self):
        ids=self.sample()
        self.default({'name':'Acme'})
        one=Path(self.ok('views','add',ids['decision'])['path']);two=Path(self.ok('views','add',ids['both'])['path'])
        self.assertIn('<span>Acme</span>',one.read_text(encoding='utf-8'))
        self.brand('hive',{'name':'Hive Docs','logo':self.file('logo.svg',SVG)})   # re-renders the registered pages
        page=one.read_text(encoding='utf-8');self.assertIn('<span>Hive Docs</span>',page);self.assertIn(self.uri('svg+xml',SVG),page)
        self.assertIn('<span>Acme</span>',two.read_text(encoding='utf-8'))   # tagged with two projects: the default brand
        art=Path(self.ok('views','render',ids['decision'],'--target','artifact')['path']).read_text(encoding='utf-8')
        self.assertIn('<span>Hive Docs</span>',art)

    def test_the_brand_folder_is_protected_and_reported(self):
        self.assertEqual(protected_rule(str(self.root/'brand'/'default.logo'),str(self.base),self.root),'config')
        self.assertEqual(self.ok('version')['capabilities']['brand'],1)
