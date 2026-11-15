import unittest
from test_routepilot import rp
class WildcardTests(unittest.TestCase):
    def test_good_wildcard(self):
        r=dict(id='abcabcabcabc',type='wildcard',value='*.filimo.com',interface='eth0',gateway='',enabled=True)
        rp.validate({'rules':[r]})
        self.assertEqual(rp.dests(r),[])
    def test_reject_bad_wildcard(self):
        for value in ['*','*.','filimo.com','*.*.com','*.test.com;evil','*.localhost']:
            r=dict(id='abcabcabcabc',type='wildcard',value=value,interface='eth0',gateway='',enabled=True)
            with self.assertRaises(ValueError): rp.validate({'rules':[r]})
if __name__=='__main__': unittest.main()
