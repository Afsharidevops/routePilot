#!/usr/bin/env python3
"""RoutePilot: local IPv4 destination routing GUI + reconciler for Ubuntu.
No VPN-server changes. Stdlib GUI; system routing via iproute2.
MIT-licensed original implementation inspired by RiftRoute's general design.
"""
import argparse
import fcntl
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import struct
import threading
import uuid
from pathlib import Path

APP = 'routepilot'
CONFIG = Path('/etc/routepilot/rules.json')
STATE = Path('/var/lib/routepilot/state.json')
LOCAL = Path.home() / '.config/routepilot/rules.json'
HOST = re.compile(r'^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$')
BASE_TABLE = 23000
NFT_TABLE = 'routepilot'
NAT_TABLE = 'routepilot_snat'
NAT_SIGNATURE = Path('/var/lib/routepilot/snat-signature.json')
DNSMASQ_CONF = Path('/etc/dnsmasq.d/routepilot.conf')
RULE_PRIORITY = 850
MAX_RULES = 100
LOCK = Path("/run/routepilot.lock")

def cmd(*args, check=False):
    p = subprocess.run(args, text=True, capture_output=True)
    if check and p.returncode: raise RuntimeError(f'{" ".join(args)}: {p.stderr.strip()}')
    return p

def jcmd(*args):
    p = cmd(*args)
    if p.returncode: return []
    try: return json.loads(p.stdout)
    except ValueError: return []

def interfaces():
    result = []
    for x in jcmd('ip', '-j', 'link', 'show'):
        name = x.get('ifname', '')
        if name != 'lo': result.append(name)
    return result

def gateway_for(dev):
    # The physical gateway remains available via NetworkManager even when a
    # full-tunnel VPN replaces the kernel's default route.
    if shutil.which('nmcli'):
        p = cmd('nmcli', '-g', 'IP4.GATEWAY', 'device', 'show', dev)
        if p.returncode == 0:
            for line in p.stdout.splitlines():
                val = line.strip()
                if val:
                    try: return str(ipaddress.IPv4Address(val))
                    except ValueError: pass
    for r in jcmd('ip', '-j', '-4', 'route', 'show', 'default', 'dev', dev):
        if r.get('gateway'): return r['gateway']
    return ''

def validate(data):
    if not isinstance(data, dict) or not isinstance(data.get('rules'), list):
        raise ValueError('Configuration must be an object with a rules array')
    if len(data['rules']) > MAX_RULES: raise ValueError('Maximum 100 rules')
    ids = set()
    for idx, r in enumerate(data['rules']):
        if not isinstance(r, dict): raise ValueError(f'Rule {idx + 1} is invalid')
        rid = r.get('id', '')
        if not re.fullmatch(r'[0-9a-f]{12}', str(rid)) or rid in ids:
            raise ValueError(f'Rule {idx + 1}: invalid/duplicate ID')
        ids.add(rid)
        typ, val, dev = r.get('type'), r.get('value', ''), r.get('interface', '')
        if typ not in ('ip', 'cidr', 'fqdn', 'wildcard'): raise ValueError(f'Rule {idx + 1}: unsupported type')
        if not isinstance(val, str): raise ValueError('Invalid target')
        if typ == 'ip': ipaddress.IPv4Address(val)
        elif typ == 'cidr': ipaddress.IPv4Network(val, strict=False)
        elif typ == 'wildcard':
            if not val.startswith('*.') or not HOST.fullmatch(val[2:]):
                raise ValueError(f'Rule {idx + 1}: wildcard must look like *.example.com')
        elif not HOST.fullmatch(val): raise ValueError(f'Rule {idx + 1}: invalid FQDN')
        if not isinstance(dev, str) or not re.fullmatch(r'[a-zA-Z0-9_.:@-]{1,64}', dev):
            raise ValueError(f'Rule {idx + 1}: invalid interface')
        gw = r.get('gateway', '')
        if not isinstance(gw, str): raise ValueError('Invalid gateway')
        if gw: ipaddress.IPv4Address(gw)
        if not isinstance(r.get('enabled', True), bool): raise ValueError('Invalid enabled field')
    return data

def read_json(path, default):
    try: return json.loads(path.read_text())
    except FileNotFoundError: return default
    except ValueError as e: raise RuntimeError(f'Corrupt JSON {path}: {e}')

def atomic_write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.routepilot-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
            f.write('\n')
            f.flush(); os.fsync(f.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)

def install_config(path):
    if os.geteuid() != 0: raise RuntimeError('Root privileges required (pkexec)')
    # Never execute config contents or pass unvalidated text to a shell.
    source = Path(path).resolve(strict=True)
    if not source.is_file() or source.stat().st_size > 100_000:
        raise ValueError('Invalid configuration file')
    data = validate(json.loads(source.read_text()))
    atomic_write(CONFIG, data)
    print('Saved rules. The daemon will apply changes shortly.')

def is_p2p(dev):
    for link in jcmd('ip', '-j', 'link', 'show', 'dev', dev):
        return 'POINTOPOINT' in link.get('flags', [])
    return False

def dests(rule):
    typ, value = rule['type'], rule['value']
    if typ == 'ip': return [str(ipaddress.IPv4Address(value)) + '/32']
    if typ == 'cidr': return [str(ipaddress.IPv4Network(value, strict=False))]
    if typ == 'wildcard':
        # The apex is handled like a normal FQDN; arbitrary subdomains still
        # require DNS observation via dnsmasq (never guess wildcard addresses).
        value = value[2:]
    try:
        ips = {i[4][0] for i in socket.getaddrinfo(value, None, socket.AF_INET, socket.SOCK_STREAM)}
        return sorted({str(ipaddress.IPv4Address(x)) + '/32' for x in ips})[:32]
    except (OSError, ValueError):
        return []

def rule_add(dest, table, priority):
    p = cmd('ip', '-4', 'rule', 'add', 'priority', str(priority), 'to', dest, 'lookup', str(table))
    if p.returncode and 'File exists' not in p.stderr:
        raise RuntimeError(p.stderr.strip())

def rule_delete(dest, table, priority):
    cmd('ip', '-4', 'rule', 'del', 'priority', str(priority), 'to', dest, 'lookup', str(table))

def managed_rule_exists(dest, table, priority):
    """True only for an exact destination/table/priority triple."""
    occupied = False
    for r in jcmd('ip', '-j', '-4', 'rule', 'show'):
        if int(r.get('priority', -1)) != priority: continue
        target = r.get('dst', 'all')
        lookup = str(r.get('table', r.get('lookup', '')))
        if target == dest or (target == dest.split('/')[0] and dest.endswith('/32')):
            if lookup == str(table): return True, True
            occupied = True
        elif lookup != str(table):
            occupied = True
    return False, occupied

def nft_available():
    return shutil.which('nft') is not None


def dns_name(data, offset, depth=0):
    """Decode a compressed DNS name with strict bounds and recursion limits."""
    if depth > 12: raise ValueError('DNS compression loop')
    labels=[]
    while True:
        if offset >= len(data): raise ValueError('short DNS name')
        n=data[offset]; offset+=1
        if n == 0: break
        if n & 0xc0 == 0xc0:
            if offset >= len(data): raise ValueError('short DNS pointer')
            ptr=((n&0x3f)<<8)|data[offset]; offset+=1
            part,_=dns_name(data,ptr,depth+1)
            if part: labels.append(part)
            break
        if n&0xc0 or n>63 or offset+n>len(data): raise ValueError('bad DNS label')
        labels.append(data[offset:offset+n].decode('ascii').lower()); offset+=n
    return '.'.join(labels), offset


