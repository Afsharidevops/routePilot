# RoutePilot

**A local, graphical IPv4 split-routing manager for Ubuntu.**

RoutePilot lets you route selected IP addresses, IPv4 networks, domain names, and observed wildcard subdomains via a chosen network interface/gateway while leaving the system's other routes alone. It is particularly useful when a full-tunnel OpenVPN/Pritunl connection is active but selected destinations should use your regular Wi-Fi/Ethernet gateway.

> **Status:** Community / experimental project, v2.0 GUI. Tested in one real-world Ubuntu 26.04 + OpenVPN/Pritunl environment; not certified across all Ubuntu releases or VPN/firewall configurations. **IPv4 only.** Use on a machine with local access until you've tested your own network. RoutePilot is an independent Python implementation, **not a fork of RiftRoute**.

<!-- Add a screenshot before publishing: screenshots/routepilot-light.png -->

## Highlights

- **Desktop GUI** with light/dark mode, color-coded rules, search, filters, and theme-aware Add/Edit dialog.
- **Destination types:** individual IPv4 (`203.0.113.10`), CIDR (`192.168.80.0/24`), exact FQDN (`filimo.com`), and wildcard (`*.namava.ir`).
- **Per-rule routing:** choose from available interfaces; optionally enter a gateway or let RoutePilot read the interface gateway from NetworkManager.
- **Bulk actions:** Ctrl/Shift multi-select, enable, disable, or delete multiple rules; click **Apply / Save** to persist changes.
- **Persistent local service** using `systemd`, re-evaluating route configuration roughly every 15 seconds.
- **Wildcard DNS observation:** watches *unencrypted IPv4 UDP/53 DNS replies* and adds publicly routable A records from matching subdomains to expiring `nftables` sets.
- **Wildcard auto-SNAT:** scoped `nftables` masquerade rule by route mark **and** egress interface, avoiding VPN source addresses leaking onto the physical link for those wildcard matches.
- **Monitoring:** service indicator, approximate applied state, wildcard IP counts, diagnostic commands, and log viewer. A displayed `Applied` status is **not** a connectivity test.
- **Backup & restore:** export/import validated JSON rules; imported rules are **not** activated until you select **Apply / Save**.
- **Uninstaller** that attempts to clean up RoutePilot-owned routing and firewall state before removing the application.

## Requirements

- Ubuntu with `apt`, `systemd`, and NetworkManager; installer accepts Ubuntu **22.04 through 26.04** release identifiers. Runtime behavior has **not** been tested on every version (and some interim Ubuntu releases are end-of-life).
- Python 3 + Tk (`python3-tk`), `iproute2`, `nftables`, `polkitd`, `pkexec`.
- Administrator access for installation and applying routing changes.
- A working direct network gateway/interface if you intend to bypass a VPN.

The installer installs its own dependencies using `apt`. **No changes to a Pritunl or OpenVPN server are required.** It does not replace the client's DNS servers or change the Tailscale DNS settings.

## Installation / upgrade

Download/clone the project and run inside its directory:

```bash
sudo bash install.sh
```

Close and reopen RoutePilot from **Applications**. Installing over a previous version keeps `/etc/routepilot/rules.json` intact. You do **not** need to uninstall first.

If GNOME shows an outdated launcher/icon, inspect and (if needed) migrate legacy per-user launchers **without sudo**:

```bash
bash repair-user-launcher.sh --check
bash repair-user-launcher.sh --fix
```

Then unpin an old Dock favorite, log out/in, open RoutePilot from Applications, and pin it again. The script backs up eligible old user launchers; review its output.

## Quick start: bypass a VPN for one website

Suppose Pritunl/OpenVPN provides the system default route through `tun0`, but you want `filimo.com` to use Wi-Fi `wlp56s0` instead.

