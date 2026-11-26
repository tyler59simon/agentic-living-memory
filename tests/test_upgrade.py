"""`alm upgrade`: pull the source checkout, then run its install (one direct, journaled write). The git side runs for real against a local
bare origin; install.py is replaced, so nothing here touches LOCALAPPDATA, the registry PATH or ~/.local/bin."""
import contextlib,io,json,os,subprocess,sys,unittest
from unittest.mock import patch
from test_core import StoreFixture
from core import upgrade

REAL_INSTALL=upgrade._install   # the tests below replace it
RECEIPT={'ok':True,'operation_id':'i-'+'c'*32,'undo':'undo i-'+'c'*32,'warnings':['launcher note'],
         'summary':{'files':3,'next_steps':['"py" "setup.py" claude --hooks'],'wiring_current':['codex']}}

class Checkout(StoreFixture):
    """A source checkout on main with a local bare origin (origin/HEAD set), a second clone to push from, and an install stamp."""
    def git(self,where,*args):
        lead=['-C',str(where)] if where else []
        return subprocess.run(['git','-c','user.name=t','-c','user.email=t@t','-c','commit.gpgsign=false',*lead,*args],capture_output=True,text=True,check=True).stdout.strip()
    def commit(self,where,name):
        (where/name).write_text(name);self.git(where,'add','.');self.git(where,'commit','-qm',name)
    def setUp(self):
        super().setUp()
        self.origin,self.src,self.other=self.base/'origin.git',self.base/'src',self.base/'other'
        self.git(None,'init','-q','--bare','-b','main',str(self.origin))
        self.git(None,'init','-q','-b','main',str(self.src));self.commit(self.src,'one')
        self.git(self.src,'remote','add','origin',str(self.origin));self.git(self.src,'push','-q','-u','origin','main');self.git(self.src,'remote','set-head','origin','main')
        self.git(None,'clone','-q',str(self.origin),str(self.other))
        self.installed=self.git(self.src,'rev-parse','HEAD')
        self.stamp={'source':str(self.src),'commit':self.installed,'interpreter':sys.executable}

class InstallNotice(Checkout):
    def start(self):
        from core import installer_lifecycle,runtime
        project=self.base/'project';project.mkdir(exist_ok=True)
        with patch.object(installer_lifecycle,'installed_stamp',side_effect=lambda root=None:self.stamp):
            return runtime.process('claude','session-start',{'cwd':str(project),'session_id':'notice-'+os.urandom(4).hex()},self.root)['context']
    def test_session_start_says_when_the_default_branch_moved_past_the_install(self):
        self.assertNotIn('Living Memory install:',self.start())   # at the installed commit
        self.commit(self.src,'two');head=self.git(self.src,'rev-parse','HEAD')
        line=next(l for l in self.start().splitlines() if l.startswith('Living Memory install:'))
        self.assertIn(f'main at {head[:12]}',line);self.assertIn(self.installed[:12],line);self.assertIn('alm upgrade',line)
        self.git(self.src,'checkout','-q','-b','feature')
        self.assertNotIn('Living Memory install:',self.start())   # feature-branch work is never offered for install
        self.git(self.src,'checkout','-q','main');self.stamp=None
        self.assertNotIn('Living Memory install:',self.start())   # a source checkout run has no install stamp

class Upgrade(Checkout):
    def setUp(self):
        super().setUp()
        stamp=patch.object(upgrade,'installed_stamp',side_effect=lambda:self.stamp);stamp.start();self.addCleanup(stamp.stop)
        self.calls=[]
        def install(python,source,*args):
            self.calls.append(args)
            return RECEIPT
        fake=patch.object(upgrade,'_install',side_effect=install);fake.start();self.addCleanup(fake.stop)
    def push_from_other(self):
        self.commit(self.other,'two');self.git(self.other,'push','-q','origin','main');return self.git(self.other,'rev-parse','HEAD')
    def test_pulls_installs_then_relays_the_wiring_steps_and_the_undo(self):
        new=self.push_from_other();result=upgrade.run()
        self.assertEqual((result['status'],result['from'],result['to'],result['commits']),('upgraded',self.installed[:12],new[:12],1))
        self.assertEqual(self.git(self.src,'rev-parse','HEAD'),new)
        self.assertEqual(self.calls,[()])   # install.py with no arguments installs directly
        self.assertEqual((result['next_steps'],result['wiring_current'],result['files']),(RECEIPT['summary']['next_steps'],['codex'],3))
        self.assertEqual((result['operation_id'],result['undo']),(RECEIPT['operation_id'],RECEIPT['undo']))
        self.assertEqual(result['warnings'],['launcher note']);self.assertIn('Restart',result['restart'])
    def test_already_current_installs_nothing(self):
        import install
        with patch.object(install,'next_steps',return_value=([],['claude'])):result=upgrade.run()
        self.assertEqual((result['status'],result['commits'],result['wiring_current']),('current',0,['claude']));self.assertEqual(self.calls,[])
    def test_refusals(self):
        self.stamp={'source':str(self.src),'commit':None}
        with self.assertRaisesRegex(ValueError,'records none'):upgrade.run()
        self.stamp={'source':str(self.src),'commit':self.installed}
        (self.src/'loose.txt').write_text('x')
        with self.assertRaisesRegex(ValueError,'uncommitted changes'):upgrade.run()
        (self.src/'loose.txt').unlink();self.git(self.src,'checkout','-q','-b','feature')
        with self.assertRaisesRegex(ValueError,'on feature; upgrade installs only from main'):upgrade.run()
        self.git(self.src,'checkout','-q','main');self.commit(self.src,'local');self.push_from_other()   # diverged from origin
        with self.assertRaisesRegex(ValueError,'git pull --ff-only failed'):upgrade.run()
        self.assertEqual(self.calls,[])
    def test_an_install_failure_names_its_error(self):
        with patch.object(upgrade,'_run',return_value=(1,'','Traceback (most recent call last):\nValueError: Installation target overlaps the source tree')):
            with self.assertRaisesRegex(ValueError,'install.py failed in .*overlaps the source tree'):REAL_INSTALL(sys.executable,self.src)
        with patch.object(upgrade,'_run',return_value=(0,'progress line\n{"ok": true, "operation_id": "i-x"}\n','')):
            self.assertEqual(REAL_INSTALL(sys.executable,self.src),{'ok':True,'operation_id':'i-x'})   # the last JSON line on stdout
    def test_render_gives_paste_ready_steps(self):
        from core.render import render
        self.push_from_other();text=render(upgrade.run());lead='& ' if os.name=='nt' else ''
        self.assertIn('Upgraded from',text);self.assertIn('  '+lead+RECEIPT['summary']['next_steps'][0],text);self.assertIn('Restart',text)

