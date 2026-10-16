# edcb-bridge

[![PyPI](
  <https://img.shields.io/pypi/v/edcb-bridge?color=blue>
  )](
  <https://pypi.org/project/edcb-bridge/>
) [![CI](
  <https://github.com/eggplants/edcb-bridge/actions/workflows/ci.yml/badge.svg>
  )](
  <https://github.com/eggplants/edcb-bridge/actions/workflows/ci.yml>
)

[![ghcr size](
  <https://ghcr-badge.egpl.dev/eggplants/edcb-bridge/size>
)](
  <https://github.com/eggplants/edcb-bridge/pkgs/container/edcb-bridge>
)

A bridge server that lets [KonomiTV](https://github.com/tsukumijima/KonomiTV)'s recording reservation features for the [EDCB](https://github.com/xtne6f/EDCB) backend work with
[EPGStation](https://github.com/l3tnun/EPGStation) (v2 / v3) or [mirakc](https://github.com/mirakc/mirakc).

## Installation

```bash
# mise via github release
mise use -g github:eggplants/edcb-bridge

# mise via pipx
mise use -g pipx:edcb-bridge

# pipx
pipx install edcb-bridge

# pip
pip install edcb-bridge
```

### Docker

```bash
docker pull ghcr.io/eggplants/edcb-bridge

docker run -d -p 4510:4510 \
  -e MIRAKC_URL=http://192.168.1.10:40772 \
  -e EPGSTATION_URL=http://192.168.1.10:8888 \
  ghcr.io/eggplants/edcb-bridge
```

#### docker compose

See: <https://github.com/eggplants/mb/blob/main/compose/compose.yaml>

```yaml
# docker compose up -d edcb-bridge
# docker compose logs -f edcb-bridge
services:
  edcb-bridge:
    # https://github.com/eggplants/edcb-bridge/pkgs/container/edcb-bridge
    image: ghcr.io/eggplants/edcb-bridge
    container_name: edcb-bridge
    restart: unless-stopped
    ports:
      - 4510:4510
    environment:
      TZ: Asia/Tokyo
      MIRAKC_URL: http://mirakc:40772
      RECORDER: epgstation
      EPGSTATION_URL: http://epgstation:8888
      RECORDED_DIR: /mnt/hdd1/recorded
    depends_on:
      - mirakc
      - epgstation
```

## CLI

```bash
edcb-bridge \
  --listen-port 4510 \
  --mirakc-url http://192.168.1.10:40772 \
  --epgstation-url http://192.168.1.10:8888 \
  --recorder epgstation \
  --recorded-dir /mnt/hdd1/recorded
```

<details><summary>Help</summary>

```shellsession
$ edcb-bridge --help
usage: edcb-bridge [-h] [--listen-host LISTEN_HOST] [--listen-port LISTEN_PORT] [--mirakc-url MIRAKC_URL]
                   [--recorder {epgstation,mirakc}] [--epgstation-url EPGSTATION_URL] [--recorded-dir RECORDED_DIR]
                   [--mirakc-content-path-template MIRAKC_CONTENT_PATH_TEMPLATE] [--start-margin START_MARGIN]
                   [--end-margin END_MARGIN] [--epg-cache-ttl EPG_CACHE_TTL] [--http-timeout HTTP_TIMEOUT]
                   [--log-level {DEBUG,INFO,WARNING,ERROR}] [-V]

EDCB (EpgTimerSrv) CtrlCmd compatible bridge to EPGStation / mirakc for KonomiTV.

Every option can also be given by the environment variable shown in brackets.

options:
  -h, --help                            show this help message and exit
  --listen-host LISTEN_HOST             address to listen on for CtrlCmd [LISTEN_HOST] (default: 0.0.0.0)
  --listen-port LISTEN_PORT             port to listen on for CtrlCmd [LISTEN_PORT] (default: 4510)
  --mirakc-url MIRAKC_URL               mirakc (or Mirakurun) URL to read services and programs from [MIRAKC_URL]
                                        (default: http://127.0.0.1:40772)
  --recorder {epgstation,mirakc}        recording backend [RECORDER] (default: epgstation)
  --epgstation-url EPGSTATION_URL       EPGStation URL (recorder=epgstation) [EPGSTATION_URL] (default:
                                        http://127.0.0.1:8888)
  --recorded-dir RECORDED_DIR           recording folder as seen by KonomiTV [RECORDED_DIR] (default:
                                        /mnt/hdd1/recorded)
  --mirakc-content-path-template MIRAKC_CONTENT_PATH_TEMPLATE
                                        recorded file name (recorder=mirakc) [MIRAKC_CONTENT_PATH_TEMPLATE] (default:
                                        {start:%Y%m%d%H%M}_{sid}_{title}.m2ts)
  --start-margin START_MARGIN           start margin in seconds shown in KonomiTV (display only) [START_MARGIN]
                                        (default: 0)
  --end-margin END_MARGIN               end margin in seconds shown in KonomiTV (display only) [END_MARGIN] (default: 0)
  --epg-cache-ttl EPG_CACHE_TTL         seconds to cache the program guide [EPG_CACHE_TTL] (default: 60.0)
  --http-timeout HTTP_TIMEOUT           HTTP timeout in seconds [HTTP_TIMEOUT] (default: 20.0)
  --log-level {DEBUG,INFO,WARNING,ERROR}
                                        log level [LOG_LEVEL] (default: INFO)
  -V, --version                         show program's version number and exit
```

</details>

<details><summary>Environment variables</summary>

| Variable | Default | Description |
| --- | --- | --- |
| `LISTEN_HOST` / `LISTEN_PORT` | `0.0.0.0` / `4510` | CtrlCmd listen address |
| `MIRAKC_URL` | `http://127.0.0.1:40772` | Source of service and program information |
| `RECORDER` | `epgstation` | Recording backend (`epgstation` / `mirakc`) |
| `EPGSTATION_URL` | `http://127.0.0.1:8888` | EPGStation |
| `RECORDED_DIR` | `/mnt/hdd1/recorded` | Recording folder as seen from KonomiTV. Used to convert between EDCB's destination folder (absolute path) and EPGStation's `directory` (relative path) |
| `MIRAKC_CONTENT_PATH_TEMPLATE` | `{start:%Y%m%d%H%M}_{sid}_{title}.m2ts` | File name used when `RECORDER=mirakc` (`{start}` `{onid}` `{sid}` `{eid}` `{title}`) |
| `START_MARGIN` / `END_MARGIN` | `0` | Margins shown on KonomiTV's recording settings screen (display only) |
| `EPG_CACHE_TTL` | `60` | Program information cache lifetime in seconds |
| `HTTP_TIMEOUT` | `20` | HTTP timeout in seconds |
| `LOG_LEVEL` | `INFO` | `DEBUG` logs every command |

</details>

## KonomiTV configuration

```yaml
# by default located in: /opt/KonomiTV/config.yaml
# restart pm2 service after changed: sudo pm2 restart KonomiTV
general:
  backend: EDCB
  always_receive_tv_from_mirakurun: true
  edcb_url: tcp://127.0.0.1:4510/
  mirakurun_url: http://127.0.0.1:40772/
```

## Using mirakc as the recording backend

Add the recording settings to [mirakc's `config.yml`](https://mirakc.github.io/dekiru-mirakc/v3/config/recording.html) and mount the recording folder.

```yaml
recording:
  basedir: /var/lib/mirakc/recording
```

```yaml
  mirakc:
    volumes:
      - /mnt/hdd1/recorded:/var/lib/mirakc/recording
```

With `RECORDED_DIR=/mnt/hdd1/recorded` in this setup, the destination subfolder specified in KonomiTV is
reflected in `contentPath`.

## Limitations

- **EPGStation**
  - Manual reservations cannot be disabled (EPGStation has no disabled state; they can still be deleted)
  - Deleting a reservation created by a rule leaves it behind as a "skipped (disabled)" reservation, due to how
    EPGStation works
  - EDCB-specific settings such as priority, margins, program tracking, exact-time recording, and post-recording
    actions are ignored
  - Automatic reservations do not save "fuzzy search", "genre/time exclusion", "paid programs only", or the memo
    field
  - Day-of-week and time ranges are rounded to whole hours
  - Only subfolders under `RECORDED_DIR` can be used as the destination. File name templates are passed through
    as-is in EPGStation's `recordedFormat` syntax (e.g. `%TITLE%`)
- **mirakc**: Only program-based reservations are supported (time-based reservations, automatic reservations,
  and changing settings are not supported; disabling is treated as deletion)
- mirakc does not return the TSID, so it is derived from the NID for terrestrial channels and from the channel
  name (`BS15_1` / `CS4`) for BS/CS (it cannot be derived for other channel types such as BS4K)
- Past programs (`EnumPgArc`), recorded information (`EnumRecInfo`), and station logos return empty results
  (KonomiTV falls back to its bundled logos)

## License

[MIT License](
  <https://github.com/eggplants/edcb-bridge/blob/master/LICENSE.txt>
)
