# Connectivity — reach any unit from any PC

**Goal:** a unit can be detected and connected to no matter which PC runs the client,
across the three ways a PC reaches a Pi in the field:

1. **Same WiFi** — Pi `wlan0` and the PC on one access point.
2. **Long-range bridge → Pi `eth0`** — a WiFi→Ethernet bridge feeds the Pi's Ethernet.
3. **Direct cable** — Pi `eth0` straight into the PC, no DHCP server, no router.

## The core idea

A fixed IP is fast but belongs to *one* network — it can't be valid on all three
modes at once. The thing that survives all three is **mDNS + dynamic addressing**:

| Mode | Reaches the unit by |
|------|---------------------|
| Same WiFi | DHCP address + mDNS (`broadcaster-N.local`) |
| Transparent bridge → eth0 | DHCP address + mDNS (needs multicast to traverse the bridge) |
| Direct cable | **link-local (169.254.x) + mDNS** — the zero-config case |

A static IP actively *breaks* modes 2 (if the bridge NATs) and 3 (Pi on `10.0.0.1`
while the PC's Ethernet auto-assigns `169.254.x` → different subnets, no traffic). So
the plan is: **default to DHCP + mDNS, no static**, and make the client fast and
resilient around it. Identity is already solved — every unit carries a permanent
`uid` + `machine_id` fingerprint, and the client tries multiple addresses per unit
and learns new ones (`api/client.py`, `state/unit_ledger.py`, `state/discovery.py`).

## Three changes

### 1. Resolved-IP disk cache — "fast on any PC"
The client resolves `.local`→IP and pins it (`AgentClient._resolve_and_pin_ip`), but
only in-memory per session. Persist it so a fresh launch — on any PC — connects by IP
immediately.

- `state/address_cache.py`: `AddressCache`, a JSON file beside `units.yaml` keyed by
  `machine_id → {ip, host, port, ts}` (mirrors `unit_ledger.py`).
- Write from `units_tab._sync_machine_ids()` (already runs post-warmup with the live
  client) — record `machine_id → active IP`.
- Read in `units_tab._make_client()` — seed the client's address list with the cached
  IP first, so `warmup()` takes the fast path before mDNS.
- **Safety:** verify the `/info` `machine_id` on the cached IP matches the expected
  unit; DHCP may have reassigned that IP to another device. Mismatch → drop the entry
  and fall back. Self-healing, never connects to the wrong box.

Effort: S · Risk: low.

### 2. Subnet-probe discovery fallback — rescues a multicast-filtered bridge
If the long-range bridge drops mDNS multicast, `broadcaster-N.local` won't resolve
even though the unit is on a routable IP.

- Extend `state/discovery.py` with an active sweep of the PC's local `/24`
  (`state/netutil.py` for the local IPv4 + prefix): bounded thread pool, TCP-connect
  `:8765`, `GET /health`, then `GET /info` for identity → `DiscoveredUnit`.
- Results feed the existing `Discovery._found` map, so the "Discovered on the network"
  picker and machine-id auto-learn pick them up unchanged.
- Triggers: the manual **Rescan** button; optionally auto on a known unit going
  `OFFLINE`. Sweeps only the PC's own `/24` (the bridge/direct-cable case).

Effort: M · Risk: low-med (keep it triggered, never a busy loop).

### 3. DHCP-default provisioning — the actual fix for all three modes
Make a static IP opt-in; default to hostname-only over DHCP.

- `ui/provision_dialog.py`: an **"Assign a static IP"** checkbox, off by default; the
  addressing-scheme block greys out when off.
- `state/provisioner.py` + `deploy/provision_network.sh`: gate the static writers
  behind `PROV_STATIC=1`. DHCP mode sets only the hostname + cloud-init
  `preserve_hostname`, restarts avahi + the agent so it re-advertises as
  `broadcaster-N`, and **skips the reboot** — no re-IP, no dropped session, the unit
  stays reachable throughout. (Static mode is unchanged, and additionally sets
  `network: {config: disabled}` since it then owns the network.)
- Registered addresses in DHCP mode: `[broadcaster-N.local, <address provisioned
  over>]` (+ the IP cache from #1).
- Optional: enable eth0 link-local + a short DHCP timeout so direct-cable comes up in
  ~2 s instead of waiting out DHCP.

Effort: S–M · Risk: low. **Highest value — makes the three modes work.**

## Build order

1. **#3 DHCP-default provisioning** — correctness across all three modes, removes the
   reboot/re-IP fragility.
2. **#1 resolved-IP cache** — kills "`.local` is slow" on every PC.
3. **#2 subnet probe** — resilience for the multicast-filtered bridge.

Each is independent and ships with tests.

## Out of scope (higher tiers)

Cross-subnet or off-site access (PCs on different VLANs/sites) needs an app-level
rendezvous registry or an overlay mesh (Tailscale/ZeroTier). The client's
multi-address model would layer those in as extra per-unit addresses without change,
but they're a separate project from the three modes above.
