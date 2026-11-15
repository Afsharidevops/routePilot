import pathlib, unittest
class ColorRowsTests(unittest.TestCase):
    def test_gui_tags_and_selection(self):
        src=pathlib.Path(__file__).with_name("routepilot.py").read_text()
        self.assertIn("tree.tag_configure('enabled'",src)
        self.assertIn("tree.tag_configure('disabled'",src)
        self.assertIn("tag='enabled' if is_enabled else 'disabled'",src)
        self.assertIn('tree.selection_set(existing)',src)
if __name__=='__main__': unittest.main()
