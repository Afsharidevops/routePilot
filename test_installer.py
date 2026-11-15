import pathlib, unittest, subprocess
class InstallerTest(unittest.TestCase):
    def test_no_removed_package(self):
        s=pathlib.Path(__file__).with_name('install.sh').read_text()
        self.assertNotIn('apt-get install -y python3 python3-tk iproute2 policykit-1',s)
        self.assertIn('polkitd pkexec',s)
    def test_shell_syntax(self):
        p=pathlib.Path(__file__).with_name('install.sh')
        subprocess.run(['bash','-n',str(p)],check=True)
    def test_python_syntax(self):
        p=pathlib.Path(__file__).with_name('routepilot.py')
        subprocess.run(['python3','-m','py_compile',str(p)],check=True)
if __name__=='__main__':unittest.main()
