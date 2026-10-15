"""
KonomiTV の CtrlCmdUtil (本物の EDCB クライアント実装) からブリッジを叩く結合テスト

mirakc / EPGStation は httpx.MockTransport によるインメモリのフェイクに置き換える。
EPGStation へのリクエストボディは EPGStation 自身の OpenAPI 定義でバリデーションする。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import jsonschema
import pytest

from edcb_bridge.backends import epgstation
from edcb_bridge.codec import JST
from edcb_bridge.config import Config, Recorder
from edcb_bridge.server import CtrlCmdServer

from .konomitv_client import load_ctrlcmdutil

if TYPE_CHECKING:
    from collections.abc import Callable

CtrlCmdUtil = load_ctrlcmdutil()

# リクエストボディは対応する EPGStation の全バージョン (v2 / v3) の定義を満たす必要がある
OPENAPI_SPECS = [
    json.loads((Path(__file__).parent / "fixtures" / f"epgstation-{version}-openapi.json").read_text())
    for version in ("v2", "v3")
]

NOW_MS = int(dt.datetime.now(JST).timestamp() * 1000)
HOUR_MS = 3600 * 1000

# フェイク mirakc のデータ (NHK 総合・東京 / TOKYO MX1)
SERVICES: list[dict[str, Any]] = [
    {
        "id": 3273601024,
        "serviceId": 1024,
        "networkId": 32736,
        "type": 1,
        "remoteControlKeyId": 1,
        "name": "ＮＨＫ総合１・東京",
        "channel": {"type": "GR", "channel": "T27"},
    },
    {
        "id": 3239123608,
        "serviceId": 23608,
        "networkId": 32391,
        "type": 1,
        "remoteControlKeyId": 9,
        "name": "ＴＯＫＹＯ　ＭＸ１",
        "channel": {"type": "GR", "channel": "T16"},
    },
]
CHANNELS: list[dict[str, Any]] = [
    {"type": "GR", "channel": "T27", "name": "NHK総合・東京", "services": []},
    {"type": "GR", "channel": "T16", "name": "TOKYO MX", "services": []},
]
PROGRAMS: list[dict[str, Any]] = [
    {
        "id": 327360102400100,
        "eventId": 100,
        "serviceId": 1024,
        "networkId": 32736,
        "startAt": NOW_MS + HOUR_MS,
        "duration": 1800000,
        "isFree": True,
        "name": "ＮＨＫニュース７",
        "description": "今日のニュース",
        "extended": {"出演者": "山田太郎"},
        "genres": [{"lv1": 0, "lv2": 0, "un1": 15, "un2": 15}],
        "video": {"type": "mpeg2", "resolution": "1080i", "streamContent": 1, "componentType": 179},
        "audios": [{"componentType": 3, "isMain": True, "samplingRate": 48000, "langs": ["jpn"]}],
    },
    {
        "id": 323912360800200,
        "eventId": 200,
        "serviceId": 23608,
        "networkId": 32391,
        "startAt": NOW_MS + 2 * HOUR_MS,
        "duration": 1800000,
        "isFree": True,
        "name": "アニメ　テスト　＃１",
        "description": "あらすじ",
        "genres": [{"lv1": 7, "lv2": 0, "un1": 15, "un2": 15}],
        "relatedItems": [
            {"type": "shared", "networkId": None, "serviceId": 23608, "eventId": 200},
            {"type": "shared", "networkId": None, "serviceId": 23609, "eventId": 200},
        ],
    },
    {
        "id": 323912360800199,
        "eventId": 199,
        "serviceId": 23608,
        "networkId": 32391,
        "startAt": NOW_MS - 2 * HOUR_MS,
        "duration": 1800000,
        "isFree": True,
        "name": "アニメ　テスト　＃０",
        "description": "終了済み",
    },
]


def validate(schema_name: str, body: Any) -> None:
    """EPGStation (v2 / v3) の OpenAPI 定義でリクエストボディを検証する"""
    for spec in OPENAPI_SPECS:
        schema = {"$ref": f"#/components/schemas/{schema_name}", "components": spec["components"]}
        jsonschema.Draft7Validator(schema).validate(body)


def strip_parent_directory(save_option: dict | None) -> dict:
    return {k: v for k, v in (save_option or {}).items() if k != "parentDirectoryName"}


class FakeBackends:
    """mirakc と EPGStation のインメモリフェイク"""

    def __init__(self) -> None:
        self.reserves: dict[int, dict] = {}
        self.rules: dict[int, dict] = {}
        self.recording: list[dict] = []
        self.schedules: list[dict] = []
        self.next_id = 1
        self.requests: list[tuple[str, str, Any]] = []
        self.program_by_id = {p["id"]: p for p in PROGRAMS}
        # (メソッド, パスの正規表現, ハンドラー)。ハンドラーはボディと正規表現のグループを受け取る
        self.routes: list[tuple[str, str, Callable[..., httpx.Response]]] = [
            # mirakc
            ("GET", r"/api/services", lambda _body: ok(SERVICES)),
            ("GET", r"/api/channels", lambda _body: ok(CHANNELS)),
            ("GET", r"/api/programs", lambda _body: ok(PROGRAMS)),
            ("GET", r"/api/recording/schedules", lambda _body: ok(self.schedules)),
            ("POST", r"/api/recording/schedules", self.add_schedule),
            ("DELETE", r"/api/recording/schedules/(\d+)", self.delete_schedule),
            ("GET", r"/api/recording/recorders", self.recorders),
            # EPGStation
            ("GET", r"/api/config", lambda _body: ok({"recorded": ["recorded"]})),
            (
                "GET",
                r"/api/reserves",
                lambda _body: ok({"reserves": [*self.reserves.values()], "total": len(self.reserves)}),
            ),
            ("POST", r"/api/reserves", self.add_reserve),
            ("GET", r"/api/reserves/(\d+)", lambda _body, rid: ok(self.reserves[int(rid)])),
            ("PUT", r"/api/reserves/(\d+)", self.update_reserve),
            ("DELETE", r"/api/reserves/(\d+)/skip", self.unskip_reserve),
            ("DELETE", r"/api/reserves/(\d+)", self.delete_reserve),
            ("GET", r"/api/recording", lambda _body: ok({"records": self.recording, "total": len(self.recording)})),
            ("GET", r"/api/rules", lambda _body: ok({"rules": [*self.rules.values()], "total": len(self.rules)})),
            ("POST", r"/api/rules", self.add_rule),
            ("GET", r"/api/rules/(\d+)", lambda _body, rid: ok(self.rules[int(rid)])),
            ("PUT", r"/api/rules/(\d+)", self.update_rule),
            ("DELETE", r"/api/rules/(\d+)", lambda _body, rid: ok(self.rules.pop(int(rid)))),
        ]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        for method, pattern, handler in self.routes:
            if request.method == method and (m := re.fullmatch(pattern, path)):
                try:
                    return handler(body, *m.groups())
                except KeyError:
                    return ok({"code": 404}, 404)
        return ok({"error": f"not found: {request.method} {path}"}, 404)

    def new_id(self) -> int:
        self.next_id += 1
        return self.next_id - 1

    # mirakc

    def add_schedule(self, body: dict) -> httpx.Response:
        schedule = {
            "program": self.program_by_id[body["programId"]],
            "state": "scheduled",
            "options": body["options"],
            "tags": body["tags"],
        }
        self.schedules.append(schedule)
        return ok(schedule, 201)

    def delete_schedule(self, _body: None, program_id: str) -> httpx.Response:
        self.schedules = [s for s in self.schedules if s["program"]["id"] != int(program_id)]
        return ok(None)

    def recorders(self, _body: None) -> httpx.Response:
        return ok([{"programId": s["program"]["id"]} for s in self.schedules if s["state"] == "recording"])

    # EPGStation

    def add_reserve(self, body: dict) -> httpx.Response:
        validate("ManualReserveOption", body)
        if "programId" in body:
            p = self.program_by_id[body["programId"]]
            item = {
                "programId": p["id"],
                "channelId": p["networkId"] * 100000 + p["serviceId"],
                "startAt": p["startAt"],
                "endAt": p["startAt"] + p["duration"],
                "name": p["name"],
                "isTimeSpecified": False,
            }
        else:
            t = body["timeSpecifiedOption"]
            item = {k: t[k] for k in ("channelId", "startAt", "endAt", "name")} | {"isTimeSpecified": True}
        rid = self.new_id()
        self.reserves[rid] = item | {
            "id": rid,
            "isSkip": False,
            "isConflict": False,
            "isOverlap": False,
            "allowEndLack": body["allowEndLack"],
            "isDeleteOriginalAfterEncode": False,
            **strip_parent_directory(body.get("saveOption")),
        }
        return ok({"reserveId": rid}, 201)

    def update_reserve(self, body: dict, rid: str) -> httpx.Response:
        validate("EditManualReserveOption", body)
        reserve = self.reserves[int(rid)]
        reserve.pop("directory", None)
        reserve.update(strip_parent_directory(body["saveOption"]))
        return ok(None)

    def unskip_reserve(self, _body: None, rid: str) -> httpx.Response:
        self.reserves[int(rid)]["isSkip"] = False
        return ok(None)

    def delete_reserve(self, _body: None, rid: str) -> httpx.Response:
        # ルール由来の予約は削除されずスキップ状態になる (EPGStation の仕様)
        if self.reserves[int(rid)].get("ruleId") is not None:
            self.reserves[int(rid)]["isSkip"] = True
        else:
            del self.reserves[int(rid)]
        return ok(None)

    def add_rule(self, body: dict) -> httpx.Response:
        validate("AddRuleOption", body)
        rid = self.new_id()
        self.rules[rid] = {"id": rid, "reservesCnt": 3, **body}
        return ok({"ruleId": rid}, 201)

    def update_rule(self, body: dict, rid: str) -> httpx.Response:
        validate("AddRuleOption", body)
        self.rules[int(rid)] = {"id": int(rid), "reservesCnt": 3, **body}
        return ok(None)


def ok(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status) if data is None else httpx.Response(status, json=data)


@pytest.fixture
def fake() -> FakeBackends:
    return FakeBackends()


async def start_bridge(
    fake: FakeBackends, recorder: Recorder = "epgstation"
) -> tuple[Any, asyncio.Server, CtrlCmdServer]:
    config = Config(
        recorder=recorder,
        listen_host="127.0.0.1",
        listen_port=0,
        mirakc_url="http://mirakc",
        epgstation_url="http://epgstation",
        recorded_dir="/mnt/hdd1/recorded",
    )
    bridge = CtrlCmdServer(config)
    transport = httpx.MockTransport(fake.handle)
    bridge.http = bridge.epg.client = httpx.AsyncClient(transport=transport)
    bridge.backend.client = httpx.AsyncClient(transport=transport, base_url=bridge.backend.client.base_url)
    tcp = await asyncio.start_server(bridge.handle_connection, "127.0.0.1", 0)
    port = tcp.sockets[0].getsockname()[1]
    return CtrlCmdUtil(f"tcp://127.0.0.1:{port}/"), tcp, bridge


@pytest.fixture
async def edcb(fake: FakeBackends):
    client, tcp, bridge = await start_bridge(fake)
    yield client
    tcp.close()
    await bridge.aclose()


@pytest.fixture
async def edcb_mirakc(fake: FakeBackends):
    client, tcp, bridge = await start_bridge(fake, "mirakc")
    yield client
    tcp.close()
    await bridge.aclose()


def all_service_keys() -> list[int]:
    return [32736 << 32 | 32736 << 16 | 1024, 32391 << 32 | 32391 << 16 | 23608]


def konomitv_rec_setting(folder: str | None = None, template: str | None = None, *, enabled: bool = True) -> dict:
    """KonomiTV の EncodeEDCBRecSettingData() が生成するのと同じ形の RecSettingData"""
    rec_folder_list = []
    if folder is not None:
        rec_folder_list.append(
            {
                "rec_folder": folder,
                "write_plug_in": "Write_Default.dll",
                "rec_name_plug_in": "RecName_Macro.dll" + (f"?{template}" if template else ""),
            }
        )
    return {
        "rec_mode": 1 if enabled else 5,
        "priority": 2,
        "tuijyuu_flag": True,
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


# ---------------------------------------------------------------------------
# サービス・番組情報
# ---------------------------------------------------------------------------


async def test_chset5_and_services(edcb):
    chset5 = (await edcb.sendFileCopy("ChSet5.txt")).decode("utf-8-sig").splitlines()
    assert chset5[0].split("\t") == [
        "ＮＨＫ総合１・東京",
        "NHK総合・東京",
        "32736",
        "32736",
        "1024",
        "1",
        "0",
        "1",
        "1",
        "1",
    ]
    services = await edcb.sendEnumService()
    assert [(s["onid"], s["tsid"], s["sid"], s["remote_control_key_id"]) for s in services] == [
        (32736, 32736, 1024, 1),
        (32391, 32391, 23608, 9),
    ]


async def test_enum_pg_info_ex(edcb):
    # KonomiTV の Program.updateFromEDCB() と同じ引数 (全サービス・全期間)
    result = await edcb.sendEnumPgInfoEx([0xFFFFFFFFFFFF, 0xFFFFFFFFFFFF, 1, 0x7FFFFFFFFFFFFFFF])
    events = {e["eid"]: e for s in result for e in s["event_list"]}
    assert set(events) == {100, 199, 200}
    news = events[100]
    assert news["short_info"] == {"event_name": "ＮＨＫニュース７", "text_char": "今日のニュース"}
    assert news["ext_info"]["text_char"] == "- 出演者\n山田太郎\n"
    assert news["content_info"]["nibble_list"] == [{"content_nibble": 0x0000, "user_nibble": 0x0F0F}]
    assert news["duration_sec"] == 1800
    assert news["audio_info"]["component_list"][0]["sampling_rate"] == 7
    assert len(events[200]["event_group_info"]["event_data_list"]) == 2

    # KonomiTV の ReservationsRouter.GetServiceEventInfo() と同じ (サービス・時間範囲指定)
    start = dt.datetime.fromtimestamp((NOW_MS + 2 * HOUR_MS) / 1000, tz=JST)

    def filetime(dt: dt.datetime) -> int:
        return int((dt.timestamp() + 9 * 3600) * 10000000) + 116444736000000000

    result = await edcb.sendEnumPgInfoEx(
        [0, 32391 << 32 | 32391 << 16 | 23608, filetime(start), filetime(start + dt.timedelta(minutes=1))]
    )
    assert [e["eid"] for s in result for e in s["event_list"]] == [200]


async def test_search_pg(edcb):
    base = {"service_list": all_service_keys()}
    assert [e["eid"] for e in await edcb.sendSearchPg([{**base, "and_key": "ニュース"}])] == [100]
    # 全角半角を区別しない / 終了済み番組は返さない
    assert [e["eid"] for e in await edcb.sendSearchPg([{**base, "and_key": "アニメ テスト"}])] == [200]
    # 除外キーワード。メモ欄付き
    assert await edcb.sendSearchPg([{**base, "and_key": "アニメ", "not_key": ":note:memo あらすじ"}]) == []
    # 番組名のみ
    assert await edcb.sendSearchPg([{**base, "and_key": "山田", "title_only_flag": True}]) == []
    assert [e["eid"] for e in await edcb.sendSearchPg([{**base, "and_key": "山田"}])] == [100]
    # ジャンル (アニメ・特撮 すべて)
    assert [
        e["eid"]
        for e in await edcb.sendSearchPg([{**base, "content_list": [{"content_nibble": 0x07FF, "user_nibble": 0}]}])
    ] == [200]
    # 正規表現
    assert [e["eid"] for e in await edcb.sendSearchPg([{**base, "and_key": "＃[0-9１]", "reg_exp_flag": True}])] == [
        200
    ]
    # チャンネル範囲
    assert await edcb.sendSearchPg([{"service_list": [], "and_key": "ニュース"}]) == []


async def test_files_and_status(edcb):
    files = await edcb.sendFileCopy2(["EpgTimerSrv.ini", "Bitrate.ini", "LogoData.ini", "LogoData\\*.*"])
    assert [f["name"] for f in files] == ["EpgTimerSrv.ini", "Bitrate.ini", "LogoData.ini", "LogoData\\*.*"]
    ini = files[0]["data"].decode("utf-8-sig")
    assert "[REC_DEF_FOLDER]" in ini
    assert "0=/mnt/hdd1/recorded" in ini
    status = await edcb.sendGetNotifySrvStatus()
    assert status is not None
    assert status["notify_id"] == 100
    assert status["param1"] == 0
    assert await edcb.sendNwTVIDSetCh({"use_sid": 1, "onid": 1, "tsid": 1, "sid": 1}) is None


# ---------------------------------------------------------------------------
# EPGStation での録画予約
# ---------------------------------------------------------------------------


async def test_reserve_lifecycle(edcb, fake, monkeypatch):
    # 録画中一覧の短期キャッシュがあると、録画開始をすぐに検知できない
    monkeypatch.setattr(epgstation, "RECORDING_CACHE_TTL", 0)
    start = dt.datetime.fromtimestamp((NOW_MS + HOUR_MS) / 1000, tz=JST)
    # KonomiTV の AddReservationAPI() が組み立てるのと同じ ReserveData
    assert (
        await edcb.sendAddReserve(
            [
                {
                    "title": "ＮＨＫニュース７",
                    "start_time": start,
                    "start_time_epg": start,
                    "duration_second": 1800,
                    "station_name": "ＮＨＫ総合１・東京",
                    "onid": 32736,
                    "tsid": 32736,
                    "sid": 1024,
                    "eid": 100,
                    "comment": "",
                    "rec_setting": konomitv_rec_setting("/mnt/hdd1/recorded/news", "%TITLE%"),
                }
            ]
        )
        is True
    )
    posted = next(b for m, p, b in fake.requests if m == "POST" and p == "/api/reserves")
    assert posted["programId"] == 327360102400100
    assert posted["saveOption"] == {"parentDirectoryName": "recorded", "directory": "news", "recordedFormat": "%TITLE%"}

    reserves = await edcb.sendEnumReserve()
    assert len(reserves) == 1
    r = reserves[0]
    assert (r["onid"], r["tsid"], r["sid"], r["eid"], r["duration_second"]) == (32736, 32736, 1024, 100, 1800)
    assert r["start_time"] == start.replace(microsecond=0)
    assert r["station_name"] == "ＮＨＫ総合１・東京"
    assert r["rec_setting"]["rec_mode"] == 1
    assert r["rec_setting"]["rec_folder_list"][0]["rec_folder"] == "/mnt/hdd1/recorded/news"
    assert r["rec_setting"]["rec_folder_list"][0]["rec_name_plug_in"] == "RecName_Macro.dll?%TITLE%"

    # 保存先の変更 (KonomiTV の UpdateReservationAPI() は取得した ReserveData の rec_setting だけ差し替えて送る)
    r["rec_setting"] = konomitv_rec_setting("/mnt/hdd1/recorded/news2")
    assert await edcb.sendChgReserve([r]) is True
    assert fake.reserves[r["reserve_id"]]["directory"] == "news2"

    # 手動予約は無効化できない
    r["rec_setting"] = konomitv_rec_setting(enabled=False)
    assert await edcb.sendChgReserve([r]) is False

    # 録画中判定
    assert await edcb.sendGetRecFilePath(r["reserve_id"]) is None
    fake.recording = [
        {
            "id": 1,
            "programId": 327360102400100,
            "channelId": 3273601024,
            "startAt": NOW_MS + HOUR_MS,
            "endAt": NOW_MS + HOUR_MS + 1800000,
            "name": "x",
            "isRecording": True,
            "isEncoding": False,
            "isProtected": False,
            "videoFiles": [{"filename": "news/a.m2ts"}],
        }
    ]
    assert await edcb.sendGetRecFilePath(r["reserve_id"]) == "/mnt/hdd1/recorded/news/a.m2ts"
    status = await edcb.sendGetNotifySrvStatus()
    assert status["param1"] == 1

    assert await edcb.sendDelReserve([r["reserve_id"]]) is True
    assert await edcb.sendEnumReserve() == []


async def test_time_specified_reserve(edcb, fake):
    start = dt.datetime(2030, 1, 1, 12, 0, tzinfo=JST)
    assert (
        await edcb.sendAddReserve(
            [
                {
                    "title": "時刻指定",
                    "start_time": start,
                    "duration_second": 600,
                    "station_name": "x",
                    "onid": 32391,
                    "tsid": 32391,
                    "sid": 23608,
                    "eid": 0xFFFF,
                    "rec_setting": konomitv_rec_setting(),
                }
            ]
        )
        is True
    )
    posted = next(b for m, p, b in fake.requests if m == "POST" and p == "/api/reserves")
    assert posted["timeSpecifiedOption"] == {
        "name": "時刻指定",
        "channelId": 3239123608,
        "startAt": int(start.timestamp() * 1000),
        "endAt": int(start.timestamp() * 1000) + 600000,
    }
    r = (await edcb.sendEnumReserve())[0]
    assert r["eid"] == 0xFFFF
    assert r["start_time"] == start


async def test_rule_reserve_disable_enable(edcb, fake):
    fake.reserves[50] = {
        "id": 50,
        "ruleId": 1,
        "programId": 323912360800200,
        "channelId": 3239123608,
        "startAt": NOW_MS + 2 * HOUR_MS,
        "endAt": NOW_MS + 2 * HOUR_MS + 1800000,
        "name": "アニメ",
        "isSkip": False,
        "isConflict": True,
        "isOverlap": False,
        "allowEndLack": True,
        "isTimeSpecified": False,
        "isDeleteOriginalAfterEncode": False,
    }
    r = (await edcb.sendEnumReserve())[0]
    assert r["comment"] == "EPG自動予約"
    assert r["overlap_mode"] == 2
    r["rec_setting"] = konomitv_rec_setting(enabled=False)
    assert await edcb.sendChgReserve([r]) is True
    assert (await edcb.sendEnumReserve())[0]["rec_setting"]["rec_mode"] == 5
    r["rec_setting"] = konomitv_rec_setting(enabled=True)
    assert await edcb.sendChgReserve([r]) is True
    assert fake.reserves[50]["isSkip"] is False


# ---------------------------------------------------------------------------
# キーワード自動予約 (EPGStation ルール)
# ---------------------------------------------------------------------------


async def test_auto_add_lifecycle(edcb, fake):
    # KonomiTV の EncodeEDCBSearchKeyInfo() が生成するのと同じ形の SearchKeyInfo
    search_info = {
        "and_key": "アニメ",
        "not_key": ":note:メモ\\s1 再放送",
        "key_disabled": False,
        "case_sensitive": False,
        "reg_exp_flag": False,
        "title_only_flag": True,
        "content_list": [{"content_nibble": 0x07FF, "user_nibble": 0}, {"content_nibble": 0x0100, "user_nibble": 0}],
        "date_list": [
            {
                "start_day_of_week": 6,
                "start_hour": 23,
                "start_min": 0,
                "end_day_of_week": 0,
                "end_hour": 1,
                "end_min": 59,
            }
        ],
        "service_list": all_service_keys(),
        "video_list": [],
        "audio_list": [],
        "aimai_flag": False,
        "not_contet_flag": False,
        "not_date_flag": False,
        "free_ca_flag": 1,
        "chk_rec_end": True,
        "chk_rec_day": 14,
        "chk_rec_no_service": True,
        "chk_duration_min": 20,
        "chk_duration_max": 40,
    }
    assert (
        await edcb.sendAddAutoAdd(
            [{"search_info": search_info, "rec_setting": konomitv_rec_setting("/mnt/hdd1/recorded/anime")}]
        )
        is True
    )
    rule = next(b for m, p, b in fake.requests if m == "POST" and p == "/api/rules")
    so = rule["searchOption"]
    assert so["keyword"] == "アニメ"
    assert so["ignoreKeyword"] == "再放送"
    assert so["GR"] is True
    assert "channelIds" not in so
    assert so["genres"] == [{"genre": 7}, {"genre": 1, "subGenre": 0}]
    assert so["times"] == [{"start": 23, "range": 3, "week": 0x40}]
    assert (so["durationMin"], so["durationMax"], so["isFree"]) == (1200, 2400, True)
    assert (so["name"], so["description"], so["extended"]) == (True, False, False)
    assert rule["reserveOption"] == {
        "enable": True,
        "allowEndLack": True,
        "avoidDuplicate": True,
        "periodToAvoidDuplicate": 14,
    }
    assert rule["saveOption"]["directory"] == "anime"

    data = (await edcb.sendEnumAutoAdd())[0]
    si = data["search_info"]
    assert data["add_count"] == 3
    assert si["and_key"] == "アニメ"
    assert si["not_key"] == "再放送"
    assert sorted(si["service_list"]) == sorted(all_service_keys())
    assert si["content_list"] == search_info["content_list"]
    assert si["date_list"] == search_info["date_list"]
    assert (si["chk_duration_min"], si["chk_duration_max"], si["free_ca_flag"]) == (20, 40, 1)
    assert (si["chk_rec_end"], si["chk_rec_day"], si["title_only_flag"]) == (True, 14, True)
    assert data["rec_setting"]["rec_folder_list"][0]["rec_folder"] == "/mnt/hdd1/recorded/anime"

    # チャンネルを絞り込んで無効化
    si["service_list"] = [all_service_keys()[1]]
    si["key_disabled"] = True
    assert await edcb.sendChgAutoAdd([data]) is True
    updated = fake.rules[data["data_id"]]
    assert updated["searchOption"]["channelIds"] == [3239123608]
    assert updated["reserveOption"]["enable"] is False
    data = (await edcb.sendEnumAutoAdd())[0]
    assert data["search_info"]["key_disabled"] is True
    assert data["search_info"]["service_list"] == [all_service_keys()[1]]

    assert await edcb.sendDelAutoAdd([data["data_id"]]) is True
    assert await edcb.sendEnumAutoAdd() == []


# ---------------------------------------------------------------------------
# mirakc での録画予約
# ---------------------------------------------------------------------------


async def test_mirakc_reserve_lifecycle(edcb_mirakc, fake):
    start = dt.datetime.fromtimestamp((NOW_MS + 2 * HOUR_MS) / 1000, tz=JST)
    assert (
        await edcb_mirakc.sendAddReserve(
            [
                {
                    "title": "アニメ　テスト　＃１",
                    "start_time": start,
                    "duration_second": 1800,
                    "station_name": "x",
                    "onid": 32391,
                    "tsid": 32391,
                    "sid": 23608,
                    "eid": 200,
                    "rec_setting": konomitv_rec_setting("/mnt/hdd1/recorded/anime"),
                }
            ]
        )
        is True
    )
    posted = next(b for m, p, b in fake.requests if m == "POST" and p == "/api/recording/schedules")
    assert posted["programId"] == 323912360800200
    assert posted["options"]["contentPath"] == f"anime/{start:%Y%m%d%H%M}_23608_アニメ　テスト　＃１.m2ts"

    r = (await edcb_mirakc.sendEnumReserve())[0]
    assert (r["onid"], r["sid"], r["eid"]) == (32391, 23608, 200)
    assert 0 < r["reserve_id"] < 2**31
    assert r["rec_setting"]["rec_folder_list"][0]["rec_folder"] == "/mnt/hdd1/recorded/anime"

    assert await edcb_mirakc.sendGetRecFilePath(r["reserve_id"]) is None
    fake.schedules[0]["state"] = "recording"
    assert await edcb_mirakc.sendGetRecFilePath(r["reserve_id"]) == posted["options"]["contentPath"]

    # 時刻指定予約・自動予約は非対応
    assert await edcb_mirakc.sendAddReserve([{**r, "eid": 0xFFFF}]) is False
    assert await edcb_mirakc.sendEnumAutoAdd() == []
    assert await edcb_mirakc.sendAddAutoAdd([{"search_info": {"and_key": "x"}, "rec_setting": {}}]) is False

    assert await edcb_mirakc.sendDelReserve([r["reserve_id"]]) is True
    assert await edcb_mirakc.sendEnumReserve() == []
