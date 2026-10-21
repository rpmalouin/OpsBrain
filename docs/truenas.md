# TrueNAS integration

Optional — adds a storage panel and storage telemetry to the collector. Polls the
TrueNAS **JSON-RPC 2.0 over WebSocket** API (`wss://<host>/api/current`) for pool,
system, alerts, and disk data.

> **Do not use the REST API.** `/api/v2.0` is deprecated in TrueNAS 25.10 and removed in
> 26.04. While it lives, every Basic-auth REST request also increments TrueNAS's
> `RESTAPIUsage` alert — an hourly poll of four endpoints was enough to raise
> "The deprecated REST API was used to authenticate N times in the last 24 hours".
> The JSON-RPC transport costs one WebSocket connection per cycle and no REST auths.

## 1. Create a credentials file

The collector reads this (never commit it):

```bash
printf 'username=your_truenas_user\npassword=your_truenas_password\n' > ~/.smbcred
chmod 600 ~/.smbcred
```

The user needs at least read access to the TrueNAS API (`pool.query`, `system.info`,
`alert.list`, `disk.query`). The creds file path is expanded literally with
`Path(raw).expanduser()` (the config resolver would otherwise prefix a repo-relative
path onto it).

## 2. Enable the source

```yaml
sources:
  truenas:
    transport: jsonrpc                    # default; "rest" is legacy-only
    ws_url: wss://truenas/api/current     # or your TrueNAS IP/hostname
    creds_file: ~/.smbcred
    enabled: true
    timeout_s: 8
```

TrueNAS serves a self-signed certificate on that endpoint, so the client connects with
certificate verification disabled. The API key/credential never leaves the LAN.

## 3. Run a cycle

```bash
python3 scheduler/scheduler.py --once
```

The collector opens **one** WebSocket session, authenticates with `auth.login_ex`
(`mechanism: PASSWORD_PLAIN`), then calls `pool.query`, `system.info`, `alert.list` and
`disk.query`, merging them into `collector.json` under `truenas`. Requires the
`websockets` package (`requirements.txt`).

## 4. Dashboard

A **TrueNAS** panel shows pool status/health, system version/model/RAM/uptime, disk count,
and active alerts.

## Degradation

If auth fails or the endpoint is down, the collector marks the source unavailable and
degrades gracefully (no crash, per-call `ok:false`). `up` is derived from pool data (array
reachable + authed). The only reason to switch `transport: rest` back on is a host with no
`websockets` package — which reintroduces the deprecation warning.
