from pathlib import Path
import unittest
class IconTests(unittest.TestCase):
    def test_icon_files(self):
        root=Path(__file__).resolve().parent
        self.assertTrue((root/'assets/routepilot.svg').exists())
        self.assertEqual((root/'assets/routepilot.png').read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
    def test_desktop_and_tk(self):
        root=Path(__file__).resolve().parent
        self.assertIn('Icon=/opt/routepilot/routepilot.png',(root/'install.sh').read_text())
        self.assertIn('root.iconphoto(True, app_icon)',(root/'routepilot.py').read_text())
if __name__=='__main__': unittest.main()
