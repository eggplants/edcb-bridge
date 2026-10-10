"""Runtime configuration of the bridge."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from collections.abc import Mapping

Recorder = Literal["epgstation", "mirakc"]
RECORDERS: tuple[str, ...] = get_args(Recorder)


@dataclass(frozen=True)
class Config:
    """Settings of the bridge. Every field can be given by an environment variable (upper-cased name)."""

    # CtrlCmd (EpgTimerSrv 互換) を待ち受けるアドレス・ポート
    listen_host: str = "0.0.0.0"  # noqa: S104 - it is a LAN server by design
    listen_port: int = 4510
    # 番組情報・サービス情報の取得元。mirakc か Mirakurun
    mirakc_url: str = "http://127.0.0.1:40772"
    # 録画バックエンド
    recorder: Recorder = "epgstation"
    # EPGStation の URL (recorder=epgstation のとき)
    epgstation_url: str = "http://127.0.0.1:8888"
    # 録画フォルダのパス (KonomiTV から見た絶対パス)
    ## EDCB の rec_folder (絶対パス) と、EPGStation の directory (録画フォルダからの相対パス) の相互変換に使う
    recorded_dir: str = "/mnt/hdd1/recorded"
    # mirakc 録画時のファイル名テンプレート (recorder=mirakc のとき、mirakc の recording.basedir からの相対パス)
    mirakc_content_path_template: str = "{start:%Y%m%d%H%M}_{sid}_{title}.m2ts"
    # 録画開始・終了マージン (秒、EpgTimerSrv.ini の [SET] として KonomiTV に返すだけの表示用の値)
    start_margin: int = 0
    end_margin: int = 0
    # 番組情報キャッシュの有効期間。単位は秒
    epg_cache_ttl: float = 60.0
    # HTTP タイムアウト (秒)
    http_timeout: float = 20.0

    def __post_init__(self) -> None:
        """Validate and normalize the values.

        Raises:
            ValueError: If `recorder` is not a known backend.
        """
        if self.recorder not in RECORDERS:
            msg = f"recorder must be one of {', '.join(RECORDERS)}: {self.recorder}"
            raise ValueError(msg)
        for name in ("mirakc_url", "epgstation_url", "recorded_dir"):
            object.__setattr__(self, name, getattr(self, name).rstrip("/"))

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Config:
        """Build a config from environment variables, falling back to the defaults.

        Args:
            environ: Variables to read instead of `os.environ`. Used by the tests.

        Returns:
            The config.
        """
        environ = os.environ if environ is None else environ
        values: dict[str, object] = {}
        for name, field in cls.__dataclass_fields__.items():
            raw = environ.get(name.upper())
            if raw is None:
                continue
            default = field.default
            values[name] = type(default)(raw) if isinstance(default, int | float) else raw
        return cls(**values)  # ty: ignore[invalid-argument-type]
