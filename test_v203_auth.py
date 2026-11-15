import ast, pathlib, unittest
p=pathlib.Path(__file__).with_name("routepilot.py")
class AuthorizationFlowTests(unittest.TestCase):
 def test_async_and_single_submit(self):
  code=p.read_text()
  self.assertIn("if apply_state['busy']:",code)
  self.assertIn("threading.Thread(target=privileged_write, daemon=True).start()",code)
  self.assertIn("root.after(100, finish_apply)",code)
 def test_success_after_process_exit(self):
  code=p.read_text()
  self.assertIn("if code == 0 and 'Saved rules.' in out:",code)
  self.assertIn("elif code in (126, 127)",code)
  self.assertIn("atomic_write(LOCAL, snapshot, 0o600)",code)
 def test_original_routing_engine_preserved(self):
  prev=(p.parent.parent/'routepilot-ubuntu-v2.0.2-fixed-rule-editor'/'routepilot.py').read_text().split('def gui():')[0]
  curr=p.read_text().split('def gui():')[0]
  self.assertEqual(prev,curr)