class UpgradeCommand(StoreFixture):
    def run_cli(self,*argv):
        from revision_fill import main
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=main(list(argv))
        return code,out.getvalue()+err.getvalue()
    def test_agents_get_no_auto_allow_and_subagents_and_hivemind_are_refused(self):
        from core.runtime import ALLOWED_SUBCOMMANDS,HIVEMIND_GOVERNED
        self.assertNotIn('upgrade',ALLOWED_SUBCOMMANDS);self.assertIn('upgrade',HIVEMIND_GOVERNED)   # the hook refuses it in Hivemind terminals too
        with patch('core.upgrade.run') as run:
            code,text=self.run_cli('--json','upgrade','--actor','subagent');self.assertNotEqual(code,0);self.assertIn('read-only',text)
            with patch.dict(os.environ,{'HIVEMIND_SESSION':'s'}):code,text=self.run_cli('--json','upgrade');self.assertNotEqual(code,0);self.assertIn('hivemind',text)
            run.assert_not_called()

class InstallerEntryPoints(StoreFixture):
    def install_main(self,argv):
        import install
        out=io.StringIO()
        with patch.object(sys,'argv',['install.py',*argv]),contextlib.redirect_stdout(out):code=install.main()
        return code,out.getvalue()
    def test_the_upgrader_from_before_plans_were_removed_still_installs(self):
        import install
        receipt={'ok':True,'operation_id':'i-'+'d'*32,'undo':'undo i-'+'d'*32,'summary':{'files':2,'next_steps':[]}}
        with patch.object(install,'install',return_value=receipt) as direct,patch('core.storage.ensure_setup') as setup,patch.object(install,'print_next_steps'),\
             patch.object(install,'next_steps',return_value=(['setup.py claude --hooks'],['codex'])),patch.object(install,'_package_changes',return_value={'a':'x','b':'y'}):
            code,out=self.install_main(['--plan'])   # the old upgrader's first call (all of its `upgrade --plan`) writes nothing
            plan=json.loads(out.splitlines()[-1]);self.assertEqual(code,0);direct.assert_not_called();setup.assert_not_called()
            self.assertEqual(plan['summary'],{'files':2,'next_steps':['setup.py claude --hooks'],'wiring_current':['codex']})   # what it shows the user
            code,out=self.install_main(['--apply-plan',plan['plan_id'],'--expected-plan-sha256',plan['digest']])   # its second call installs
            self.assertEqual((code,json.loads(out.splitlines()[-1])['operation_id']),(0,receipt['operation_id']));self.assertEqual(direct.call_count,1)
            for wrong in (['--apply-plan','p-old','--expected-plan-sha256','x'],['--apply-plan',plan['plan_id'],'--expected-plan-sha256','x']):
                with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):self.install_main(wrong)
            self.assertEqual(direct.call_count,1)
    def test_agents_cannot_run_the_installer_or_harness_setup_directly(self):
        import install
        from integrations import setup
        for env in ({'HIVEMIND_SESSION':'s'},{'LIVING_MEMORY_ACTOR':'subagent'},{'LIVING_MEMORY_ACTOR':'viewer'}):
            with self.subTest(env=env),patch.dict(os.environ,env),patch.object(install,'install') as direct,patch.object(setup,'setup') as wire:
                with self.assertRaises(SystemExit):self.install_main([])
                with self.assertRaises(SystemExit):setup.main(['claude'])
                direct.assert_not_called();wire.assert_not_called()

if __name__=='__main__':unittest.main()
