# RoutePilot v1.3 — TUN DNS observer correction

- Corrects passive DNS capture on Linux TUN interfaces by using AF_PACKET SOCK_DGRAM to receive L3 IPv4 packets.
- Stores public A answers for `*.domain` DNS queries in owned nftables sets, keyed by the selected destination Rule.
- Logs observer startup, matching names and errors in `journalctl -u routepilot`.
- Keeps multi-selection GUI, custom gateway selection, old rules and uninstall.
- DOES NOT edit the system DNS resolver, VPN server, or Tailscale config.

## Important limits

- Only **unencrypted UDP/53 IPv4** DNS answers *observed after the daemon starts* are handled. DoH/DoT, DNS over TCP, browser caches and IPv6 do not work.
- The observer intentionally skips private/non-global IPv4 addresses. Domains such as test.namava.ir resolving to 172.18.x.x stay on their existing route.
- Browser DNS caches may require a browser restart to trigger a new DNS lookup.
- The routing table may already contain a public IP from a separately resolved apex domain. This does not prove wildcard observation.
- Not yet tested on live Ubuntu / Pritunl; test locally before depending on it.

## Test after install

```
sudo journalctl -u routepilot -n 30 --no-pager | grep 'DNS observer'
resolvectl query static.namava.ir
sudo nft list set inet routepilot w1
```

If the set remains empty, verify that DNS is unencrypted UDP/53 traffic and that a fresh query crossed the system after installation.