1. Keep your VPN connected and open **RoutePilot**.
2. Choose **+ Add** → **Type: fqdn** → **Destination: filimo.com**.
3. Select your physical **Network interface** (example: `wlp56s0`).
4. Leave **Gateway** empty to let the app use NetworkManager's interface gateway, or enter an explicit valid IPv4 gateway.
5. Check **Enabled**, save the rule, then click **Apply / Save** in the main window.

Replace interface names and addresses with **your own** values. Domain names can resolve to multiple changing IP addresses; other service/CDN hostnames may require separate rules.

### Wildcard subdomains

For `*.namava.ir`, add a **wildcard** rule. The observer learns **public IPv4 addresses** when relevant plaintext DNS answers actually cross the system. The program also resolves the apex (`namava.ir`) separately, but `*.namava.ir` by itself is not a guarantee that every subdomain will be learned.

**Important constraints:** a wildcard won't observe HTTPS/DoH, DoT, DNS over TCP, browser/resolver cache hits, or IPv6 answers. Private ranges such as `172.18.0.0/16` are intentionally excluded from wildcard auto-routing. DNS traffic can still travel through the VPN even when destination traffic takes the direct route. IP sharing between different hostnames means routing by observed destination IP may affect other sites using that IP.

## Understanding rule status

| Display | Meaning |
| --- | --- |
| **ON / OFF** | Whether the rule is enabled in the GUI configuration. Color reflects enabled state, not successful connectivity. |
| **Applied** | The route has been recorded in the service's applied state. |
| **Waiting DNS** | An enabled wildcard has no currently observed public IPv4 addresses in its set. This is informational, **not an error**. |
| **Observed N IP(s)** | The wildcard set currently contains N addresses (subject to expiry). |
| **Not applied / Issue** | State is missing or an error/warning was reported; inspect Diagnostics and logs. |

The dark help bubble in the table is a **tooltip** explaining these labels; it appears on pointer hover. It is not an error message.

## Verifying routing

Inspect the route tables and rules:

```bash
ip -4 rule show
ip -4 route get 185.147.178.23
sudo systemctl status routepilot --no-pager
sudo journalctl -u routepilot -n 50 --no-pager
```

For a wildcard rule (table/set identifiers vary by rule order):

```bash
sudo nft list table inet routepilot
sudo nft list table ip routepilot_snat
sudo nft list set inet routepilot w1
```

If your wildcard mark is `0x2001`, compare the route choice with and without the mark:

```bash
ip -4 route get 94.182.100.130
ip -4 route get 94.182.100.130 mark 0x2001
```

An `ip route get ... mark` query demonstrates *potential* policy routing, not actual packet egress. To check egress and the source address, capture traffic during a test on the selected **physical** interface:

```bash
sudo tcpdump -ni wlp56s0 'host 94.182.100.130'
```

`tcpdump` may need separate installation (`sudo apt install tcpdump`). Destination addresses, DNS results, mark values, set names, and gateways can change. Never paste example commands into a production system without checking that they apply to your network.

## How it works

```text
Rules JSON (/etc/routepilot/rules.json)
            |
            v
Systemd daemon (route reconciliation)
            |
            +-- Exact IP/CIDR/FQDN --> iproute2 policy rules / route tables
            |
            +-- Wildcard --> passive plaintext UDP/53 DNS observer
                               |
                               v
                          nftables IPv4 sets
                               |
                               v
                          route mark + ip rule
                               |
                               v
                          chosen interface/gateway
                               |
                               v
                          scoped masquerade (wildcard)
```

The daemon operates on the **local Ubuntu client**. It does not configure a remote VPN server. DNS collection uses a privileged passive packet observer, so enable wildcard rules only if that local DNS visibility is acceptable for your environment.

## Files and services

| Path | Purpose |
| --- | --- |
| `/opt/routepilot/routepilot.py` | Installed GUI and daemon |
| `/opt/routepilot/uninstall.sh` | Installed uninstaller |
| `/etc/routepilot/rules.json` | Persistent routing rules |
| `/var/lib/routepilot/state.json` | Daemon's applied-state record |
| `/etc/systemd/system/routepilot.service` | System service |
| `/usr/share/applications/routepilot-manager.desktop` | GNOME launcher |
| `~/.config/routepilot/` | Per-user GUI settings / staging |

