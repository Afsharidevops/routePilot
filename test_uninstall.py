from pathlib import Path
import unittest
ROOT = Path(__file__).resolve().parent
class UninstallTests(unittest.TestCase):
    def test_script_controls(self):
        s=(ROOT/'uninstall.sh').read_text()
        self.assertIn('systemctl stop routepilot.service',s)
        self.assertLess(s.index('systemctl stop routepilot.service'),s.index('routepilot.py cleanup'))
        self.assertIn('--purge-config',s)
        self.assertIn('if ! /usr/bin/python3 /opt/routepilot/routepilot.py cleanup;',s)
    def test_gui(self):
        s=(ROOT/'routepilot.py').read_text()
        self.assertIn("text='Uninstall…'",s)
        self.assertIn("['pkexec', '/bin/bash', str(script)]",s)
    def test_installer_copies_uninstaller(self):
        self.assertIn('install -m 755 "$SOURCE_DIR/uninstall.sh" /opt/routepilot/uninstall.sh', (ROOT/'install.sh').read_text())
if __name__=='__main__': unittest.main()