def parse_dns_ipv4(packet):
    """Return (queried FQDN, globally routable A records) for a plain DNS reply."""
    if len(packet)<12: return None
    _,flags,qd,an,_,_=struct.unpack_from('!HHHHHH',packet)
    if not flags & 0x8000 or flags & 0xf or qd!=1 or an>100: return None
    try:
        name,off=dns_name(packet,12)
        if off+4>len(packet): return None
        qtype,qclass=struct.unpack_from('!HH',packet,off); off+=4
        if qclass!=1: return None
        addresses=set()
        for _ in range(an):
            _,off=dns_name(packet,off)
            if off+10>len(packet): return None
            typ,cls,ttl,ln=struct.unpack_from('!HHIH',packet,off);off+=10
            if off+ln>len(packet): return None
            if typ==1 and cls==1 and ln==4:
                ip=ipaddress.IPv4Address(packet[off:off+4])
                if ip.is_global: addresses.add((str(ip), min(max(ttl,30),900)))
            off+=ln
        return name, sorted(addresses)
    except (ValueError,UnicodeError,struct.error,ipaddress.AddressValueError):
        return None


def dns_from_ipv4(packet):
    """Parse L3 IPv4 UDP/53 response received via AF_PACKET SOCK_DGRAM.

    SOCK_DGRAM (cooked packet socket) strips the link header, which is crucial
    for tun/tap, loopback and physical interfaces with different L2 formats.
    """
    if len(packet) < 28: return None
    version_ihl = packet[0]
    ihl = (version_ihl & 15) * 4
    if version_ihl >> 4 != 4 or ihl < 20 or len(packet) < ihl + 8: return None
    if packet[9] != 17: return None  # UDP only
    # Skip fragmented IPv4 packets. Never interpret a fragment as DNS.
    if int.from_bytes(packet[6:8], 'big') & 0x3fff: return None
    length = int.from_bytes(packet[2:4], 'big')
    if length < ihl + 8 or length > len(packet): return None
    source = int.from_bytes(packet[ihl:ihl+2], 'big')
    if source != 53: return None
    udp_length = int.from_bytes(packet[ihl+4:ihl+6], 'big')
    if udp_length < 8 or udp_length > length - ihl: return None
    return packet[ihl+8:ihl+udp_length]


def matching_wildcards(domain, active):
    """Strict suffix boundary: a.bad.test matches *.bad.test, bad.test does not."""
    return [idx for idx, suffix in active if domain.lower().rstrip('.').endswith('.'+suffix)]


def dns_from_ethernet(frame):
    """Compatibility parser for tests/older tools using Ethernet frames."""
    if len(frame) < 14: return None
    proto = int.from_bytes(frame[12:14], 'big'); off = 14
    for _ in range(2):
        if proto not in (0x8100, 0x88a8): break
        if len(frame) < off + 4: return None
        proto = int.from_bytes(frame[off+2:off+4], 'big'); off += 4
    if proto != 0x0800: return None
    return dns_from_ipv4(frame[off:])


def wildcard_setup(rules):
    """Provision nft marks. Passive DNS capture is handled by the daemon.

    No DNS resolver, VPN, Tailscale, or dnsmasq configuration is modified.
    """
    active=[(i,r) for i,r in enumerate(rules) if r.get('enabled',True) and r['type']=='wildcard']
    legacy=DNSMASQ_CONF
    if legacy.exists():
        try:
            if 'RoutePilot wildcard' in legacy.read_text(): legacy.unlink()
        except OSError: pass
    if not active:
        cmd('nft','delete','table','inet',NFT_TABLE)
        return []
    if not nft_available(): return ['Wildcard requires nftables']
    import hashlib
    signature=hashlib.sha256(repr([(i,r['value'],r['interface'],r.get('gateway','')) for i,r in active]).encode()).hexdigest()
    signature_path=Path('/var/lib/routepilot/nft-signature')
    current=read_json(signature_path,{}).get('signature','')
    existing=cmd('nft','list','table','inet',NFT_TABLE)
    if current!=signature or existing.returncode:
        # Only remove RoutePilot's own table, never system/firewall tables.
        cmd('nft','delete','table','inet',NFT_TABLE)
        script=['table inet routepilot {']
        for i,_ in active:
            script.append(f' set w{i} {{ type ipv4_addr; flags timeout; timeout 5m; }}')
        script.append(' chain output { type route hook output priority mangle; policy accept;')
        for i,_ in active:
            script.append(f'  ip daddr @w{i} meta mark set {8192+i}')
        script.append(' }')
        script.append('}')
        proc=subprocess.run(['nft','-f','-'],input='\n'.join(script),text=True,capture_output=True)
        if proc.returncode: return [f'Wildcard nft setup failed: {proc.stderr.strip()}']
        atomic_write(signature_path,{'signature':signature})
    return []


def wildcard_snat_spec(prepared):
    """NAT only wildcard-marked flows on the rule's selected egress interface.

    No broad masquerade on a physical interface; never rewrite VPN/Docker/
    Tailscale NAT tables. Marks are exact, not prefix masked.
    """
    return sorted({(8192 + int(item['table']) - BASE_TABLE, item['interface'])
                   for item in prepared if item.get('type') == 'wildcard'})


def wildcard_snat_script(spec):
    """Return nftables source used to initialize the RoutePilot-owned NAT table."""
    lines = [f'table ip {NAT_TABLE} {{',
             ' chain postrouting { type nat hook postrouting priority 99; policy accept;']
    for mark, dev in spec:
        # Interface values are validated before they enter prepared.
        if not re.fullmatch(r'[a-zA-Z0-9_.:@-]{1,64}', dev):
            raise ValueError('Invalid NAT interface')
        lines.append(f'  meta mark 0x{mark:x} oifname "{dev}" masquerade')
    lines.extend([' }', '}'])
    return '\n'.join(lines) + '\n'


def wildcard_snat_cleanup():
    """Remove only our owned NAT table. Never touch external NAT rules."""
    if NAT_SIGNATURE.exists():
        p = cmd('nft', 'delete', 'table', 'ip', NAT_TABLE)
        if p.returncode and 'No such file' not in p.stderr:
            raise RuntimeError(f'Cannot remove RoutePilot NAT: {p.stderr.strip()}')
        NAT_SIGNATURE.unlink()
    elif cmd('nft', 'list', 'table', 'ip', NAT_TABLE).returncode == 0:
        raise RuntimeError('Unowned routepilot_snat table exists; refusing to delete it')


def wildcard_snat_reconcile(prepared):
    """Recreate narrowly scoped NAT only when config changes or table is lost.

    The signature is an ownership token and persisted across service restarts.
    Missing signature + existing table is treated as collision, never overwritten.
    """
    spec = wildcard_snat_spec(prepared)
    existing = cmd('nft', 'list', 'table', 'ip', NAT_TABLE).returncode == 0
    if not spec:
        if not NAT_SIGNATURE.exists():
            return []
        wildcard_snat_cleanup()
        return []
    import hashlib
    signature = hashlib.sha256(repr(spec).encode()).hexdigest()
    recorded = read_json(NAT_SIGNATURE, {}).get('signature')
    if existing and not NAT_SIGNATURE.exists():
        return ['NAT table collision: unowned routepilot_snat already exists; no changes made']
    if existing and recorded == signature:
        return []
    # Make a replacement script, then atomically load the complete nft batch.
    # If a malformed rule is supplied, nft rejects the whole batch.
    script = (f'delete table ip {NAT_TABLE}\n' if existing else '') + wildcard_snat_script(spec)
    result = subprocess.run(['nft', '-f', '-'], input=script, text=True, capture_output=True)
    if result.returncode:
        return [f'Wildcard NAT setup failed: {result.stderr.strip()}']
    atomic_write(NAT_SIGNATURE, {'signature': signature, 'spec': spec})
    return []


def wildcard_cleanup():
    cmd('nft','delete','table','inet',NFT_TABLE)
    sig=Path('/var/lib/routepilot/nft-signature')
    if sig.exists(): sig.unlink()
    # Old RoutePilot generated this file; do not alter any other DNS config.
    if DNSMASQ_CONF.exists():
        try:
            if 'RoutePilot wildcard' in DNSMASQ_CONF.read_text(): DNSMASQ_CONF.unlink()
        except OSError: pass