The application maintains its own nftables state, including the wildcard table `inet routepilot` and the wildcard SNAT table `ip routepilot_snat`. **Avoid manually modifying these owned tables** unless recovering a broken installation.

## Backup, restore, and uninstall

Use **Export backup** and **Import backup** in the GUI. The import validates the JSON before loading it into the editor; select **Apply / Save** to commit it. Keep exported backups private if they contain sensitive internal network names.

From a working installation, uninstall while you have local access:

```bash
sudo bash /opt/routepilot/uninstall.sh
```

By default, saved rules remain in `/etc/routepilot/rules.json` for recovery. To remove saved configuration and state as well:

```bash
sudo bash /opt/routepilot/uninstall.sh --purge-config
```

Uninstallation tries to clean the RoutePilot-owned routing and nftables state before deleting binaries. **If cleanup fails, it stops rather than silently deleting the application.** Do not remove `/opt/routepilot` by hand while kernel rules remain active.

## Troubleshooting

**A rule is green but traffic still uses the VPN:** confirm **Apply / Save** was clicked, check the running daemon and logs, inspect `ip -4 rule show`, and check whether a VPN firewall/kill switch or higher-priority policy prevents the desired route.

**Wildcard shows `Waiting DNS`:** the observer has not recorded any *currently unexpired public IPv4 A records* for that wildcard. Trigger a real DNS query for a known existing subdomain, then check the logs and nft set. Browser DoH, cached answers, and private IP answers do not populate the set.

**The direct interface sends packets with the VPN source IP:** inspect `sudo nft list table ip routepilot_snat`. Auto-SNAT is implemented for **wildcard rules**, not for every IP/CIDR/FQDN rule; source-address handling for other rules depends on their routing behavior.

**The menu icon is a gear:** check for a legacy user launcher, run `repair-user-launcher.sh --check`, and reopen via the current GNOME Applications entry.

**Network access breaks:** use a local console and review the recent changes, route tables, daemon logs, and state. Because RoutePilot affects kernel networking, do not test unverified configurations on an SSH-only machine.

## Development and tests

```bash
python3 -m py_compile routepilot.py
python3 -m unittest discover -v
bash -n install.sh uninstall.sh repair-user-launcher.sh
```

Unit tests are not a substitute for testing DNS, GUI behavior, actual packet source addresses, VPN firewalls, or reboot/reconnect scenarios on each supported Ubuntu release.

## Known limitations

- **IPv4 only**; IPv6 traffic is not governed by these rules.
- Wildcard detection is passive and limited to visible plaintext UDP/53 A answers; it cannot promise full domain coverage.
- Wildcard mappings expire; the current implementation uses a fixed expiry interval rather than faithfully tracking all DNS TTLs.
- Shared destination IPs can cause other domains to follow the same path.
- Source NAT automation is scoped to wildcard rules.
- VPN firewall/kill switches may block direct egress despite valid routes.
- `Applied` / observed counts are **not** end-to-end site availability tests.
- Settings can survive reboots, but performance after all network/VPN transitions is not guaranteed.
- Do not treat this as a security isolation boundary or a leak-proof VPN split-tunnel solution.

## Project background

RoutePilot is an independent, locally managed Python/Tkinter implementation. Its development was inspired by the *idea* of tools such as [RiftRoute](https://github.com/Amirhat/riftroute), but it is **not a fork** and does not include RiftRoute source code.

### License

**Before publishing:** add a `LICENSE` file that accurately matches the rights to all code and included artwork. The source header describes the Python code as MIT-licensed, but a complete root-level license file and asset rights should be reviewed before making a public release.

---

*Contributions and reproducible bug reports are welcome. Include Ubuntu version, VPN type, a minimal rule example, sanitized route output, and relevant service logs (remove IPs or names that should remain private).*
