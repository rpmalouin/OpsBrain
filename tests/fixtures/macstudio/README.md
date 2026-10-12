# Node fixtures — the Mac Studio

Two files, and they are different in kind. Read the README before editing either.

## `macstudio_node.json` — a real producer snapshot (2026-10-10, schema 1)

Written by the Mac's own 15-minute cron (`~/.hermes/scripts/mac-node-snapshot.py`) and captured here
from `/appdata/OpsBrain/logs/macstudio_node.json`. It is the **secondary** source in the merge tests
(where a live poll still owns online/offline) and the shape every consumer key is read from:

```
schema, node, kind, label, hostname, dns_name, address, generated_at, generated_at_utc, producer,
interval_minutes, host, time_machine, disks, mounts, volumes_watched, volumes_skipped, services,
gateway, hermes_cron, local_model, netdata, alerts, alert_count, build_seconds
```

Values worth remembering, because tests and the panel depend on them:

* `alert_count: 1`, `alerts: ["cron: hermes-patch-watchdog last_status=error streak=533"]` — the
  producer's own curated verdicts. The merge counts these as-is and never re-derives a threshold.
* `time_machine.latest_backup_age_hours: 0.72`, `gateway.age_seconds: 18.4` (present, fresh) and
  seven mounts all `mounted: true, read_only: false` — which is why this fixture yields **zero**
  derived alerts, and why `test_merge_fresh_snapshot_counts_the_producers_own_alerts` can assert
  `anomalies == 1` without special-casing anything.
* `disks[]` carries `total_gib / used_gib / free_gib / used_percent` for all six watched volumes;
  `/Volumes/Data2` sits at 82–83 %, which is the **capacity is never judged** case (surfaced, never
  alerted).
* `netdata: {up: false, ...}` and the `{"name": "netdata", "port": 19999}` entry in `services[]` are
  the retired probe. They stay in this capture as history; the live producer drops both (schema 2).

## `macmon_pipe.json` — PENDING, and deliberately absent

The `soc` block's fixture has **not** been captured yet: macmon runs only on the Mac, and there is no
SSH path to it from dockerVM. The Mac's reply to ask `20261012T230213Z-7f85d600` is expected to carry
one literal `macmon pipe --soc-info -s 1 -i 1000` line, which lands here and becomes the fixture the
`soc` tests assert against.

Until then those tests build small inline `soc` payloads in `tests/test_local_snapshot.py`
(`_macmon()`), written to macmon's documented field names read out of the fork's own source
(`Metrics` in `src/metrics.rs`, `MemMetrics`, `FanMetric`, `CpuTierMetrics`; `SocInfo` in
`src/sources.rs`). That is a synthetic payload used as a unit-test input, and it is labelled as one —
**do not** copy it into this directory: a file here is a measurement, and a fabricated one would be
indistinguishable from a real capture six months from now.

Also still unverified from this host: whether `cpu_power` / `gpu_power` are non-zero on **macOS 27**,
where upstream documents that the CPU/ANE IOReport counters can stop updating without a private
entitlement. `soc.available` / `soc.error` exist so a failed read is visible; if the watts turn out to
be unavailable, the node renders temperatures, memory and usage and the power row is dropped rather
than shown as zero.

## The alarm-retirement measurement (2026-10-12)

Replaying the retired Netdata capture through the shipped filter (`keep CRITICAL/WARNING/ERROR`,
`alarm_skip_names = [disk_fill_rate, disk_inode_rate]`, the 21-path `alarm_skip_mounts`) kept **zero**
alarms: the only CRITICAL was `disk_space_usage` on `/Volumes/makemkv_v2.0.0`, a transient installer
disk image already on the skip-list, and the rest were UNINITIALIZED rate alarms or CLEAR. No Mac
Netdata alarm name ever reached `logs/notifications.jsonl` over the life of the node. That is why the
Netdata path was retired with nothing migrated out of it, and why the node's findings are now the
producer's `alerts[]` plus the collector's own derived checks.
