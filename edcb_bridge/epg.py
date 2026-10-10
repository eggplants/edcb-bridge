"""
mirakc (Mirakurun 互換 API) から取得したサービス・番組情報を EDCB の構造体に変換する
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .codec import JST

if TYPE_CHECKING:
    import httpx

    from .config import Config

log = logging.getLogger(__name__)

# ARIB STD-B10 のサンプリング周波数コード
SAMPLING_RATE_CODES = {16000: 1, 22050: 2, 24000: 3, 32000: 5, 44100: 6, 48000: 7}

# ARIB STD-B10 のサービス形式種別: ワンセグ (部分受信) のデータサービス
ONESEG_SERVICE_TYPE = 0xC0

# 地デジのネットワーク ID の範囲 (地デジでは TSID = ONID)
GR_NETWORK_ID_MIN = 0x7880
GR_NETWORK_ID_MAX = 0x7FE8

# ContentData.content_nibble の中分類に入る「大分類内のすべて」を表す値 (EDCB 独自)
CONTENT_NIBBLE_ALL_MIDDLE = 0xFF

# CMD_EPG_SRV_ENUM_PG_INFO_EX の引数の最小要素数 (開始時刻範囲の始点・終点)
MIN_PG_INFO_KEY_LIST_LENGTH = 2


def service_key(onid: int, tsid: int, sid: int) -> int:
    """EDCB で使われる (onid << 32 | tsid << 16 | sid) 形式のキー"""
    return onid << 32 | tsid << 16 | sid


def mirakurun_service_id(onid: int, sid: int) -> int:
    """Mirakurun / EPGStation 形式のサービス ID (= EPGStation の channelId)"""
    return onid * 100000 + sid


def mirakurun_program_id(onid: int, sid: int, eid: int) -> int:
    """Mirakurun / EPGStation 形式の番組 ID"""
    return (onid * 100000 + sid) * 100000 + eid


def split_mirakurun_program_id(program_id: int) -> tuple[int, int, int]:
    """Mirakurun 形式の番組 ID を (onid, sid, eid) に分解する"""
    return program_id // 10000000000, program_id // 100000 % 100000, program_id % 100000


def guess_tsid(channel_type: str, channel: str, onid: int) -> int:
    """
    mirakc の API は TSID を返さないため、チャンネル種別とチャンネル名から推測する

    - 地デジ: TSID は原則ネットワーク ID と同じ
    - BS: "BS15_1" のような (トランスポンダ番号, スロット番号) 形式から算出 (0x4000 | tp << 4 | slot)
    - CS: "CS4" / "ND04" のようなトランスポンダ番号から算出 (0x6000 | nd << 4)
    """
    if channel_type == "GR":
        return onid
    if channel_type == "BS":
        m = re.fullmatch(r"BS(\d+)_(\d+)", channel)
        if m:
            return 0x4000 | int(m.group(1)) << 4 | int(m.group(2))
    if channel_type == "CS":
        m = re.fullmatch(r"(?:CS|ND)(\d+)", channel)
        if m:
            return 0x6000 | int(m.group(1)) << 4
    # 推測できない場合は 0 (KonomiTV は TSID なしのチャンネルでは予約できない)
    log.warning("Cannot determine TSID: type=%s channel=%s onid=%d", channel_type, channel, onid)
    return 0


def to_jst(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, tz=JST)


def filetime_to_datetime(ft: int) -> dt.datetime:
    """
    EDCB の FILETIME 値を datetime に変換する
    EDCB の FILETIME は「JST のローカル時刻を UTC とみなした」値なので、9時間を引いて実時刻に戻す
    """
    unix_sec = (ft - 116444736000000000) / 10000000 - 9 * 3600
    try:
        return dt.datetime.fromtimestamp(unix_sec, tz=JST)
    except (OverflowError, OSError, ValueError):
        # 0x7fffffffffffffff (無限大) や 1 (無限小) のような範囲外の値
        return dt.datetime.max.replace(tzinfo=JST) if unix_sec > 0 else dt.datetime.min.replace(tzinfo=JST)


@dataclass
class Service:
    onid: int
    tsid: int
    sid: int
    service_type: int
    name: str
    channel_type: str
    channel: str
    network_name: str
    remote_control_key_id: int

    @property
    def key(self) -> int:
        return service_key(self.onid, self.tsid, self.sid)

    @property
    def mirakurun_id(self) -> int:
        return mirakurun_service_id(self.onid, self.sid)

    def to_service_info(self) -> dict:
        return {
            "onid": self.onid,
            "tsid": self.tsid,
            "sid": self.sid,
            "service_type": self.service_type,
            "partial_reception_flag": 1 if self.service_type == ONESEG_SERVICE_TYPE else 0,
            "service_provider_name": "",
            "service_name": self.name,
            "network_name": self.network_name,
            "ts_name": self.network_name,
            "remote_control_key_id": self.remote_control_key_id,
        }


@dataclass
class EpgSnapshot:
    services: list[Service] = field(default_factory=list)
    # (onid, sid) -> Service
    services_by_id: dict[tuple[int, int], Service] = field(default_factory=dict)
    # (onid, sid) -> mirakc の番組情報のリスト (開始時刻順)
    programs: dict[tuple[int, int], list[dict[str, Any]]] = field(default_factory=dict)
    fetched_at: float = 0.0


class EpgSource:
    """mirakc からサービス・番組情報を取得してキャッシュする"""

    def __init__(self, config: Config, client: httpx.AsyncClient) -> None:
        self.config = config
        self.client = client
        self.snapshot = EpgSnapshot()
        self.lock = asyncio.Lock()

    async def get(self) -> EpgSnapshot:
        async with self.lock:
            if time.monotonic() - self.snapshot.fetched_at < self.config.epg_cache_ttl:
                return self.snapshot
            try:
                self.snapshot = await self._fetch()
            except Exception:
                # 取得に失敗しても、古いキャッシュがあればそれを返す
                if self.snapshot.fetched_at == 0.0:
                    raise
                log.exception("Failed to refresh EPG from mirakc, using stale cache")
            return self.snapshot

    async def _fetch(self) -> EpgSnapshot:
        base = self.config.mirakc_url
        channels_res, services_res, programs_res = await asyncio.gather(
            self.client.get(f"{base}/api/channels"),
            self.client.get(f"{base}/api/services"),
            self.client.get(f"{base}/api/programs"),
        )
        for res in (channels_res, services_res, programs_res):
            res.raise_for_status()

        # チャンネル (物理チャンネル) 名を引けるようにしておく
        channel_names: dict[tuple[str, str], str] = {}
        for ch in channels_res.json():
            channel_names[(ch["type"], ch["channel"])] = ch.get("name", "")

        snapshot = EpgSnapshot(fetched_at=time.monotonic())
        for s in services_res.json():
            ch = s.get("channel") or {}
            ch_type = ch.get("type", "")
            ch_name = ch.get("channel", "")
            onid = int(s["networkId"])
            tsid = (
                int(s["transportStreamId"])
                if s.get("transportStreamId") is not None
                else guess_tsid(ch_type, ch_name, onid)
            )
            service = Service(
                onid=onid,
                tsid=tsid,
                sid=int(s["serviceId"]),
                service_type=int(s.get("type", 1)),
                name=s.get("name", ""),
                channel_type=ch_type,
                channel=ch_name,
                network_name=channel_names.get((ch_type, ch_name), ch_type),
                remote_control_key_id=int(s.get("remoteControlKeyId") or 0),
            )
            snapshot.services.append(service)
            snapshot.services_by_id[(service.onid, service.sid)] = service

        for p in programs_res.json():
            snapshot.programs.setdefault((int(p["networkId"]), int(p["serviceId"])), []).append(p)
        for plist in snapshot.programs.values():
            plist.sort(key=lambda p: p.get("startAt", 0))

        log.info(
            "EPG refreshed: %d services, %d programs",
            len(snapshot.services),
            sum(len(v) for v in snapshot.programs.values()),
        )
        return snapshot


def build_chset5(snapshot: EpgSnapshot) -> bytes:
    """EDCB の ChSet5.txt を生成する (UTF-8 BOM 付き、CRLF)"""
    lines = []
    for s in snapshot.services:
        # 列: サービス名, ネットワーク名, ONID, TSID, SID, サービスタイプ,
        #     部分受信, EPG 取得, 検索対象, リモコン ID
        is_partial = s.service_type == ONESEG_SERVICE_TYPE
        lines.append(
            "\t".join(
                [
                    s.name,
                    s.network_name,
                    str(s.onid),
                    str(s.tsid),
                    str(s.sid),
                    str(s.service_type),
                    "1" if is_partial else "0",
                    "1",
                    "0" if is_partial else "1",
                    str(s.remote_control_key_id),
                ]
            )
        )
    return "﻿".encode() + ("\r\n".join(lines) + "\r\n").encode("utf-8")


def build_extended_text(extended: dict[str, str] | None) -> str:
    """Mirakurun 形式の extended を EDCB の拡張形式イベントテキスト ("- 見出し\\n本文\\n") に変換する"""
    if not extended:
        return ""
    return "".join(f"- {head}\n{body}\n" for head, body in extended.items())


def program_to_event_info(service: Service, p: dict[str, Any]) -> dict:
    """mirakc の番組情報を EDCB の EventInfo に変換する"""
    v: dict[str, Any] = {
        "onid": service.onid,
        "tsid": service.tsid,
        "sid": service.sid,
        "eid": int(p["eventId"]),
        "free_ca_flag": 0 if p.get("isFree", True) else 1,
    }
    if p.get("startAt") is not None:
        v["start_time"] = to_jst(p["startAt"])
    # 放送時間未定の番組では duration が 0 や巨大な値になることがある
    duration = p.get("duration")
    if duration is not None and 0 < duration < 86400 * 1000 * 7:
        v["duration_sec"] = int(duration // 1000)

    if p.get("name") is not None:
        v["short_info"] = {"event_name": p.get("name", ""), "text_char": p.get("description", "") or ""}
    ext = build_extended_text(p.get("extended"))
    if ext:
        v["ext_info"] = {"text_char": ext}

    genres = p.get("genres") or []
    if genres:
        v["content_info"] = {
            "nibble_list": [
                {
                    "content_nibble": (g.get("lv1", 0xF) & 0xF) << 8 | (g.get("lv2", 0xF) & 0xF),
                    "user_nibble": (g.get("un1", 0xF) & 0xF) << 8 | (g.get("un2", 0xF) & 0xF),
                }
                for g in genres
            ]
        }

    video = p.get("video")
    if video and video.get("streamContent") is not None:
        v["component_info"] = {
            "stream_content": video.get("streamContent", 1),
            "component_type": video.get("componentType", 0),
            "component_tag": 0,
            "text_char": "",
        }

    audios = p.get("audios") or ([p["audio"]] if p.get("audio") else [])
    if audios:
        v["audio_info"] = {
            "component_list": [
                {
                    "stream_content": 2,
                    "component_type": a.get("componentType", 3),
                    "component_tag": 0x10 + i,
                    "stream_type": 0x0F,
                    "simulcast_group_tag": 0xFF,
                    "es_multi_lingual_flag": 1 if len(a.get("langs") or []) > 1 else 0,
                    "main_component_flag": 1 if a.get("isMain", i == 0) else 0,
                    "quality_indicator": 3,
                    "sampling_rate": SAMPLING_RATE_CODES.get(a.get("samplingRate", 48000), 7),
                    "text_char": "",
                }
                for i, a in enumerate(audios)
            ]
        }

    # イベント共有 (サブチャンネルでの同一番組) とイベントリレー
    ## KonomiTV は「要素が 1 つだけで、それが自分自身でない」グループを持つ番組をスキップするため、自分自身も含めて返す
    shared = [x for x in (p.get("relatedItems") or []) if x.get("type") == "shared"]
    if shared:
        v["event_group_info"] = {
            "group_type": 1,
            "event_data_list": [
                {
                    "onid": x.get("networkId") or service.onid,
                    "tsid": service.tsid,
                    "sid": x["serviceId"],
                    "eid": x["eventId"],
                }
                for x in shared
            ],
        }
    relay = [x for x in (p.get("relatedItems") or []) if x.get("type") == "relay"]
    if relay:
        v["event_relay_info"] = {
            "group_type": 2,
            "event_data_list": [
                {
                    "onid": x.get("networkId") or service.onid,
                    "tsid": service.tsid,
                    "sid": x["serviceId"],
                    "eid": x["eventId"],
                }
                for x in relay
            ],
        }
    return v


def enum_pg_info(snapshot: EpgSnapshot, key_list: list[int]) -> list[dict]:
    """
    CMD_EPG_SRV_ENUM_PG_INFO_EX の実装
    key_list の最終 2 要素は開始時刻の範囲 (FILETIME)、それ以外は (OR マスク, サービスキー) のペア
    """
    # 最低でも時間範囲の 2 要素が必要で、全体は常にペアの並びになる
    if len(key_list) < MIN_PG_INFO_KEY_LIST_LENGTH or len(key_list) % 2 != 0:
        return []
    start_min = filetime_to_datetime(key_list[-2])
    start_max = filetime_to_datetime(key_list[-1])
    pairs = [(key_list[i], key_list[i + 1]) for i in range(0, len(key_list) - 2, 2)]

    result = []
    for s in snapshot.services:
        if not any((s.key | mask) == (key | mask) for mask, key in pairs):
            continue
        events = []
        for p in snapshot.programs.get((s.onid, s.sid), []):
            if p.get("startAt") is None:
                continue
            start = to_jst(p["startAt"])
            if start_min <= start < start_max:
                events.append(program_to_event_info(s, p))
        if events:
            result.append({"service_info": s.to_service_info(), "event_list": events})
    return result
