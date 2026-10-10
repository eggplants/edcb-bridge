"""
EDCB CtrlCmd バイナリプロトコルのエンコーダー / デコーダー (サーバー側)

KonomiTV の app/utils/edcb/CtrlCmdUtil.py (= xtne6f/edcb.py) はクライアント側の実装なので、
ここではその逆方向 (リクエストの読み取り・レスポンスの書き込み) を実装している。
辞書のキー名は KonomiTV の TypedDict (app/utils/edcb/__init__.py) と同一にしている。
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")

# EDCB の日付は OS のタイムゾーンに関わらず常に UTC+9
JST = dt.timezone(dt.timedelta(hours=9))
UNIX_EPOCH = dt.datetime(1970, 1, 1, 9, tzinfo=JST)

# EDCB/EpgTimer の CtrlCmdDef.cs より
CMD_SUCCESS = 1
CMD_ERR = 0
CMD_NON_SUPPORT = 3
CMD_VER = 5

# 中身が空のときの各データのサイズ。先頭のサイズフィールド自身も含む
EMPTY_STRUCT_SIZE = 4  # 構造体はサイズの int だけ
EMPTY_STRING_SIZE = 6  # 文字列はサイズの int と終端 NUL の ushort
EMPTY_VECTOR_SIZE = 8  # 配列はサイズと要素数の int 2 つ

# SearchKeyInfo.and_key 先頭の番組長指定 "D!{1MMMMNNNN}" の長さ
DURATION_PREFIX_LENGTH = 13
# SearchKeyInfo.chk_rec_day に加算して「全サービスで重複チェック」を表すオフセット
CHK_REC_NO_SERVICE_OFFSET = 40000
# SearchKeyInfo 末尾の chk_rec_end (byte) + chk_rec_day (ushort) のサイズ
CHK_REC_FIELDS_SIZE = 3


class ReadError(Exception):
    """バッファをデータ構造として読み取るのに失敗した"""


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class Writer:
    """レスポンスのペイロードを組み立てる"""

    def __init__(self) -> None:
        self.buf = bytearray()

    def byte(self, v: int) -> None:
        self.buf.extend((int(v) & 0xFF).to_bytes(1, "little"))

    def ushort(self, v: int) -> None:
        self.buf.extend((int(v) & 0xFFFF).to_bytes(2, "little"))

    def int32(self, v: int) -> None:
        self.buf.extend(int(v).to_bytes(4, "little", signed=True))

    def uint32(self, v: int) -> None:
        self.buf.extend((int(v) & 0xFFFFFFFF).to_bytes(4, "little"))

    def long(self, v: int) -> None:
        self.buf.extend(int(v).to_bytes(8, "little", signed=True))

    def flag(self, v: object) -> None:
        self.byte(1 if v else 0)

    def systemtime(self, v: dt.datetime) -> None:
        v = v.astimezone(JST)
        self.ushort(v.year)
        self.ushort(v.month)
        self.ushort(v.isoweekday() % 7)
        self.ushort(v.day)
        self.ushort(v.hour)
        self.ushort(v.minute)
        self.ushort(v.second)
        self.ushort(v.microsecond // 1000)

    def string(self, v: str) -> None:
        vv = v.encode("utf_16_le")
        self.int32(6 + len(vv))
        self.buf.extend(vv)
        self.ushort(0)

    def vector(self, write_func: Callable[[Writer, T], None], v: list[T]) -> None:
        pos = len(self.buf)
        self.int32(0)
        self.int32(len(v))
        for e in v:
            write_func(self, e)
        self._fix_size(pos)

    def struct(self, body: Callable[[], object]) -> None:
        """先頭にサイズ (自身を含む) を持つ構造体を書き込む"""
        pos = len(self.buf)
        self.int32(0)
        body()
        self._fix_size(pos)

    def _fix_size(self, pos: int) -> None:
        self.buf[pos : pos + 4] = (len(self.buf) - pos).to_bytes(4, "little", signed=True)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class Reader:
    """リクエストのペイロードを読み取る"""

    def __init__(self, buf: bytes) -> None:
        self.buf = memoryview(buf)
        self.pos = 0
        self.size = len(buf)

    def remaining(self) -> int:
        return self.size - self.pos

    def _need(self, n: int) -> None:
        if self.size - self.pos < n:
            raise ReadError

    def byte(self) -> int:
        self._need(1)
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def ushort(self) -> int:
        self._need(2)
        v = int.from_bytes(self.buf[self.pos : self.pos + 2], "little")
        self.pos += 2
        return v

    def int32(self) -> int:
        self._need(4)
        v = int.from_bytes(self.buf[self.pos : self.pos + 4], "little", signed=True)
        self.pos += 4
        return v

    def uint32(self) -> int:
        self._need(4)
        v = int.from_bytes(self.buf[self.pos : self.pos + 4], "little")
        self.pos += 4
        return v

    def long(self) -> int:
        self._need(8)
        v = int.from_bytes(self.buf[self.pos : self.pos + 8], "little", signed=True)
        self.pos += 8
        return v

    def systemtime(self) -> dt.datetime:
        self._need(16)
        b = self.buf
        p = self.pos
        try:
            v = dt.datetime(
                b[p] | b[p + 1] << 8,
                b[p + 2] | b[p + 3] << 8,
                b[p + 6] | b[p + 7] << 8,
                b[p + 8] | b[p + 9] << 8,
                b[p + 10] | b[p + 11] << 8,
                b[p + 12] | b[p + 13] << 8,
                tzinfo=JST,
            )
        except ValueError:
            v = UNIX_EPOCH
        self.pos += 16
        return v

    def string(self) -> str:
        vs = self.int32()
        if vs < EMPTY_STRING_SIZE or self.size - self.pos < vs - 4:
            raise ReadError
        v = str(self.buf[self.pos : self.pos + vs - 6], "utf_16_le")
        self.pos += vs - 4
        return v

    def vector(self, read_func: Callable[[Reader], T]) -> list[T]:
        vs = self.int32()
        vc = self.int32()
        if vs < EMPTY_VECTOR_SIZE or vc < 0 or self.size - self.pos < vs - 8:
            raise ReadError
        end = self.pos + vs - 8
        saved_size = self.size
        self.size = end
        try:
            v = [read_func(self) for _ in range(vc)]
        finally:
            self.size = saved_size
        self.pos = end
        return v

    def struct(self, body: Callable[[], T]) -> T:
        """先頭にサイズを持つ構造体を読み取る (未知の末尾フィールドは読み飛ばす)"""
        vs = self.int32()
        if vs < EMPTY_STRUCT_SIZE or self.size - self.pos < vs - 4:
            raise ReadError
        end = self.pos + vs - 4
        saved_size = self.size
        self.size = end
        try:
            v = body()
        finally:
            self.size = saved_size
        self.pos = end
        return v


# ---------------------------------------------------------------------------
# 構造体の読み書き
# ---------------------------------------------------------------------------


def write_service_info(w: Writer, v: dict) -> None:
    def body() -> None:
        w.ushort(v["onid"])
        w.ushort(v["tsid"])
        w.ushort(v["sid"])
        w.byte(v["service_type"])
        w.byte(v.get("partial_reception_flag", 0))
        w.string(v.get("service_provider_name", ""))
        w.string(v.get("service_name", ""))
        w.string(v.get("network_name", ""))
        w.string(v.get("ts_name", ""))
        w.byte(v.get("remote_control_key_id", 0))

    w.struct(body)


def _write_content_data(w: Writer, v: dict) -> None:
    def body() -> None:
        cn = v.get("content_nibble", 0)
        un = v.get("user_nibble", 0)
        # EDCB は上位・下位バイトを入れ替えた形でやり取りする
        w.ushort((cn >> 8 | cn << 8) & 0xFFFF)
        w.ushort((un >> 8 | un << 8) & 0xFFFF)

    w.struct(body)


def _read_content_data(r: Reader) -> dict:
    def body() -> dict:
        cn = r.ushort()
        un = r.ushort()
        return {
            "content_nibble": (cn >> 8 | cn << 8) & 0xFFFF,
            "user_nibble": (un >> 8 | un << 8) & 0xFFFF,
        }

    return r.struct(body)


def _write_event_data(w: Writer, v: dict) -> None:
    def body() -> None:
        w.ushort(v["onid"])
        w.ushort(v["tsid"])
        w.ushort(v["sid"])
        w.ushort(v["eid"])

    w.struct(body)


def _write_event_group_info(w: Writer, v: dict) -> None:
    def body() -> None:
        w.byte(v["group_type"])
        w.vector(_write_event_data, v["event_data_list"])

    w.struct(body)


def _write_audio_component_info_data(w: Writer, v: dict) -> None:
    def body() -> None:
        for key in (
            "stream_content",
            "component_type",
            "component_tag",
            "stream_type",
            "simulcast_group_tag",
            "es_multi_lingual_flag",
            "main_component_flag",
            "quality_indicator",
            "sampling_rate",
        ):
            w.byte(v.get(key, 0))
        w.string(v.get("text_char", ""))

    w.struct(body)


def write_event_info(w: Writer, v: dict) -> None:
    def body() -> None:
        w.ushort(v["onid"])
        w.ushort(v["tsid"])
        w.ushort(v["sid"])
        w.ushort(v["eid"])
        w.flag("start_time" in v)
        w.systemtime(v.get("start_time", UNIX_EPOCH))
        w.flag("duration_sec" in v)
        w.int32(v.get("duration_sec", 0))

        # 省略可能な各構造体は、存在しない場合サイズ 4 の空構造体として書き込む
        def optional(key: str, write: Callable[[], None]) -> None:
            if key in v:
                write()
            else:
                w.int32(EMPTY_STRUCT_SIZE)

        optional(
            "short_info",
            lambda: w.struct(lambda: (w.string(v["short_info"]["event_name"]), w.string(v["short_info"]["text_char"]))),
        )
        optional("ext_info", lambda: w.struct(lambda: w.string(v["ext_info"]["text_char"])))
        optional(
            "content_info", lambda: w.struct(lambda: w.vector(_write_content_data, v["content_info"]["nibble_list"]))
        )
        optional(
            "component_info",
            lambda: w.struct(
                lambda: (
                    w.byte(v["component_info"]["stream_content"]),
                    w.byte(v["component_info"]["component_type"]),
                    w.byte(v["component_info"]["component_tag"]),
                    w.string(v["component_info"]["text_char"]),
                )
            ),
        )
        optional(
            "audio_info",
            lambda: w.struct(lambda: w.vector(_write_audio_component_info_data, v["audio_info"]["component_list"])),
        )
        optional("event_group_info", lambda: _write_event_group_info(w, v["event_group_info"]))
        optional("event_relay_info", lambda: _write_event_group_info(w, v["event_relay_info"]))
        w.byte(v.get("free_ca_flag", 0))

    w.struct(body)


def write_service_event_info(w: Writer, v: dict) -> None:
    def body() -> None:
        write_service_info(w, v["service_info"])
        w.vector(write_event_info, v["event_list"])

    w.struct(body)


def _write_rec_file_set_info(w: Writer, v: dict) -> None:
    def body() -> None:
        w.string(v.get("rec_folder", ""))
        w.string(v.get("write_plug_in", ""))
        w.string(v.get("rec_name_plug_in", ""))
        w.string("")

    w.struct(body)


def _read_rec_file_set_info(r: Reader) -> dict:
    def body() -> dict:
        v = {
            "rec_folder": r.string(),
            "write_plug_in": r.string(),
            "rec_name_plug_in": r.string(),
        }
        r.string()
        return v

    return r.struct(body)


def write_rec_setting_data(w: Writer, v: dict) -> None:
    def body() -> None:
        w.byte(v.get("rec_mode", 1))
        w.byte(v.get("priority", 2))
        w.flag(v.get("tuijyuu_flag", False))
        w.uint32(v.get("service_mode", 0))
        w.flag(v.get("pittari_flag", False))
        w.string(v.get("bat_file_path", ""))
        w.vector(_write_rec_file_set_info, v.get("rec_folder_list", []))
        w.byte(v.get("suspend_mode", 0))
        w.flag(v.get("reboot_flag", False))
        w.flag("start_margin" in v and "end_margin" in v)
        w.int32(v.get("start_margin", 0))
        w.int32(v.get("end_margin", 0))
        w.flag(v.get("continue_rec_flag", False))
        w.byte(v.get("partial_rec_flag", 0))
        w.uint32(v.get("tuner_id", 0))
        w.vector(_write_rec_file_set_info, v.get("partial_rec_folder", []))

    w.struct(body)


def read_rec_setting_data(r: Reader) -> dict:
    def body() -> dict:
        v: dict = {
            "rec_mode": r.byte(),
            "priority": r.byte(),
            "tuijyuu_flag": r.byte() != 0,
            "service_mode": r.uint32(),
            "pittari_flag": r.byte() != 0,
            "bat_file_path": r.string(),
            "rec_folder_list": r.vector(_read_rec_file_set_info),
            "suspend_mode": r.byte(),
            "reboot_flag": r.byte() != 0,
        }
        use_margin_flag = r.byte() != 0
        start_margin = r.int32()
        end_margin = r.int32()
        if use_margin_flag:
            v["start_margin"] = start_margin
            v["end_margin"] = end_margin
        v["continue_rec_flag"] = r.byte() != 0
        v["partial_rec_flag"] = r.byte()
        v["tuner_id"] = r.uint32()
        v["partial_rec_folder"] = r.vector(_read_rec_file_set_info)
        return v

    return r.struct(body)


def write_reserve_data(w: Writer, v: dict) -> None:
    def body() -> None:
        w.string(v.get("title", ""))
        w.systemtime(v.get("start_time", UNIX_EPOCH))
        w.uint32(v.get("duration_second", 0))
        w.string(v.get("station_name", ""))
        w.ushort(v.get("onid", 0))
        w.ushort(v.get("tsid", 0))
        w.ushort(v.get("sid", 0))
        w.ushort(v.get("eid", 0))
        w.string(v.get("comment", ""))
        w.int32(v.get("reserve_id", 0))
        w.byte(0)  # recWaitFlag (未使用)
        w.byte(v.get("overlap_mode", 0))
        w.string("")  # recFilePath (未使用)
        w.systemtime(v.get("start_time_epg", v.get("start_time", UNIX_EPOCH)))
        write_rec_setting_data(w, v.get("rec_setting", {}))
        w.int32(0)  # reserveStatus
        w.vector(lambda ww, s: ww.string(s), v.get("rec_file_name_list", []))
        w.int32(0)  # param1

    w.struct(body)


def read_reserve_data(r: Reader) -> dict:
    def body() -> dict:
        v: dict = {
            "title": r.string(),
            "start_time": r.systemtime(),
            "duration_second": r.uint32(),
            "station_name": r.string(),
            "onid": r.ushort(),
            "tsid": r.ushort(),
            "sid": r.ushort(),
            "eid": r.ushort(),
            "comment": r.string(),
            "reserve_id": r.int32(),
        }
        r.byte()
        v["overlap_mode"] = r.byte()
        r.string()
        v["start_time_epg"] = r.systemtime()
        v["rec_setting"] = read_rec_setting_data(r)
        r.int32()
        v["rec_file_name_list"] = r.vector(lambda rr: rr.string())
        return v

    return r.struct(body)


def _write_search_date_info(w: Writer, v: dict) -> None:
    def body() -> None:
        w.byte(v["start_day_of_week"])
        w.ushort(v["start_hour"])
        w.ushort(v["start_min"])
        w.byte(v["end_day_of_week"])
        w.ushort(v["end_hour"])
        w.ushort(v["end_min"])

    w.struct(body)


def _read_search_date_info(r: Reader) -> dict:
    def body() -> dict:
        return {
            "start_day_of_week": r.byte(),
            "start_hour": r.ushort(),
            "start_min": r.ushort(),
            "end_day_of_week": r.byte(),
            "end_hour": r.ushort(),
            "end_min": r.ushort(),
        }

    return r.struct(body)


def write_search_key_info(w: Writer, v: dict) -> None:
    """SearchKeyInfo を書き込む (AutoAdd2 で使う chk_rec_end / chk_rec_day 付きの形式)"""

    def body() -> None:
        chk_duration = int(v.get("chk_duration_min", 0) * 10000 + v.get("chk_duration_max", 0)) % 100000000
        w.string(
            ("^!{999}" if v.get("key_disabled", False) else "")
            + ("C!{999}" if v.get("case_sensitive", False) else "")
            + (f"D!{{1{chk_duration:08d}}}" if chk_duration > 0 else "")
            + v.get("and_key", "")
        )
        w.string(v.get("not_key", ""))
        w.int32(1 if v.get("reg_exp_flag", False) else 0)
        w.int32(1 if v.get("title_only_flag", False) else 0)
        w.vector(_write_content_data, v.get("content_list", []))
        w.vector(_write_search_date_info, v.get("date_list", []))
        w.vector(lambda ww, x: ww.long(x), v.get("service_list", []))
        w.vector(lambda ww, x: ww.ushort(x), v.get("video_list", []))
        w.vector(lambda ww, x: ww.ushort(x), v.get("audio_list", []))
        w.flag(v.get("aimai_flag", False))
        w.flag(v.get("not_contet_flag", False))
        w.flag(v.get("not_date_flag", False))
        w.byte(v.get("free_ca_flag", 0))
        w.flag(v.get("chk_rec_end", False))
        chk_rec_day = v.get("chk_rec_day", 0)
        w.ushort(chk_rec_day % 10000 + CHK_REC_NO_SERVICE_OFFSET if v.get("chk_rec_no_service", False) else chk_rec_day)

    w.struct(body)


def read_search_key_info(r: Reader) -> dict:
    """SearchKeyInfo を読み取る (SearchPg では chk_rec_end / chk_rec_day が省略される)"""

    def body() -> dict:
        and_key = r.string()
        key_disabled = and_key.startswith("^!{999}")
        and_key = and_key.removeprefix("^!{999}")
        case_sensitive = and_key.startswith("C!{999}")
        and_key = and_key.removeprefix("C!{999}")
        chk_duration_min = 0
        chk_duration_max = 0
        if (
            len(and_key) >= DURATION_PREFIX_LENGTH
            and and_key.startswith("D!{1")
            and and_key[DURATION_PREFIX_LENGTH - 1] == "}"
            and and_key[4 : DURATION_PREFIX_LENGTH - 1].isdigit()
        ):
            chk_duration_max = int(and_key[3:12])
            and_key = and_key[DURATION_PREFIX_LENGTH:]
            chk_duration_min = chk_duration_max // 10000 % 10000
            chk_duration_max = chk_duration_max % 10000
        v: dict = {
            "and_key": and_key,
            "not_key": r.string(),
            "key_disabled": key_disabled,
            "case_sensitive": case_sensitive,
            "reg_exp_flag": r.int32() != 0,
            "title_only_flag": r.int32() != 0,
            "content_list": r.vector(_read_content_data),
            "date_list": r.vector(_read_search_date_info),
            "service_list": r.vector(lambda rr: rr.long()),
            "video_list": r.vector(lambda rr: rr.ushort()),
            "audio_list": r.vector(lambda rr: rr.ushort()),
            "aimai_flag": r.byte() != 0,
            "not_contet_flag": r.byte() != 0,
            "not_date_flag": r.byte() != 0,
            "free_ca_flag": r.byte(),
            "chk_rec_end": False,
            "chk_rec_day": 0,
            "chk_rec_no_service": False,
            "chk_duration_min": chk_duration_min,
            "chk_duration_max": chk_duration_max,
        }
        if r.remaining() >= CHK_REC_FIELDS_SIZE:
            v["chk_rec_end"] = r.byte() != 0
            chk_rec_day = r.ushort()
            v["chk_rec_day"] = chk_rec_day % 10000 if chk_rec_day >= CHK_REC_NO_SERVICE_OFFSET else chk_rec_day
            v["chk_rec_no_service"] = chk_rec_day >= CHK_REC_NO_SERVICE_OFFSET
        return v

    return r.struct(body)


def write_auto_add_data(w: Writer, v: dict) -> None:
    def body() -> None:
        w.int32(v["data_id"])
        write_search_key_info(w, v["search_info"])
        write_rec_setting_data(w, v["rec_setting"])
        w.int32(v.get("add_count", 0))

    w.struct(body)


def read_auto_add_data(r: Reader) -> dict:
    def body() -> dict:
        return {
            "data_id": r.int32(),
            "search_info": read_search_key_info(r),
            "rec_setting": read_rec_setting_data(r),
            "add_count": r.int32(),
        }

    return r.struct(body)


def write_file_data(w: Writer, v: dict) -> None:
    def body() -> None:
        w.string(v["name"])
        w.int32(len(v["data"]))
        w.int32(0)
        w.buf.extend(v["data"])

    w.struct(body)


def write_notify_srv_info(w: Writer, v: dict) -> None:
    def body() -> None:
        w.uint32(v["notify_id"])
        w.systemtime(v["time"])
        w.uint32(v.get("param1", 0))
        w.uint32(v.get("param2", 0))
        w.uint32(v.get("count", 0))
        w.string(v.get("param4", ""))
        w.string(v.get("param5", ""))
        w.string(v.get("param6", ""))

    w.struct(body)


def write_nwplay_time_shift_info(w: Writer, v: dict) -> None:
    def body() -> None:
        w.int32(v["ctrl_id"])
        w.string(v["file_path"])

    w.struct(body)


def read_set_ch_info(r: Reader) -> dict:
    def body() -> dict:
        return {
            "use_sid": r.int32(),
            "onid": r.ushort(),
            "tsid": r.ushort(),
            "sid": r.ushort(),
            "use_bon_ch": r.int32(),
            "space_or_id": r.int32(),
            "ch_or_mode": r.int32(),
        }

    return r.struct(body)
