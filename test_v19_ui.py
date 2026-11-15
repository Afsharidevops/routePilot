import ast,unittest,pathlib
BASE=pathlib.Path(__file__).resolve().parent
PREV=BASE.parent/'routepilot-ubuntu-v1.8-all-features'/'routepilot.py'
class Tests(unittest.TestCase):
 def test_engine_unchanged(self):
  a={n.name:ast.dump(n,include_attributes=False) for n in ast.parse(PREV.read_text()).body if isinstance(n,ast.FunctionDef)}
  b={n.name:ast.dump(n,include_attributes=False) for n in ast.parse((BASE/'routepilot.py').read_text()).body if isinstance(n,ast.FunctionDef)}
  for name in ('reconcile','_reconcile_locked','wildcard_setup','wildcard_snat_reconcile','wildcard_snat_cleanup','observe_dns_forever','cleanup','install_config','validate'):
   self.assertEqual(a[name],b[name],name)
 def test_waiting_does_not_override_color(self):
  source=(BASE/'routepilot.py').read_text()
  self.assertNotIn("elif live=='Waiting DNS':tag='waiting'",source)
  self.assertIn("tag='enabled' if is_enabled else 'disabled'",source)
 def test_features(self):
  s=(BASE/'routepilot.py').read_text()
  for name in ('class Tooltip:', "'LIVE STATUS','IPs'", 'theme_button.configure', "style='RP.TEntry'", "style='RP.TCheckbutton'"):
   self.assertIn(name,s)
if __name__=='__main__':unittest.main()
