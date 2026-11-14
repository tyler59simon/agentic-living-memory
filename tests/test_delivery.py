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
        self.assertEqual(find_note(a['id'],'demo',self.root)[1]['body'],'Claim one')
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
    def test_parent_and_child_hits_are_labelled(self):
        from core.init_domain import init_domain
        from core.notes import capture
        (self.base/'project'/'pkg').mkdir(parents=True);init_domain('pkg',[str(self.base/'project'/'pkg')],self.root)
        init_domain('sub',[str(self.base/'project'/'pkg'/'sub')],self.root)
        parent=self.note('Orbital parent','Orbital mechanics, parent');kid=capture('Orbital child','Orbital mechanics, child',['subject/project','concern/architecture'],'pkg',self.root)
        script=Path(__file__).resolve().parents[1]/'living_memory.py'
        def search(domain):
            result=subprocess.run([sys.executable,str(script),'search','orbital mechanics','--root',str(self.root),'--domain',domain],text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr);return {h['title']:h['sourceScope'] for h in json.loads(result.stdout)['results']}
        self.assertEqual(search('demo'),{'Orbital parent':'active','Orbital child':'child'})
        self.assertEqual(search('pkg'),{'Orbital parent':'parent','Orbital child':'active'})
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
        self.assertEqual(find_note(a['id'],'demo',self.root)[1]['body'],'Reviewed Windows edit')

