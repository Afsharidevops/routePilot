import pathlib, unittest, sys, unittest.mock as mock
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import routepilot as app
class V11Tests(unittest.TestCase):
    def test_bulk_ui(self):
        src = pathlib.Path(app.__file__).read_text()
        for expected in ("selectmode='extended'", "def selected_many():", "def set_enabled(enabled):", "Delete selected", "Enable selected", "Disable selected"):
            self.assertIn(expected,src)
    def test_wildcard_apex_resolve(self):
        rule={'type':'wildcard','value':'*.namava.ir'}
        with mock.patch.object(app.socket,'getaddrinfo',return_value=[(None,None,None,None,('94.182.176.33',0))]) as g:
            self.assertEqual(app.dests(rule), ['94.182.176.33/32'])
            self.assertEqual(g.call_args.args[0],'namava.ir')
