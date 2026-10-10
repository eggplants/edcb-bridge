"""
CMD_EPG_SRV_SEARCH_PG (番組検索) の実装

EpgTimerSrv の検索仕様のうち、KonomiTV の番組検索 UI から指定されうる条件を mirakc の番組情報に対して評価する。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import unicodedata
from typing import Any

from edcb_bridge.codec import JST
from edcb_bridge.epg import CONTENT_NIBBLE_ALL_MIDDLE, EpgSnapshot, program_to_event_info, to_jst

log = logging.getLogger(__name__)

# SearchKeyInfo.free_ca_flag の値
FREE_CA_FREE_ONLY = 1
FREE_CA_PAID_ONLY = 2


def normalize(text: str, *, case_sensitive: bool) -> str:
    """全角英数・記号を半角に寄せて比較しやすくする (EpgTimerSrv も全角半角を区別しない)"""
    text = unicodedata.normalize("NFKC", text)
    return text if case_sensitive else text.lower()


def strip_note(not_key: str) -> str:
    """除外キーワード先頭の EpgTimer メモ欄 (:note:...) を取り除く"""
    return re.sub(r"^:note:[^ 　]*[ 　]?", "", not_key)


def _match_keywords(key: str, target: str, *, regex: bool, case_sensitive: bool, any_match: bool) -> bool:
    """キーワードが番組テキストにマッチするか

    Args:
        key: 空白区切りのキーワード (regex なら正規表現)
        target: 検索対象の番組テキスト
        regex: key を正規表現として扱うか
        case_sensitive: 大文字小文字を区別するか
        any_match: True ならいずれかの語にマッチすれば True (除外キーワード用)、False なら全語マッチで True

    Returns:
        マッチしたかどうか
    """
    if regex:
        try:
            return re.search(key, target, 0 if case_sensitive else re.IGNORECASE) is not None
        except re.error:
            log.warning("Invalid regex in search key: %s", key)
            return False
    words = [normalize(w, case_sensitive=case_sensitive) for w in re.split(r"[ 　]+", key) if w != ""]
    if not words:
        return not any_match
    t = normalize(target, case_sensitive=case_sensitive)

    # EpgTimerSrv と同様に "|" で区切られた語は OR 条件として扱う
    def word_hit(w: str) -> bool:
        return any(x != "" and x in t for x in w.split("|"))

    return any(word_hit(w) for w in words) if any_match else all(word_hit(w) for w in words)


def _match_date(start: dt.datetime, date_list: list[dict]) -> bool:
    """番組開始時刻が曜日・時刻範囲のいずれかに含まれるか (単位: 週の中の分)"""
    minute_of_week = (start.isoweekday() % 7) * 1440 + start.hour * 60 + start.minute
    for d in date_list:
        s = d["start_day_of_week"] * 1440 + d["start_hour"] * 60 + d["start_min"]
        e = d["end_day_of_week"] * 1440 + d["end_hour"] * 60 + d["end_min"]
        if s <= e:
            if s <= minute_of_week <= e:
                return True
        elif minute_of_week >= s or minute_of_week <= e:  # 週をまたぐ範囲
            return True
    return False


def _match_content(p: dict[str, Any], content_list: list[dict]) -> bool:
    for g in p.get("genres") or []:
        lv1 = g.get("lv1", 0xF) & 0xF
        lv2 = g.get("lv2", 0xF) & 0xF
        for c in content_list:
            c1 = c["content_nibble"] >> 8
            c2 = c["content_nibble"] & 0xFF
            if c1 == lv1 and c2 in {CONTENT_NIBBLE_ALL_MIDDLE, lv2}:
                return True
    return False


def _match_attributes(p: dict[str, Any], start: dt.datetime, key: dict) -> bool:
    """放送種別・番組長・ジャンル・曜日時刻の条件にマッチするか"""
    free_ca_flag = key.get("free_ca_flag", 0)
    is_free = p.get("isFree", True)
    if (free_ca_flag == FREE_CA_FREE_ONLY and not is_free) or (free_ca_flag == FREE_CA_PAID_ONLY and is_free):
        return False

    # 番組長は分単位で比較する
    duration_min = (p.get("duration") or 0) // 60000
    if key.get("chk_duration_min", 0) > 0 and duration_min < key["chk_duration_min"]:
        return False
    if 0 < key.get("chk_duration_max", 0) < duration_min:
        return False

    content_list = key.get("content_list", [])
    if content_list and _match_content(p, content_list) == key.get("not_contet_flag", False):
        return False
    date_list = key.get("date_list", [])
    return not (date_list and _match_date(start, date_list) == key.get("not_date_flag", False))


def _match_text(p: dict[str, Any], key: dict) -> bool:
    """キーワード・除外キーワードの条件にマッチするか"""
    title = p.get("name") or ""
    if key.get("title_only_flag", False):
        target = title
    else:
        extended = p.get("extended") or {}
        target = "\n".join([title, p.get("description") or "", *extended.keys(), *extended.values()])
    options = {"regex": key.get("reg_exp_flag", False), "case_sensitive": key.get("case_sensitive", False)}
    and_key = key.get("and_key", "")
    if and_key != "" and not _match_keywords(and_key, target, **options, any_match=False):
        return False
    not_key = strip_note(key.get("not_key", ""))
    return not (not_key != "" and _match_keywords(not_key, target, **options, any_match=True))


def program_matches(p: dict[str, Any], service_key_value: int, key: dict, now: dt.datetime) -> bool:
    """番組が検索条件 (SearchKeyInfo) にマッチするか

    Returns:
        マッチしたかどうか。終了済みの番組と開始時刻未定の番組は常にマッチしない
    """
    if p.get("startAt") is None or service_key_value not in key.get("service_list", []):
        return False
    start = to_jst(p["startAt"])
    duration_sec = (p.get("duration") or 0) // 1000
    if start + dt.timedelta(seconds=max(duration_sec, 1)) <= now:
        return False
    return _match_attributes(p, start, key) and _match_text(p, key)


def search_programs(snapshot: EpgSnapshot, key_list: list[dict]) -> list[dict]:
    """いずれかの検索条件にマッチした番組の EventInfo を返す"""
    now = dt.datetime.now(JST)
    return [
        program_to_event_info(s, p)
        for s in snapshot.services
        for p in snapshot.programs.get((s.onid, s.sid), [])
        if any(program_matches(p, s.key, key, now) for key in key_list)
    ]
