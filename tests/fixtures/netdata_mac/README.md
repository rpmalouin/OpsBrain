# Netdata-sourced node fixtures (the Mac Studio)

Captured from the **Mac Studio**'s Netdata instance on 2026-10-10 ~14:54Z:

```sh
IP=10.1.10.34
curl -s "http://$IP:19999/api/v1/info"                                              > info.json
curl -s "http://$IP:19999/api/v1/alarms?all"                                        > alarms.json   # then trimmed
curl -s "http://$IP:19999/api/v1/data?chart=disk_space.%2FVolumes%2FData&after=-10&points=2" > data_data_volume.json
```

`alarms.json` is **trimmed**: the live host returns 847 alarms (CRITICAL 1, CLEAR 526,
UNDEFINED 318, UNINITIALIZED 2). The fixture keeps every CRITICAL/WARNING/ERROR, the first two
UNINITIALIZED and six CLEAR entries — 9 total — which is enough to exercise the keep-set and the
mount skip-list without a 1 MB test file. Keys are unchanged, so the payload shape is still real.

Values worth remembering (these are why the adapter filters):

* the only CRITICAL is `disk_space_usage` on `disk_space./Volumes/makemkv_v2.0.0` — a transient
  **mounted installer disk image**, not a real volume;
* UNDEFINED is noise (318 entries) and `federation_collector`'s keep-set is CRITICAL/WARNING/ERROR;
* `disk_space.<mount>` data dimensions are **GiB, not percent** (`avail`, `used`,
  `reserved for root`), so a percentage must be computed.
