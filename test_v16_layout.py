import ast, pathlib, unittest
p=pathlib.Path(__file__).with_name('routepilot.py')
class LayoutTests(unittest.TestCase):
 def test_python_syntax(self): ast.parse(p.read_text())
 def test_reserved_footer_precedes_table(self):
  s=p.read_text(); self.assertLess(s.index("footer.pack(side='bottom'"),s.index("tablewrap ="))
 def test_actions_placed_in_two_rows(self):
  s=p.read_text();self.assertIn("grid(row=0,column=col",s);self.assertIn("grid(row=1,column=4",s)
 def test_no_change_to_routing_engine(self):
  s=p.read_text(); old=pathlib.Path(__file__).parent / 'routing-engine-sha256.txt'
  import hashlib
  engine=s[:s.index('def gui():')]
  self.assertEqual(hashlib.sha256(engine.encode()).hexdigest(),old.read_text().strip())
if __name__=='__main__': unittest.main()
