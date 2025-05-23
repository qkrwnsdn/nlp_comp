#!/usr/bin/env python3
"""
multimodal_route_planner.py (rev‑2025‑05‑01)
===============================================================
🆕  ODsay 전용 멀티모달 경로 플래너 + 개인화 학습 + 최적 칸 추천
----------------------------------------------------------------
* **학습 모드**(`--learn`) : 이용한 경로를 기록하고
  평균 혼잡 레벨·사용 패턴을 분석해 개인화 가중치를 조정합니다.

* **지하철 최적 칸 추천** : 각 지하철 구간마다 ‘가장 여유로운 칸’을
  히스토리 기반 또는 휴리스틱으로 추정해 표시합니다.

**주의**: `origin`, `dest`에 역명·주소 또는 `위도,경도` 입력 가능.
"""
from __future__ import annotations

import argparse
import csv
import math
import random
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

import orjson
import folium
import pandas as pd
import polyline
import requests
from tqdm import tqdm
from typing import Dict, List, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# 설정 및 파일
CONF_DIR = Path.home() / ".route_planner"
CONF_DIR.mkdir(exist_ok=True)
PREF_FILE = CONF_DIR / "prefs.json"
HIST_FILE = CONF_DIR / "history.csv"
DEFAULT_PREFS = {
    "crowd_weight": 2.0,
    "max_crowd": 4,
    "mode_penalty": {"SUBWAY": 0.0, "BUS": 0.0, "WALK": 0.0},
    "runs": 0,
}
# API 키
ODSAY_KEY = open("odsay_api.txt").read().strip()
print(ODSAY_KEY)
KAKAO_REST_KEY = open("kakao_api.txt").read().strip()
# 혼잡도 CSV
SUBWAY_CSV = Path("seoul_subway_crowd.csv")
BUS_CSV = Path("seoul_bus_crowd.csv")

# 상수
AVG_WALK_SPEED = 1.3  # m/s
DAY_TYPE = {0: 1, 1: 1, 2: 1, 3: 1, 4: 1, 5: 2, 6: 3}
COLOR = {"SUBWAY": "red", "BUS": "green", "WALK": "gray"}
HEADERS = {"Authorization": f"KakaoAK {KAKAO_REST_KEY}"}
MODE_COLOR = {"SUBWAY": "red", "BUS": "green", "WALK": "gray"}
BUS_COLOR = "#006400"  # 진한 녹색
WALK_COLOR = "#555555"  # 진한 회색
# 지하철 호선별 표준 색상 (예시: 서울 지하철)
SUBWAY_LINE_COLORS = {
    "1호선": "#0052A4",
    "2호선": "#00A84D",
    "3호선": "#EF7C1C",
    "4호선": "#00A2E3",
    "5호선": "#996CAC",
    "6호선": "#CD7C2F",
    "7호선": "#747F00",
    "8호선": "#C3002F",
    "9호선": "#BDB092",
    "경의중앙선": "#77C7C4",
    "공항철도": "#0090D2",
    "경춘선": "#0C8040",
    "수인선": "#FAAFBE",
    "신분당선": "#DB005B",
    "우이신설선": "#B0D840",
    "의정부경전철": "#B0B0B0",
}

CROWD_COLOR = {1: "green", 2: "yellow", 3: "orange", 4: "red"}
OUTER_LINE_WEIGHT = 15  # 외곽선 굵기 (모드/호선별 라인)
CENTER_LINE_WEIGHT = 2  # 중심선 굵기 (혼잡도 기반 그라디언트)


# ─────────────────────────────────────────────────────────────────────────────
def load_prefs():
    # if PREF_FILE.exists():
    #    return orjson.loads(PREF_FILE.read_bytes())
    if PREF_FILE.exists():
        try:
            return orjson.loads(PREF_FILE.read_bytes())
        except:
            DEFAULT_PREFS = {
                "crowd_weight": 2.0,
                "max_crowd": 4,
                "mode_penalty": {"SUBWAY": 10.0, "BUS": 0.0, "WALK": 2.0},
                "mode_preference": {"SUBWAY": 0.0, "BUS": 10.0, "WALK": 1.0},
                "walk_limit_min": 15,
                "runs": 0,
            }
        return DEFAULT_PREFS.copy()


