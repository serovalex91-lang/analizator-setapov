import os
from datetime import datetime, timedelta

import pytest

import levels_repo as lr


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_levels.db"
    monkeypatch.setattr(lr, "DB_PATH", str(db_path))
    yield str(db_path)


def iso_minus(minutes: int) -> str:
    return (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()

def iso_minus_hours(hours: int) -> str:
    return (datetime.utcnow() - timedelta(hours=hours)).isoformat()


def test_empty_db_returns_none(temp_db):
    res = lr.get_latest_levels("TESTUSDT", max_age_minutes=1440, prefer_timeframes=["4h", "1h", "12h", "30m"])
    assert res is None


def test_all_empty_sides_returns_none(temp_db):
    # Insert two snapshots with empty lists on both sides
    lr.upsert_levels("TESTUSDT", "4h", [], [], source_ts=iso_minus(60))
    lr.upsert_levels("TESTUSDT", "4h", [], [], source_ts=iso_minus(30))
    res = lr.get_latest_levels("TESTUSDT", max_age_minutes=1440, prefer_timeframes=["4h"]) 
    assert res is None


def test_newest_has_resistance_old_has_support_same_tf(temp_db):
    # Older: support only
    ts_support = iso_minus(120 + 48*60)
    lr.upsert_levels("TESTUSDT", "4h", [(10.0, 11.0)], [], source_ts=ts_support)
    # Newer: resistance only
    ts_res = iso_minus(60 + 48*60)
    lr.upsert_levels("TESTUSDT", "4h", [], [(20.0, 21.0)], source_ts=ts_res)
    res = lr.get_latest_levels("TESTUSDT", max_age_minutes=1440, prefer_timeframes=["4h"]) 
    assert res is not None
    assert res["timeframe"] == "4h"
    assert res["support"] == [[10.0, 11.0]]
    assert res["resistance"] == [[20.0, 21.0]]
    # source_ts should reflect the newest among found sides
    expected_ts = max(ts_support, ts_res)
    assert res["source_ts"] == expected_ts


def test_collect_both_sides_across_multiple_snapshots_same_tf(temp_db):
    # Newest: empty both
    lr.upsert_levels("TESTUSDT", "4h", [], [], source_ts=iso_minus(10 + 48*60))
    # Next: resistance only
    lr.upsert_levels("TESTUSDT", "4h", [], [(30.0, 31.0)], source_ts=iso_minus(20 + 48*60))
    # Next: support only
    lr.upsert_levels("TESTUSDT", "4h", [(12.0, 13.0)], [], source_ts=iso_minus(30 + 48*60))
    res = lr.get_latest_levels("TESTUSDT", max_age_minutes=1440, prefer_timeframes=["4h"]) 
    assert res is not None
    assert res["support"] == [[12.0, 13.0]]
    assert res["resistance"] == [[30.0, 31.0]]


def test_timeframe_priority_4h_over_1h_and_12h_over_origin(temp_db):
    # 1h newer but valid; 4h older but valid → expect 4h due to priority
    lr.upsert_levels("TESTUSDT", "1h", [(101.0, 102.0)], [(201.0, 202.0)], source_ts=iso_minus(10 + 48*60))
    lr.upsert_levels("TESTUSDT", "4h", [(11.0, 12.0)], [(21.0, 22.0)], source_ts=iso_minus(30 + 48*60))
    res = lr.get_latest_levels("TESTUSDT", max_age_minutes=1440, prefer_timeframes=["4h", "1h", "12h", "30m"]) 
    assert res is not None
    assert res["timeframe"] == "4h"
    assert res["support"] == [[11.0, 12.0]]
    assert res["resistance"] == [[21.0, 22.0]]

    # If 4h has no valid sides, should fall back to 1h
    # Insert an empty 4h snapshot newer than previous
    lr.upsert_levels("TESTUSDT", "4h", [], [], source_ts=iso_minus(5 + 48*60))
    # With older-than filter, empty 4h snapshot nearer to threshold is excluded; valid older 4h remains → still 4h
    res2 = lr.get_latest_levels("TESTUSDT", max_age_minutes=15 + 48*60, prefer_timeframes=["4h", "1h", "12h", "30m"]) 
    assert res2 is not None
    assert res2["timeframe"] == "4h"
    assert res2["support"] == [[11.0, 12.0]]
    assert res2["resistance"] == [[21.0, 22.0]]


def test_ignore_non_allowed_timeframes_only_wrong_tfs_return_none(temp_db):
    # Insert zones on non-allowed TFs only: 15m, 30m, 120m, 24h
    lr.upsert_levels("TESTUSDT", "15m", [[1.0, 2.0]], [[9.0, 10.0]], source_ts=iso_minus(10))
    lr.upsert_levels("TESTUSDT", "30m", [[3.0, 4.0]], [[11.0, 12.0]], source_ts=iso_minus(20))
    lr.upsert_levels("TESTUSDT", "120m", [[5.0, 6.0]], [[13.0, 14.0]], source_ts=iso_minus(30))
    lr.upsert_levels("TESTUSDT", "24h", [[7.0, 8.0]], [[15.0, 16.0]], source_ts=iso_minus(40))
    # Even if prefer_timeframes include them, the repo must only consider 4h/1h/12h
    res = lr.get_latest_levels("TESTUSDT", max_age_minutes=1440, prefer_timeframes=["30m", "120m", "24h"]) 
    assert res is None


def test_ignore_wrong_tfs_but_use_older_allowed_tfs(temp_db):
    # Fresh but wrong TFs (should be ignored)
    lr.upsert_levels("TESTUSDT", "30m", [[100.0, 101.0]], [[200.0, 201.0]], source_ts=iso_minus(5 + 48*60))
    lr.upsert_levels("TESTUSDT", "120m", [[102.0, 103.0]], [[202.0, 203.0]], source_ts=iso_minus(8 + 48*60))
    # Older but allowed TFs (should be used)
    lr.upsert_levels("TESTUSDT", "1h", [[11.0, 12.0]], [[21.0, 22.0]], source_ts=iso_minus(60 + 48*60))
    lr.upsert_levels("TESTUSDT", "4h", [[13.0, 14.0]], [[23.0, 24.0]], source_ts=iso_minus(90 + 48*60))


def test_oldest_allowed_over_newer_wrong_and_middle_allowed(temp_db):
    # support snapshots at 12h (allowed 1h), 50h (allowed 1h), 60h (allowed 1h). Expect 50h as per spec.
    lr.upsert_levels("TESTUSDT", "1h", [[111.0, 112.0]], [], source_ts=iso_minus_hours(12))
    lr.upsert_levels("TESTUSDT", "1h", [[151.0, 152.0]], [], source_ts=iso_minus_hours(50))
    lr.upsert_levels("TESTUSDT", "1h", [[161.0, 162.0]], [], source_ts=iso_minus_hours(60))
    res = lr.get_latest_levels("TESTUSDT", max_age_minutes=48*60, prefer_timeframes=["1h"]) 
    assert res is not None
    assert res["support"] == [[151.0, 152.0]]
    # resistance empty
    assert res["resistance"] == []
    res = lr.get_latest_levels("TESTUSDT", max_age_minutes=1440, prefer_timeframes=["4h", "1h", "12h"])
    assert res is not None
    # Only 1h present in allowed TFs in this test → timeframe 1h
    assert res["timeframe"] == "1h"
    assert res["support"] == [[151.0, 152.0]]
    assert res["resistance"] == []


