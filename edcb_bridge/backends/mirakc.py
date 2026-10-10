"""
mirakc の録画機能 (/api/recording/schedules) を録画バックエンドとして使う

mirakc config.yml で recording.basedir を設定しておく必要がある。
mirakc には自動予約 (キーワード予約) の仕組みがないため、録画予約 (番組指定) のみ対応する。
"""

from __future__ import annotations

import logging
import re
import zlib
from typing import TYPE_CHECKING, Any

import httpx

from edcb_bridge.backends.base import EID_NONE, BackendError, RecorderBackend, is_enabled_rec_mode
from edcb_bridge.epg import EpgSnapshot, mirakurun_program_id, split_mirakurun_program_id, to_jst

if TYPE_CHECKING:
    import datetime as dt

    from edcb_bridge.config import Config

log = logging.getLogger(__name__)

# 録画予約として扱う (= 終了していない) スケジュールの状態
ACTIVE_STATES = {"scheduled", "tracking", "recording", "rescheduling"}


def reserve_id_from_program_id(program_id: int) -> int:
    """
    mirakc の番組 ID (int32 に収まらない) から EDCB の予約 ID (int32) を決定的に算出する
    状態を持たずに済むよう CRC32 を使う (衝突時は先に見つかった方が採用される)
    """
    return (zlib.crc32(str(program_id).encode()) & 0x7FFFFFFF) or 1


def sanitize_file_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip() or "untitled"


class MirakcRecorderBackend(RecorderBackend):
    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.client = httpx.AsyncClient(base_url=config.mirakc_url + "/api", timeout=config.http_timeout)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            res = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as ex:
            msg = f"mirakc request failed: {method} {path}: {ex!r}"
            raise BackendError(msg) from ex
        if res.status_code == httpx.codes.NOT_FOUND and path.startswith("/recording"):
            msg = "mirakc recording API is not available (set recording.basedir in mirakc config.yml)"
            raise BackendError(msg)
        if res.is_error:
            msg = f"mirakc returned HTTP {res.status_code}: {method} {path}: {res.text[:500]}"
            raise BackendError(msg)
        if res.content and res.headers.get("content-type", "").startswith("application/json"):
            return res.json()
        return None

    async def _schedules(self) -> list[dict]:
        schedules = await self._request("GET", "/recording/schedules")
        return [s for s in schedules if s.get("state") in ACTIVE_STATES]

    async def _find_schedule(self, reserve_id: int) -> dict | None:
        for s in await self._schedules():
            if reserve_id_from_program_id(int(s["program"]["id"])) == reserve_id:
                return s
        return None

    def _content_path(self, reserve: dict) -> str:
        """録画ファイルのパス (mirakc の recording.basedir からの相対パス)"""
        directory = self.rec_folder_to_relative_dir(reserve.get("rec_setting", {}))
        start: dt.datetime = reserve["start_time"]
        file_name = self.config.mirakc_content_path_template.format(
            start=start,
            sid=reserve["sid"],
            onid=reserve["onid"],
            eid=reserve["eid"],
            title=sanitize_file_name(reserve.get("title", "")),
        )
        return f"{directory}/{file_name}" if directory else file_name

    async def enum_reserve(self, snapshot: EpgSnapshot) -> list[dict]:
        result = []
        for s in await self._schedules():
            program = s["program"]
            onid, sid, eid = split_mirakurun_program_id(int(program["id"]))
            service = snapshot.services_by_id.get((onid, sid))
            start = to_jst(program["startAt"])
            content_path: str = (s.get("options") or {}).get("contentPath", "")
            directory = content_path.rsplit("/", 1)[0] if "/" in content_path else None
            result.append(
                {
                    "title": program.get("name", ""),
                    "start_time": start,
                    "start_time_epg": start,
                    "duration_second": max(int((program.get("duration") or 0) // 1000), 0),
                    "station_name": service.name if service else "",
                    "onid": onid,
                    "tsid": service.tsid if service else 0,
                    "sid": sid,
                    "eid": eid,
                    "comment": "",
                    "reserve_id": reserve_id_from_program_id(int(program["id"])),
                    "overlap_mode": 0,
                    "rec_setting": self.rec_setting_from(enabled=True, directory=directory),
                    "rec_file_name_list": [content_path] if content_path else [],
                }
            )
        return result

    async def add_reserve(self, snapshot: EpgSnapshot, reserve_list: list[dict]) -> None:
        for reserve in reserve_list:
            if reserve.get("eid", EID_NONE) == EID_NONE:
                msg = "mirakc does not support time-specified (program) reservations"
                raise BackendError(msg)
            body = {
                "programId": mirakurun_program_id(reserve["onid"], reserve["sid"], reserve["eid"]),
                "options": {
                    "contentPath": self._content_path(reserve),
                    "priority": reserve.get("rec_setting", {}).get("priority", 2),
                },
                "tags": ["konomitv"],
            }
            added = await self._request("POST", "/recording/schedules", json=body)
            log.info("Added mirakc recording schedule: %s", (added or {}).get("program", {}).get("id"))

    async def chg_reserve(self, snapshot: EpgSnapshot, reserve_list: list[dict]) -> None:
        for reserve in reserve_list:
            # mirakc のスケジュールは更新できないため、無効化 = 削除として扱う
            if not is_enabled_rec_mode(reserve.get("rec_setting", {}).get("rec_mode", 1)):
                await self.del_reserve([reserve["reserve_id"]])
            else:
                log.warning("mirakc recording schedules cannot be modified; settings change ignored")

    async def del_reserve(self, reserve_ids: list[int]) -> None:
        for reserve_id in reserve_ids:
            schedule = await self._find_schedule(reserve_id)
            if schedule is None:
                msg = f"Reservation not found: {reserve_id}"
                raise BackendError(msg)
            await self._request("DELETE", f"/recording/schedules/{schedule['program']['id']}")

    async def get_recording_file_path(self, snapshot: EpgSnapshot, reserve_id: int) -> str | None:
        schedule = await self._find_schedule(reserve_id)
        if schedule is None or schedule.get("state") != "recording":
            return None
        return (schedule.get("options") or {}).get("contentPath", "")

    async def is_recording(self) -> bool:
        try:
            return len(await self._request("GET", "/recording/recorders") or []) > 0
        except BackendError:
            return False
