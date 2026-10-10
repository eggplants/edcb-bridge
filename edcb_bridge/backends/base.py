"""
録画バックエンドの共通インターフェース

各メソッドは EDCB の構造体 (KonomiTV の TypedDict と同じキーを持つ dict) を受け取り / 返す。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

    from edcb_bridge.config import Config
    from edcb_bridge.epg import EpgSnapshot

log = logging.getLogger(__name__)

# EDCB で「イベント ID なし (プログラム予約)」を表す値
EID_NONE = 0xFFFF

# rec_mode 0-4 は有効、5-9 は無効 (歴史的経緯で無効状態が後から追加された)
MAX_ENABLED_REC_MODE = 4


class BackendError(Exception):
    """バックエンドが要求を処理できなかった (CtrlCmd では CMD_ERR として返す)"""


class RecorderBackend:
    """録画予約・自動予約を担当するバックエンド"""

    # バックエンドの HTTP API クライアント (サブクラスが base_url 付きで作る)
    client: httpx.AsyncClient

    def __init__(self, config: Config) -> None:
        self.config = config

    # 録画予約
    async def enum_reserve(self, snapshot: EpgSnapshot) -> list[dict]:
        raise NotImplementedError

    async def add_reserve(self, snapshot: EpgSnapshot, reserve_list: list[dict]) -> None:
        raise NotImplementedError

    async def chg_reserve(self, snapshot: EpgSnapshot, reserve_list: list[dict]) -> None:
        raise NotImplementedError

    async def del_reserve(self, reserve_ids: list[int]) -> None:
        raise NotImplementedError

    async def get_recording_file_path(self, snapshot: EpgSnapshot, reserve_id: int) -> str | None:
        """録画中の予約なら録画ファイルパスを返す"""
        raise NotImplementedError

    async def is_recording(self) -> bool:
        raise NotImplementedError

    # キーワード自動予約 (EPG 自動予約)
    async def enum_auto_add(self, snapshot: EpgSnapshot) -> list[dict]:
        return []

    async def add_auto_add(self, snapshot: EpgSnapshot, data_list: list[dict]) -> None:
        msg = "This recorder backend does not support keyword auto reservation"
        raise BackendError(msg)

    async def chg_auto_add(self, snapshot: EpgSnapshot, data_list: list[dict]) -> None:
        msg = "This recorder backend does not support keyword auto reservation"
        raise BackendError(msg)

    async def del_auto_add(self, ids: list[int]) -> None:
        msg = "This recorder backend does not support keyword auto reservation"
        raise BackendError(msg)

    async def aclose(self) -> None:
        pass

    # 共通ヘルパー

    def rec_folder_to_relative_dir(self, rec_setting: dict) -> str | None:
        """
        EDCB の rec_folder_list (絶対パス) を、録画フォルダからの相対ディレクトリに変換する
        録画フォルダ配下でないパスは無視する (None はデフォルトフォルダを意味する)
        """
        folders = rec_setting.get("rec_folder_list") or []
        if not folders:
            return None
        path = folders[0].get("rec_folder", "").replace("\\", "/").rstrip("/")
        root = self.config.recorded_dir
        if path in (root, ""):
            return None
        if path.startswith(root + "/"):
            return path[len(root) + 1 :]
        log.warning("Recording folder %s is outside of RECORDED_DIR (%s), ignored", path, root)
        return None

    def rec_setting_from(
        self,
        *,
        enabled: bool,
        directory: str | None,
        file_name_template: str | None = None,
    ) -> dict:
        """EDCB の RecSettingData を組み立てる

        Args:
            enabled: 予約が有効か
            directory: 録画フォルダからの相対ディレクトリ (None ならデフォルトフォルダ)
            file_name_template: 録画ファイル名テンプレート

        Returns:
            KonomiTV の RecSettingDataRequired と同じキーを持つ dict
        """
        rec_folder_list = []
        if directory:
            rec_name_plug_in = "RecName_Macro.dll" + (f"?{file_name_template}" if file_name_template else "")
            rec_folder_list.append(
                {
                    "rec_folder": f"{self.config.recorded_dir}/{directory}",
                    "write_plug_in": "Write_Default.dll",
                    "rec_name_plug_in": rec_name_plug_in,
                }
            )
        return {
            "rec_mode": 1 if enabled else 5,  # 1: 指定サービスのみ / 5: 指定サービスのみ (無効)
            "priority": 2,
            "tuijyuu_flag": False,
            "service_mode": 0,
            "pittari_flag": False,
            "bat_file_path": "",
            "rec_folder_list": rec_folder_list,
            "suspend_mode": 0,
            "reboot_flag": False,
            "continue_rec_flag": False,
            "partial_rec_flag": 0,
            "tuner_id": 0,
            "partial_rec_folder": [],
        }


def is_enabled_rec_mode(rec_mode: int) -> bool:
    """rec_mode 0-4 は有効、5-9 は無効"""
    return rec_mode <= MAX_ENABLED_REC_MODE