def observe_dns_forever():
    """Passive IPv4 DNS/UDP listener; does not change system DNS settings.

    Linux AF_PACKET/SOCK_DGRAM returns L3 IP packets, regardless of Ethernet,
    TUN or loopback link layer. This is especially important for Pritunl tun0.
    """
    while True:
        try:
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(0x0800))
            sock.settimeout(2)
            print('DNS observer listening for IPv4 UDP/53 responses (including tun devices)', flush=True)
            next_config = 0
            active = []
            generation = None
            try:
                while True:
                    if time.monotonic() >= next_config:
                        data = validate(read_json(CONFIG, {'rules': []}))
                        new_active = [(i, r['value'][2:].lower()) for i, r in enumerate(data['rules'])
                                      if r['type'] == 'wildcard' and r.get('enabled', True)]
                        new_generation = tuple(new_active)
                        if generation != new_generation:
                            print('DNS observer active wildcard suffixes: '+(', '.join(x[1] for x in new_active) or '(none)'), flush=True)
                        generation = new_generation
                        active = new_active
                        next_config = time.monotonic() + 5
                    try:
                        frame, addr = sock.recvfrom(65535)
                    except socket.timeout:
                        continue
                    # Ignore outgoing packets (loopback and other interfaces can be noisy).
                    # Still accept incoming packets from tun0, ethernet and loopback.
                    if len(addr) >= 3 and addr[2] == socket.PACKET_OUTGOING:
                        continue
                    payload = dns_from_ipv4(frame)
                    if payload is None: continue
                    answer = parse_dns_ipv4(payload)
                    if not answer: continue
                    name, ips = answer
                    if not ips: continue
                    for idx in matching_wildcards(name, active):
                        for ip, ttl in ips:
                            # Short TTL ensures reassignment after IP changes;
                            # future DNS replies can renew the element.
                            timeout = min(ttl, 300)
                            p = cmd('nft', 'add', 'element', 'inet', NFT_TABLE, f'w{idx}',
                                    '{', f'{ip} timeout {timeout}s', '}')
                            if p.returncode == 0:
                                print(f'DNS wildcard matched {name} -> {ip} via nft w{idx}', flush=True)
                            elif 'File exists' in p.stderr:
                                pass
                            else:
                                print(f'DNS observer nft update failed for {name}: {p.stderr.strip()}', flush=True)
            finally:
                sock.close()
        except Exception as exc:
            print('DNS observer error:', exc, flush=True)
            time.sleep(5)


def reconcile():
    if os.geteuid() != 0:
        raise RuntimeError('Root privileges required')
    LOCK.touch(exist_ok=True)
    with LOCK.open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _reconcile_locked()

def _reconcile_locked():
    data = validate(read_json(CONFIG, {'rules': []}))
    old = read_json(STATE, {'applied': []})
    old_applied = old.get('applied', [])
    old_map = {item.get('id'): item for item in old_applied}
    available = set(interfaces())
    warnings, desired = [], []
    for idx, r in enumerate(data['rules']):
        if not r.get('enabled', True):
            continue
        dev = r['interface']
        if dev not in available:
            warnings.append(f"{r['value']}: {dev} unavailable")
            continue
        gw = r.get('gateway', '') or gateway_for(dev)
        if not gw and not is_p2p(dev):
            warnings.append(f"{r['value']}: no gateway for {dev}; specify one")
            continue
        destinations = dests(r)
        if not destinations and r['type']!='wildcard':
            previous = old_map.get(r['id'], {})
            destinations = previous.get('destinations', [])
            if destinations:
                warnings.append(f"{r['value']}: DNS failed; retained previous resolved IPs")
            else:
                warnings.append(f"{r['value']}: DNS failed; rule not applied")
                continue
        desired.append({'id': r['id'], 'table': BASE_TABLE+idx,
                        'priority': RULE_PRIORITY+idx, 'interface': dev,
                        'gateway': gw, 'destinations': destinations, 'type':r['type']})
    # New routes are prepared before changing rules; failed preparation
    # retains a previously functioning rule rather than deleting it.
    prepared = []
    for item in desired:
        args = ['ip', '-4', 'route', 'replace', 'default']
        if item['gateway']: args += ['via', item['gateway']]
        args += ['dev', item['interface'], 'table', str(item['table'])]
        result = cmd(*args)
        if result.returncode:
            warnings.append(f"{item['interface']}: route setup failed: {result.stderr.strip()}")
            previous = old_map.get(item['id'])
            if previous: prepared.append(previous)
        else:
            prepared.append(item)
    old_keys = {(x['priority'],x['table'],d) for x in old_applied for d in x.get('destinations', [])}
    wanted_keys = {(x['priority'],x['table'],d) for x in prepared for d in x['destinations']}
    confirmed = set(old_keys & wanted_keys)
    # Remove replaced/deleted old entries first so editing a rule works.
    # Only delete exact previously tracked triples, never unrelated policy rules.
    for priority,table,dest in sorted(old_keys-wanted_keys):
        owned,_ = managed_rule_exists(dest,table,priority)
        if owned: rule_delete(dest,table,priority)
    # Install new entries; refuse to overwrite external policy rules.
    for priority,table,dest in sorted(wanted_keys-old_keys):
        owned,occupied = managed_rule_exists(dest,table,priority)
        if occupied and not owned:
            warnings.append(f"Policy priority {priority} conflicts; skipped {dest}")
            continue
        if not owned:
            try: rule_add(dest,table,priority)
            except RuntimeError as e:
                warnings.append(f"Cannot add {dest}: {e}")
                continue
        confirmed.add((priority,table,dest))
    # Also repair our previously tracked rules after VPN/network resets.
    for priority,table,dest in sorted(old_keys & wanted_keys):
        owned,occupied = managed_rule_exists(dest,table,priority)
        if not owned and not occupied:
            try: rule_add(dest,table,priority)
            except RuntimeError as e: warnings.append(f"Repair {dest}: {e}")
    for t in {x['table'] for x in old_applied} - {x['table'] for x in prepared}:
        if BASE_TABLE <= int(t) < BASE_TABLE+MAX_RULES:
            cmd('ip','-4','route','flush','table',str(t))
    # Manage fwmark policy rules separately for wildcard DNS sets.
    previous_wild = {(x['priority'],x['table']) for x in old_applied if x.get('type')=='wildcard'}
    current_wild = {(x['priority'],x['table']) for x in prepared if x.get('type')=='wildcard'}
    def mark_rule(prio,table,operation):
        mark=8192+(int(table)-BASE_TABLE)
        return cmd('ip','-4','rule',operation,'priority',str(prio),'fwmark',str(mark),'lookup',str(table))
    for prio,table in sorted(previous_wild-current_wild): mark_rule(prio,table,'del')
    current_rules = cmd('ip','-4','rule','show').stdout
    for prio,table in sorted(current_wild):
        mark=8192+int(table)-BASE_TABLE
        expected=f'fwmark 0x{mark:x}'
        if not any(line.strip().startswith(f'{prio}:') and expected in line and f'lookup {table}' in line for line in current_rules.splitlines()):
            result=mark_rule(prio,table,'add')
            if result.returncode: warnings.append(f'Wildcard mark rule failed: {result.stderr.strip()}')
    wildcard_warnings=wildcard_setup(data['rules'])
    warnings.extend(wildcard_warnings)
    if not wildcard_warnings:
        warnings.extend(wildcard_snat_reconcile(prepared))
    else:
        warnings.append('Wildcard NAT unchanged because nft wildcard setup failed')
    if any(r['type']=='wildcard' and r.get('enabled',True) for r in data['rules']):
        warnings.append('Wildcard uses passive UDP/53 DNS observation. DoH/DoT, cached DNS and DNS over TCP are not observable; private addresses are excluded.')
    state = {'applied': prepared, 'warnings': warnings, 'updated': time.time()}
    atomic_write(STATE,state)
    return warnings