def save_prefs(prefs: Dict):
    PREF_FILE.write_bytes(orjson.dumps(prefs, option=orjson.OPT_INDENT_2))


def append_history(row: Dict):
    write_header = not HIST_FILE.exists()
    with HIST_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        if write_header:
            w.writeheader()
        w.writerow(row)


# ─────────────────────────────────────────────────────────────────────────────
def geocode(addr: str):
    for ep in ("address", "keyword"):
        url = f"https://dapi.kakao.com/v2/local/search/{ep}.json"
        r = requests.get(
            url, headers=HEADERS, params={"query": addr}, timeout=5, verify=False
        )
        r.raise_for_status()
        docs = r.json().get("documents", [])
        if docs:
            return float(docs[0]["y"]), float(docs[0]["x"])
    raise ValueError(f"주소/역 '{addr}' 검색 실패")


# ─────────────────────────────────────────────────────────────────────────────
def parse_location(s: str):
    try:
        lat, lng = map(float, s.split(","))
        return lat, lng
    except:
        return geocode(s)


# ─────────────────────────────────────────────────────────────────────────────
def odsay_best_route(origin: Tuple[float, float], dest: Tuple[float, float]):
    """
    ODsay API로 다중 경로를 받아와 개인화 점수 계산 후 최적 경로 1개 선택
    """
    common = {
        "apiKey": ODSAY_KEY,
        "lang": 0,
        "output": "json",
        "SX": origin[1],
        "SY": origin[0],
        "EX": dest[1],
        "EY": dest[0],
        "OPT": 0,
        "SearchPathType": 0,
        "reqCoordType": "WGS84GEO",
        "resCoordType": "WGS84GEO",
    }

    for endpoint, extra in [
        ("https://api.odsay.com/v1/api/searchPubTransPath", {"SearchType": 0}),
        ("https://api.odsay.com/v1/api/searchPubTransPathT", {"SearchType": 0}),
    ]:
        try:
            r = requests.get(
                endpoint, params={**common, **extra}, timeout=8, verify=False
            )
            r.raise_for_status()
            result = r.json().get("result", {})
            paths = result.get("path", [])
            if not paths:
                continue

            all_segs = []
            for path in paths:
                segs = paths_to_segs(path.get("subPath", []))
                score = score_route(segs)
                all_segs.append((score, segs))

            if all_segs:
                best = min(all_segs, key=lambda x: x[0])
                return best[1]  # 최적 경로 반환

        except requests.RequestException:
            continue

    return []  # 실패 시 빈 경로


def score_route(segs: List[dict], *, prefs: Dict | None = None) -> float:
    if prefs is None:
        prefs = load_prefs()
    score = 0.0
    total_walk_min = 0
    for s in segs:
        mode = s["mode"]
        duration = s["duration_min"]
        crowd = s["crowd"]

        # 걷기 제한 시간 초과 페널티
        if mode == "WALK":
            total_walk_min += duration

        # 기본 이동 시간 + 모드별 페널티 + 혼잡도 가중치
        score += duration
        score += prefs["mode_penalty"].get(mode, 0.0)
        score += prefs["crowd_weight"] * max(0, crowd - 1)

    # 걷기 시간 초과 시 큰 페널티
    if total_walk_min > prefs.get("walk_limit_min", 15):
        score += 999

    return score


# ─────────────────────────────────────────────────────────────────────────────
# 혼잡 로딩
_sub_df: pd.DataFrame | None = None
_bus_df: pd.DataFrame | None = None


def _load_sub_df():
    global _sub_df
    if _sub_df is None:
        if not SUBWAY_CSV.exists():
            raise FileNotFoundError
        _sub_df = pd.read_csv(SUBWAY_CSV)
    return _sub_df


def _load_bus_df():
    global _bus_df
    if _bus_df is None and BUS_CSV.exists():
        _bus_df = pd.read_csv(BUS_CSV)
    return _bus_df


