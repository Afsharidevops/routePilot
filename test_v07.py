import unittest
from pathlib import Path
class InstallerRegression(unittest.TestCase):
 def test_no_typo(self):
  s=(Path(__file__).parent/'install.sh').read_text()
  self.assertNotIn('rm -f/routepilot.desktop',s)
  self.assertIn('Icon=/opt/routepilot/routepilot.png',s)
  self.assertIn('install -m 644 "$SOURCE_DIR/assets/routepilot.png" /usr/share/icons/hicolor/256x256/apps/routepilot.png',s)
 def test_original_config_preserved(self):
  s=(Path(__file__).parent/'install.sh').read_text()
  self.assertIn('if [[ ! -e /etc/routepilot/rules.json ]]',s)
if __name__=="__main__": unittest.main()
