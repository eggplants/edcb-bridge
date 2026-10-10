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

KonomiTV の EDCB バックエンド向け録画予約機能を、**EPGStation** または **mirakc** で使えるようにする中間サーバー。

[English](README.md)

## インストール

```bash
# mise (GitHub Release)
mise use -g github:eggplants/edcb-bridge

# mise (pipx)
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

[`setup_mirakc_and_epgs_on_nanopi.sh`](https://gist.github.com/eggplants/afaa31d65a1603f4868c0c400d7d5430) では `~/_tv/compose.yaml` の `services:` に追加する。

```yaml
  edcb-bridge:
    image: ghcr.io/eggplants/edcb-bridge:0.0
    container_name: edcb-bridge
    restart: unless-stopped
    ports:
      - 4510:4510
    environment:
      TZ: Asia/Tokyo
      MIRAKC_URL: http://mirakc:40772
      RECORDER: epgstation            # epgstation | mirakc
      EPGSTATION_URL: http://epgstation:8888
      RECORDED_DIR: /mnt/hdd1/recorded  # KonomiTV から見た録画フォルダ
    depends_on:
      - mirakc
      - epgstation
```

```sh
docker compose up -d edcb-bridge
docker compose logs -f edcb-bridge
```

## CLI

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

### 環境変数

| 変数 | 既定値 | 説明 |
| --- | --- | --- |
| `LISTEN_HOST` / `LISTEN_PORT` | `0.0.0.0` / `4510` | CtrlCmd の待ち受け |
| `MIRAKC_URL` | `http://127.0.0.1:40772` | サービス・番組情報の取得元 |
| `RECORDER` | `epgstation` | 録画バックエンド (`epgstation` / `mirakc`) |
| `EPGSTATION_URL` | `http://127.0.0.1:8888` | EPGStation |
| `RECORDED_DIR` | `/mnt/hdd1/recorded` | KonomiTV から見た録画フォルダ。EDCB の保存先フォルダ (絶対パス) と EPGStation の `directory` (相対パス) の変換に使う |
| `MIRAKC_CONTENT_PATH_TEMPLATE` | `{start:%Y%m%d%H%M}_{sid}_{title}.m2ts` | `RECORDER=mirakc` 時のファイル名 (`{start}` `{onid}` `{sid}` `{eid}` `{title}`) |
| `START_MARGIN` / `END_MARGIN` | `0` | KonomiTV の録画設定画面に表示するマージン (表示のみ) |
| `EPG_CACHE_TTL` | `60` | 番組情報キャッシュ秒数 |
| `HTTP_TIMEOUT` | `20` | HTTP タイムアウト秒数 |
| `LOG_LEVEL` | `INFO` | `DEBUG` で全コマンドをログ出力 |

## KonomiTV の設定 (`config.yaml`)

```yaml
general:
  backend: EDCB
  # ライブ視聴は mirakc から直接受信する (必須)
  always_receive_tv_from_mirakurun: true
  edcb_url: tcp://127.0.0.1:4510/
  mirakurun_url: http://127.0.0.1:40772/
```

変更後に KonomiTV を再起動する (`sudo pm2 restart KonomiTV`)。

## mirakc を録画バックエンドにする場合

mirakc の `config.yml` に録画設定を追加し、録画フォルダをマウントする。

```yaml
recording:
  basedir: /var/lib/mirakc/recording
```

```yaml
  mirakc:
    volumes:
      - /mnt/hdd1/recorded:/var/lib/mirakc/recording
```

この場合 `RECORDED_DIR=/mnt/hdd1/recorded` とすると、KonomiTV で指定した保存先サブフォルダが `contentPath` に反映される。

## 制限事項

- **EPGStation**
  - 手動予約は「無効化」できない (EPGStation に無効状態がないため。削除は可能)
  - ルール由来の予約を削除すると、EPGStation の仕様で「スキップ (無効)」状態の予約として残る
  - 優先度・マージン・追従・ぴったり録画・録画後動作などの EDCB 固有設定は無視される
  - 自動予約の「あいまい検索」「ジャンル/日時の除外指定」「有料のみ」、メモ欄は保存されない
  - 曜日・時間指定は時単位に丸められる
  - 保存先は `RECORDED_DIR` 配下のサブフォルダのみ指定可能。ファイル名テンプレートは EPGStation の `recordedFormat` の書式 (`%TITLE%` など) でそのまま渡される
- **mirakc**: 番組指定の録画予約のみ (時刻指定予約・自動予約・設定変更は不可、無効化は削除として扱う)
- TSID は mirakc が返さないため、地デジは NID、BS/CS はチャンネル名 (`BS15_1` / `CS4`) から算出する
- 過去番組 (`EnumPgArc`)・録画済み情報 (`EnumRecInfo`)・局ロゴは空を返す (KonomiTV は同梱ロゴにフォールバック)

## License

[MIT License](
  <https://github.com/eggplants/edcb-bridge/blob/master/LICENSE.txt>
)