class LauncherInstall(StoreFixture):
    """install.py's `alm` launcher and PATH change, in temp folders: nothing here touches the real ~/.local/bin, registry or profile."""
    def setUp(self):
        super().setUp()
        import install
        from core import launcher
        self.install,self.launcher=install,launcher;self.home=self.base/'local'/'agentic-living-memory';self.bin=self.base/'userhome'/'.local'/'bin';self.exe=self.bin/launcher.FILE
        self.old_path=os.environ.get('PATH','')
        patch.multiple(install,INSTALL_HOME=self.home,INSTALL_DIR=self.home/'app',LAUNCHER_DIR=self.bin).start();self.addCleanup(patch.stopall)
        self.on_path(True)
    def on_path(self,yes):
        os.environ['PATH']=(str(self.bin)+os.pathsep if yes else '')+self.old_path;self.addCleanup(os.environ.__setitem__,'PATH',self.old_path)
    def plan(self,uninstall=False):return self.install.plan_install(uninstall)
    def apply(self,proposed):return self.install.apply_plan(self.home,proposed['plan_id'],proposed['digest'])
    def test_create_then_keep(self):
        proposed=self.plan();self.assertEqual(proposed['summary']['launcher']['action'],'create');self.assertNotIn('path_change',proposed['summary'])
        receipt=self.apply(proposed);self.assertEqual(receipt['launcher']['action'],'create');self.assertTrue(self.launcher.ours(self.exe));self.assertFalse(receipt.get('warnings'))
        self.assertEqual(self.plan()['summary']['launcher']['action'],'keep')
        self.assertTrue(self.launcher.active(self.home/'app'/'living_memory.py'))
        shown=subprocess.run([str(self.exe),'--json','version'],capture_output=True,text=True);self.assertEqual(json.loads(shown.stdout)['ok'],True)
    def test_replace_a_launcher_that_differs_from_what_would_be_built(self):
        self.apply(self.plan());self.exe.write_bytes(self.exe.read_bytes()+b'x')   # still ours (marker), but no longer what would be built
        self.assertEqual(self.plan()['summary']['launcher']['action'],'replace')
    def test_foreign_file_is_left_alone(self):
        self.bin.mkdir(parents=True);self.exe.write_bytes(b'foreign program')
        proposed=self.plan();record=proposed['summary']['launcher'];self.assertEqual(record['action'],'none');self.assertIn('not ours',record['reason'])
        self.apply(proposed);self.assertEqual(self.exe.read_bytes(),b'foreign program');self.assertTrue((self.home/'app/living_memory.py').exists())
        self.apply(self.plan(True));self.assertEqual(self.exe.read_bytes(),b'foreign program')   # uninstall keeps it too
    def test_file_changed_between_plan_and_apply_is_stale(self):
        proposed=self.plan();self.bin.mkdir(parents=True);self.exe.write_bytes(b'appeared later')
        with self.assertRaisesRegex(ValueError,'stale'):self.apply(proposed)
        self.assertFalse((self.home/'app/living_memory.py').exists());self.assertEqual(self.exe.read_bytes(),b'appeared later')
    def test_uninstall_removes_ours_and_says_the_path_entry_stays(self):
        self.apply(self.plan());proposed=self.plan(True);self.assertEqual(proposed['summary']['launcher']['action'],'remove');self.assertIn('left in place',proposed['summary']['path'])
        self.apply(proposed);self.assertFalse(self.exe.exists());self.assertFalse((self.home/'app/living_memory.py').exists())
    @unittest.skipUnless(os.name=='nt','Windows builds the launcher from pip\'s stub')
    def test_no_stub_means_no_launcher(self):
        with patch.object(self.launcher,'_stub',return_value=None):record=self.plan()['summary']['launcher']
        self.assertEqual(record['action'],'none');self.assertIn('ensurepip',record['reason'])
    def test_launcher_that_does_not_run_is_removed_with_a_warning(self):
        with patch('subprocess.run',return_value=subprocess.CompletedProcess([],1,b'',b'blocked')):receipt=self.apply(self.plan())
        self.assertFalse(self.exe.exists());self.assertIn('blocked',receipt['warnings'][0]);self.assertEqual(receipt['launcher']['action'],'failed');self.assertNotIn('path_change',receipt)
    @unittest.skipUnless(os.name=='nt','Windows registry')
    def test_windows_path_change_keeps_the_value_type_and_is_idempotent(self):
        import winreg
        store={'v':('C:\\Tools;%USERPROFILE%\\x',winreg.REG_EXPAND_SZ)}
        class Key:
            def __enter__(self):return self
            def __exit__(self,*a):return False
        patches=[patch('winreg.OpenKey',return_value=Key()),patch('winreg.QueryValueEx',side_effect=lambda k,n:store['v']),patch('winreg.SetValueEx',side_effect=lambda k,n,r,kind,v:store.update(v=(v,kind))),patch('ctypes.windll',create=True)]
        for each in patches:each.start();self.addCleanup(each.stop)
        self.on_path(False);proposed=self.plan();change=proposed['summary']['path_change'];self.assertEqual((change['kind'],change['add']),('registry',str(self.bin)))
        receipt=self.apply(proposed);self.assertEqual(store['v'],(f"C:\\Tools;%USERPROFILE%\\x;{self.bin}",winreg.REG_EXPAND_SZ));self.assertIn('new terminal',receipt['notice'])
        self.assertNotIn('path_change',self.plan()['summary'])   # already in the registry value
        store['v']=('C:\\Tools',winreg.REG_EXPAND_SZ);stale=self.plan();store['v']=('C:\\Other',winreg.REG_EXPAND_SZ)
        with self.assertRaisesRegex(ValueError,'stale'):self.apply(stale)
        self.assertEqual(store['v'][0],'C:\\Other');store['v']=('C:\\Tools',winreg.REG_EXPAND_SZ)
        self.apply(self.plan(True));self.assertEqual(store['v'][0],'C:\\Tools')   # uninstall leaves PATH alone
    def test_profile_choice_by_shell(self):
        home=self.base/'h';home.mkdir()
        with patch.object(Path,'home',return_value=home):
            for shell,expected in (('/bin/zsh','.zprofile'),('/bin/bash','.profile'),('/bin/fish','.profile'),('','.profile')):
                with patch.dict(os.environ,{'SHELL':shell}):self.assertEqual(self.install._profile(),home/expected)
            (home/'.bash_profile').write_text('x')
            with patch.dict(os.environ,{'SHELL':'/bin/bash'}):self.assertEqual(self.install._profile(),home/'.bash_profile')
    def test_profile_line_is_appended_once_to_the_end(self):
        profile=self.base/'h'/'.profile';profile.parent.mkdir();profile.write_text('alias x=y')
        change={'kind':'profile','target':str(profile),'before':None,'add':self.install.PROFILE_LINE}
        self.install._add_to_path_posix(change);self.assertEqual(profile.read_text(),'alias x=y\n'+self.install.PROFILE_LINE+'\n')
    @unittest.skipIf(os.name=='nt','POSIX profile file')
    def test_posix_path_change_by_profile_with_stale_check(self):
        home=self.base/'h';home.mkdir();self.on_path(False)
        with patch.object(Path,'home',return_value=home),patch.dict(os.environ,{'SHELL':'/bin/zsh'}):
            proposed=self.plan();change=proposed['summary']['path_change'];self.assertEqual((change['kind'],change['target']),('profile',str(home/'.zprofile')))
            self.apply(proposed);text=(home/'.zprofile').read_text();self.assertEqual(text.count(self.install.PROFILE_LINE),1)
            self.assertNotIn('path_change',self.plan()['summary'])
            (home/'.zprofile').write_text('');stale=self.plan();(home/'.zprofile').write_text('# edited')
            with self.assertRaisesRegex(ValueError,'stale'):self.apply(stale)
            self.apply(self.plan(True));self.assertIn('edited',(home/'.zprofile').read_text())

if __name__=='__main__':unittest.main()
