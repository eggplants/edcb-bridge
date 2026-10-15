"""
EPGStation (v2 / v3) を録画バックエンドとして使う

- EDCB の録画予約 (ReserveData)  <-> EPGStation の予約 (/api/reserves)
- EDCB の自動予約 (AutoAddData)  <-> EPGStation のルール (/api/rules)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from edcb_bridge.backends.base import EID_NONE, BackendError, RecorderBackend, is_enabled_rec_mode
from edcb_bridge.epg import (
    CONTENT_NIBBLE_ALL_MIDDLE,
    GR_NETWORK_ID_MAX,
    GR_NETWORK_ID_MIN,
    EpgSnapshot,
    Service,
    mirakurun_program_id,
    mirakurun_service_id,
    split_mirakurun_program_id,
    to_jst,
)
from edcb_bridge.search import FREE_CA_FREE_ONLY, FREE_CA_PAID_ONLY, strip_note

if TYPE_CHECKING:
    import datetime as dt

    from edcb_bridge.config import Config

log = logging.getLogger(__name__)

# EPGStation の SearchTime.week のビット (日曜 = 0x01 ... 土曜 = 0x40)
WEEK_BITS = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40]

# ARIB のジャンル大分類の最大値 (0xE: 拡張, 0xF: その他)
MAX_GENRE_LV1 = 0xF

HOURS_PER_DAY = 24
MINUTES_PER_DAY = HOURS_PER_DAY * 60

# 録画中一覧のキャッシュ秒数
RECORDING_CACHE_TTL = 5.0

# チューナー不足で録画できないことを表す ReserveData.overlap_mode の値
OVERLAP_MODE_UNAVAILABLE = 2


class EPGStationBackend(RecorderBackend):
    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.client = httpx.AsyncClient(base_url=config.epgstation_url + "/api", timeout=config.http_timeout)
        # EPGStation の録画保存先の名前 (config.yml の recorded[0].name)
        self._parent_directory_name: str | None = None
        # 録画中一覧の短期キャッシュ (KonomiTV は予約ごとに録画中判定を並列で問い合わせてくるため)
        self._recording_cache: tuple[float, list[dict]] | None = None
        self._recording_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------
    # HTTP ヘルパー
    # ------------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            res = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as ex:
            msg = f"EPGStation request failed: {method} {path}: {ex!r}"
            raise BackendError(msg) from ex
        if res.is_error:
            msg = f"EPGStation returned HTTP {res.status_code}: {method} {path}: {res.text[:500]}"
            raise BackendError(msg)
        if res.content and res.headers.get("content-type", "").startswith("application/json"):
            return res.json()
        return None

    async def _parent_directory(self) -> str:
        if self._parent_directory_name is None:
            config = await self._request("GET", "/config")
            recorded = config.get("recorded") or ["recorded"]
            self._parent_directory_name = recorded[0]
        return self._parent_directory_name

    async def _save_option(self, rec_setting: dict) -> dict:
        directory = self.rec_folder_to_relative_dir(rec_setting)
        save_option: dict[str, Any] = {"parentDirectoryName": await self._parent_directory()}
        if directory:
            save_option["directory"] = directory
        folders = rec_setting.get("rec_folder_list") or []
        if folders and "?" in folders[0].get("rec_name_plug_in", ""):
            template = folders[0]["rec_name_plug_in"].split("?", 1)[1]
            if template:
                save_option["recordedFormat"] = template
        return save_option

    # ------------------------------------------------------------------
    # 録画予約
    # ------------------------------------------------------------------

    def _reserve_to_edcb(self, snapshot: EpgSnapshot, r: dict) -> dict:
        channel_id = int(r["channelId"])
        onid, sid = channel_id // 100000, channel_id % 100000
        service: Service | None = snapshot.services_by_id.get((onid, sid))
        eid = EID_NONE
        if r.get("programId") is not None and not r.get("isTimeSpecified", False):
            _, _, eid = split_mirakurun_program_id(int(r["programId"]))
        start = to_jst(r["startAt"])
        is_disabled = r.get("isSkip", False) or r.get("isOverlap", False)
        return {
            "title": r.get("name", ""),
            "start_time": start,
            "start_time_epg": start,
            "duration_second": max(int((r["endAt"] - r["startAt"]) // 1000), 0),
            "station_name": service.name if service else "",
            "onid": onid,
            # サービスが見つからない場合、地デジなら TSID = ONID とみなす
            "tsid": service.tsid if service else (onid if GR_NETWORK_ID_MIN <= onid <= GR_NETWORK_ID_MAX else 0),
            "sid": sid,
            "eid": eid,
            "comment": "EPG自動予約" if r.get("ruleId") is not None else "",
            "reserve_id": int(r["id"]),
            # EPGStation の isConflict はチューナー不足で録画不可を意味する
            "overlap_mode": OVERLAP_MODE_UNAVAILABLE if r.get("isConflict", False) else 0,
            "rec_setting": self.rec_setting_from(
                enabled=not is_disabled,
                directory=r.get("directory"),
                file_name_template=r.get("recordedFormat"),
            ),
            "rec_file_name_list": [],
        }

    async def _get_all_reserves(self) -> list[dict]:
        reserves: list[dict] = []
        offset = 0
        while True:
            data = await self._request(
                "GET",
                "/reserves",
                params={
                    "type": "all",
                    "isHalfWidth": "false",
                    "offset": offset,
                    "limit": 1000,
                },
            )
            reserves.extend(data["reserves"])
            offset += len(data["reserves"])
            if len(data["reserves"]) == 0 or offset >= data["total"]:
                return reserves

    async def enum_reserve(self, snapshot: EpgSnapshot) -> list[dict]:
        return [self._reserve_to_edcb(snapshot, r) for r in await self._get_all_reserves()]

    async def add_reserve(self, snapshot: EpgSnapshot, reserve_list: list[dict]) -> None:
        for reserve in reserve_list:
            body: dict[str, Any] = {
                "allowEndLack": True,
                "saveOption": await self._save_option(reserve.get("rec_setting", {})),
            }
            if reserve.get("eid", EID_NONE) != EID_NONE:
                # 番組指定予約
                body["programId"] = mirakurun_program_id(reserve["onid"], reserve["sid"], reserve["eid"])
            else:
                # プログラム予約。EPGStation では時刻指定予約になる
                start: dt.datetime = reserve["start_time"]
                start_ms = int(start.timestamp() * 1000)
                body["timeSpecifiedOption"] = {
                    "name": reserve.get("title", ""),
                    "channelId": mirakurun_service_id(reserve["onid"], reserve["sid"]),
                    "startAt": start_ms,
                    "endAt": start_ms + int(reserve.get("duration_second", 0)) * 1000,
                }
            added = await self._request("POST", "/reserves", json=body)
            log.info("Added EPGStation reserve: %s (%s)", added, reserve.get("title", ""))

            # 無効状態で追加するよう指定された場合は、追加後にスキップ状態にする (ルール予約のみ可能)
            if not is_enabled_rec_mode(reserve.get("rec_setting", {}).get("rec_mode", 1)):
                log.warning("EPGStation cannot add a disabled manual reservation; it was added as enabled")

    async def chg_reserve(self, snapshot: EpgSnapshot, reserve_list: list[dict]) -> None:
        for reserve in reserve_list:
            reserve_id = reserve["reserve_id"]
            current = await self._request("GET", f"/reserves/{reserve_id}", params={"isHalfWidth": "false"})
            want_enabled = is_enabled_rec_mode(reserve.get("rec_setting", {}).get("rec_mode", 1))
            is_rule_reserve = current.get("ruleId") is not None

            # 有効 / 無効の切り替え
            if want_enabled:
                if current.get("isSkip", False):
                    await self._request("DELETE", f"/reserves/{reserve_id}/skip")
                if current.get("isOverlap", False):
                    await self._request("DELETE", f"/reserves/{reserve_id}/overlap")
            elif not (current.get("isSkip", False) or current.get("isOverlap", False)):
                if not is_rule_reserve:
                    # EPGStation の手動予約には「無効」状態がない
                    msg = "EPGStation cannot disable a manual reservation (delete it instead)"
                    raise BackendError(msg)
                # ルール予約を削除するとスキップ (無効) 状態になる
                await self._request("DELETE", f"/reserves/{reserve_id}")

            # 保存先の変更 (EPGStation で更新できるのは手動予約のみ)
            if not is_rule_reserve:
                save_option = await self._save_option(reserve.get("rec_setting", {}))
                if save_option.get("directory") != current.get("directory") or save_option.get(
                    "recordedFormat"
                ) != current.get("recordedFormat"):
                    await self._request(
                        "PUT",
                        f"/reserves/{reserve_id}",
                        json={
                            "allowEndLack": current.get("allowEndLack", True),
                            "saveOption": save_option,
                        },
                    )

    async def del_reserve(self, reserve_ids: list[int]) -> None:
        for reserve_id in reserve_ids:
            # ルールによる予約の場合、EPGStation では削除ではなくスキップ (無効) 状態になる
            await self._request("DELETE", f"/reserves/{reserve_id}")

    async def _get_recording(self) -> list[dict]:
        async with self._recording_lock:
            now = time.monotonic()
            if self._recording_cache is not None and now - self._recording_cache[0] < RECORDING_CACHE_TTL:
                return self._recording_cache[1]
            data = await self._request("GET", "/recording", params={"isHalfWidth": "false", "limit": 1000})
            self._recording_cache = (now, data["records"])
            return data["records"]

    async def get_recording_file_path(self, snapshot: EpgSnapshot, reserve_id: int) -> str | None:
        records = await self._get_recording()
        if not records:
            return None
        try:
            reserve = await self._request("GET", f"/reserves/{reserve_id}", params={"isHalfWidth": "false"})
        except BackendError:
            return None
        for record in records:
            same_program = reserve.get("programId") is not None and record.get("programId") == reserve["programId"]
            same_time = record.get("channelId") == reserve["channelId"] and record["startAt"] == reserve["startAt"]
            if same_program or same_time:
                video_files = record.get("videoFiles") or []
                if video_files and video_files[0].get("filename"):
                    return f"{self.config.recorded_dir}/{video_files[0]['filename']}"
                return f"{self.config.recorded_dir}/{record.get('name', '')}"
        return None

    async def is_recording(self) -> bool:
        return len(await self._get_recording()) > 0

    # ------------------------------------------------------------------
    # キーワード自動予約 <-> ルール
    # ------------------------------------------------------------------

    def _rule_to_edcb(self, snapshot: EpgSnapshot, rule: dict) -> dict:
        so: dict = rule.get("searchOption") or {}
        ro: dict = rule.get("reserveOption") or {}
        save: dict = rule.get("saveOption") or {}
        title_only = so.get("name", True) and not so.get("description", False) and not so.get("extended", False)
        return {
            "data_id": int(rule["id"]),
            "search_info": {
                "and_key": so.get("keyword") or "",
                "not_key": so.get("ignoreKeyword") or "",
                "key_disabled": not ro.get("enable", True),
                "case_sensitive": so.get("keyCS", False),
                "reg_exp_flag": so.get("keyRegExp", False),
                "title_only_flag": title_only,
                "content_list": genres_to_content_list(so.get("genres") or []),
                "date_list": times_to_date_list(so.get("times") or []),
                "service_list": search_option_to_service_list(snapshot, so),
                "video_list": [],
                "audio_list": [],
                "aimai_flag": False,
                "not_contet_flag": False,
                "not_date_flag": False,
                "free_ca_flag": FREE_CA_FREE_ONLY if so.get("isFree", False) else 0,
                "chk_rec_end": ro.get("avoidDuplicate", False),
                "chk_rec_day": ro.get("periodToAvoidDuplicate") or 0,
                "chk_rec_no_service": True,
                "chk_duration_min": (so.get("durationMin") or 0) // 60,
                "chk_duration_max": (so.get("durationMax") or 0) // 60,
            },
            "rec_setting": self.rec_setting_from(
                enabled=True,
                directory=save.get("directory"),
                file_name_template=save.get("recordedFormat"),
            ),
            "add_count": rule.get("reservesCnt") or 0,
        }

    async def _auto_add_to_rule(self, snapshot: EpgSnapshot, data: dict, current: dict | None = None) -> dict:
        key: dict = data["search_info"]
        rec_setting: dict = data.get("rec_setting", {})

        warn_unsupported_search_conditions(key)
        title_only = key.get("title_only_flag", False)
        search_option: dict[str, Any] = {
            "keyword": key.get("and_key", ""),
            "ignoreKeyword": strip_note(key.get("not_key", "")),
            "keyCS": key.get("case_sensitive", False),
            "keyRegExp": key.get("reg_exp_flag", False),
            "name": True,
            "description": not title_only,
            "extended": not title_only,
            "ignoreKeyCS": key.get("case_sensitive", False),
            "ignoreKeyRegExp": key.get("reg_exp_flag", False),
            "ignoreName": True,
            "ignoreDescription": not title_only,
            "ignoreExtended": not title_only,
            **service_list_to_search_option(snapshot, key.get("service_list", [])),
        }
        if genres := content_list_to_genres(key.get("content_list", [])):
            search_option["genres"] = genres
        if times := date_list_to_times(key.get("date_list", [])):
            search_option["times"] = times
        if key.get("free_ca_flag") == FREE_CA_FREE_ONLY:
            search_option["isFree"] = True
        if key.get("chk_duration_min", 0) > 0:
            search_option["durationMin"] = key["chk_duration_min"] * 60
        if key.get("chk_duration_max", 0) > 0:
            search_option["durationMax"] = key["chk_duration_max"] * 60

        reserve_option = build_reserve_option(key, rec_setting)

        rule: dict[str, Any] = {
            "isTimeSpecification": False,
            "searchOption": search_option,
            "reserveOption": reserve_option,
            "saveOption": await self._save_option(rec_setting),
        }
        # EDCB 側にない項目 (タグ・エンコード設定) は既存ルールの値を引き継ぐ
        if current is not None:
            if "tags" in (current.get("reserveOption") or {}):
                reserve_option["tags"] = current["reserveOption"]["tags"]
            if current.get("encodeOption"):
                rule["encodeOption"] = current["encodeOption"]
        return rule

    async def enum_auto_add(self, snapshot: EpgSnapshot) -> list[dict]:
        rules: list[dict] = []
        offset = 0
        while True:
            data = await self._request("GET", "/rules", params={"offset": offset, "limit": 1000})
            rules.extend(data["rules"])
            offset += len(data["rules"])
            if len(data["rules"]) == 0 or offset >= data["total"]:
                break
        return [self._rule_to_edcb(snapshot, rule) for rule in rules if not rule.get("isTimeSpecification", False)]

    async def add_auto_add(self, snapshot: EpgSnapshot, data_list: list[dict]) -> None:
        for data in data_list:
            added = await self._request("POST", "/rules", json=await self._auto_add_to_rule(snapshot, data))
            log.info("Added EPGStation rule: %s (%s)", added, data["search_info"].get("and_key", ""))

    async def chg_auto_add(self, snapshot: EpgSnapshot, data_list: list[dict]) -> None:
        for data in data_list:
            rule_id = data["data_id"]
            current = await self._request("GET", f"/rules/{rule_id}")
            await self._request("PUT", f"/rules/{rule_id}", json=await self._auto_add_to_rule(snapshot, data, current))

    async def del_auto_add(self, ids: list[int]) -> None:
        for rule_id in ids:
            await self._request("DELETE", f"/rules/{rule_id}")


# ----------------------------------------------------------------------
# EDCB の検索条件 <-> EPGStation の searchOption の変換
# ----------------------------------------------------------------------


def warn_unsupported_search_conditions(key: dict) -> None:
    """EPGStation が表現できない検索条件が指定されていたら警告する (条件は無視される)"""
    if any(key.get(k) for k in ("aimai_flag", "not_contet_flag", "not_date_flag")) or (
        key.get("free_ca_flag") == FREE_CA_PAID_ONLY
    ):
        log.warning("Fuzzy search, excluding genres / dates and paid-only are not supported by EPGStation, ignored")


def build_reserve_option(key: dict, rec_setting: dict) -> dict[str, Any]:
    """EDCB の検索条件・録画設定から EPGStation の reserveOption を組み立てる"""
    reserve_option: dict[str, Any] = {
        "enable": not key.get("key_disabled", False) and is_enabled_rec_mode(rec_setting.get("rec_mode", 1)),
        "allowEndLack": True,
        "avoidDuplicate": key.get("chk_rec_end", False),
    }
    if key.get("chk_rec_end", False):
        reserve_option["periodToAvoidDuplicate"] = key.get("chk_rec_day", 0)
    return reserve_option


def search_option_to_service_list(snapshot: EpgSnapshot, so: dict) -> list[int]:
    """EPGStation の channelIds (なければ GR/BS/CS/SKY フラグ) を EDCB の service_list に変換する"""
    if so.get("channelIds"):
        services = [snapshot.services_by_id.get((c // 100000, c % 100000)) for c in so["channelIds"]]
        return [s.key for s in services if s is not None]
    return [s.key for s in snapshot.services if so.get(s.channel_type, False)]


def service_list_to_search_option(snapshot: EpgSnapshot, service_list: list[int]) -> dict[str, Any]:
    """EDCB の service_list を EPGStation の channelIds (全サービスなら放送波種別フラグ) に変換する"""
    requested = set(service_list)
    if requested and requested >= {s.key for s in snapshot.services}:
        return dict.fromkeys({s.channel_type for s in snapshot.services}, True)
    return {"channelIds": [mirakurun_service_id(k >> 32, k & 0xFFFF) for k in service_list]}


def genres_to_content_list(genres: list[dict]) -> list[dict]:
    """EPGStation の genres を EDCB の content_list に変換する (中分類なし = 大分類内のすべて)"""
    return [
        {
            "content_nibble": (g["genre"] & 0xF) << 8
            | (CONTENT_NIBBLE_ALL_MIDDLE if g.get("subGenre") is None else g["subGenre"] & 0xF),
            "user_nibble": 0,
        }
        for g in genres
    ]


def content_list_to_genres(content_list: list[dict]) -> list[dict]:
    """EDCB の content_list を EPGStation の genres に変換する (拡張ジャンルは表現できないので捨てる)"""
    genres = []
    for c in content_list:
        lv1 = c["content_nibble"] >> 8
        lv2 = c["content_nibble"] & 0xFF
        if lv1 > MAX_GENRE_LV1:
            continue
        genres.append({"genre": lv1} if lv2 == CONTENT_NIBBLE_ALL_MIDDLE else {"genre": lv1, "subGenre": lv2 & 0xF})
    return genres


def times_to_date_list(times: list[dict]) -> list[dict]:
    """EPGStation の times (曜日ビット + 開始時 + 時間数) を EDCB の date_list に変換する"""
    date_list = []
    for t in times:
        # 開始時・時間数がなければ丸一日
        start_hour = t.get("start")
        range_hour = t.get("range")
        if start_hour is None or range_hour is None:
            start_hour, range_hour = 0, HOURS_PER_DAY
        for dow, bit in enumerate(WEEK_BITS):
            if not t.get("week", 0) & bit:
                continue
            end_minutes = (dow * HOURS_PER_DAY + start_hour + range_hour) * 60 - 1
            date_list.append(
                {
                    "start_day_of_week": dow,
                    "start_hour": start_hour,
                    "start_min": 0,
                    "end_day_of_week": end_minutes // MINUTES_PER_DAY % 7,
                    "end_hour": end_minutes // 60 % HOURS_PER_DAY,
                    "end_min": end_minutes % 60,
                }
            )
    return date_list


def date_list_to_times(date_list: list[dict]) -> list[dict]:
    """EDCB の date_list を EPGStation の times に変換する (EPGStation は時単位なので切り上げる)"""
    times = []
    for d in date_list:
        start = d["start_day_of_week"] * MINUTES_PER_DAY + d["start_hour"] * 60 + d["start_min"]
        end = d["end_day_of_week"] * MINUTES_PER_DAY + d["end_hour"] * 60 + d["end_min"]
        if end < start:
            end += 7 * MINUTES_PER_DAY
        range_hour = max(1, -(-(end - start + 1) // 60))
        if d["start_hour"] == 0 and d["start_min"] == 0 and range_hour % HOURS_PER_DAY == 0:
            # 丸一日 (複数日) の指定は曜日だけで表す
            times.extend(
                {"week": WEEK_BITS[(d["start_day_of_week"] + i) % 7]} for i in range(range_hour // HOURS_PER_DAY)
            )
        else:
            times.append(
                {
                    "start": d["start_hour"],
                    "range": min(range_hour, 7 * HOURS_PER_DAY),
                    "week": WEEK_BITS[d["start_day_of_week"]],
                }
            )
    return times