def cleanup():
    if os.geteuid()!=0: raise RuntimeError('Root privileges required')
    LOCK.touch(exist_ok=True)
    with LOCK.open('r+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        old = read_json(STATE, {'applied': []})
        for x in old.get('applied', []):
            for d in x.get('destinations', []):
                owned,_ = managed_rule_exists(d,x['table'],x['priority'])
                if owned: rule_delete(d,x['table'],x['priority'])
            if BASE_TABLE <= int(x['table']) < BASE_TABLE+MAX_RULES:
                cmd('ip','-4','route','flush','table',str(x['table']))
        for x in old.get('applied', []):
            if x.get('type')=='wildcard':
                mark=8192+int(x['table'])-BASE_TABLE
                cmd('ip','-4','rule','del','priority',str(x['priority']),'fwmark',str(mark),'lookup',str(x['table']))
        wildcard_cleanup()
        wildcard_snat_cleanup()
        atomic_write(STATE,{'applied':[],'warnings':[],'updated':time.time()})

def explain(target):
    addr = ipaddress.IPv4Address(target)
    p = cmd('ip','-4','route','get',str(addr))
    print(p.stdout.strip() if p.returncode==0 else p.stderr.strip())
    if p.returncode: raise RuntimeError('Cannot determine route')

def daemon():
    if os.geteuid() != 0: raise RuntimeError('Daemon must run as root')
    print('RoutePilot reconciler started', flush=True)
    threading.Thread(target=observe_dns_forever,daemon=True,name='dns-observer').start()
    while True:
        try:
            warnings = reconcile()
            if warnings: print('; '.join(warnings), flush=True)
        except Exception as e: print('Reconciliation failed:', e, flush=True)
        time.sleep(15)

def gui():
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    root = tk.Tk(className='RoutePilot'); root.title('RoutePilot  •  Route Manager')
    # Fit the initial window within the usable screen height on smaller laptops.
    screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f'{min(1160,max(800,screen_w-80))}x{min(790,max(540,screen_h-125))}')
    try:
        icon_path = Path('/opt/routepilot/routepilot.png')
        if not icon_path.is_file(): icon_path = Path(__file__).resolve().parent / 'assets' / 'routepilot.png'
        app_icon = tk.PhotoImage(file=str(icon_path))
        root.iconphoto(True, app_icon)
        root.after_idle(lambda: root.iconphoto(True, app_icon))
        root._routepilot_icon = app_icon
    except (tk.TclError, OSError) as exc:
        print(f'RoutePilot: could not load window icon: {exc}', file=sys.stderr)
    root.minsize(820, 600)
    PREFS = Path.home() / '.config/routepilot/ui.json'
    try: ui_prefs=json.loads(PREFS.read_text())
    except (OSError, ValueError): ui_prefs={}
    dark_mode = tk.BooleanVar(value=ui_prefs.get('theme')=='dark')
    colors = {'bg':'#f1f5f9','card':'#ffffff','ink':'#0f172a', 'muted':'#64748b',
              'primary':'#0f766e','primary_hover':'#115e59','border':'#dce5ef',
              'green_bg':'#e6f8ee','green':'#166534','red_bg':'#fff0f0','red':'#9f3030'}
    light_colors = dict(colors)
    dark_colors = {'bg':'#111827','card':'#1f2937','ink':'#f8fafc','muted':'#a7b4c6',
                   'primary':'#0d9488','primary_hover':'#0f766e','border':'#334155',
                   'green_bg':'#173d37','green':'#9ae6b4','red_bg':'#492e38','red':'#fecdd3'}
    if dark_mode.get(): colors.update(dark_colors)
    root.configure(bg=colors['bg'])
    style = ttk.Style(root)
    if 'clam' in style.theme_names(): style.theme_use('clam')
    style.configure('RP.TFrame', background=colors['bg'])
    style.configure('Card.TFrame', background=colors['card'])
    style.configure('RP.TLabel', background=colors['bg'], foreground=colors['ink'], font=('Sans',10))
    style.configure('Muted.TLabel', background=colors['bg'], foreground=colors['muted'], font=('Sans',10))
    style.configure('Card.TLabel', background=colors['card'], foreground=colors['ink'], font=('Sans',10))
    style.configure('RP.Treeview', background=colors['card'], fieldbackground=colors['card'],
                    foreground=colors['ink'], rowheight=35, borderwidth=0, font=('Sans',10))
    style.configure('RP.Treeview.Heading', background='#e9f0f7', foreground='#334155',
                    font=('Sans',10,'bold'), padding=(9,10), relief='flat')
    style.map('RP.Treeview', background=[('selected','#cfe7ff')], foreground=[('selected','#0f172a')])
    style.configure('Primary.TButton', padding=(13,9), font=('Sans',10,'bold'),
                    background=colors['primary'], foreground='white', borderwidth=0)
    style.map('Primary.TButton', background=[('active',colors['primary_hover'])], foreground=[('active','white')])
    style.configure('Soft.TButton', padding=(11,9), font=('Sans',10), background='#eaf0f6', foreground=colors['ink'])
    style.configure('RP.TEntry', padding=7, fieldbackground=colors['card'], foreground=colors['ink'])
    style.configure('RP.TCombobox', padding=6, fieldbackground=colors['card'], foreground=colors['ink'])
    style.configure('RP.TCheckbutton', background=colors['bg'], foreground=colors['ink'])
    style.configure('Dialog.TLabel', background=colors['bg'], foreground=colors['ink'])
    style.configure('Danger.TButton', padding=(11,9), font=('Sans',10), background='#fee2e2', foreground='#991b1b')
    data = read_json(CONFIG, {'rules': []})
    try: validate(data)
    except Exception: data = {'rules': []}
    def set_theme(is_dark, save=True):
        dark_mode.set(bool(is_dark))
        colors.update(dark_colors if dark_mode.get() else light_colors)
        root.configure(bg=colors['bg'])
        for key in ('RP.TFrame',): style.configure(key, background=colors['bg'])
        style.configure('Card.TFrame', background=colors['card'])
        style.configure('RP.TLabel', background=colors['bg'], foreground=colors['ink'])
        style.configure('Muted.TLabel', background=colors['bg'], foreground=colors['muted'])
        style.configure('Card.TLabel', background=colors['card'], foreground=colors['ink'])
        style.configure('RP.Treeview', background=colors['card'],fieldbackground=colors['card'],foreground=colors['ink'])
        style.configure('RP.Treeview.Heading', background='#334155' if dark_mode.get() else '#e9f0f7',
                        foreground=colors['ink'])
        style.map('RP.Treeview', background=[('selected','#365678' if dark_mode.get() else '#cfe7ff')],
                  foreground=[('selected','#ffffff' if dark_mode.get() else '#0f172a')])
        style.configure('Soft.TButton', background='#374151' if dark_mode.get() else '#eaf0f6', foreground=colors['ink'])
        style.configure('RP.TEntry', fieldbackground=colors['card'], foreground=colors['ink'])
        style.configure('RP.TCombobox', fieldbackground=colors['card'], foreground=colors['ink'])
        style.configure('RP.TCheckbutton', background=colors['bg'], foreground=colors['ink'])
        style.configure('Dialog.TLabel', background=colors['bg'], foreground=colors['ink'])
        style.configure('Danger.TButton', background='#63343e' if dark_mode.get() else '#fee2e2', foreground='#fecdd3' if dark_mode.get() else '#991b1b')
        style.configure('Primary.TButton', background=colors['primary'],foreground='white')
        for widget in root.winfo_children():
            def recolor(w):
                if isinstance(w, (tk.Frame, tk.Label)):
                    try:
                        old=w.cget('bg')
                        mapping={'#f1f5f9':colors['bg'],'#ffffff':colors['card'],
                                 '#111827':colors['bg'],'#1f2937':colors['card']}
                        if old in mapping: w.configure(bg=mapping[old])
                    except tk.TclError: pass
                if isinstance(w, tk.Label):
                    try:
                        old=w.cget('bg')
                        if old in ('#f1f5f9','#111827'): w.configure(bg=colors['bg'],fg=colors['ink'])
                        elif old in ('#ffffff','#1f2937'): w.configure(bg=colors['card'],fg=colors['ink'])
                    except tk.TclError:pass
                for ch in w.winfo_children(): recolor(ch)
            recolor(widget)
        for label in (brand_title, list_title):
            label.configure(style='RP.TLabel')
        for card, card_title, card_value in cards:
            card.configure(bg=colors['card'], highlightbackground=colors['border'])
            card_title.configure(bg=colors['card'],fg=colors['muted'])
            card_value.configure(bg=colors['card'],fg=colors['ink'])
        theme_button.configure(text='☀  Light mode' if dark_mode.get() else '☾  Dark mode')
        tree.tag_configure('enabled',background=colors['green_bg'],foreground=colors['green'])
        tree.tag_configure('disabled',background=colors['red_bg'],foreground=colors['red'])
        if save:
            PREFS.parent.mkdir(parents=True,exist_ok=True)
            atomic_write(PREFS,{'theme':'dark' if dark_mode.get() else 'light'},0o600)
    frame = ttk.Frame(root, padding=(18, 14), style='RP.TFrame'); frame.pack(fill='both', expand=True)
    # Reserve footer space *before* the expanding table, so action controls
    # are always visible even at the initial window size.
    footer = ttk.Frame(frame, style='RP.TFrame')
    footer.pack(side='bottom', fill='x')
    header = ttk.Frame(frame, style='RP.TFrame'); header.pack(fill='x', pady=(0,14))
    brand = ttk.Frame(header, style='RP.TFrame'); brand.pack(side='left')
    brand_title = ttk.Label(brand, text='◈  RoutePilot', font=('Sans',24,'bold'), style='RP.TLabel')
    brand_title.pack(anchor='w')
    ttk.Label(brand, text='Smart destination routing  ·  Ubuntu 22.04–26.04  ·  Local-only rules',
              style='Muted.TLabel').pack(anchor='w', pady=(4,0))
    badge = tk.Label(header, text='●  Checking service', bg='#e2e8f0', fg='#475569',
                     font=('Sans',10,'bold'), padx=14, pady=9)
    badge.pack(side='right', anchor='n', pady=5)
    theme_button = ttk.Button(header, text='☀  Light mode' if dark_mode.get() else '☾  Dark mode',style='Soft.TButton',
               command=lambda:set_theme(not dark_mode.get()))
    theme_button.pack(side='right',padx=12,pady=7)

    summary = ttk.Frame(frame, style='RP.TFrame'); summary.pack(fill='x', pady=(0,14))
    counts = {}
    cards = []
    for text, key in [('TOTAL RULES','total'), ('ENABLED','enabled'), ('DISABLED','disabled'), ('WILDCARD','wildcard')]:
        card = tk.Frame(summary, bg=colors['card'], highlightbackground=colors['border'], highlightthickness=1,
                        padx=16, pady=11)
        card.pack(side='left', fill='x', expand=True, padx=(0,10))
        card_title = tk.Label(card, text=text, bg=colors['card'], fg=colors['muted'], font=('Sans',9,'bold'))
        card_title.pack(anchor='w')
        value=tk.Label(card,text='0',bg=colors['card'],fg=colors['ink'],font=('Sans',20,'bold'))
        value.pack(anchor='w',pady=(3,0));counts[key]=value
        cards.append((card,card_title,value))
    list_header=ttk.Frame(frame,style='RP.TFrame');list_header.pack(fill='x',pady=(0,8))
    list_title = ttk.Label(list_header, text='Routing rules', font=('Sans',14,'bold'), style='RP.TLabel')
    list_title.pack(side='left')
    selection_hint = tk.StringVar(value='Ctrl / Shift + click to select multiple')
    ttk.Label(list_header,textvariable=selection_hint,style='Muted.TLabel').pack(side='right')
    filters=ttk.Frame(frame,style='RP.TFrame');filters.pack(fill='x',pady=(0,8))
    query=tk.StringVar()
    filter_mode=tk.StringVar(value='All')
    ttk.Label(filters,text='Search',style='Muted.TLabel').pack(side='left',padx=(0,7))
    search_entry=ttk.Entry(filters,textvariable=query,width=36,style='RP.TEntry')
    search_entry.pack(side='left',padx=(0,12))
    ttk.Label(filters,text='Filter',style='Muted.TLabel').pack(side='left',padx=(0,7))
    ttk.Combobox(filters,textvariable=filter_mode,values=['All','Enabled','Disabled','Wildcard','Issues'],
                 state='readonly',width=13,style='RP.TCombobox').pack(side='left')
    live_hint = tk.StringVar(value='Status is read from daemon state and nftables; not a connectivity test')
    ttk.Label(filters,textvariable=live_hint,style='Muted.TLabel').pack(side='right')
    class Tooltip:
        def __init__(self, widget, message):
            self.widget=widget;self.message=message;self.window=None;self.timer=None
            widget.bind('<Enter>',self.schedule,add='+')
            widget.bind('<Leave>',self.hide,add='+')
            widget.bind('<ButtonPress>',self.hide,add='+')
        def schedule(self,event=None):
            self.hide();self.timer=root.after(550,self.show)
        def show(self):
            self.timer=None
            if self.window or not self.widget.winfo_exists():return
            self.window=tk.Toplevel(self.widget)
            self.window.wm_overrideredirect(True)
            self.window.attributes('-topmost',True)
            x=self.widget.winfo_rootx()+14;y=self.widget.winfo_rooty()+self.widget.winfo_height()+7
            self.window.wm_geometry(f'+{x}+{y}')
            tk.Label(self.window,text=self.message,bg='#263449',fg='#f8fafc',
                     font=('Sans',9),padx=11,pady=8,justify='left',wraplength=330).pack()
        def hide(self,event=None):
            if self.timer is not None:
                root.after_cancel(self.timer);self.timer=None
            if self.window:
                self.window.destroy();self.window=None

    tablewrap = ttk.Frame(frame,style='Card.TFrame',padding=2);tablewrap.pack(fill='both',expand=True)
    cols = ('enabled', 'type', 'target', 'interface', 'gateway','live','ips')
    tree = ttk.Treeview(tablewrap, columns=cols, show='headings', selectmode='extended', height=12,
                        style='RP.Treeview')
    for col, label, width in zip(cols, ['STATUS','TYPE','DESTINATION','INTERFACE','GATEWAY','LIVE STATUS','IPs'],[95,95,250,120,125,145,65]):
        tree.heading(col, text=label); tree.column(col,width=width,anchor='w',stretch=(col=='target'))
    tree.tag_configure('enabled', background=colors['green_bg'], foreground=colors['green'])
    tree.tag_configure('disabled', background=colors['red_bg'], foreground=colors['red'])
    tree.tag_configure('waiting', background='#ffefc1',foreground='#704c00')
    tree.tag_configure('issue', background=colors['red_bg'],foreground=colors['red'])
    scroll=ttk.Scrollbar(tablewrap,orient='vertical',command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side='left',fill='both',expand=True);scroll.pack(side='right',fill='y')
    # Status explanations are shown in the README; never pop tooltips over the rule table.
    status = tk.StringVar(value='Settings are stored locally. Click Apply / Save to activate changes.')
    # Non-modal, in-app feedback: avoid the desktop's outdated Tk message box.
    # Keep one toast at a time and cancel older timers before creating a new one.
    toast_state = {'widget': None, 'timer': None}
    def notify(message, kind='success', duration=4200):
        old_timer = toast_state['timer']
        if old_timer is not None:
            try: root.after_cancel(old_timer)
            except (tk.TclError, ValueError): pass
        old = toast_state['widget']
        if old is not None:
            try: old.destroy()
            except tk.TclError: pass
        toast_state['timer'] = None
        toast_state['widget'] = None
        if kind == 'error':
            bg, fg, edge, glyph, title = '#fff1f2', '#9f1239', '#fecdd3', '✕', 'Could not save'
        elif kind == 'cancel':
            bg, fg, edge, glyph, title = '#e2e8f0', '#334155', '#cbd5e1', '•', 'Authentication cancelled'
        else:
            bg, fg, edge, glyph, title = '#dcfce7', '#166534', '#bbf7d0', '✓', 'Changes saved'
        toast = tk.Frame(tablewrap, bg=bg, highlightthickness=1, highlightbackground=edge,
                         padx=15, pady=10)
        tk.Label(toast, text=glyph, bg=bg, fg=fg, font=('Sans',18,'bold')).pack(side='left', padx=(0,12))
        texts = tk.Frame(toast, bg=bg)
        texts.pack(side='left', fill='x', expand=True)
        tk.Label(texts, text=title, bg=bg, fg=fg, font=('Sans',11,'bold'), anchor='w').pack(fill='x')
        tk.Label(texts, text=message, bg=bg, fg=fg, font=('Sans',9), anchor='w',
                 wraplength=560, justify='left').pack(fill='x', pady=(2,0))
        def close():
            if toast_state['widget'] is toast:
                toast_state['widget'] = None
                toast_state['timer'] = None
            if toast.winfo_exists(): toast.destroy()
        tk.Button(toast, text='×', command=close, borderwidth=0, relief='flat',
                  highlightthickness=0, bg=bg, fg=fg, font=('Sans',15),
                  activebackground=bg, activeforeground=fg, cursor='hand2').pack(side='right', padx=(12,0))
        # Overlay over the table area, not a geometry-consuming row: footer stays visible.
        toast.place(relx=0.5, y=16, anchor='n', relwidth=0.77)
        toast.lift()
        toast_state['widget'] = toast
        toast_state['timer'] = root.after(duration, close)

    def update_service_badge():
        try:
            r=subprocess.run(['systemctl','is-active','routepilot'],capture_output=True,text=True,timeout=2)
            live=r.returncode==0 and r.stdout.strip()=='active'
            badge.configure(text='●  Service running' if live else '●  Service offline',
                            bg='#dcfce7' if live else '#fee2e2',fg='#166534' if live else '#991b1b')
        except (OSError, subprocess.TimeoutExpired):
            badge.configure(text='●  Service unknown',bg='#fef3c7',fg='#92400e')
        root.after(12000,update_service_badge)
    def wildcard_ip_counts():
        try:
            result=subprocess.run(['nft','-j','list','table','inet',NFT_TABLE],capture_output=True,text=True,timeout=3)
            if result.returncode:return {}
            output={}
            for item in json.loads(result.stdout).get('nftables',[]):
                st=item.get('set',{})
                name=st.get('name','')
                if re.fullmatch(r'w\d+',name):
                    # nft -j emits set elements as plain addresses or dicts with timeouts.
                    output[name]=len(st.get('elem',[]))
            return output
        except (OSError,ValueError,subprocess.TimeoutExpired):return {}
    def rule_live(r,applied,warnings,counts_ip):
        if not r.get('enabled',True):return 'Disabled'
        if any(r['value'] in w for w in warnings):return 'Issue ⚠'
        if r['id'] not in applied:return 'Not applied'
        if r['type']=='wildcard':
            idx=next((i for i,rr in enumerate(data['rules']) if rr['id']==r['id']),None)
            if idx is None:return 'Waiting DNS'
            count=counts_ip.get(f'w{idx}',0)
            return f'Observed {count} IP(s)' if count else 'Waiting DNS'
        return 'Applied'
    def refresh():
        previous_selection = tree.selection()
        tree.delete(*tree.get_children())
        st=read_json(STATE,{'applied':[],'warnings':[]})
        applied={x.get('id') for x in st.get('applied',[])}
        warnings=st.get('warnings',[])
        ip_counts=wildcard_ip_counts()
        search=query.get().strip().casefold()
        mode=filter_mode.get()
        for r in data['rules']:
            is_enabled = r.get('enabled', True)
            live=rule_live(r,applied,warnings,ip_counts)
            if search and not any(search in str(r.get(k,'')).casefold() for k in ('value','type','interface','gateway')):continue
            if mode=='Enabled' and not is_enabled:continue
            if mode=='Disabled' and is_enabled:continue
            if mode=='Wildcard' and r['type']!='wildcard':continue
            if mode=='Issues' and not (live in ('Issue ⚠','Not applied','Waiting DNS')):continue
            tag='enabled' if is_enabled else 'disabled'
            if live in ('Issue ⚠','Not applied'):tag='issue'
            # Waiting DNS is informational: an enabled rule remains green.
            tree.insert('', 'end', iid=r['id'], tags=(tag,),
                        values=('● ON' if is_enabled else '○ OFF',r['type'],r['value'],
                                r['interface'],r.get('gateway','') or '(auto)',live,
                                str(ip_counts.get('w'+str(next((i for i,x in enumerate(data['rules']) if x['id']==r['id']),-1)),0)) if r['type']=='wildcard' else '—'))
        counts['total'].config(text=str(len(data['rules'])))
        counts['enabled'].config(text=str(sum(r.get('enabled',True) for r in data['rules'])))
        counts['disabled'].config(text=str(sum(not r.get('enabled',True) for r in data['rules'])))
        counts['wildcard'].config(text=str(sum(r['type']=='wildcard' for r in data['rules'])))
        existing=[rid for rid in previous_selection if tree.exists(rid)]
        if existing:
            tree.selection_set(existing);tree.focus(existing[-1])
    query.trace_add('write',lambda *_:refresh())
    filter_mode.trace_add('write',lambda *_:refresh())
    def refresh_live():
        if not root.winfo_exists():return
        try:refresh()
        except (OSError,ValueError,RuntimeError):pass
        root.after(7000,refresh_live)
    def selected_many():
        ids = set(tree.selection())
        return [r for r in data['rules'] if r['id'] in ids]
    def selected():
        matches=selected_many()
        return matches[0] if len(matches)==1 else None
    def edit(existing=None):
        """Self-contained theme-aware rule editor; does not mutate saved rules until Save."""
        win = tk.Toplevel(root, class_='RoutePilot')
        win.title('Edit rule  •  RoutePilot' if existing else 'New rule  •  RoutePilot')
        win.transient(root)
        win.configure(bg=colors['bg'])
        win.geometry('650x660')
        win.minsize(515, 430)
        win.resizable(True, True)
        try:
            if hasattr(root, '_routepilot_icon'):
                win.iconphoto(True, root._routepilot_icon)
        except tk.TclError:
            pass
        win.grab_set()
        palette = dict(colors)  # Match the theme at the instant the dialog opens.
        # A dedicated bottom frame is packed before the scrollable form: Tk always
        # reserves space for actions, even on small screens or large display scaling.
        footer = tk.Frame(win, bg=palette['bg'], padx=24, pady=10)
        footer.pack(side='bottom', fill='x')
        outer = tk.Frame(win, bg=palette['bg'], padx=24, pady=15)
        outer.pack(fill='both', expand=True)
        tk.Label(outer, text='✦  EDIT ROUTING RULE' if existing else '✦  NEW ROUTING RULE',
                 bg=palette['bg'], fg=palette['primary'], font=('Sans', 10, 'bold')).pack(anchor='w')
        tk.Label(outer, text='Edit destination' if existing else 'Add destination',
                 bg=palette['bg'], fg=palette['ink'], font=('Sans', 21, 'bold')).pack(anchor='w', pady=(4, 2))
        tk.Label(outer, text='Choose which network interface handles traffic for this destination.',
                 bg=palette['bg'], fg=palette['muted'], font=('Sans', 10), wraplength=530,
                 justify='left').pack(anchor='w', pady=(0, 15))
        # The form scrolls independently while footer buttons stay visible.
        scroller = tk.Frame(outer, bg=palette['bg'])
        scroller.pack(fill='both', expand=True)
        canvas = tk.Canvas(scroller, bg=palette['card'], highlightthickness=1,
                           highlightbackground=palette['border'], bd=0)
        vbar = ttk.Scrollbar(scroller, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        card = tk.Frame(canvas, bg=palette['card'], padx=18, pady=14)
        card_window = canvas.create_window((0, 0), window=card, anchor='nw')
        def form_size(_event=None):
            canvas.configure(scrollregion=canvas.bbox('all'))
        def resize_form(event):
            canvas.itemconfigure(card_window, width=event.width)
        card.bind('<Configure>', form_size)
        canvas.bind('<Configure>', resize_form)
        def wheel(event):
            if event.delta:
                canvas.yview_scroll(-int(event.delta/abs(event.delta)), 'units')
            elif getattr(event,'num',None) in (4,5):
                canvas.yview_scroll(-1 if event.num==4 else 1, 'units')
        canvas.bind('<MouseWheel>', wheel)
        canvas.bind('<Button-4>', wheel)
        canvas.bind('<Button-5>', wheel)
        typ = tk.StringVar(value=existing['type'] if existing else 'fqdn')
        target = tk.StringVar(value=existing['value'] if existing else '')
        available = interfaces()
        dev = tk.StringVar(value=existing['interface'] if existing else (available[0] if available else ''))
        gw = tk.StringVar(value=existing.get('gateway','') if existing else '')
        enabled = tk.BooleanVar(value=existing.get('enabled', True) if existing else True)
        field_bg=palette['card']; foreground=palette['ink']; muted=palette['muted']
        form_style = 'RuleDlg'+str(win.winfo_id())
        style.configure(form_style+'.TEntry', padding=8, fieldbackground=field_bg,
                        foreground=foreground, bordercolor=palette['border'])
        style.configure(form_style+'.TCombobox', padding=7, fieldbackground=field_bg,
                        foreground=foreground)
        style.map(form_style+'.TCombobox', fieldbackground=[('readonly',field_bg)],
                  foreground=[('readonly',foreground)])
        def field(label):
            box = tk.Frame(card, bg=field_bg)
            box.pack(fill='x', pady=(0, 11))
            tk.Label(box, text=label, bg=field_bg, fg=foreground,
                     font=('Sans', 10, 'bold')).pack(anchor='w', pady=(0, 5))
            return box
        kind_box=field('Destination type')
        kind=ttk.Combobox(kind_box, textvariable=typ,
                          values=['fqdn','wildcard','ip','cidr'], state='readonly',
                          style=form_style+'.TCombobox')
        kind.pack(fill='x')
        dest_box=field('Destination')
        dest=ttk.Entry(dest_box, textvariable=target, style=form_style+'.TEntry')
        dest.pack(fill='x')
        hint=tk.Label(dest_box, bg=field_bg, fg=muted, font=('Sans', 9), justify='left')
        hint.pack(anchor='w', pady=(5, 0))
        def update_hint(*_):
            hint.configure(text={'fqdn':'Example: filimo.com',
                                 'wildcard':'Example: *.namava.ir  ·  subdomains matched via observable DNS',
                                 'ip':'Example: 1.1.1.1',
                                 'cidr':'Example: 192.168.80.0/24'}[typ.get()])
        typ.trace_add('write',update_hint);update_hint()
        iface_box=field('Network interface')
        iface=ttk.Combobox(iface_box,textvariable=dev,values=available,
                            style=form_style+'.TCombobox')
        iface.pack(fill='x')
        gw_box=field('Gateway  ·  optional')
        gateway=ttk.Entry(gw_box,textvariable=gw,style=form_style+'.TEntry')
        gateway.pack(fill='x')
        tk.Label(gw_box,text='Leave blank for the interface default gateway; TUN interfaces may use a direct route.',
                 bg=field_bg,fg=muted,font=('Sans',9),wraplength=470,
                 justify='left').pack(anchor='w',pady=(5,0))
        enabled_row=tk.Frame(card,bg=field_bg)
        enabled_row.pack(fill='x',pady=(0,4))
        chk=ttk.Checkbutton(enabled_row,text='Enable this rule',variable=enabled,style='RP.TCheckbutton')
        chk.pack(side='left')
        # ttk.Checkbutton's style is shared by the parent, but this is a white/dark card.
        style.configure(form_style+'.TCheckbutton',background=field_bg,foreground=foreground,
                        font=('Sans',10,'bold'))
        chk.configure(style=form_style+'.TCheckbutton')
        error=tk.StringVar(value='')
        error_label=tk.Label(footer,textvariable=error,bg=palette['bg'],fg='#dc2626' if not dark_mode.get() else '#fda4af',
                             font=('Sans',9),anchor='w',justify='left',wraplength=530)
        error_label.pack(fill='x',pady=(8,3))
        bottom=tk.Frame(footer,bg=palette['bg'])
        bottom.pack(fill='x',pady=(3,0))
        def save():
            obj={'id':existing['id'] if existing else uuid.uuid4().hex[:12],
                 'type':typ.get(),'value':target.get().strip().lower(),
                 'interface':dev.get().strip(),'gateway':gw.get().strip(),
                 'enabled':enabled.get()}
            try:
                validate({'rules':[obj]})
            except Exception as exc:
                error.set(str(exc))
                return
            if existing: data['rules'][data['rules'].index(existing)]=obj
            else: data['rules'].append(obj)
            refresh();win.destroy();status.set('Unsaved changes — click Apply / Save')
        ttk.Button(bottom,text='Save changes' if existing else 'Add rule',
                   command=save,style='Primary.TButton').pack(side='right',padx=(9,0))
        ttk.Button(bottom,text='Cancel',command=win.destroy,style='Soft.TButton').pack(side='right')
        dest.focus_set()
        win.bind('<Escape>',lambda _e:win.destroy())
        win.bind('<Control-Return>',lambda _e:save())
    def remove():
        matches=selected_many()
        if not matches: return
        prompt = (f"Delete {matches[0]['value']}?" if len(matches)==1
                  else f"Delete {len(matches)} selected rules? This cannot be undone after Apply / Save.")
        if messagebox.askyesno('Delete selected rules',prompt,parent=root):
            ids={r['id'] for r in matches}
            data['rules'][:]=[r for r in data['rules'] if r['id'] not in ids]
            refresh();status.set(f'{len(matches)} rule(s) removed — click Apply / Save')
    def set_enabled(enabled):
        matches=selected_many()
        for rule in matches: rule['enabled']=enabled
        if matches:
            refresh();status.set(f'{len(matches)} rule(s) set to {"enabled" if enabled else "disabled"} — click Apply / Save')
    def toggle():
        matches=selected_many()
        if matches:
            # Mixed selection becomes enabled; all enabled becomes disabled.
            target=not all(r.get('enabled',True) for r in matches)
            set_enabled(target)
    # Run pkexec outside Tk's event loop so Enter inside the authentication
    # dialog cannot race with GUI updates or trigger a misleading toast.
    import threading
    import queue
    auth_result_queue = queue.SimpleQueue()
    apply_state = {'busy': False}

    def apply():
        if apply_state['busy']:
            return
        try:
            snapshot = json.loads(json.dumps(validate(data)))
            # A private temporary file is the *pending* payload. Do not overwrite
            # the user's saved rules before privileged installation succeeds.
            LOCAL.parent.mkdir(parents=True, exist_ok=True)
            fd, pending = tempfile.mkstemp(prefix='.routepilot-pending-', suffix='.json', dir=LOCAL.parent)
            try:
                with os.fdopen(fd, 'w') as handle:
                    json.dump(snapshot, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(pending, 0o600)
            except BaseException:
                os.unlink(pending)
                raise
        except Exception as exc:
            status.set(f'Cannot submit rules: {exc}')
            notify(str(exc), kind='error', duration=9000)
            return
        apply_state['busy'] = True
        apply_button.state(['disabled'])
        status.set('Waiting for administrator authentication… No changes confirmed yet.')

        def privileged_write():
            try:
                if os.geteuid() == 0:
                    args = [sys.executable, os.path.realpath(__file__), 'install-config', pending]
                else:
                    args = ['pkexec', sys.executable, '/opt/routepilot/routepilot.py', 'install-config', pending]
                p = subprocess.run(args, capture_output=True, text=True, timeout=120)
                auth_result_queue.put((p.returncode, p.stdout, p.stderr, snapshot))
            except (OSError, subprocess.TimeoutExpired) as exc:
                auth_result_queue.put((-1, '', str(exc), snapshot))
            finally:
                try: os.unlink(pending)
                except OSError: pass

        threading.Thread(target=privileged_write, daemon=True).start()
        root.after(100, finish_apply)

    def finish_apply():
        try:
            code, out, err, snapshot = auth_result_queue.get_nowait()
        except queue.Empty:
            if root.winfo_exists():
                root.after(100, finish_apply)
            return
        apply_state['busy'] = False
        apply_button.state(['!disabled'])
        if code == 0 and 'Saved rules.' in out:
            try:
                atomic_write(LOCAL, snapshot, 0o600)
            except OSError as exc:
                # System config is already installed; do not claim failure of
                # privileged operation, but explicitly warn about local cache.
                status.set('System rules saved; local copy could not be updated.')
                notify(f'System configuration installed, but local copy failed: {exc}', kind='error', duration=12000)
                return
            status.set('System configuration saved. Daemon reconciles routes within 15 seconds.')
            notify('Administrator confirmed. Rules installed; daemon will refresh routes shortly.')
        elif code in (126, 127) or (code != 0 and 'cancel' in (err + out).lower()):
            status.set('Cancelled — system routing rules were not changed.')
            notify('No administrator approval. Existing system rules are unchanged.', kind='cancel', duration=6500)
        else:
            detail = (err or out or f'Privileged command failed (exit {code})').strip()
            status.set('Apply failed — system configuration not confirmed.')
            notify(detail[:700], kind='error', duration=12000)

    def selection_changed(_event=None):
        n=len(tree.selection())
        selection_hint.set(f'{n} selected  ·  Ctrl / Shift + click for multi-select' if n
                           else 'Ctrl / Shift + click to select multiple')
    tree.bind('<<TreeviewSelect>>',selection_changed)
    tree.bind('<Double-1>',lambda event: edit(selected()) if selected() else None)
    bar=ttk.Frame(footer,style='RP.TFrame');bar.pack(fill='x',pady=(10,8))
    for col in range(5): bar.columnconfigure(col, weight=1, uniform='actions')

    actions = [('＋ Add',lambda:edit()),
               ('Edit',lambda:edit(selected()) if selected() else messagebox.showinfo('Edit','Select exactly one rule to edit.')),
               ('Enable selected',lambda:set_enabled(True)),
               ('Disable selected',lambda:set_enabled(False)),
               ('Delete selected',remove)]
    for col, (label, fn) in enumerate(actions):
        btnstyle='Danger.TButton' if 'Delete' in label else 'Soft.TButton'
        b=ttk.Button(bar,text=label,command=fn,style=btnstyle)
        b.grid(row=0,column=col,sticky='ew',padx=3,pady=3)
        Tooltip(b, {'Enable selected':'Enable all selected rules; click Apply / Save to persist.',
                    'Disable selected':'Disable selected rules; click Apply / Save to persist.',
                    'Delete selected':'Delete selected rules after confirmation; click Apply / Save to persist.'}.get(label,label))
    apply_button = ttk.Button(bar,text='Apply / Save',command=apply,style='Primary.TButton')
    apply_button.grid(row=1,column=0,sticky='ew',padx=3,pady=3)
    def diagnostics():
        state=read_json(STATE,{})
        issues='\n'.join(state.get('warnings',[])) or 'No reported IPv4 errors.'
        messagebox.showinfo('Service status',f"Last reconciliation: {time.ctime(state['updated']) if state.get('updated') else 'Not yet'}\n\n{issues}\n\nOnly IPv4 is managed. IPv6 and DoH require separate handling.")
    def uninstall_app():
        if not messagebox.askyesno('Uninstall RoutePilot',
                'Uninstall RoutePilot from this computer?\n\n'
                'This removes RoutePilot-managed routes, its background service, GUI, and icons.\n'
                'Saved rules in /etc/routepilot/rules.json will be preserved.\n\n'
                'Pritunl and OpenVPN will not be modified.\n\n'
                'Administrator authentication is required.', parent=root):
            return
        script = Path('/opt/routepilot/uninstall.sh')
        if not script.is_file():
            messagebox.showerror('Uninstall unavailable',
                'Uninstaller not installed. Upgrade RoutePilot first, or run sudo bash uninstall.sh from the extracted package.', parent=root)
            return
        try:
            result = subprocess.run(['pkexec', '/bin/bash', str(script)],
                                    text=True, capture_output=True)
        except OSError as exc:
            messagebox.showerror('Uninstall error', str(exc), parent=root)
            return
        if result.returncode:
            messagebox.showerror('Uninstall failed', (result.stderr or result.stdout or
                'Authentication cancelled or cleanup failed.').strip(), parent=root)
        else:
            messagebox.showinfo('Uninstalled', 'RoutePilot was removed. Saved rules were preserved.', parent=root)
            root.destroy()
    ttk.Button(bar,text='Uninstall…',command=uninstall_app,style='Danger.TButton').grid(row=1,column=4,sticky='ew',padx=3,pady=3)
    ttk.Button(bar,text='Diagnostics',command=diagnostics,style='Soft.TButton').grid(row=1,column=3,sticky='ew',padx=3,pady=3)
    def view_logs():
        win=tk.Toplevel(root);win.title('RoutePilot · Recent service logs');win.geometry('900x440')
        box=tk.Text(win,wrap='word',background='#0f172a',foreground='#d1fae5',
                    font=('Monospace',10),padx=14,pady=14)
        box.pack(fill='both',expand=True)
        try:
            result=subprocess.run(['journalctl','-u','routepilot','-n','120','--no-pager'],
                                  capture_output=True,text=True,timeout=5)
            box.insert('1.0',result.stdout or result.stderr or 'No log entries available.')
        except (OSError,subprocess.TimeoutExpired) as exc: box.insert('1.0',str(exc))
        box.configure(state='disabled')
    ttk.Button(bar,text='View logs',command=view_logs,style='Soft.TButton').grid(row=1,column=2,sticky='ew',padx=3,pady=3)
    def export_backup():
        path=filedialog.asksaveasfilename(parent=root,title='Export RoutePilot rules',defaultextension='.json',
                    initialfile='routepilot-rules-backup.json',filetypes=[('JSON files','*.json')])
        if not path:return
        try:
            validate(data)
            atomic_write(Path(path),data,0o600)
            notify('Rules exported to the selected JSON file.')
        except (OSError,ValueError) as exc:notify(str(exc),kind='error',duration=9000)
    def import_backup():
        path=filedialog.askopenfilename(parent=root,title='Import RoutePilot rules',filetypes=[('JSON files','*.json')])
        if not path:return
        try:
            candidate=validate(read_json(Path(path),{'rules':[]}))
            if not messagebox.askyesno('Restore rules',
                f"Replace {len(data['rules'])} unsaved rules with {len(candidate['rules'])} rules from backup?\n\n"
                'Changes will NOT affect routing until you click Apply / Save.',parent=root):return
            data['rules'][:]=candidate['rules']
            refresh();status.set('Restored backup in editor — click Apply / Save to activate')
            notify('Backup loaded. Review the rules, then click Apply / Save.')
        except (OSError,ValueError,RuntimeError) as exc: notify(str(exc),kind='error',duration=9000)
    ttk.Button(bar,text='Export backup',command=export_backup,style='Soft.TButton').grid(row=2,column=0,sticky='ew',padx=3,pady=3)
    ttk.Button(bar,text='Import backup',command=import_backup,style='Soft.TButton').grid(row=2,column=1,sticky='ew',padx=3,pady=3)
    ttk.Button(bar,text='Refresh status',command=refresh,style='Soft.TButton').grid(row=2,column=2,sticky='ew',padx=3,pady=3)

    ttk.Label(footer,textvariable=status,style='Muted.TLabel',wraplength=1000).pack(anchor='w')
    tk.Label(footer,text='ⓘ  IPv4 only  •  Wildcard observes UDP/53 (not DoH/DoT)  •  Green means enabled, not necessarily routed',
             bg='#fef3c7',fg='#854d0e',font=('Sans',9),anchor='w',padx=12,pady=6).pack(fill='x',pady=(7,0))
    refresh();update_service_badge();root.after(7000,refresh_live);root.mainloop()

def main():
    p=argparse.ArgumentParser()
    p.add_argument('action',nargs='?',choices=['gui','daemon','install-config','once','cleanup','explain'],default='gui')
    p.add_argument('path',nargs='?')
    args=p.parse_args()
    try:
        if args.action=='gui': gui()
        elif args.action=='daemon': daemon()
        elif args.action=='once': print('\n'.join(reconcile()))
        elif args.action=='cleanup': cleanup()
        elif args.action=='explain':
            if not args.path: p.error('explain requires IPv4 address')
            explain(args.path)
        elif args.action=='install-config':
            if not args.path: p.error('install-config requires source JSON path')
            install_config(args.path)
    except Exception as e:
        print(f'RoutePilot: {e}',file=sys.stderr);sys.exit(1)
if __name__=='__main__': main()
