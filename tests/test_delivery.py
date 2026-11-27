import json,sys,subprocess,unittest,os
from pathlib import Path
from unittest.mock import patch
from test_core import StoreFixture
from core.repository import read_json,inventory,parse,serialize
from core.governance import retag,undo

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
                return setup.setup('codex',self.base/'codex')['summary'].get('warning','')
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
            setup.setup('codex',home)
        stops=read_json(home/'hooks.json')['hooks']['Stop'];self.assertEqual(len(stops),1);self.assertNotIn('EncodedCommand',json.dumps(stops))
    def test_needs_rewire_covers_factory_instructions_only_and_antigravity(self):
        from integrations import setup
        from core.wiring import needs_rewire
        package,moved=self.base/'pkg',self.base/'moved'
        for name in ('factory','antigravity'):self.assertIsNone(needs_rewire(name,setup,package))   # never wired
        for name in ('factory','antigravity'):
            home=Path(os.environ[name.upper()+'_HOME'])
            with patch.object(setup,'ROOT',package):setup.setup(name,home)   # Factory: instructions only
            self.assertIs(needs_rewire(name,setup,package),False,name)
            self.assertIs(needs_rewire(name,setup,moved),True,name)   # the block or the handlers name the package path
    def test_package_install_rerun_and_uninstall(self):
        import install
        home=self.base/'local'/'agentic-living-memory'
        with patch.multiple(install,INSTALL_HOME=home,INSTALL_DIR=home/'app',LAUNCHER_DIR=self.base/'bin',_path_step=lambda:None):   # no real launcher folder or PATH
            done=install.install();self.assertIn('next_steps',done['summary']);self.assertTrue(done['operation_id'].startswith('i-'))
            self.assertTrue((home/'app/living_memory.py').exists());self.assertTrue((home/'app/.core-ownership-v1.json').exists())
            self.assertFalse((home/'app/skills').exists())
            install.install()   # rerun keeps, never duplicates
            removed=install.install(True)
            self.assertFalse((home/'app/living_memory.py').exists());self.assertFalse((home/'app/.core-ownership-v1.json').exists())
            undo(home,removed['operation_id']);self.assertTrue((home/'app/living_memory.py').exists())   # the journal in INSTALL_HOME puts the package back
    def test_codex_setup_and_uninstall_preserve_other_hooks(self):
        from integrations.setup import setup
        from core.instructions import instruction_block
        home=self.base/'codex';home.mkdir();hooks=home/'hooks.json'
        original={'hooks':{'Stop':[{'hooks':[{'type':'command','command':'other.exe'}]}]},'custom':True}
        hooks.write_text(json.dumps(original));(home/'AGENTS.md').write_text('# my codex rules\n',encoding='utf-8')
        setup('codex',home)
        config=read_json(hooks);self.assertEqual(len(config['hooks']['Stop']),2);self.assertTrue(config['custom'])
        self.assertEqual(config['hooks']['SessionStart'][0]['hooks'][0]['additionalContextLimit'],9000)
        agents=(home/'AGENTS.md').read_text(encoding='utf-8')
        self.assertTrue(agents.startswith('# my codex rules\n'));self.assertIn(instruction_block('codex'),agents)
        setup('codex',home)   # rerun replaces, never duplicates
        self.assertEqual(len(read_json(hooks)['hooks']['Stop']),2)
        self.assertEqual((home/'AGENTS.md').read_text(encoding='utf-8').count('agentic-living-memory:begin'),1)
        setup('codex',home,uninstall=True)
        self.assertEqual(read_json(hooks)['hooks'],original['hooks']);self.assertTrue(read_json(hooks)['custom'])
        self.assertEqual((home/'AGENTS.md').read_text(encoding='utf-8').strip(),'# my codex rules')
        self.assertFalse((home/'.living-memory-ownership.json').exists())
    def test_adapter_rerun_preserves_later_unrelated_changes(self):
        from integrations.setup import setup
        home=self.base/'claude';setup('claude',home)
        settings=read_json(home/'settings.json');settings['theme']='light';settings['hooks']['Other']=[{'hooks':[{'command':'other'}]}]
        (home/'settings.json').write_text(json.dumps(settings))
        setup('claude',home)
        self.assertEqual(read_json(home/'settings.json')['hooks']['Other'],settings['hooks']['Other'])
    def managed(self,files):
        """A managed settings folder holding files (relative name -> text), with no registry policy."""
        from integrations import setup
        folder=self.base/'managed';folder.mkdir(exist_ok=True)
        for name,text in files.items():(folder/name).parent.mkdir(parents=True,exist_ok=True);(folder/name).write_text(text,encoding='utf-8')
        return patch.multiple(setup,MANAGED_DIR=folder,_registry_policies=lambda:iter(()))
    def test_claude_setup_warns_when_managed_settings_allow_only_managed_hooks(self):
        from integrations.setup import setup
        for name in ('managed-settings.json','managed-settings.d/50-hooks.json'):
            with self.subTest(name=name),self.managed({name:'{"allowManagedHooksOnly": true}'}):
                warning=setup('claude',self.base/'claude').get('summary',{}).get('warning','')
                self.assertIn('allowManagedHooksOnly',warning);self.assertIn(str(self.base/'managed'/name),warning)
            (self.base/'managed'/name).unlink()
    def test_claude_setup_ignores_managed_settings_without_the_hooks_lock(self):
        from integrations.setup import setup
        for text in ('{"allowManagedHooksOnly": false}','{"model": "x"}','{"allowManagedHooksOnly": "true"}','{not json','[true]'):
            with self.subTest(text=text),self.managed({'managed-settings.json':text}):
                self.assertNotIn('warning',setup('claude',self.base/'claude')['summary'])
        with self.managed({}):self.assertNotIn('warning',setup('claude',self.base/'claude')['summary'])
    def test_hooks_lock_warning_only_for_claude_setup(self):
        from integrations.setup import setup
        with self.managed({'managed-settings.json':'{"allowManagedHooksOnly": true}'}):
            self.assertNotIn('warning',setup('claude',self.base/'claude',uninstall=True)['summary'])
            self.assertNotIn('warning',setup('codex',self.base/'codex')['summary'])
    def test_setup_main_prints_hooks_lock_warning_to_stderr(self):
        import io
        from contextlib import redirect_stderr,redirect_stdout
        from integrations.setup import main
        out,err=io.StringIO(),io.StringIO()
        with self.managed({'managed-settings.json':'{"allowManagedHooksOnly": true}'}),patch('core.storage.ensure_setup'),redirect_stdout(out),redirect_stderr(err):
            self.assertEqual(main(['claude','--home',str(self.base/'claude')]),0)
        self.assertIn('Warning: allowManagedHooksOnly',err.getvalue());self.assertIn('warning',json.loads(out.getvalue())['summary'])
    def test_migration_preserves_canonical_bytes(self):
        from core.migration import migrate_schema
        a=self.note();before=Path(a['path']).read_bytes();migrate_schema(self.root)
        self.assertEqual(Path(a['path']).read_bytes(),before)
    def test_retag_does_not_erase_notes(self):
        a=self.note();done=retag(self.root,{'concern/architecture':'concern/decision'})
        self.assertIn('concern/decision',inventory('demo',self.root)[0]['tags'])
        self.assertEqual(inventory('demo',self.root)[0]['id'],a['id'])
        undo(self.root,done['operation_id']);self.assertIn('concern/architecture',inventory('demo',self.root)[0]['tags'])
    def test_bug_lifecycle_moves_and_reopens(self):
        from core.notes import change,find_note
        a=self.note(type='bug');change(a['id'],'demo','resolve',self.root)
        path,note=find_note(a['id'],'demo',self.root);self.assertEqual(path.parent.name,'archive');self.assertEqual(note['prefix'],'BUG');self.assertEqual(note['type'],'bug');self.assertEqual(note['status'],'resolved')
        change(a['id'],'demo','reopen',self.root)
        self.assertEqual(find_note(a['id'],'demo',self.root)[0].parent.name,'notes')
    def test_merge_retains_ids_and_supersession(self):
        from core.governance import merge
        from core.notes import find_note
        a=self.note('First','Claim one');b=self.note('Second','Claim two')
        done=merge(self.root,{'target':'demo:'+a['id']+'@'+parse(Path(a['path']))['revision'],'sources':['demo:'+b['id']+'@'+parse(Path(b['path']))['revision']],'body':'Combined reviewed claim'})
        self.assertEqual(find_note(b['id'],'demo',self.root)[1]['superseded_by'],a['id'])
        self.assertEqual(len(inventory('demo',self.root)),1)
        undo(self.root,done['operation_id']);self.assertEqual(find_note(a['id'],'demo',self.root)[1]['body'],'Claim one');self.assertEqual(len(inventory('demo',self.root)),2)
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
    def inst(self,uninstall=False,homes=()):return self.install.install(uninstall,homes)
    def test_create_then_keep(self):
        receipt=self.inst();self.assertEqual(receipt['launcher']['action'],'create');self.assertNotIn('path_change',receipt);self.assertTrue(self.launcher.ours(self.exe));self.assertFalse(receipt.get('warnings'))
        self.assertEqual(self.inst()['launcher']['action'],'keep')
        self.assertTrue(self.launcher.active(self.home/'app'/'living_memory.py'))
        shown=subprocess.run([str(self.exe),'--json','version'],capture_output=True,text=True);self.assertEqual(json.loads(shown.stdout)['ok'],True)
    def test_ownership_record_names_the_installing_python(self):   # REG-4: Hivemind reads it to start ALM
        self.inst();record=json.loads((self.home/'app'/'.core-ownership-v1.json').read_text(encoding='utf-8'))
        self.assertEqual(record['interpreter'],sys.executable)
    def test_a_second_uninstall_finds_nothing_to_remove(self):
        self.inst();self.inst(True);again=self.inst(True);self.assertEqual((again['launcher']['action'],again['launcher']['reason']),('none','no launcher installed'));self.assertFalse(self.exe.exists())
    def test_replace_a_launcher_that_differs_from_what_would_be_built(self):
        self.inst();self.exe.write_bytes(self.exe.read_bytes()+b'x')   # still ours (marker), but no longer what would be built
        self.assertEqual(self.inst()['launcher']['action'],'replace');self.assertTrue(self.launcher.ours(self.exe))
    def test_foreign_file_is_left_alone(self):
        self.bin.mkdir(parents=True);self.exe.write_bytes(b'foreign program')
        record=self.inst()['launcher'];self.assertEqual(record['action'],'none');self.assertIn('not ours',record['reason'])
        self.assertEqual(self.exe.read_bytes(),b'foreign program');self.assertTrue((self.home/'app/living_memory.py').exists())
        self.inst(True);self.assertEqual(self.exe.read_bytes(),b'foreign program')   # uninstall keeps it too
    def test_uninstall_refuses_while_a_harness_still_runs_the_package(self):
        from integrations import setup
        self.inst();claude=Path(os.environ['CLAUDE_HOME'])
        with patch.object(setup,'ROOT',self.home/'app'):setup.setup('claude',claude)
        with self.assertRaises(ValueError) as caught:self.inst(True)
        message=str(caught.exception);self.assertIn(str(claude/'settings.json'),message);self.assertIn('claude --uninstall',message);self.assertTrue(os.name!='nt' or 'unwire it with: & "'+sys.executable+'"' in message,message);self.assertIn(str(self.home/'app'/'integrations'/'setup.py'),message)
        self.assertTrue((self.home/'app/living_memory.py').exists())
        with patch.object(setup,'ROOT',self.home/'app'):setup.setup('claude',claude,uninstall=True)
        self.inst(True);self.assertFalse((self.home/'app/living_memory.py').exists())
    def test_uninstall_does_not_check_a_custom_home_nobody_named(self):
        from integrations import setup
        self.inst();custom=self.base/'custom-claude'
        with patch.object(setup,'ROOT',self.home/'app'):setup.setup('claude',custom)
        self.inst(True);self.assertFalse((self.home/'app/living_memory.py').exists())
    def test_with_claude_config_dir_set_uninstall_still_checks_the_default_claude_folder(self):   # CODEX-R4A
        from integrations import setup
        self.inst();user=self.base/'user';default=user/'.claude';chosen=self.base/'claude-config'
        with patch.object(setup,'ROOT',self.home/'app'):setup.setup('claude',default)
        with patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':str(chosen)}),patch('pathlib.Path.home',return_value=user):
            os.environ.pop('CLAUDE_HOME')
            with self.assertRaisesRegex(ValueError,'still in use by: claude'):self.inst(True)
        self.assertTrue((self.home/'app/living_memory.py').exists())
    def test_with_claude_config_dir_set_uninstall_checks_that_folder(self):   # CODEX-R4B
        from integrations import setup
        self.inst();chosen=self.base/'claude-config'
        with patch.object(setup,'ROOT',self.home/'app'):setup.setup('claude',chosen)
        with patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':str(chosen)}),patch('pathlib.Path.home',return_value=self.base/'user'):
            os.environ.pop('CLAUDE_HOME')
            with self.assertRaisesRegex(ValueError,'still in use by: claude'):self.inst(True)
        self.assertTrue((self.home/'app/living_memory.py').exists())
    def test_uninstall_checks_a_custom_home_given_with_home(self):
        from integrations import setup
        self.inst();custom=self.base/'custom-claude'
        with patch.object(setup,'ROOT',self.home/'app'):setup.setup('claude',custom)
        with self.assertRaisesRegex(ValueError,'--home'):self.inst(True,[('claude',str(custom))])
        self.assertTrue((self.home/'app/living_memory.py').exists())
    def test_uninstall_removes_ours_and_says_the_path_entry_stays(self):
        self.inst();receipt=self.inst(True);self.assertEqual(receipt['launcher']['action'],'remove');self.assertIn('left in place',receipt['summary']['path'])
        self.assertFalse(self.exe.exists());self.assertFalse((self.home/'app/living_memory.py').exists())
    @unittest.skipUnless(os.name=='nt','Windows builds the launcher from pip\'s stub')
    def test_no_stub_means_no_launcher(self):
        with patch.object(self.launcher,'_stub',return_value=None):record=self.inst()['launcher']
        self.assertEqual(record['action'],'none');self.assertIn('ensurepip',record['reason'])
    def test_launcher_that_does_not_run_is_removed_with_a_warning(self):
        with patch('subprocess.run',return_value=subprocess.CompletedProcess([],1,b'',b'blocked')):receipt=self.inst()
        self.assertFalse(self.exe.exists());self.assertIn('blocked',receipt['warnings'][0]);self.assertEqual(receipt['launcher']['action'],'failed');self.assertNotIn('path_change',receipt)
    def test_launcher_removed_by_antivirus_during_the_probe_is_still_a_warning(self):   # REG-20
        def quarantined(*a,**k):
            self.exe.unlink();return subprocess.CompletedProcess([],1,b'',b'quarantined')
        with patch('subprocess.run',side_effect=quarantined):receipt=self.inst()
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
        self.on_path(False);receipt=self.inst();self.assertEqual(receipt['path_change'],str(self.bin))
        self.assertEqual(store['v'],(f"C:\\Tools;%USERPROFILE%\\x;{self.bin}",winreg.REG_EXPAND_SZ));self.assertIn('new terminal',receipt['notice'])
        self.assertNotIn('path_change',self.inst());self.assertEqual(store['v'][0],f"C:\\Tools;%USERPROFILE%\\x;{self.bin}")   # a rerun finds the entry in the registry value and adds it once
        self.inst(True);self.assertEqual(store['v'][0],f"C:\\Tools;%USERPROFILE%\\x;{self.bin}")   # uninstall leaves PATH alone
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
    @unittest.skipUnless(os.name=='nt','Windows registry Path')
    def test_windows_path_write_keeps_a_folder_added_after_the_step_was_read(self):
        import winreg
        change={'kind':'registry','target':'HKCU\\Environment\\Path','before':'C:\\a','before_type':winreg.REG_EXPAND_SZ,'add':'C:\\lm\\bin'}
        written=[]
        with patch.object(self.install,'_registry_path',return_value=('C:\\a;C:\\other-installer',winreg.REG_SZ)),\
             patch.object(winreg,'OpenKey'),patch.object(winreg,'SetValueEx',side_effect=lambda key,name,res,kind,value:written.append((kind,value))),\
             patch('ctypes.windll.user32.SendMessageTimeoutW'):
            self.install._add_to_path_windows(change)
        self.assertEqual(written,[(winreg.REG_SZ,'C:\\a;C:\\other-installer;C:\\lm\\bin')])
    @unittest.skipIf(os.name=='nt','POSIX profile file')
    def test_posix_path_change_by_profile(self):
        home=self.base/'h';home.mkdir();self.on_path(False)
        with patch.object(Path,'home',return_value=home),patch.dict(os.environ,{'SHELL':'/bin/zsh'}):
            receipt=self.inst();self.assertEqual(receipt['path_change'],self.install.PROFILE_LINE)
            text=(home/'.zprofile').read_text();self.assertEqual(text.count(self.install.PROFILE_LINE),1)
            self.assertNotIn('path_change',self.inst());self.assertEqual((home/'.zprofile').read_text(),text)   # a rerun adds it once
            (home/'.zprofile').write_text(text+'# edited\n')
            self.inst(True);self.assertIn('edited',(home/'.zprofile').read_text())   # uninstall leaves the profile alone
    def run_main(self,*argv):
        import contextlib,io
        out,err=io.StringIO(),io.StringIO()
        with patch('core.storage.ensure_setup'),patch('sys.argv',['install.py',*argv]),contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):self.install.main()
        return out.getvalue(),err.getvalue()
    def test_install_prints_paste_ready_next_steps_on_stderr_and_keeps_stdout_json(self):
        (self.base/'home-claude').mkdir();lead='& ' if os.name=='nt' else ''
        out,err=self.run_main();result=json.loads(out);steps=result['summary']['next_steps'];self.assertTrue(steps)
        self.assertTrue(result['operation_id'].startswith('i-'));self.assertNotIn('Installed',out)   # stdout is still the one JSON result
        for step in steps:self.assertIn('  '+lead+step,err)
        self.assertIn(f'{lead}"{sys.executable}" "',err)
        out,err=self.run_main();json.loads(out);self.assertNotIn('claude --hooks',err)   # an update leaves out a harness never wired
    def harness(self,name):return Path(os.environ[name.upper()+'_HOME'])
    def wire(self,name,hooks=True):
        from integrations import setup
        with patch.object(setup,'ROOT',self.home/'app'):setup.setup(name,self.harness(name),hooks)
    def test_an_update_lists_only_harnesses_whose_wiring_would_change(self):
        named=lambda steps:[step.rsplit(' ',2)[1] for step in steps]
        for name in ('claude','codex','copilot'):self.harness(name).mkdir(parents=True,exist_ok=True)
        first=self.inst()['summary'];self.assertEqual(named(first['next_steps']),['claude','codex','copilot']);self.assertEqual(first['wiring_current'],[])   # a first install lists every harness found
        self.wire('claude');self.wire('codex')
        summary=self.inst()['summary'];self.assertEqual(summary['next_steps'],[]);self.assertEqual(summary['wiring_current'],['claude','codex'])   # copilot was never wired: left out
        out,err=self.run_main();self.assertIn('Harness wiring is current (claude, codex); nothing to rerun.',err)
        rules=self.harness('claude')/'CLAUDE.md';rules.write_text(rules.read_text(encoding='utf-8').replace('Change memory only through CMD','Change memory through CMD',1),encoding='utf-8')   # an older block
        hooks=self.harness('codex')/'hooks.json';config=read_json(hooks);config['hooks']['Stop'][0]['hooks'][0]['timeout']=99;hooks.write_text(json.dumps(config))   # an older handler
        summary=self.inst()['summary'];self.assertEqual(named(summary['next_steps']),['claude','codex']);self.assertEqual(summary['wiring_current'],[])
        out,err=self.run_main();self.assertIn('claude --hooks',err);self.assertIn('codex --hooks',err)
    def stamped_source(self):
        source=self.base/'src';(source/'core').mkdir(parents=True);(source/'core/a.py').write_text('a')
        def git(*a):return subprocess.run(['git','-c','user.name=t','-c','user.email=t@t','-c','commit.gpgsign=false','-C',str(source),*a],capture_output=True,text=True,check=True).stdout.strip()
        git('init','-q');git('add','.');git('commit','-qm','one');return source,git
    def doctor(self):
        import contextlib,io
        from revision_fill import main
        out=io.StringIO()
        with contextlib.redirect_stdout(out):main(['--root',str(self.root),'doctor'])
        return json.loads(out.getvalue())
    def test_install_stamps_the_source_commit_and_doctor_warns_when_head_moves(self):
        from core import installer_lifecycle as lifecycle
        source,git=self.stamped_source();first=git('rev-parse','HEAD')
        with patch.object(self.install,'SOURCE',source):self.inst()
        stamp=lifecycle.installed_stamp(self.home/'app');self.assertEqual((stamp['commit'],stamp['dirty'],stamp['source']),(first,False,str(source)))
        self.assertIsNone(lifecycle.source_warning(stamp))
        (source/'core/a.py').write_text('b')
        with patch.object(self.install,'SOURCE',source):self.inst()
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
        from revision_fill import main
        from core import installer_lifecycle as lifecycle
        out=io.StringIO()
        with contextlib.redirect_stdout(out):main(['--root',str(self.root),'version'])
        shown=json.loads(out.getvalue());self.assertEqual(shown['release'],lifecycle.CORE_RELEASE);self.assertIn('install',shown)

if __name__=='__main__':unittest.main()
