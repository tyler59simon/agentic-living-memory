import json,sys,subprocess,unittest,os
from pathlib import Path
from unittest.mock import patch
from test_core import StoreFixture
from core.repository import read_json,inventory,parse,serialize
from core.governance import apply,retag_plan

class Delivery(StoreFixture):
    @unittest.skipUnless(os.name=='nt','Windows shell launch regression')
    def test_generated_hooks_execute_through_windows_shells(self):
        from integrations.setup import hook_command
        script=Path(__file__).resolve().parents[1]/'hooks/codex_hooks.py'
        env={**os.environ,'LIVING_MEMORY_ROOT':str(self.root)}
        for action in ('session-start','user-prompt','subagent-start','pre-tool-use','stop'):
            command=hook_command(script,action)['commandWindows']
            payload=json.dumps({'cwd':str(self.base/'project'),'session_id':'shell-regression','turn_id':'shell-turn','prompt':'Unicode: λ 漢字'})
            for shell in ('powershell','cmd'):
                invocation=['powershell.exe','-NoProfile','-NonInteractive','-Command',command] if shell=='powershell' else 'cmd.exe /d /s /c "'+command+'"'
                result=subprocess.run(invocation,input=payload,encoding='utf-8',capture_output=True,env=env,timeout=30)
                self.assertEqual(result.returncode,0,(shell,action,result.stderr))
                if result.stdout.strip():json.loads(result.stdout)
    def test_package_install_rerun_and_uninstall(self):
        import install
        home=self.base/'local'/'agentic-living-memory'
        with patch.multiple(install,INSTALL_HOME=home,INSTALL_DIR=home/'app'):
            proposed=install.plan_install();self.assertIn('next_steps',proposed['summary']);apply(home,proposed['plan_id'],proposed['digest'])
            self.assertTrue((home/'app/living_memory.py').exists());self.assertTrue((home/'app/.core-ownership-v1.json').exists())
            self.assertFalse((home/'app/skills').exists())
            proposed=install.plan_install();apply(home,proposed['plan_id'],proposed['digest'])   # rerun keeps, never duplicates
            proposed=install.plan_install(True);apply(home,proposed['plan_id'],proposed['digest'])
        self.assertFalse((home/'app/living_memory.py').exists());self.assertFalse((home/'app/.core-ownership-v1.json').exists())
    def test_codex_setup_and_uninstall_preserve_other_hooks(self):
        from integrations.setup import setup_plan
        from core.instructions import instruction_block
        home=self.base/'codex';home.mkdir();hooks=home/'hooks.json'
        original={'hooks':{'Stop':[{'hooks':[{'type':'command','command':'other.exe'}]}]},'custom':True}
        hooks.write_text(json.dumps(original));(home/'AGENTS.md').write_text('# my codex rules\n',encoding='utf-8')
        proposed=setup_plan('codex',home);apply(home,proposed['plan_id'],proposed['digest'])
        config=read_json(hooks);self.assertEqual(len(config['hooks']['Stop']),2);self.assertTrue(config['custom'])
        self.assertEqual(config['hooks']['SessionStart'][0]['hooks'][0]['additionalContextLimit'],4000)
        agents=(home/'AGENTS.md').read_text(encoding='utf-8')
        self.assertTrue(agents.startswith('# my codex rules\n'));self.assertIn(instruction_block('codex'),agents)
        proposed=setup_plan('codex',home);apply(home,proposed['plan_id'],proposed['digest'])   # rerun replaces, never duplicates
        self.assertEqual(len(read_json(hooks)['hooks']['Stop']),2)
        self.assertEqual((home/'AGENTS.md').read_text(encoding='utf-8').count('agentic-living-memory:begin'),1)
        proposed=setup_plan('codex',home,uninstall=True);apply(home,proposed['plan_id'],proposed['digest'])
        self.assertEqual(read_json(hooks)['hooks'],original['hooks']);self.assertTrue(read_json(hooks)['custom'])
        self.assertEqual((home/'AGENTS.md').read_text(encoding='utf-8').strip(),'# my codex rules')
        self.assertFalse((home/'.living-memory-ownership.json').exists())
    def test_adapter_rerun_preserves_later_unrelated_changes(self):
        from integrations.setup import setup_plan
        home=self.base/'claude';proposed=setup_plan('claude',home);apply(home,proposed['plan_id'],proposed['digest'])
        settings=read_json(home/'settings.json');settings['theme']='light';settings['hooks']['Other']=[{'hooks':[{'command':'other'}]}]
        (home/'settings.json').write_text(json.dumps(settings))
        proposed=setup_plan('claude',home);apply(home,proposed['plan_id'],proposed['digest'])
        self.assertEqual(read_json(home/'settings.json')['hooks']['Other'],settings['hooks']['Other'])
    def test_safe_write_requires_approved_plan(self):
        from core.safe_write import safe_write
        from core.notes import find_note
        a=self.note();path,note=find_note(a['id'],'demo',self.root);text=path.read_text().replace('rebuildable','replaceable')
        proposed=safe_write('demo','notes/'+path.name,text,self.root,note['revision'])
        self.assertIn('rebuildable',path.read_text());apply(self.root,proposed['plan_id'],proposed['digest']);self.assertIn('replaceable',path.read_text())
    def test_migration_preserves_canonical_bytes(self):
        from core.migration import migration_plan
        a=self.note();before=Path(a['path']).read_bytes();proposed=migration_plan(self.root);apply(self.root,proposed['plan_id'],proposed['digest'])
        self.assertEqual(Path(a['path']).read_bytes(),before)
    def test_retag_does_not_erase_notes(self):
        a=self.note();proposed=retag_plan(self.root,{'concern/architecture':'concern/decision'});apply(self.root,proposed['plan_id'],proposed['digest'])
        self.assertIn('concern/decision',inventory('demo',self.root)[0]['tags'])
        self.assertEqual(inventory('demo',self.root)[0]['id'],a['id'])
    def test_bug_lifecycle_moves_and_reopens(self):
        from core.notes import change,find_note
        a=self.note(type='bug');proposed=change(a['id'],'demo','resolve',self.root,plan_only=True);apply(self.root,proposed['plan_id'],proposed['digest'])
        path,note=find_note(a['id'],'demo',self.root);self.assertEqual(path.parent.name,'archive');self.assertEqual(note['prefix'],'BUG');self.assertEqual(note['type'],'bug');self.assertEqual(note['status'],'resolved')
        proposed=change(a['id'],'demo','reopen',self.root,plan_only=True);apply(self.root,proposed['plan_id'],proposed['digest'])
        self.assertEqual(find_note(a['id'],'demo',self.root)[0].parent.name,'notes')
    def test_merge_retains_ids_and_supersession(self):
        from core.governance import merge_plan
        from core.notes import find_note
        a=self.note('First','Claim one');b=self.note('Second','Claim two')
        proposal=merge_plan(self.root,{'target':'demo:'+a['id'],'sources':['demo:'+b['id']],'body':'Combined reviewed claim'})
        self.assertEqual(find_note(a['id'],'demo',self.root)[1]['body'],'## 📌 Executive Summary\nClaim one')
        apply(self.root,proposal['plan_id'],proposal['digest'])
        self.assertEqual(find_note(b['id'],'demo',self.root)[1]['superseded_by'],a['id'])
        self.assertEqual(len(inventory('demo',self.root)),1)
    def test_installer_restarts_after_interruption(self):
        from core import installer_lifecycle as lifecycle,repository
        source=self.base/'source';(source/'core').mkdir(parents=True);(source/'core/a.py').write_text('a');(source/'core/b.py').write_text('b');target=self.base/'package'
        proposal=lifecycle.build_plan(target,source);real=repository._write;counter=[0]
        def crash(path,data):
            counter[0]+=1
            if counter[0]==2:raise OSError('interruption')
            real(path,data)
        with patch.object(repository,'_write',crash):
            with self.assertRaises(OSError):lifecycle.apply_plan(proposal,proposal['planSha256'])
        receipt=lifecycle.apply_plan(proposal,proposal['planSha256']);self.assertEqual(receipt['state'],'committed');self.assertEqual((target/'core/b.py').read_text(),'b')
    def test_scoped_fallback_and_all_labels(self):
        from core.init_domain import init_domain
        from core.notes import capture
        init_domain('foreign',[str(self.base/'other')],self.root)
        config=read_json(self.root/'bindings.json');config['search_scope']['foreign_domains']=['foreign'];(self.root/'bindings.json').write_text(json.dumps(config))
        a=self.note();b=capture('Orbital','Orbital mechanics',['subject/project','concern/architecture'],'foreign',self.root)
        script=Path(__file__).resolve().parents[1]/'living_memory.py'
        def search(query,scope='fallback'):
            result=subprocess.run([sys.executable,str(script),'search',query,'--scope',scope,'--root',str(self.root),'--domain','demo'],text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr);return json.loads(result.stdout)
        self.assertFalse(search('cache')['fallback']);self.assertTrue(search('orbital')['fallback'])
        self.assertEqual(search('orbital','all')['results'][0]['sourceScope'],'foreign')
        self.assertEqual(search('orbital','active')['results'],[])
    def test_filtered_dirty_search_requires_rebuild(self):
        from core.search import search_domain
        a=self.note();path=Path(a['path']);path.write_text(path.read_text().replace('rebuildable','newkeyword'))
        self.assertTrue(search_domain('newkeyword','demo',root=self.root))
        with self.assertRaisesRegex(ValueError,'stale_filtered'):search_domain('newkeyword','demo',root=self.root,filters={'status':'active'})
        self.assertTrue(search_domain('newkeyword','demo',root=self.root,filters={'status':'active'},rebuild=True))
    def test_windows_note_revision_uses_exact_bytes(self):
        import hashlib
        from core.notes import find_note,change
        a=self.note();path=Path(a['path']);path.write_bytes(path.read_bytes().replace(b'\n',b'\r\n'));note=parse(path)
        self.assertEqual(note['revision'],hashlib.sha256(path.read_bytes()).hexdigest())
        change(a['id'],'demo','update',self.root,expected_revision=note['revision'],body='Reviewed Windows edit')
        self.assertEqual(find_note(a['id'],'demo',self.root)[1]['body'],'## 📌 Executive Summary\nReviewed Windows edit')

if __name__=='__main__':unittest.main()
