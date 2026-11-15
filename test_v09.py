from pathlib import Path
import unittest
p=Path(__file__).resolve().parent
class PackageTests(unittest.TestCase):
 def test_absolute_icon(self):
  s=(p/'install.sh').read_text(); self.assertIn('Icon=/opt/routepilot/routepilot.png',s)
  self.assertIn('Categories=Network;',s)
 def test_repair_safe_backup(self):
  s=(p/'repair-user-launcher.sh').read_text(); self.assertIn('mv -- "$LEGACY"',s)
  self.assertIn('EUID == 0',s)
 def test_icon_real_png(self):
  a=(p/'assets/routepilot.png').read_bytes(); self.assertEqual(a[:8],b'\x89PNG\r\n\x1a\n')
