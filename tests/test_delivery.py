import json,sys,subprocess,unittest,os
from pathlib import Path
from unittest.mock import patch
from test_core import StoreFixture
from core.repository import read_json,inventory,parse,serialize
from core.governance import apply,retag_plan

class Delivery(StoreFixture):
    def through_shells(self,command,action):
        """Run one hook command through PowerShell and through cmd.exe, as Codex may: both must exit 0 and print JSON or nothing."""
        env={**os.environ,'LIVING_MEMORY_ROOT':str(self.root)}
        payload=json.dumps({'cwd':str(self.base/'project'),'session_id':'shell-regression','turn_id':'shell-turn','prompt':'Unicode: λ 漢字'})
        for shell in ('powershell','cmd'):
            invocation=['powershell.exe','-NoProfile','-NonInteractive','-Command',command] if shell=='powershell' else 'cmd.exe /d /s /c "'+command+'"'
            result=subprocess.run(invocation,input=payload,encoding='utf-8',capture_output=True,env=env,timeout=30)
            self.assertEqual(result.returncode,0,(shell,action,command,result.stderr))
            if result.stdout.strip():json.loads(result.stdout)
    @unittest.skipUnless(os.name=='nt','Windows shell launch regression')
    def test_generated_hooks_execute_through_windows_shells(self):
        from integrations.setup import hook_command
        script=Path(__file__).resolve().parents[1]/'hooks/codex_hooks.py'
        for action in ('session-start','user-prompt','subagent-start','pre-tool-use','stop'):
            self.through_shells(hook_command(script,action)['commandWindows'],action)
    @unittest.skipUnless(os.name=='nt','Windows shell launch regression')
    def test_an_interpreter_path_with_spaces_runs_in_both_shells(self):
        """REG-28: no py launcher and an interpreter under a folder with a space (a junction needs no admin rights): its 8.3 short path, else the encoded fallback."""
        from integrations import setup
        if 'WindowsApps' in sys.executable:self.skipTest('a Microsoft Store Python cannot be started through a junction')
        link=self.base/'Program Files'/'Python Link';link.parent.mkdir()
        made=subprocess.run(['cmd.exe','/c','mklink','/J',str(link),str(Path(sys.executable).parent)],capture_output=True)
        if made.returncode:self.skipTest('cannot make a junction here')
        spaced=str(link/Path(sys.executable).name);script=Path(__file__).resolve().parents[1]/'hooks/codex_hooks.py'
        with patch.object(sys,'executable',spaced),patch.object(setup.shutil,'which',return_value=None):
            short=setup._short_path(spaced)
            if setup._plain_path(short):
                command=setup.hook_command(script,'stop')['commandWindows']
                self.assertTrue(command.startswith(short+' -X utf8 '),command);self.assertNotIn('EncodedCommand',command)
                for action in ('user-prompt','stop'):self.through_shells(setup.hook_command(script,action)['commandWindows'],action)
            with patch.object(setup,'_short_path',return_value=None):   # 8.3 names are off on the volume
                for action in ('user-prompt','stop'):
                    command=setup.hook_command(script,action)['commandWindows'];self.assertIn('-EncodedCommand',command);self.through_shells(command,action)
    @unittest.skipUnless(os.name=='nt','Windows command shape')
    def test_hook_command_takes_the_first_program_path_that_is_safe_unquoted(self):
        import base64
        from integrations import setup
        script=Path(__file__).resolve().parents[1]/'hooks/codex_hooks.py';version=f'-{sys.version_info.major}.{sys.version_info.minor}'
        def command(launcher,executable,short):
            with patch.object(setup.shutil,'which',return_value=launcher),patch.object(sys,'executable',executable),patch.object(setup,'_short_path',return_value=short):
                return setup.hook_command(script,'stop')['commandWindows']
        tail=f'-X utf8 "{script}" stop'
        self.assertEqual(command(r'C:\Windows\py.EXE',r'C:\Python\python.exe',None),rf'C:\Windows\py.EXE {version} {tail}')   # the launcher, as before
        self.assertEqual(command(r'C:\Program Files\py.exe',r'C:\Python\python.exe',None),rf'C:\Python\python.exe {tail}')   # no launcher it can use: this interpreter, unquoted
        self.assertEqual(command(None,r'C:\Python\python.exe',None),rf'C:\Python\python.exe {tail}')
        self.assertEqual(command(None,r'C:\Program Files\Python\python.exe',r'C:\PROGRA~1\Python\python.exe'),rf'C:\PROGRA~1\Python\python.exe {tail}')   # its 8.3 short path
        self.assertEqual(command(None,r'C:\Dev & Co\python.exe',r'C:\DEVCO~1\python.exe'),rf'C:\DEVCO~1\python.exe {tail}')   # a character a shell reads is not plain either
        for short in (None,r'C:\Program Files\Python\python.exe'):   # 8.3 names off: the call fails or hands back the long name
            fallback=command(None,r'C:\Program Files\Python\python.exe',short);self.assertTrue(fallback.startswith('powershell.exe -NoProfile -NonInteractive -EncodedCommand '))
            self.assertEqual(base64.b64decode(fallback.rsplit(' ',1)[1]).decode('utf-16-le'),rf'& "C:\Program Files\Python\python.exe" {tail}')
    @unittest.skipUnless(os.name=='nt','Windows command shape')
    def test_codex_setup_warns_about_the_encoded_command_only_when_it_is_used(self):
        from integrations import setup
        def warning(launcher,executable,short):
            with patch.object(setup.shutil,'which',return_value=launcher),patch.object(sys,'executable',executable),patch.object(setup,'_short_path',return_value=short):
                return setup.setup_plan('codex',self.base/'codex')['summary'].get('warning','')
        spaced=r'C:\Program Files\Python\python.exe';text=warning(None,spaced,None)
        self.assertIn('EDR',text);self.assertIn('py launcher',text)
        self.assertEqual(warning(None,r'C:\Python\python.exe',None),'');self.assertEqual(warning(r'C:\Windows\py.EXE',spaced,None),'');self.assertEqual(warning(None,spaced,r'C:\PROGRA~1\Python\python.exe'),'')
    def test_a_script_path_no_quoting_can_protect_is_refused(self):
        from integrations.setup import hook_command
        for bad in ('$env','a"b','back`tick')+(('100%',) if os.name=='nt' else ()):
            with self.subTest(bad=bad),self.assertRaisesRegex(ValueError,'no quoting'):hook_command(self.base/bad/'codex_hooks.py','stop')
    @unittest.skipUnless(os.name=='nt','Windows command shape')
    def test_an_older_encoded_handler_is_still_recognised_and_replaced(self):
        """REG-28: handlers an older build wrote (an encoded PowerShell command) are found by doctor and replaced by the plain form."""
        import base64
        from integrations import setup
        from core import wiring
        script=Path(__file__).resolve().parents[1]/'hooks/codex_hooks.py'
        encoded=base64.b64encode(f'& "C:\\Python\\python.exe" -X utf8 "{script}" stop'.encode('utf-16-le')).decode('ascii')
        handler={'type':'command','command':f'python3 "{script}" stop','commandWindows':'powershell.exe -NoProfile -NonInteractive -EncodedCommand '+encoded}
        self.assertEqual(wiring._script(handler,'codex'),str(script));self.assertTrue(setup._ours(handler,'codex'))
        home=self.base/'codex';home.mkdir();(home/'hooks.json').write_text(json.dumps({'hooks':{'Stop':[{'hooks':[handler]}]}}))
        with patch.object(setup.shutil,'which',return_value=r'C:\Windows\py.EXE'):
            proposed=setup.setup_plan('codex',home);apply(home,proposed['plan_id'],proposed['digest'])
        stops=read_json(home/'hooks.json')['hooks']['Stop'];self.assertEqual(len(stops),1);self.assertNotIn('EncodedCommand',json.dumps(stops))
    def test_needs_rewire_covers_factory_instructions_only_and_antigravity(self):
        from integrations import setup
        from core.wiring import needs_rewire
        package,moved=self.base/'pkg',self.base/'moved'
        for name in ('factory','antigravity'):self.assertIsNone(needs_rewire(name,setup,package))   # never wired
        for name in ('factory','antigravity'):
            home=Path(os.environ[name.upper()+'_HOME'])
            with patch.object(setup,'ROOT',package):wired=setup.setup_plan(name,home);apply(home,wired['plan_id'],wired['digest'])   # Factory: instructions only
            self.assertIs(needs_rewire(name,setup,package),False,name)
            self.assertIs(needs_rewire(name,setup,moved),True,name)   # the block or the handlers name the package path
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
    def managed(self,files):
        """A managed settings folder holding files (relative name -> text), with no registry policy."""
        from integrations import setup
        folder=self.base/'managed';folder.mkdir(exist_ok=True)
        for name,text in files.items():(folder/name).parent.mkdir(parents=True,exist_ok=True);(folder/name).write_text(text,encoding='utf-8')
        return patch.multiple(setup,MANAGED_DIR=folder,_registry_policies=lambda:iter(()))
    def test_claude_setup_warns_when_managed_settings_allow_only_managed_hooks(self):
        from integrations.setup import setup_plan
        for name in ('managed-settings.json','managed-settings.d/50-hooks.json'):
            with self.subTest(name=name),self.managed({name:'{"allowManagedHooksOnly": true}'}):
                warning=setup_plan('claude',self.base/'claude').get('summary',{}).get('warning','')
                self.assertIn('allowManagedHooksOnly',warning);self.assertIn(str(self.base/'managed'/name),warning)
            (self.base/'managed'/name).unlink()
    def test_claude_setup_ignores_managed_settings_without_the_hooks_lock(self):
        from integrations.setup import setup_plan
        for text in ('{"allowManagedHooksOnly": false}','{"model": "x"}','{"allowManagedHooksOnly": "true"}','{not json','[true]'):
            with self.subTest(text=text),self.managed({'managed-settings.json':text}):
                proposed=setup_plan('claude',self.base/'claude');self.assertNotIn('warning',proposed['summary'])
        with self.managed({}):self.assertNotIn('warning',setup_plan('claude',self.base/'claude')['summary'])
    def test_hooks_lock_warning_only_for_claude_setup(self):
        from integrations.setup import setup_plan
        with self.managed({'managed-settings.json':'{"allowManagedHooksOnly": true}'}):
            self.assertNotIn('warning',setup_plan('claude',self.base/'claude',uninstall=True)['summary'])
            self.assertNotIn('warning',setup_plan('codex',self.base/'codex')['summary'])
    def test_setup_main_prints_hooks_lock_warning_to_stderr(self):
        import io
        from contextlib import redirect_stderr,redirect_stdout
        from integrations.setup import main
        out,err=io.StringIO(),io.StringIO()
        with self.managed({'managed-settings.json':'{"allowManagedHooksOnly": true}'}),patch('core.storage.ensure_setup'),redirect_stdout(out),redirect_stderr(err):
            self.assertEqual(main(['claude','--home',str(self.base/'claude')]),0)
        self.assertIn('Warning: allowManagedHooksOnly',err.getvalue());self.assertIn('warning',json.loads(out.getvalue())['summary'])
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
    def test_ownership_record_names_the_installing_python(self):   # REG-4: Hivemind reads it to start ALM
        self.apply(self.plan());record=json.loads((self.home/'app'/'.core-ownership-v1.json').read_text(encoding='utf-8'))
        self.assertEqual(record['interpreter'],sys.executable)
    def test_retrying_an_applied_plan_succeeds(self):   # Codex review: the launcher step already done must not read as stale
        proposed=self.plan();self.apply(proposed);again=self.apply(proposed);self.assertEqual(again['launcher']['action'],'create');self.assertTrue(self.launcher.ours(self.exe))
        removal=self.plan(True);self.apply(removal);self.apply(removal);self.assertFalse(self.exe.exists())
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
    def test_uninstall_refuses_while_a_harness_still_runs_the_package(self):
        from integrations import setup
        from core.governance import apply
        self.apply(self.plan());claude=Path(os.environ['CLAUDE_HOME'])
        with patch.object(setup,'ROOT',self.home/'app'):unwire=setup.setup_plan('claude',claude);apply(claude,unwire['plan_id'],unwire['digest'])
        with self.assertRaises(ValueError) as caught:self.plan(True)
        message=str(caught.exception);self.assertIn(str(claude/'settings.json'),message);self.assertIn('claude --uninstall-plan',message);self.assertTrue(os.name!='nt' or 'unwire it with: & "'+sys.executable+'"' in message,message);self.assertIn(str(self.home/'app'/'integrations'/'setup.py'),message)
        self.assertTrue((self.home/'app/living_memory.py').exists())
        with patch.object(setup,'ROOT',self.home/'app'):removal=setup.setup_plan('claude',claude,uninstall=True);apply(claude,removal['plan_id'],removal['digest'])
        self.apply(self.plan(True));self.assertFalse((self.home/'app/living_memory.py').exists())
    def test_applying_a_saved_uninstall_plan_refuses_once_a_harness_is_wired(self):   # INST-4.1: the check ran at plan time only
        from integrations import setup
        from core.governance import apply
        self.apply(self.plan());removal=self.plan(True);claude=Path(os.environ['CLAUDE_HOME']);custom=self.base/'custom-claude'
        with patch.object(setup,'ROOT',self.home/'app'):wired=setup.setup_plan('claude',claude);apply(claude,wired['plan_id'],wired['digest'])
        with self.assertRaisesRegex(ValueError,'still in use by: claude'):self.apply(removal)
        self.assertTrue((self.home/'app/living_memory.py').exists())
        with patch.object(setup,'ROOT',self.home/'app'):off=setup.setup_plan('claude',claude,uninstall=True);apply(claude,off['plan_id'],off['digest'])
        with patch.object(setup,'ROOT',self.home/'app'):wired=setup.setup_plan('claude',custom);apply(custom,wired['plan_id'],wired['digest'])
        self.apply(removal)   # a custom home nobody named is not checked
        self.assertFalse((self.home/'app/living_memory.py').exists())
    def test_saved_uninstall_plan_remembers_its_home_and_apply_takes_more(self):
        from integrations import setup
        from core.governance import apply
        self.apply(self.plan());custom=self.base/'custom-claude'
        removal=self.install.plan_install(True,[('claude',str(custom))]);self.assertEqual(removal['summary']['homes'],[['claude',str(custom.resolve())]])
        with patch.object(setup,'ROOT',self.home/'app'):wired=setup.setup_plan('claude',custom);apply(custom,wired['plan_id'],wired['digest'])
        with self.assertRaisesRegex(ValueError,'--home'):self.apply(removal)
        other=self.base/'other-claude';two=self.plan(True)
        with patch.object(setup,'ROOT',self.home/'app'):wired=setup.setup_plan('claude',other);apply(other,wired['plan_id'],wired['digest'])
        with self.assertRaisesRegex(ValueError,'--home'):self.install.apply_plan(self.home,two['plan_id'],two['digest'],[('claude',str(other))])
        self.assertTrue((self.home/'app/living_memory.py').exists())
    def test_saved_uninstall_plan_stores_a_relative_home_as_an_absolute_path(self):   # INST-4.1b
        from integrations import setup
        from core.governance import apply
        self.apply(self.plan());here=os.getcwd();elsewhere=self.base/'elsewhere';elsewhere.mkdir()
        os.chdir(self.base);self.addCleanup(os.chdir,here)
        removal=self.install.plan_install(True,[('claude','claude-local')]);self.assertEqual(removal['summary']['homes'],[['claude',str((self.base/'claude-local').resolve())]])
        with patch.object(setup,'ROOT',self.home/'app'):wired=setup.setup_plan('claude',self.base/'claude-local');apply(self.base/'claude-local',wired['plan_id'],wired['digest'])
        os.chdir(elsewhere)
        with self.assertRaisesRegex(ValueError,'--home'):self.apply(removal)
        self.assertTrue((self.home/'app/living_memory.py').exists())
    def test_with_claude_config_dir_set_uninstall_still_checks_the_default_claude_folder(self):   # CODEX-R4A
        from integrations import setup
        from core.governance import apply
        self.apply(self.plan());user=self.base/'user';default=user/'.claude';chosen=self.base/'claude-config'
        with patch.object(setup,'ROOT',self.home/'app'):wired=setup.setup_plan('claude',default);apply(default,wired['plan_id'],wired['digest'])
        with patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':str(chosen)}),patch('pathlib.Path.home',return_value=user):
            os.environ.pop('CLAUDE_HOME')
            with self.assertRaisesRegex(ValueError,'still in use by: claude'):self.plan(True)
        self.assertTrue((self.home/'app/living_memory.py').exists())
    def test_a_saved_uninstall_plan_rechecks_the_claude_folder_it_was_planned_for(self):   # CODEX-R4B
        from integrations import setup
        from core.governance import apply
        self.apply(self.plan());chosen=self.base/'claude-config'
        with patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':str(chosen)}),patch('pathlib.Path.home',return_value=self.base/'user'):
            os.environ.pop('CLAUDE_HOME');removal=self.plan(True)
        with patch.object(setup,'ROOT',self.home/'app'):wired=setup.setup_plan('claude',chosen);apply(chosen,wired['plan_id'],wired['digest'])
        with self.assertRaisesRegex(ValueError,'still in use by: claude'):self.apply(removal)   # applied where CLAUDE_HOME names another folder
        self.assertTrue((self.home/'app/living_memory.py').exists())
    def test_a_tampered_uninstall_plan_is_rejected_before_any_harness_is_probed(self):   # NEW-1
        self.apply(self.plan());removal=self.plan(True);path=self.home/'.plans'/(removal['plan_id']+'.json');value=json.loads(path.read_text())
        value['summary']['homes']=[['claude',str(self.base/'anywhere')]];path.write_text(json.dumps(value))
        with patch.object(self.install,'legacy_references') as scan,patch.object(self.install,'harness_configs') as configs:
            with self.assertRaises(Exception):self.apply(removal)
        scan.assert_not_called();configs.assert_not_called();self.assertTrue((self.home/'app/living_memory.py').exists())
    def test_uninstall_checks_a_custom_home_given_with_home(self):
        from integrations import setup
        from core.governance import apply
        self.apply(self.plan());custom=self.base/'custom-claude'
        with patch.object(setup,'ROOT',self.home/'app'):wired=setup.setup_plan('claude',custom);apply(custom,wired['plan_id'],wired['digest'])
        self.plan(True)   # the default homes are clean
        with self.assertRaisesRegex(ValueError,'--home'):self.install.plan_install(True,[('claude',str(custom))])
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
    def test_launcher_removed_by_antivirus_during_the_probe_is_still_a_warning(self):   # REG-20
        def quarantined(*a,**k):
            self.exe.unlink();return subprocess.CompletedProcess([],1,b'',b'quarantined')
        with patch('subprocess.run',side_effect=quarantined):receipt=self.apply(self.plan())
        self.assertFalse(self.exe.exists());self.assertIn('quarantined',receipt['warnings'][0]);self.assertEqual(receipt['launcher']['action'],'failed')
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
        self.apply(proposed);self.assertEqual(store['v'][0],f"C:\\Tools;%USERPROFILE%\\x;{self.bin}")   # a retry finds the entry written and adds it once
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
    def run_main(self,*argv):
        import contextlib,io
        out,err=io.StringIO(),io.StringIO()
        with patch('core.storage.ensure_setup'),patch('sys.argv',['install.py',*argv]),contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):self.install.main()
        return out.getvalue(),err.getvalue()
    def test_install_prints_paste_ready_next_steps_on_stderr_and_keeps_stdout_json(self):
        (self.base/'home-claude').mkdir();lead='& ' if os.name=='nt' else ''
        proposed=json.loads(self.run_main('--plan')[0]);steps=proposed['summary']['next_steps'];self.assertTrue(steps)
        out,err=self.run_main('--apply-plan',proposed['plan_id'],'--expected-plan-sha256',proposed['digest'])
        self.assertIn('operation_id',json.loads(out));self.assertNotIn('Installed',out)   # stdout is still the one JSON result
        for step in steps:self.assertIn('  '+lead+step,err)
        self.assertIn(f'{lead}"{sys.executable}" "',err)
        out,err=self.run_main();json.loads(out);self.assertNotIn('claude --hooks',err)   # plan and apply in one run; an update leaves out a harness never wired
    def harness(self,name):return Path(os.environ[name.upper()+'_HOME'])
    def wire(self,name,hooks=True):
        from integrations import setup
        from core.governance import apply
        with patch.object(setup,'ROOT',self.home/'app'):wired=setup.setup_plan(name,self.harness(name),hooks);apply(self.harness(name),wired['plan_id'],wired['digest'])
    def test_an_update_lists_only_harnesses_whose_wiring_would_change(self):
        named=lambda steps:[step.rsplit(' ',2)[1] for step in steps]
        for name in ('claude','codex','copilot'):self.harness(name).mkdir(parents=True,exist_ok=True)
        first=self.plan()['summary'];self.assertEqual(named(first['next_steps']),['claude','codex','copilot']);self.assertEqual(first['wiring_current'],[])   # a first install lists every harness found
        self.apply(self.plan());self.wire('claude');self.wire('codex')
        summary=self.plan()['summary'];self.assertEqual(summary['next_steps'],[]);self.assertEqual(summary['wiring_current'],['claude','codex'])   # copilot was never wired: left out
        out,err=self.run_main();self.assertIn('Harness wiring is current (claude, codex); nothing to rerun.',err)
        rules=self.harness('claude')/'CLAUDE.md';rules.write_text(rules.read_text(encoding='utf-8').replace('never edit memory files yourself','never edit memory files',1),encoding='utf-8')   # an older block
        hooks=self.harness('codex')/'hooks.json';config=read_json(hooks);config['hooks']['Stop'][0]['hooks'][0]['timeout']=99;hooks.write_text(json.dumps(config))   # an older handler
        summary=self.plan()['summary'];self.assertEqual(named(summary['next_steps']),['claude','codex']);self.assertEqual(summary['wiring_current'],[])
        out,err=self.run_main();self.assertIn('claude --hooks',err);self.assertIn('codex --hooks',err)
    def stamped_source(self):
        source=self.base/'src';(source/'core').mkdir(parents=True);(source/'core/a.py').write_text('a')
        def git(*a):return subprocess.run(['git','-c','user.name=t','-c','user.email=t@t','-c','commit.gpgsign=false','-C',str(source),*a],capture_output=True,text=True,check=True).stdout.strip()
        git('init','-q');git('add','.');git('commit','-qm','one');return source,git
    def doctor(self):
        import contextlib,io
        from core.cli import main
        out=io.StringIO()
        with contextlib.redirect_stdout(out):main(['--root',str(self.root),'doctor'])
        return json.loads(out.getvalue())
    def test_install_stamps_the_source_commit_and_doctor_warns_when_head_moves(self):
        from core import installer_lifecycle as lifecycle
        source,git=self.stamped_source();first=git('rev-parse','HEAD')
        with patch.object(self.install,'SOURCE',source):self.apply(self.plan())
        stamp=lifecycle.installed_stamp(self.home/'app');self.assertEqual((stamp['commit'],stamp['dirty'],stamp['source']),(first,False,str(source)))
        self.assertIsNone(lifecycle.source_warning(stamp))
        (source/'core/a.py').write_text('b')
        with patch.object(self.install,'SOURCE',source):self.apply(self.plan())
        self.assertTrue(lifecycle.installed_stamp(self.home/'app')['dirty'])   # installed from an uncommitted tree
        git('commit','-qam','two');moved=lifecycle.source_warning(lifecycle.installed_stamp(self.home/'app'))
        self.assertIn('newer than the installed copy',moved);self.assertIn(first[:12],moved)
        with patch.object(lifecycle,'installed_stamp',return_value=lifecycle.installed_stamp(self.home/'app')):
            shown=self.doctor();self.assertEqual(shown['install']['commit'],first);self.assertEqual(shown['release'],lifecycle.CORE_RELEASE)
            self.assertTrue(any(w.startswith('install: ') for w in shown['warnings']),shown['warnings'])
    def test_a_failed_git_status_leaves_dirty_unknown(self):   # INST-5.1
        from core import installer_lifecycle as lifecycle
        source,_=self.stamped_source();real=lifecycle._git
        with patch.object(lifecycle,'_git',side_effect=lambda root,*a:None if a[0]=='status' else real(root,*a)):stamp=lifecycle.source_stamp(source)
        self.assertEqual(set(stamp),{'commit','dirty'});self.assertIsNone(stamp['dirty']);self.assertIs(lifecycle.source_stamp(source)['dirty'],False)
    def test_stamp_needs_git_and_a_checkout(self):
        from core import installer_lifecycle as lifecycle
        source,_=self.stamped_source()
        with patch('subprocess.run',side_effect=FileNotFoundError('git')):
            self.assertEqual(lifecycle.source_stamp(source),{});self.assertIsNone(lifecycle.source_warning({'commit':'a'*40,'source':str(source)}))
            self.home.mkdir(parents=True)
            with patch.object(self.install,'SOURCE',source):self.assertNotIn('sourceCommit',self.install._package_changes(self.home/'app',False)['app/.core-ownership-v1.json'])
        inside=source/'core';self.assertEqual(lifecycle.source_stamp(inside),{})   # a folder inside someone else's checkout is not a checkout of its own
    def test_version_names_the_release_and_the_install(self):
        import contextlib,io
        from core.cli import main
        from core import installer_lifecycle as lifecycle
        out=io.StringIO()
        with contextlib.redirect_stdout(out):main(['--root',str(self.root),'version'])
        shown=json.loads(out.getvalue());self.assertEqual(shown['release'],lifecycle.CORE_RELEASE);self.assertIn('install',shown)

if __name__=='__main__':unittest.main()
