# TrueNAS integration

Optional — adds a storage panel and storage telemetry to the collector. Polls the
TrueNAS SCALE REST API (`/api/v2.0`) for pool, system, alerts, and disk data.

## 1. Create a credentials file

The collector reads this (never commit it):

```bash
printf 'username=your_truenas_user\npassword=your_truenas_password\n' > ~/.smbcred
chmod 600 ~/.smbcred
```

The user needs at least read access to the TrueNAS SCALE REST API. The creds file path is
expanded literally with `Path(raw).expanduser()` (the config resolver would otherwise
prefix a repo-relative path onto it).

## 2. Enable the source

```yaml
sources:
  truenas:
    base_url: http://truenas/api/v2.0   # or your TrueNAS IP/hostname
    creds_file: ~/.smbcred
    enabled: true
    timeout_s: 8
```

## 3. Run a cycle

```bash
python3 scheduler/scheduler.py --once
```

The collector fetches `/pool`, `/system/info`, `/alert/list`, and `/disk`, merging them
into `collector.json` under `truenas`.

## 4. Dashboard

A **TrueNAS** panel shows pool status/health, system version/model/RAM/uptime, disk count,
and active alerts.

## Degradation

If auth fails or the endpoint is down, the collector marks the source unavailable and
degrades gracefully (no crash). `up` is derived from pool data (array reachable + authed).