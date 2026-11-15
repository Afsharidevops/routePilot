import importlib.util
import struct
import unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('routepilot_dns', Path(__file__).with_name('routepilot.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def dns(name, ip):
    labels=b''.join(bytes([len(x)])+x.encode() for x in name.split('.'))+b'\0'
    header=struct.pack('!HHHHHH',1,0x8180,1,1,0,0)
    question=labels+struct.pack('!HH',1,1)
    answer=b'\xc0\x0c'+struct.pack('!HHIH',1,1,120,4)+bytes(int(x) for x in ip.split('.'))
    return header+question+answer

def ethernet(payload):
    udp=struct.pack('!HHHH',53,55000,8+len(payload),0)+payload
    ipv4=bytes([0x45,0])+struct.pack('!H',20+len(udp))+b'\0\0\0\0'+bytes([64,17])+b'\0\0'+bytes([8,8,8,8])+bytes([192,168,1,5])
    return b'\0'*12+b'\x08\x00'+ipv4+udp

class DNSObserverTest(unittest.TestCase):
    def test_public_answer(self):
        self.assertEqual(m.parse_dns_ipv4(dns('cdn.namava.ir','94.182.176.33')),
                         ('cdn.namava.ir',[('94.182.176.33',120)]))
    def test_private_skipped(self):
        self.assertEqual(m.parse_dns_ipv4(dns('test.namava.ir','172.18.179.68')),
                         ('test.namava.ir',[]))
    def test_udp_packet(self):
        self.assertEqual(m.dns_from_ethernet(ethernet(dns('a.test.ir','1.2.3.4'))),dns('a.test.ir','1.2.3.4'))
    def test_not_response(self):
        payload=bytearray(dns('cdn.namava.ir','8.8.8.8'));payload[2:4]=b'\x01\0'
        self.assertIsNone(m.parse_dns_ipv4(bytes(payload)))
    def test_truncated(self):
        self.assertIsNone(m.parse_dns_ipv4(dns('cdn.namava.ir','8.8.8.8')[:-2]))

if __name__=='__main__':unittest.main()
