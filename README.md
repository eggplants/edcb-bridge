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

[日本語](README.ja.md)

A bridge server that lets KonomiTV's recording reservation features for the EDCB backend work with
**EPGStation** or **mirakc**.

## KonomiTV configuration (`config.yaml`)

```yaml
general:
  backend: EDCB
  # Live viewing is received directly from mirakc (required)
  always_receive_tv_from_mirakurun: true
  edcb_url: tcp://127.0.0.1:4510/
  mirakurun_url: http://127.0.0.1:40772/
```

Restart KonomiTV after making changes (`sudo pm2 restart KonomiTV`).

## Running (docker compose)

With [`setup_mirakc_and_epgs_on_nanopi.sh`](https://gist.github.com/eggplants/afaa31d65a1603f4868c0c400d7d5430),
add the following under `services:` in `~/_tv/compose.yaml`.

```yaml
  edcb-bridge:
    build: ./edcb-bridge
    container_name: edcb-bridge
    restart: unless-stopped
    ports:
      - 4510:4510
    environment:
      TZ: Asia/Tokyo
      MIRAKC_URL: http://mirakc:40772
      RECORDER: epgstation            # epgstation | mirakc
      EPGSTATION_URL: http://epgstation:8888
      RECORDED_DIR: /mnt/hdd1/recorded  # recording folder as seen from KonomiTV
    depends_on:
      - mirakc
      - epgstation
```

```sh
docker compose up -d --build edcb-bridge
docker compose logs -f edcb-bridge
```

## Environment variables

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
| `LOG_LEVEL` | `INFO` | `DEBUG` logs every command |

## Using mirakc as the recording backend

Add the recording settings to mirakc's `config.yml` and mount the recording folder.

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
  name (`BS15_1` / `CS4`) for BS/CS
- Past programs (`EnumPgArc`), recorded information (`EnumRecInfo`), and station logos return empty results
  (KonomiTV falls back to its bundled logos)

## License

[MIT License](
  <https://github.com/eggplants/edcb-bridge/blob/HEAD/LICENSE.txt>
)