# ─────────────────────────────────────────────────────────────────────────────
def pct_to_level(pct: float):
    if pct < 70:
        return 1
    if pct < 100:
        return 2
    if pct < 150:
        return 3
    return 4


def subway_crowd_level(station: str, now: datetime):
    try:
        df = _load_sub_df()
        day = DAY_TYPE[now.weekday()]
        hhmm = (
            now.replace(minute=0) if now.minute < 30 else now.replace(minute=30)
        ).strftime("%H%M")
        pct = df.loc[
            (df.DAY_CODE == day) & (df.STATION_NM == station) & (df.HHMM == hhmm),
            "CONGEST_PCT",
        ].mean()
        lvl = pct_to_level(pct) if not pd.isna(pct) else 2
    except:
        lvl = 2
    if lvl >= 3:
        best = random.choice([1, 10])
    elif lvl == 2:
        best = random.choice([2, 9])
    else:
        best = random.randint(1, 10)
    return lvl, best


def bus_crowd_level(route_id: str, now: datetime):
    df = _load_bus_df()
    if df is None:
        return 2
    try:
        b = df.loc[
            (df.ROUTE_ID == int(route_id)) & (df.HH == now.hour), "BOARD_NUM"
        ].mean()
        if pd.isna(b):
            return 2
        if b < 10:
            return 1
        if b < 25:
            return 2
        if b < 40:
            return 3
        return 4
    except:
        return 2


# ─────────────────────────────────────────────────────────────────────────────
def paths_to_segs(paths: List[dict], *, prefs: Dict | None = None):
    segs = []
    now = datetime.now()
    if prefs is None:
        prefs = load_prefs()
    allowed = {"SUBWAY", "BUS", "WALK"}
    for sp in paths:
        tp = sp.get("trafficType")
        if tp == 1:
            mode = "SUBWAY"
            lane0 = sp.get("lane", [{}])[0]
            # 가능한 키 순서대로 꺼내되, 없으면 빈 문자열 처리
            name = (
                lane0.get("laneName")
                or lane0.get("name")
                or lane0.get("subwayName")
                or ""
            )
            dur, dist = sp.get("sectionTime", 0), sp.get("distance", 0)
            crowd, best = subway_crowd_level(name, now)
        elif tp == 2:
            mode, name = "BUS", sp["lane"][0]["busNo"]
            dur, dist = sp["sectionTime"], sp["distance"]
            crowd = bus_crowd_level(sp["lane"][0].get("busID", ""), now)
            best = None
        else:
            mode, name = "WALK", "도보"
            dist = sp.get("distance", 0)
            dur = dist / AVG_WALK_SPEED / 60
            crowd, best = 1, None
        # penalty
        if mode not in allowed:
            dur += 1e3
        dur += prefs.get("mode_penalty", {}).get(mode, 0)
        if crowd > prefs.get("max_crowd", 4):
            dur += 1e3
        else:
            dur += (crowd - 1) * prefs.get("crowd_weight", 2.0)
        coords = [
            (float(x["y"]), float(x["x"]))
            for x in sp.get("passStopList", {}).get("stations", [])
        ]
        segs.append(
            {
                "mode": mode,
                "name": name,
                "distance_m": dist,
                "duration_min": round(dur, 2),
                "crowd": crowd,
                "best_car": best,
                "poly": coords,
            }
        )
    return segs


# ─────────────────────────────────────────────────────────────────────────────
# 도보 fallback 시 사용


def haversine(a: Tuple[float, float], b: Tuple[float, float]):
    R = 6371000
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    d = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(d))


def crowd_gradient_color(level: int, max_level: int = 4) -> str:
    """
    혼잡도 레벨(1~max_level)에 따라 녹색에서 빨강으로 선형 그라디언트 색상을 반환합니다.
    """
    ratio = max(0, min(level - 1, max_level - 1)) / (max_level - 1)
    r = int(255 * ratio)
    g = int(255 * (1 - ratio))
    return f"#{r:02x}{g:02x}00"


