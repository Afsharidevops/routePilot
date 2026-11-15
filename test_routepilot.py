import importlib.util
import unittest
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('routepilot', __file__.replace('test_routepilot.py','routepilot.py'))
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)

class ValidationTests(unittest.TestCase):
    def rule(self, typ, val, gw=''):
        return dict(id='aabbccddee00',type=typ,value=val,interface='wlp0s1',gateway=gw,enabled=True)
    def test_valid_types(self):
        for typ,val in [('ip','1.1.1.1'),('cidr','192.0.2.0/24'),('fqdn','filimo.com')]:
            self.assertEqual(len(rp.validate({'rules':[self.rule(typ,val)]})['rules']),1)
    def test_injection_rejected(self):
        for typ,val in [('fqdn','test.com;touch /tmp/x'),('ip','1.1.1.1;true'),('cidr','1.2.0.0/24;true')]:
            with self.assertRaises((ValueError,Exception)): rp.validate({'rules':[self.rule(typ,val)]})
    def test_bad_gateway_rejected(self):
        with self.assertRaises(ValueError): rp.validate({'rules':[self.rule('ip','1.1.1.1','bad')]})
    def test_duplicate_ids_rejected(self):
        a=self.rule('ip','1.1.1.1')
        with self.assertRaises(ValueError): rp.validate({'rules':[a,a]})
    def test_rule_cap(self):
        with self.assertRaises(ValueError): rp.validate({'rules':[self.rule('ip','1.1.1.1')]*101})
    def test_dns_result_format(self):
        self.assertEqual(rp.dests(self.rule('ip','1.1.1.1')),['1.1.1.1/32'])
        self.assertEqual(rp.dests(self.rule('cidr','192.0.2.45/24')),['192.0.2.0/24'])
    def test_reconciler_dns_fallback(self):
        rule = self.rule('fqdn','filimo.com')
        old = {'applied':[dict(id=rule['id'],priority=850,table=23000,interface='wlp0s1',gateway='192.168.1.1',destinations=['203.0.113.7/32'])]}
        writes=[]
        def read(path,default): return {'rules':[rule]} if path==rp.CONFIG else old
        class FakeProcess:
            returncode=0
            stderr=''
            stdout=''
        with patch.object(rp,'read_json',side_effect=read),patch.object(rp,'interfaces',return_value=['wlp0s1']),patch.object(rp,'gateway_for',return_value='192.168.1.1'),patch.object(rp,'dests',return_value=[]),patch.object(rp,'cmd',return_value=FakeProcess()),patch.object(rp,'managed_rule_exists',return_value=(True,True)),patch.object(rp,'atomic_write',side_effect=lambda *x:writes.append(x)):
            warnings=rp._reconcile_locked()
        self.assertTrue(any('retained previous' in w for w in warnings))
        self.assertEqual(writes[-1][1]['applied'][0]['destinations'],['203.0.113.7/32'])

class WildcardObserverTests(unittest.TestCase):
    def test_wildcard_suffix_boundary(self):
        active=[(1,'namava.ir'),(3,'example.org')]
        self.assertEqual(rp.matching_wildcards('static.namava.ir',active),[1])
        self.assertEqual(rp.matching_wildcards('namava.ir',active),[])
        self.assertEqual(rp.matching_wildcards('evilnamava.ir',active),[])
        self.assertEqual(rp.matching_wildcards('deep.static.namava.ir',active),[1])

    def test_cooked_ip_dns_packet(self):
        import socket, struct
        name=b'\x06static\x06namava\x02ir\x00'
        question=name+struct.pack('!HH',1,1)
        answer=b'\xc0\x0c'+struct.pack('!HHIH',1,1,120,4)+bytes([94,182,176,33])
        dns=struct.pack('!HHHHHH',1,0x8180,1,1,0,0)+question+answer
        udp=struct.pack('!HHHH',53,54321,8+len(dns),0)+dns
        ip=bytes([0x45,0,0,20+len(udp),0,0,0,0,64,17,0,0])+socket.inet_aton('10.0.0.53')+socket.inet_aton('10.0.0.12')
        self.assertEqual(rp.dns_from_ipv4(ip+udp),dns)
        self.assertEqual(rp.parse_dns_ipv4(dns),('static.namava.ir',[('94.182.176.33',120)]))
        # Truncated packet and fragmented IP must be rejected.
        self.assertIsNone(rp.dns_from_ipv4(ip+udp[:-1]))
        fragment=bytearray(ip+udp); fragment[7]=1
        self.assertIsNone(rp.dns_from_ipv4(fragment))

if __name__=='__main__': unittest.main()

class AutoNatTests(unittest.TestCase):
    def test_mark_and_interface_scoped(self):
        prepared=[{'type':'wildcard','table':23001,'interface':'wlp56s0'},
                  {'type':'fqdn','table':23000,'interface':'tun0'},
                  {'type':'wildcard','table':23003,'interface':'enp55s0'}]
        spec=rp.wildcard_snat_spec(prepared)
        self.assertEqual(spec, [(0x2001,'wlp56s0'),(0x2003,'enp55s0')])
        nft=rp.wildcard_snat_script(spec)
        self.assertIn('meta mark 0x2001 oifname "wlp56s0" masquerade',nft)
        self.assertIn('meta mark 0x2003 oifname "enp55s0" masquerade',nft)
        self.assertNotIn('tun0',nft)
    def test_invalid_interface_rejected(self):
        with self.assertRaises(ValueError):
            rp.wildcard_snat_script([(0x2001,'eth0"; delete table ip nat')])
    def test_unowned_table_not_deleted(self):
        class P:
            returncode=0
            stderr=''
        with patch.object(rp,'NAT_SIGNATURE',__import__('pathlib').Path('/tmp/routepilot-test-nonexistent-owner-98375')), patch.object(rp,'cmd',return_value=P()) as mock:
            with self.assertRaises(RuntimeError): rp.wildcard_snat_cleanup()
            self.assertFalse(any(len(c.args)>1 and c.args[1]=='delete' for c in mock.call_args_list))
