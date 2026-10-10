"""
EpgTimerSrv 互換の CtrlCmd TCP サーバー

1 接続につき 1 コマンド (ヘッダー: cmd:int32, size:int32 + ペイロード) を受け取り、
(ret:int32, size:int32 + ペイロード) を返す。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from typing import TYPE_CHECKING

import httpx

from . import codec
from .backends import BackendError, RecorderBackend, create_backend
from .codec import CMD_ERR, CMD_NON_SUPPORT, CMD_SUCCESS, CMD_VER, JST, Reader, Writer
from .epg import EpgSource, build_chset5, enum_pg_info
from .search import search_programs

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .config import Config

log = logging.getLogger(__name__)

# EDCB/EpgTimer の CtrlCmdDef.cs より
CMD_EPG_SRV_RELOAD_EPG = 2
CMD_EPG_SRV_RELOAD_SETTING = 3
CMD_EPG_SRV_RELAY_VIEW_STREAM = 301
CMD_EPG_SRV_DEL_RESERVE = 1014
CMD_EPG_SRV_ENUM_TUNER_RESERVE = 1016
CMD_EPG_SRV_ENUM_SERVICE = 1021
CMD_EPG_SRV_SEARCH_PG = 1025
CMD_EPG_SRV_ENUM_PG_INFO_EX = 1029
CMD_EPG_SRV_ENUM_PG_ARC = 1030
CMD_EPG_SRV_DEL_AUTO_ADD = 1033
CMD_EPG_SRV_DEL_MANU_ADD = 1043
CMD_EPG_SRV_EPG_CAP_NOW = 1053
CMD_EPG_SRV_FILE_COPY = 1060
CMD_EPG_SRV_ENUM_PLUGIN = 1061
CMD_EPG_SRV_ENUM_TUNER_PROCESS = 1066
CMD_EPG_SRV_NWTV_ID_SET_CH = 1073
CMD_EPG_SRV_NWTV_ID_CLOSE = 1074
CMD_EPG_SRV_NWPLAY_CLOSE = 1081
CMD_EPG_SRV_NWPLAY_TF_OPEN = 1087
CMD_EPG_SRV_ENUM_RESERVE2 = 2011
CMD_EPG_SRV_GET_RESERVE2 = 2012
CMD_EPG_SRV_ADD_RESERVE2 = 2013
CMD_EPG_SRV_CHG_RESERVE2 = 2015
CMD_EPG_SRV_ENUM_RECINFO_BASIC2 = 2020
CMD_EPG_SRV_FILE_COPY2 = 2060
CMD_EPG_SRV_ENUM_AUTO_ADD2 = 2131
CMD_EPG_SRV_ADD_AUTO_ADD2 = 2132
CMD_EPG_SRV_CHG_AUTO_ADD2 = 2134
CMD_EPG_SRV_ENUM_MANU_ADD2 = 2141
CMD_EPG_SRV_GET_STATUS_NOTIFY2 = 2200

# NotifyUpdate.SRV_STATUS
NOTIFY_UPDATE_SRV_STATUS = 100

# 受け付けるリクエストの最大サイズ。破損したリクエストでメモリを食い潰さないようにする
MAX_REQUEST_SIZE = 16 * 1024 * 1024

Response = tuple[int, bytes]


def _write_nothing(_w: Writer, _v: object) -> None:
    """空配列の要素ライター (呼ばれることはない)"""


class CtrlCmdServer:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.http = httpx.AsyncClient(timeout=config.http_timeout)
        self.epg = EpgSource(config, self.http)
        self.backend: RecorderBackend = create_backend(config)
        self.handlers: dict[int, Callable[[Reader], Awaitable[Response]]] = {
            CMD_EPG_SRV_RELOAD_EPG: self._success,
            CMD_EPG_SRV_RELOAD_SETTING: self._success,
            CMD_EPG_SRV_EPG_CAP_NOW: self._success,
            CMD_EPG_SRV_NWPLAY_CLOSE: self._success,
            CMD_EPG_SRV_ENUM_SERVICE: self.enum_service,
            CMD_EPG_SRV_ENUM_PG_INFO_EX: self.enum_pg_info_ex,
            CMD_EPG_SRV_ENUM_PG_ARC: self.enum_pg_arc,
            CMD_EPG_SRV_SEARCH_PG: self.search_pg,
            CMD_EPG_SRV_FILE_COPY: self.file_copy,
            CMD_EPG_SRV_FILE_COPY2: self.file_copy2,
            CMD_EPG_SRV_ENUM_PLUGIN: self.enum_plugin,
            CMD_EPG_SRV_ENUM_TUNER_RESERVE: self._empty_vector,
            CMD_EPG_SRV_ENUM_TUNER_PROCESS: self._empty_vector,
            CMD_EPG_SRV_ENUM_RESERVE2: self.enum_reserve,
            CMD_EPG_SRV_GET_RESERVE2: self.get_reserve,
            CMD_EPG_SRV_ADD_RESERVE2: self.add_reserve,
            CMD_EPG_SRV_CHG_RESERVE2: self.chg_reserve,
            CMD_EPG_SRV_DEL_RESERVE: self.del_reserve,
            CMD_EPG_SRV_NWPLAY_TF_OPEN: self.nwplay_tf_open,
            CMD_EPG_SRV_ENUM_RECINFO_BASIC2: self._empty_vector2,
            CMD_EPG_SRV_ENUM_AUTO_ADD2: self.enum_auto_add,
            CMD_EPG_SRV_ADD_AUTO_ADD2: self.add_auto_add,
            CMD_EPG_SRV_CHG_AUTO_ADD2: self.chg_auto_add,
            CMD_EPG_SRV_DEL_AUTO_ADD: self.del_auto_add,
            CMD_EPG_SRV_ENUM_MANU_ADD2: self._empty_vector2,
            CMD_EPG_SRV_GET_STATUS_NOTIFY2: self.get_status_notify,
            CMD_EPG_SRV_NWTV_ID_SET_CH: self.nwtv_unsupported,
            CMD_EPG_SRV_NWTV_ID_CLOSE: self.nwtv_unsupported,
            CMD_EPG_SRV_RELAY_VIEW_STREAM: self.nwtv_unsupported,
        }

    async def aclose(self) -> None:
        await self.backend.aclose()
        await self.http.aclose()

    # ------------------------------------------------------------------
    # 接続処理
    # ------------------------------------------------------------------

    async def handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            while True:
                try:
                    header = await reader.readexactly(8)
                except asyncio.IncompleteReadError:
                    break
                cmd = int.from_bytes(header[0:4], "little", signed=True)
                size = int.from_bytes(header[4:8], "little", signed=True)
                if size < 0 or size > MAX_REQUEST_SIZE:
                    log.warning("Invalid request size from %s: cmd=%d size=%d", peer, cmd, size)
                    break
                payload = await reader.readexactly(size)
                ret, body = await self.dispatch(cmd, payload)
                writer.write(
                    ret.to_bytes(4, "little", signed=True) + len(body).to_bytes(4, "little", signed=True) + body
                )
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("Unexpected error while handling connection from %s", peer)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def dispatch(self, cmd: int, payload: bytes) -> Response:
        handler = self.handlers.get(cmd)
        if handler is None:
            log.warning("Unsupported command: %d", cmd)
            return CMD_NON_SUPPORT, b""
        try:
            ret, body = await handler(Reader(payload))
        except BackendError as ex:
            # バックエンドが要求を拒否した (KonomiTV には CMD_ERR として返り、KonomiTV 側でもエラーになる)
            log.warning("cmd=%d failed: %s", cmd, ex)
        except codec.ReadError:
            log.warning("cmd=%d: malformed request", cmd)
        except Exception:
            log.exception("cmd=%d: unexpected error", cmd)
        else:
            log.debug("cmd=%d ret=%d size=%d", cmd, ret, len(body))
            return ret, body
        return CMD_ERR, b""

    # ------------------------------------------------------------------
    # ヘルパー
    # ------------------------------------------------------------------

    @staticmethod
    def _read_cmd2_ver(r: Reader) -> int:
        """CMD2 系のリクエストは先頭にバージョン (ushort) を持つ"""
        return r.ushort()

    @staticmethod
    def _cmd2_response(write: Callable[[Writer], None] | None = None) -> Response:
        """CMD2 系のレスポンスは先頭にバージョン (ushort) を付ける"""
        w = Writer()
        w.ushort(CMD_VER)
        if write is not None:
            write(w)
        return CMD_SUCCESS, bytes(w.buf)

    async def _success(self, r: Reader) -> Response:
        return CMD_SUCCESS, b""

    async def _empty_vector(self, r: Reader) -> Response:
        w = Writer()
        w.vector(_write_nothing, [])
        return CMD_SUCCESS, bytes(w.buf)

    async def _empty_vector2(self, r: Reader) -> Response:
        return self._cmd2_response(lambda w: w.vector(_write_nothing, []))

    # ------------------------------------------------------------------
    # サービス・番組情報
    # ------------------------------------------------------------------

    async def enum_service(self, r: Reader) -> Response:
        snapshot = await self.epg.get()
        # EDCB と同様に、番組情報が 1 つ以上あるサービスのみ返す
        services = [s.to_service_info() for s in snapshot.services if snapshot.programs.get((s.onid, s.sid))]
        w = Writer()
        w.vector(codec.write_service_info, services)
        return CMD_SUCCESS, bytes(w.buf)

    async def enum_pg_info_ex(self, r: Reader) -> Response:
        key_list = r.vector(lambda rr: rr.long())
        snapshot = await self.epg.get()
        w = Writer()
        w.vector(codec.write_service_event_info, enum_pg_info(snapshot, key_list))
        return CMD_SUCCESS, bytes(w.buf)

    async def enum_pg_arc(self, r: Reader) -> Response:
        # 過去番組情報は保持していない
        r.vector(lambda rr: rr.long())
        w = Writer()
        w.vector(codec.write_service_event_info, [])
        return CMD_SUCCESS, bytes(w.buf)

    async def search_pg(self, r: Reader) -> Response:
        key_list = r.vector(codec.read_search_key_info)
        snapshot = await self.epg.get()
        w = Writer()
        w.vector(codec.write_event_info, search_programs(snapshot, key_list))
        return CMD_SUCCESS, bytes(w.buf)

    # ------------------------------------------------------------------
    # ファイル転送 (KonomiTV は ChSet5.txt / EpgTimerSrv.ini / Bitrate.ini / LogoData を要求する)
    # ------------------------------------------------------------------

    async def _get_file(self, name: str) -> bytes | None:
        normalized = name.replace("/", "\\").lower()
        if normalized == "chset5.txt":
            return build_chset5(await self.epg.get())
        if normalized == "epgtimersrv.ini":
            return self._build_epgtimersrv_ini()
        if normalized == "bitrate.ini":
            return "﻿[BITRATE]\r\n".encode()
        if normalized == "logodata.ini" or normalized.startswith("logodata\\"):
            # ロゴは提供しない (KonomiTV は同梱ロゴ / 既定ロゴにフォールバックする)
            return b""
        return None

    def _build_epgtimersrv_ini(self) -> bytes:
        """KonomiTV の録画設定プリセット API 向けに最小限の EpgTimerSrv.ini を生成する"""
        root = self.config.recorded_dir
        lines = [
            "[SET]",
            f"StartMargin={self.config.start_margin}",
            f"EndMargin={self.config.end_margin}",
            "Caption=1",
            "Data=0",
            "RecEndMode=0",
            "Reboot=0",
            "PresetID=",
            "[REC_DEF]",
            "SetName=デフォルト",
            "RecMode=1",
            "NoRecMode=1",
            "Priority=2",
            "TuijyuuFlag=0",
            "ServiceMode=0",
            "PittariFlag=0",
            "BatFilePath=",
            "SuspendMode=0",
            "RebootFlag=0",
            "UseMargineFlag=0",
            "ContinueRec=0",
            "PartialRec=0",
            "TunerID=0",
            "[REC_DEF_FOLDER]",
            "Count=1",
            f"0={root}",
            "WritePlugIn0=Write_Default.dll",
            "RecNamePlugIn0=RecName_Macro.dll",
            "[REC_DEF_FOLDER_1SEG]",
            "Count=0",
        ]
        return "﻿".encode() + ("\r\n".join(lines) + "\r\n").encode("utf-8")

    async def file_copy(self, r: Reader) -> Response:
        name = r.string()
        data = await self._get_file(name)
        if data is None:
            log.info("FileCopy: unknown file %s", name)
            return CMD_ERR, b""
        return CMD_SUCCESS, data

    async def file_copy2(self, r: Reader) -> Response:
        self._read_cmd2_ver(r)
        names = r.vector(lambda rr: rr.string())
        files = []
        for name in names:
            data = await self._get_file(name)
            # EDCB は存在しないファイルを空データとして返す
            files.append({"name": name, "data": data or b""})
        return self._cmd2_response(lambda w: w.vector(codec.write_file_data, files))

    async def enum_plugin(self, r: Reader) -> Response:
        index = r.ushort()
        w = Writer()
        w.vector(lambda ww, s: ww.string(s), ["RecName_Macro.dll"] if index == 1 else ["Write_Default.dll"])
        return CMD_SUCCESS, bytes(w.buf)

    # ------------------------------------------------------------------
    # 録画予約
    # ------------------------------------------------------------------

    async def enum_reserve(self, r: Reader) -> Response:
        snapshot = await self.epg.get()
        reserves = await self.backend.enum_reserve(snapshot)
        return self._cmd2_response(lambda w: w.vector(codec.write_reserve_data, reserves))

    async def get_reserve(self, r: Reader) -> Response:
        self._read_cmd2_ver(r)
        reserve_id = r.int32()
        snapshot = await self.epg.get()
        for reserve in await self.backend.enum_reserve(snapshot):
            if reserve["reserve_id"] == reserve_id:
                return self._cmd2_response(lambda w, reserve=reserve: codec.write_reserve_data(w, reserve))
        return CMD_ERR, b""

    async def add_reserve(self, r: Reader) -> Response:
        self._read_cmd2_ver(r)
        reserve_list = r.vector(codec.read_reserve_data)
        await self.backend.add_reserve(await self.epg.get(), reserve_list)
        return self._cmd2_response()

    async def chg_reserve(self, r: Reader) -> Response:
        self._read_cmd2_ver(r)
        reserve_list = r.vector(codec.read_reserve_data)
        await self.backend.chg_reserve(await self.epg.get(), reserve_list)
        return self._cmd2_response()

    async def del_reserve(self, r: Reader) -> Response:
        reserve_ids = r.vector(lambda rr: rr.int32())
        await self.backend.del_reserve(reserve_ids)
        return CMD_SUCCESS, b""

    async def nwplay_tf_open(self, r: Reader) -> Response:
        # 「録画中かつ視聴予約でない予約の録画ファイルパス」を返す (KonomiTV は録画中判定に使う)
        reserve_id = r.int32()
        path = await self.backend.get_recording_file_path(await self.epg.get(), reserve_id)
        if path is None:
            return CMD_ERR, b""
        w = Writer()
        codec.write_nwplay_time_shift_info(w, {"ctrl_id": 1, "file_path": path})
        return CMD_SUCCESS, bytes(w.buf)

    # ------------------------------------------------------------------
    # キーワード自動予約
    # ------------------------------------------------------------------

    async def enum_auto_add(self, r: Reader) -> Response:
        data_list = await self.backend.enum_auto_add(await self.epg.get())
        return self._cmd2_response(lambda w: w.vector(codec.write_auto_add_data, data_list))

    async def add_auto_add(self, r: Reader) -> Response:
        self._read_cmd2_ver(r)
        data_list = r.vector(codec.read_auto_add_data)
        await self.backend.add_auto_add(await self.epg.get(), data_list)
        return self._cmd2_response()

    async def chg_auto_add(self, r: Reader) -> Response:
        self._read_cmd2_ver(r)
        data_list = r.vector(codec.read_auto_add_data)
        await self.backend.chg_auto_add(await self.epg.get(), data_list)
        return self._cmd2_response()

    async def del_auto_add(self, r: Reader) -> Response:
        ids = r.vector(lambda rr: rr.int32())
        await self.backend.del_auto_add(ids)
        return CMD_SUCCESS, b""

    # ------------------------------------------------------------------
    # ステータス
    # ------------------------------------------------------------------

    async def get_status_notify(self, r: Reader) -> Response:
        # target_count によるロングポーリングには対応せず、常に現在の SRV_STATUS を即座に返す
        self._read_cmd2_ver(r)
        r.uint32()
        try:
            status = 1 if await self.backend.is_recording() else 0
        except BackendError as ex:
            log.warning("Failed to get recording status: %s", ex)
            status = 0
        info = {
            "notify_id": NOTIFY_UPDATE_SRV_STATUS,
            "time": dt.datetime.now(JST),
            "param1": status,  # 0: 通常 / 1: 録画中 / 2: EPG 取得中
            "count": 1,
        }
        return self._cmd2_response(lambda w: codec.write_notify_srv_info(w, info))

    async def nwtv_unsupported(self, r: Reader) -> Response:
        log.warning(
            "EDCB live streaming (NetworkTV) is not supported. "
            'Set "always_receive_tv_from_mirakurun: true" in KonomiTV config.yaml.'
        )
        return CMD_NON_SUPPORT, b""


async def serve(config: Config) -> None:
    server = CtrlCmdServer(config)
    tcp = await asyncio.start_server(server.handle_connection, config.listen_host, config.listen_port)
    addrs = ", ".join(str(s.getsockname()) for s in tcp.sockets)
    log.info(
        "EDCB CtrlCmd bridge listening on %s (recorder: %s, mirakc: %s)", addrs, config.recorder, config.mirakc_url
    )
    try:
        async with tcp:
            await tcp.serve_forever()
    finally:
        await server.aclose()