def draw_map(segs: list[dict], o: tuple[float, float], d: tuple[float, float]):
    """
    folium을 이용해 경로를 시각화합니다.
    - 외곽선과 중심선 굵기는 상단 상수 OUTER_LINE_WEIGHT, CENTER_LINE_WEIGHT로 조정 가능
    - 외곽선: 모드/호선별 진한 색상, weight=OUTER_LINE_WEIGHT
    - 중심선: 혼잡도 기반 그라디언트, weight=CENTER_LINE_WEIGHT
    - 정차 지점마다 혼잡도 원형 마커
    - 환승 지점에 특별 아이콘 표시
    - 레이어 컨트롤로 토글 가능
    """
    m = folium.Map(location=[(o[0] + d[0]) / 2, (o[1] + d[1]) / 2], zoom_start=13)
    folium.Marker(o, popup="출발", icon=folium.Icon(color="blue", icon="play")).add_to(
        m
    )
    folium.Marker(d, popup="도착", icon=folium.Icon(color="red", icon="flag")).add_to(m)

    prev_mode = None
    transfer_count = 0

    for idx, seg in enumerate(segs, start=1):
        mode = seg.get("mode")
        coords = seg.get("poly", [])
        if not coords:
            prev_mode = mode
            continue

        crowd = seg.get("crowd", 1)
        duration = seg.get("duration_min", 0)
        best_car = seg.get("best_car")
        name = seg.get("name", "")  # 지하철 호선명 또는 버스 번호 등

        # 외곽선 색상 결정
        if mode == "SUBWAY":
            for k in SUBWAY_LINE_COLORS.keys():
                if k in name:
                    outer_color = SUBWAY_LINE_COLORS[k]
                elif name in k:
                    outer_color = SUBWAY_LINE_COLORS[name]
                else:
                    pass
        elif mode == "BUS":
            outer_color = BUS_COLOR
        else:  # WALK
            outer_color = WALK_COLOR

        tooltip = f"{idx}. {mode} ({name}): {duration:.1f}분 | 혼잡도 {crowd}"
        if best_car:
            tooltip += f" | 추천칸 {best_car}"

        # 외곽 라인 그리기
        folium.PolyLine(
            coords,
            color=outer_color,
            weight=OUTER_LINE_WEIGHT,
            opacity=0.8,
            tooltip=tooltip,
        ).add_to(m)

        # 중심선 그리기 (혼잡도 기반 색상)
        center_color = crowd_gradient_color(crowd)
        folium.PolyLine(
            coords,
            color=center_color,
            weight=CENTER_LINE_WEIGHT,
            opacity=1.0,
            tooltip=tooltip,
        ).add_to(m)

        # 시작 마커: 혼잡도 기반 색상 및 크기
        start = coords[0]
        folium.CircleMarker(
            location=start,
            radius=5 + crowd * 2,
            color=CROWD_COLOR.get(crowd, "blue"),
            fill=True,
            fill_opacity=0.7,
            tooltip=f"혼잡도 {crowd}: {duration:.1f}분",
        ).add_to(m)

        # 환승 표시
        if prev_mode and prev_mode != mode:
            transfer_count += 1
            folium.Marker(
                location=start,
                popup=f"환승 {transfer_count}: {prev_mode}→{mode}",
                icon=folium.Icon(color="purple", icon="exchange"),
            ).add_to(m)

        prev_mode = mode

    folium.LayerControl().add_to(m)
    out = Path("route.html").resolve()
    m.save(str(out))
    return out


