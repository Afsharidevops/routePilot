import pathlib, unittest
P=pathlib.Path(__file__).parent
class T(unittest.TestCase):
 def test_install(self):
  s=(P/'install.sh').read_text(); self.assertIn('routepilot-manager.desktop',s); self.assertIn('Icon=/opt/routepilot/routepilot.png',s)
 def test_migration(self):
  s=(P/'repair-user-launcher.sh').read_text(); self.assertIn('LEGACY=',s); self.assertIn('mv -- "$LEGACY"',s); self.assertNotIn('install -m 644 "$DESKTOP" "$LOCAL"',s)
 def test_uninstall(self):
  self.assertIn('routepilot-manager.desktop',(P/'uninstall.sh').read_text())
