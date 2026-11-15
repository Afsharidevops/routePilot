import ast,hashlib,pathlib,unittest
p=pathlib.Path(__file__).with_name('routepilot.py')
class NotificationTests(unittest.TestCase):
 def test_syntax(self): ast.parse(p.read_text())
 def test_modern_toast_used(self):
  s=p.read_text(); self.assertIn('def notify(message, kind=',s)
  self.assertNotIn("messagebox.showinfo('Saved'",s)
  self.assertIn("notify('Administrator confirmed.",s)
  self.assertIn("notify(detail[:700], kind='error'",s)
 def test_routing_unchanged(self):
  s=p.read_text().split('def gui():',1)[0]
  original=(p.parent.parent/'routepilot-ubuntu-v1.6-responsive-layout'/'routepilot.py').read_text().split('def gui():',1)[0]
  self.assertEqual(hashlib.sha256(s.encode()).digest(),hashlib.sha256(original.encode()).digest())
if __name__=='__main__':unittest.main()
