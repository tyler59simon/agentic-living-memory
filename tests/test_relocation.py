import contextlib,io,json,os,unittest
from pathlib import Path
from unittest.mock import patch
from test_core import StoreFixture
from core.governance import commit,undo
from core.repository import read_json,canonical_json,digest
from integrations import setup

class Relocation(StoreFixture):
    """Moving from CODEX_HOME/agentic-living-memory to the harness-neutral install folder."""
    def setUp(self):
        super().setUp()
        self.codex=Path(os.environ['CODEX_HOME']);self.codex.mkdir(parents=True,exist_ok=True)
        self.claude=Path(os.environ['CLAUDE_HOME'])
        self.legacy=self.codex/'agentic-living-memory'
    def from_legacy(self,harness,home):
        """Wire a harness the way an older release did: from the copy inside CODEX_HOME."""
        with patch.object(setup,'ROOT',self.legacy):setup.setup(harness,home)
    def ours(self,config,harness):
        return [h for groups in setup._events(config,harness).values() for g in groups for h in g.get('hooks',[]) if setup._ours(h,harness)]
    def commands(self,handlers):return [' '.join([str(h.get('command',''))]+[str(a) for a in h.get('args',[])]) for h in handlers]

    def test_claude_handlers_move_to_the_new_copy(self):
        self.from_legacy('claude',self.claude)
        settings=read_json(self.claude/'settings.json');settings['hooks']['Stop'].append({'hooks':[{'type':'command','command':'other'}]})
        (self.claude/'settings.json').write_text(json.dumps(settings))
        setup.setup('claude',self.claude)   # no "Modified managed handlers preserved"
        settings=read_json(self.claude/'settings.json');handlers=self.ours(settings,'claude')
        self.assertEqual(len(handlers),7)
        self.assertTrue(all(str(setup._script('claude')) in c and str(self.legacy) not in c for c in self.commands(handlers)))
        self.assertIn({'hooks':[{'type':'command','command':'other'}]},settings['hooks']['Stop'])
    def test_codex_handlers_from_the_old_installer_are_replaced(self):
        self.from_legacy('codex',self.codex)
        # The old install.py kept only the instruction hash.
        state=read_json(self.codex/'.living-memory-ownership.json');(self.codex/'.living-memory-ownership.json').write_text(canonical_json({'schema':2,'instruction_hash':state['instruction_hash']}))
        setup.setup('codex',self.codex)
        config=read_json(self.codex/'hooks.json');handlers=self.ours(config,'codex')
        self.assertEqual(len(handlers),6);self.assertTrue(all(str(self.legacy) not in c for c in self.commands(handlers)))
        self.assertEqual((self.codex/'AGENTS.md').read_text(encoding='utf-8').count('agentic-living-memory:begin'),1)

    def wired_from(self,source,harness='claude'):
        with patch.object(setup,'ROOT',source):setup.setup(harness,self.claude)
    def test_handlers_wired_from_the_source_checkout_are_managed_by_the_installed_copy(self):
        source,app=self.base/'checkout',self.base/'app';app.mkdir();(app/'.core-ownership-v1.json').write_text(json.dumps({'source':str(source)}))
        self.wired_from(source)
        with patch.object(setup,'ROOT',app):
            setup.setup('claude',self.claude)   # no "Modified managed handlers preserved"
            handlers=self.ours(read_json(self.claude/'settings.json'),'claude')
            self.assertEqual(len(handlers),7);self.assertTrue(all(str(app) in c and str(source) not in c for c in self.commands(handlers)))
            setup.setup('claude',self.claude,uninstall=True)   # uninstall is not blocked either
        self.assertEqual(self.ours(read_json(self.claude/'settings.json'),'claude'),[])
    def test_handlers_wired_from_the_default_install_folder_are_managed_from_a_checkout(self):
        import install
        app=self.base/'app';self.wired_from(app)
        with patch.object(install,'INSTALL_DIR',app):setup.setup('claude',self.claude)
        self.assertTrue(all(str(setup._script('claude')) in c for c in self.commands(self.ours(read_json(self.claude/'settings.json'),'claude'))))
    def test_a_handler_path_is_matched_by_boundary_and_on_windows_by_case_and_slash(self):   # INST-3.1
        script=str(setup._script('claude'));handler=lambda command:{'type':'command','command':command}
        self.assertTrue(setup._ours(handler('"py" "'+script+'" stop'),'claude'));self.assertTrue(setup._ours(handler(script),'claude'))
        self.assertFalse(setup._ours(handler('"py" "'+script+'.bak" stop'),'claude'));self.assertFalse(setup._ours(handler(script+'.bak'),'claude'))
        self.assertFalse(setup._ours(handler('"py" "'+script+'x" stop'),'claude'))
    def test_the_legacy_codex_fallback_matches_by_boundary_too(self):   # INST-3.1b
        old=lambda command:{'type':'command','command':command};legacy='C:/Users/me/.codex/agentic-living-memory/hooks/codex_hooks.py'
        for command in ('python3 "'+legacy+'" stop',legacy,'"py" '+"'"+legacy+"'"+' stop'):self.assertTrue(setup._ours(old(command),'codex'),command)
        for command in ('python3 "'+legacy+'.bak" stop',legacy+'.bak','python3 "'+legacy+'x" stop'):self.assertFalse(setup._ours(old(command),'codex'),command)
        self.assertFalse(setup._ours(old('python3 "C:/other/codex_hooks.py" stop'),'codex'))
        self.assertFalse(setup._ours(old('python3 "C:/previous/agentic-living-memory/hooks/my_codex_hooks.py" stop'),'codex'))   # R-02: a longer file name is not ours
        self.assertTrue(setup._ours({'type':'command','command':'powershell.exe -EncodedCommand AAAA','commandWindows':'x agentic-living-memory codex_hooks.py'},'codex'))
    @unittest.skipUnless(os.name=='nt','Windows paths ignore case and slash direction')
    def test_a_windows_handler_with_other_case_or_forward_slashes_is_ours(self):
        script=str(setup._script('claude'))
        for variant in (script.upper(),script.lower(),script.replace(os.sep,'/'),script.replace(os.sep,'/').upper()):
            self.assertTrue(setup._ours({'type':'command','command':'"py" "'+variant+'" stop'},'claude'),variant)
            self.assertFalse(setup._ours({'type':'command','command':'"py" "'+variant+'.bak" stop'},'claude'),variant)
    def test_a_real_edit_of_a_managed_handler_names_the_file_and_the_repair(self):
        self.wired_from(setup.ROOT)
        settings=read_json(self.claude/'settings.json');settings['hooks']['Stop'][0]['hooks'][0]['timeout']=999
        (self.claude/'settings.json').write_text(json.dumps(settings))
        with self.assertRaises(ValueError) as caught:setup.setup('claude',self.claude)
        self.assertIn('Modified managed handlers preserved',str(caught.exception));self.assertIn(str(self.claude/'settings.json'),str(caught.exception));self.assertIn('claude --hooks',str(caught.exception))

    def edit_handler(self):
        self.wired_from(setup.ROOT)
        settings=read_json(self.claude/'settings.json');settings['hooks']['Stop'][0]['hooks'][0]['timeout']=999
        (self.claude/'settings.json').write_text(json.dumps(settings))
    def test_adopt_shows_the_handler_diff_and_undo_puts_the_edit_back(self):
        self.edit_handler();before=(self.claude/'settings.json').read_text()
        with self.assertRaisesRegex(ValueError,'--adopt'):setup.setup('claude',self.claude)
        self.assertEqual((self.claude/'settings.json').read_text(),before)   # a refusal changes nothing
        done=setup.setup('claude',self.claude,adopt=True);diff=done['summary']['adopt']['settings.json']
        self.assertIn('--- edited settings.json',diff);self.assertIn('+++ managed settings.json',diff);self.assertIn('-          "timeout": 999',diff);self.assertIn('+          "timeout": 15',diff)
        self.assertTrue(done['operation_id'].startswith('s-claude-'));self.assertNotIn('999',(self.claude/'settings.json').read_text())
        undo(self.claude,done['operation_id']);self.assertEqual((self.claude/'settings.json').read_text(),before)
    def test_adopt_shows_the_instruction_block_diff_and_keeps_text_around_it(self):
        self.wired_from(setup.ROOT);path=self.claude/'CLAUDE.md';text=path.read_text(encoding='utf-8');begin='agentic-living-memory:begin -->'
        path.write_text('Mine before.'+chr(10)+text.replace(begin,begin+chr(10)+'My own rule.',1),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'--adopt'):setup.setup('claude',self.claude)
        done=setup.setup('claude',self.claude,adopt=True);self.assertIn('-My own rule.',done['summary']['adopt']['CLAUDE.md'])
        after=path.read_text(encoding='utf-8')
        self.assertNotIn('My own rule.',after);self.assertTrue(after.startswith('Mine before.'));self.assertEqual(after.count('agentic-living-memory:begin'),1)
    COPILOT_HOOK={'type':'command','bash':'echo mine','powershell':'echo mine','timeoutSec':5}
    def copilot(self):
        home=Path(os.environ['COPILOT_HOME']);(home/'hooks').mkdir(parents=True,exist_ok=True)
        return home,home/setup.CONFIG['copilot']
    def copilot_ours(self,config):return [h for hs in config['hooks'].values() for h in hs if setup._ours(h,'copilot')]
    def test_copilot_owns_only_its_handlers(self):   # CODEX-R1B
        from core import wiring
        home,path=self.copilot();mine=self.COPILOT_HOOK
        setup.setup('copilot',home)
        config=read_json(path);config['hooks']['agentStop'].append(mine);config['hooks']['postToolUse']=[mine];path.write_text(json.dumps(config))
        setup.setup('copilot',home)   # a rerun keeps the user's hooks and is not refused
        config=read_json(path);self.assertIn(mine,config['hooks']['agentStop']);self.assertEqual(config['hooks']['postToolUse'],[mine])
        self.assertEqual(len(self.copilot_ours(config)),5);self.assertTrue(wiring._current('copilot',config,setup))
        config['hooks']['sessionStart'][0]['timeoutSec']=99;path.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError,'Modified managed handlers.*--adopt'):setup.setup('copilot',home)
        done=setup.setup('copilot',home,adopt=True);diff=done['summary']['adopt'][setup.CONFIG['copilot']]
        self.assertIn('"timeoutSec": 99',diff);self.assertNotIn('echo mine',diff)   # the diff is our handlers only
        config=read_json(path)
        self.assertEqual(config['hooks']['sessionStart'][0]['timeoutSec'],15);self.assertEqual(config['hooks']['postToolUse'],[mine])
        setup.setup('copilot',home,uninstall=True)   # uninstall leaves the user's hooks only
        self.assertEqual(read_json(path),{'version':1,'hooks':{'agentStop':[mine],'postToolUse':[mine]}});self.assertFalse((home/'.living-memory-ownership.json').exists())
        setup.setup('copilot',home)   # and setup works again over that file
        config=read_json(path);self.assertEqual(len(self.copilot_ours(config)),5);self.assertEqual(config['hooks']['postToolUse'],[mine])
    def test_copilot_whole_file_ownership_moves_to_handlers_and_adopt_keeps_user_hooks(self):   # CODEX-R1B
        home,path=self.copilot();mine=self.COPILOT_HOOK;name=setup.CONFIG['copilot']
        setup.setup('copilot',home);config=read_json(path)
        state=read_json(home/'.living-memory-ownership.json');state['handlers']={};state['files'][name]=digest(canonical_json(config).encode())   # as builds before this one wrote it
        (home/'.living-memory-ownership.json').write_text(canonical_json(state))
        config['hooks']['postToolUse']=[mine];path.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError,'--adopt'):setup.setup('copilot',home)
        done=setup.setup('copilot',home,adopt=True);self.assertNotIn('echo mine',done['summary']['adopt'][name])
        self.assertEqual(read_json(path)['hooks']['postToolUse'],[mine])
        self.assertIn(name,read_json(home/'.living-memory-ownership.json')['handlers'])
    def test_a_whole_file_copilot_record_replaces_handlers_of_a_moved_checkout(self):   # Codex review of 8e7c779
        from core import wiring
        home,path=self.copilot();name=setup.CONFIG['copilot'];moved=self.base/'moved-checkout'
        with patch.object(setup,'ROOT',moved):setup.setup('copilot',home)
        state=read_json(home/'.living-memory-ownership.json');state['handlers']={};state['files'][name]=digest(canonical_json(read_json(path)).encode())
        (home/'.living-memory-ownership.json').write_text(canonical_json(state))   # as builds before 8e7c779 wrote it
        setup.setup('copilot',home)
        config=read_json(path);handlers=[h for hs in config['hooks'].values() for h in hs]
        self.assertEqual(len(handlers),5);self.assertNotIn(str(moved),json.dumps(config))
        self.assertFalse(wiring._current('copilot',{**config,'version':2},setup))   # doctor: Copilot would not load it
        path.write_text(json.dumps({**config,'version':2}))
        row=wiring.check('copilot',setup);self.assertIn('version 1',' '.join(row['problems']));self.assertTrue(wiring.broken({'copilot':row}))
    def test_an_unowned_copilot_file_is_merged_unless_it_is_not_a_hooks_file(self):   # CODEX-R1B
        home,path=self.copilot();mine=self.COPILOT_HOOK
        for bad in ('[1, 2]',json.dumps({'hooks':{'agentStop':'echo'}}),json.dumps({'hooks':[]}),json.dumps({'version':None,'hooks':{}}),json.dumps({'version':True,'hooks':{}})):
            path.write_text(bad)
            with self.assertRaisesRegex(ValueError,'Unknown existing Copilot file preserved'):setup.setup('copilot',home)
        path.write_text(json.dumps({'version':1,'hooks':{'agentStop':[mine]}}))
        setup.setup('copilot',home);config=read_json(path)
        self.assertEqual(config['hooks']['agentStop'][0],mine);self.assertEqual(len(self.copilot_ours(config)),5)
    def test_adopt_from_the_command_line_applies_and_prints_the_replaced_diff(self):
        self.edit_handler();err,out=io.StringIO(),io.StringIO()
        with self.assertRaises(SystemExit),contextlib.redirect_stderr(io.StringIO()):setup.main(['claude','--home',str(self.claude),'--apply-plan','a'*32,'--expected-plan-sha256','b'*64])   # the plan flags are gone
        with patch('core.storage.ensure_setup',lambda location=None:self.root),contextlib.redirect_stderr(err),contextlib.redirect_stdout(out):
            self.assertEqual(setup.main(['claude','--home',str(self.claude),'--hooks','--adopt']),0)
        result=json.loads(out.getvalue());self.assertIn('Adopted settings.json',err.getvalue());self.assertIn('"timeout": 999',err.getvalue());self.assertIn(result['undo'],err.getvalue())
        self.assertNotIn('999',(self.claude/'settings.json').read_text())
    def test_claude_home_follows_claude_config_dir_unless_claude_home_is_set(self):   # INST-6
        import install
        from core.instructions import harness_home
        custom=self.base/'claude-config'
        with patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':str(custom)}):
            self.assertEqual(harness_home('claude'),self.claude)   # CLAUDE_HOME, the test override, wins
            os.environ.pop('CLAUDE_HOME')
            self.assertEqual(harness_home('claude'),custom);self.assertEqual(install.harness_home('claude'),custom)
            self.assertNotEqual(harness_home('codex'),custom)
            done=setup.setup('claude',enable_hooks=True)
            self.assertTrue((custom/'.operations'/done['operation_id']/'journal.json').is_file());self.assertTrue((custom/'settings.json').is_file())
            from core.governance import operation_root
            self.assertEqual(operation_root(self.root,done['operation_id']),custom.resolve())   # undo finds it in the harness home
    def test_setup_in_another_home_returns_an_undo_command_that_finds_it(self):
        import shlex
        from core.cli import main as cli
        other=self.base/'else$team where'   # a $ that PowerShell and POSIX shells would expand inside double quotes
        done=setup.setup('claude',other,enable_hooks=True)
        self.assertIn(f"--root '{other.resolve()}'",done['undo']);self.assertTrue((other/'settings.json').is_file())
        argv=shlex.split(done['undo'])   # single quotes: everything inside is literal, backslashes included
        with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(cli(['--json',*argv]),0)
        self.assertFalse((other/'settings.json').exists())
        plain=self.base/"o'brien home"   # no $ or backtick: double quotes, as before, which keep the apostrophe
        self.assertIn(f'--root "{plain.resolve()}"',setup.setup('claude',plain)['undo'])
        self.assertNotIn('--root',setup.setup('claude',self.claude)['undo'])   # the configured home needs no --root
    def test_a_posix_undo_path_with_a_backslash_is_single_quoted(self):
        import shlex
        path='/home/a\\\\b'   # a doubled backslash, which a POSIX shell collapses inside double quotes
        with patch.object(setup.os,'name','posix'):
            self.assertEqual(shlex.split(setup._quote(path)),[path])
            self.assertEqual(setup._quote('/home/plain'),'"/home/plain"')
    def set_up_after_an_edit(self,name,text):
        """setup while someone appends text to name after setup read it and before commit() wrote."""
        real=setup.commit
        def late(home,*args,**kwargs):
            with open(Path(home)/name,'a',encoding='utf-8') as file:file.write(text)
            return real(home,*args,**kwargs)
        with patch.object(setup,'commit',late):return setup.setup('claude',self.claude)
    def test_setup_does_not_list_a_domains_folder_in_the_harness_home(self):   # CODEX-R4C: file guards only
        (self.claude/'domains').mkdir(parents=True);real=Path.iterdir
        def unlistable(path):
            if path.name=='domains':raise PermissionError(str(path))
            return real(path)
        with patch.object(Path,'iterdir',unlistable):setup.setup('claude',self.claude)
        self.assertTrue((self.claude/'settings.json').is_file())
    def test_a_file_edited_after_setup_read_it_makes_the_write_stale(self):   # CODEX-R1C
        self.wired_from(setup.ROOT)
        for name,text in (('settings.json',' '),('CLAUDE.md','My late line.\n')):
            with self.assertRaisesRegex(ValueError,'stale_write'):self.set_up_after_an_edit(name,text)
            edited=(self.claude/name).read_text(encoding='utf-8');self.assertTrue(edited.endswith(text))
            self.assertEqual((self.claude/name).read_text(encoding='utf-8'),edited)   # the edit survives

    def test_retire_refuses_while_referenced_then_removes_owned_files(self):
        import install
        home=self.base/'local'/'agentic-living-memory'
        with patch.multiple(install,CODEX_HOME=self.codex,LEGACY_DIR=self.legacy,INSTALL_HOME=home,INSTALL_DIR=home/'app',LAUNCHER_DIR=self.base/'bin',_path_step=lambda:None):   # no real launcher folder or PATH
            commit(self.codex,install._package_changes(self.legacy,False),'legacy_install')
            (self.legacy/'.gitignore').write_text('*.pyc\n')   # a file the old install never owned
            with self.assertRaisesRegex(ValueError,'before retiring'):install.retire_legacy()
            install.install()
            self.from_legacy('claude',self.claude)
            with self.assertRaisesRegex(ValueError,'still in use by: claude'):install.retire_legacy()
            setup.setup('claude',self.claude)
            done=install.retire_legacy()
            self.assertEqual(done['summary']['not_owned_left_in_place'],['.gitignore']);self.assertTrue(done['operation_id'].startswith('i-'))
        self.assertFalse((self.legacy/'living_memory.py').exists());self.assertFalse((self.legacy/'.core-ownership-v1.json').exists())
        self.assertTrue((self.legacy/'.gitignore').exists());self.assertTrue((home/'app/living_memory.py').exists())
    def test_retire_checks_custom_homes_when_given(self):
        import install
        home=self.base/'local'/'agentic-living-memory';custom=self.base/'custom-claude'
        with patch.multiple(install,CODEX_HOME=self.codex,LEGACY_DIR=self.legacy,INSTALL_HOME=home,INSTALL_DIR=home/'app',LAUNCHER_DIR=self.base/'bin',_path_step=lambda:None):   # no real launcher folder or PATH
            commit(self.codex,install._package_changes(self.legacy,False),'legacy_install')
            install.install()
            self.from_legacy('claude',custom)
            with self.assertRaisesRegex(ValueError,'still in use by: claude'):install.retire_legacy([('claude',str(custom))])
            done=install.retire_legacy()   # the default homes are clean
            self.assertNotIn(str(custom/'settings.json'),done['summary']['checked'])
    def test_retire_without_legacy_copy(self):
        import install
        with patch.multiple(install,LEGACY_DIR=self.legacy):
            with self.assertRaisesRegex(ValueError,'No legacy package'):install.retire_legacy()

if __name__=='__main__':unittest.main()