def odsay_all_routes(origin, dest, *, prefs: Dict | None = None) -> List[List[dict]]:
    """
    ODsay API에서 얻을 수 있는 모든 후보 경로를 '세그먼트 리스트' 형태로 모아 반환.
    """
    common = {
        "apiKey": ODSAY_KEY,  # ← 개인 ODsay API 키
        "lang": 0,  # 0 = 한국어
        "output": "json",
        "SX": origin[1],  # 출발 X(경도)
        "SY": origin[0],  # 출발 Y(위도)
        "EX": dest[1],  # 도착 X
        "EY": dest[0],  # 도착 Y
        "OPT": 0,  # 0 = 종합 최적
        "SearchPathType": 0,  # 0 = 대중교통+도보
        "reqCoordType": "WGS84GEO",
        "resCoordType": "WGS84GEO",
    }
    endpoints = [
        ("https://api.odsay.com/v1/api/searchPubTransPath", {"SearchType": 0}),
        ("https://api.odsay.com/v1/api/searchPubTransPathT", {"SearchType": 0}),
    ]

    candidates: list[list[dict]] = []
    for endpoint, extra in endpoints:
        try:
            r = requests.get(
                endpoint, params={**common, **extra}, timeout=8, verify=False
            )
            r.raise_for_status()
            for path in r.json().get("result", {}).get("path", []):
                segs = paths_to_segs(path.get("subPath", []), prefs=prefs)
                if segs:  # 빈 경로 방지
                    candidates.append(segs)
        except requests.RequestException:
            continue  # 해당 엔드포인트 실패 → 다음 시도
    return candidates  # 후보 0 개면 빈 리스트


def choose_best_route(routes, *, prefs: Dict | None = None) -> Tuple[int, List[dict]]:
    """
    후보 리스트 중 score_route() 총점이 가장 낮은 경로를 골라
    (인덱스, 경로) 형태로 반환한다.  인덱스는 1-based.
    """
    if not routes:
        return -1, []  # 후보가 없으면 -1
    scored = [(score_route(r, prefs=prefs), i + 1, r) for i, r in enumerate(routes)]
    scored.sort(key=lambda x: x[0])  # 점수 오름차순
    _, best_idx, best_route = scored[0]
    return best_idx, best_route


def debug_print_scores(routes: list[list[dict]]):
    """(선택) 후보별 총점·구성 확인용 디버그 헬퍼"""
    for i, r in enumerate(routes, 1):
        print(f"[DBG] Route {i:02d}: score={score_route(r):.2f}, " f"segments={len(r)}")


def main():
    p = argparse.ArgumentParser(description="ODsay 멀티모달 플래너 + 시각화 개선 v3")
    p.add_argument("origin")
    p.add_argument("dest")
    p.add_argument("--learn", action="store_true")
    args = p.parse_args()

    o = parse_location(args.origin)
    d = parse_location(args.dest)
    routes = odsay_all_routes(o, d)  # ① 후보 목록
    debug_print_scores(routes)  # ← 원하면 주석 해제
    best_idx, segs = choose_best_route(routes)  #   # ② 개인 선호 기반 '최적 1 개'
    if best_idx != -1:
        print(f"\n[선택된 후보] {best_idx}번 경로가 최적입니다.")
    if not segs:
        dist = haversine(o, d)
        dur = dist / (AVG_WALK_SPEED * 60)
        segs = [
            {
                "mode": "WALK",
                "name": "직선도보",
                "distance_m": dist,
                "duration_min": round(dur, 2),
                "crowd": 1,
                "best_car": None,
                "poly": [o, d],
            }
        ]

    total = sum(s.get("duration_min", 0) for s in segs)
    print("[경로 요약]")
    for i, s in enumerate(segs, 1):
        car = f" | 추천칸 {s.get('best_car')}" if s.get("best_car") else ""
        print(
            f"{i}. {s.get('mode'):<6} | {s.get('name'):<10} | {s.get('duration_min',0):5.1f}분{car}"
        )
    print(f"\n▶ 예상 총 소요: {total:.1f}분")

    html_path = draw_map(segs, o, d)
    uri = html_path.as_uri()
    webbrowser.open(uri)
    print(f"[+] 지도: {uri}")

    if args.learn:
        append_history(
            {
                "datetime": datetime.now().isoformat(),
                "origin": args.origin,
                "dest": args.dest,
                "total_min": total,
                "modes": "/".join({s.get("mode") for s in segs}),
            }
        )
        print("[+] 기록 저장 →", HIST_FILE)


if __name__ == "__main__":
    main()
