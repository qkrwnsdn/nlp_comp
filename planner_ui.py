import streamlit as st
import inspect  # 👈 helper for dynamic prefs injection
from pathlib import Path
from datetime import datetime
from typing import Dict, List
# 좋아
# ──────────────────────────────────────────────────────────────────────────────
# 내부 로직 모듈 (multimodal_route_planner.py를 "planner.py"로 저장했다고 가정)
# ──────────────────────────────────────────────────────────────────────────────
from planner import (
    parse_location,
    load_prefs,
    save_prefs,
    AVG_WALK_SPEED,
    odsay_all_routes,
    choose_best_route,
    draw_map,
    haversine,
    append_history,
)

try:
    from streamlit_folium import st_folium  # pip install streamlit-folium
except ImportError:
    st_folium = None

st.set_page_config(page_title="멀티모달 경로 플래너", layout="wide")

# ──────────────────────────────────────────────────────────────────────────────
# 0️⃣  Session State 로 기본 선호도 로드 (최초 1회)
# ──────────────────────────────────────────────────────────────────────────────
if "prefs" not in st.session_state:
    st.session_state["prefs"] = load_prefs()

# ──────────────────────────────────────────────────────────────────────────────
# ①  사이드바 – 편집 위젯 -------------------------------------------------------
#     👉 "저장" 버튼을 누르지 않아도 **현재 위젯 값**이 즉시 다음 탐색에 반영됩니다.
# ──────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("⚙️  선호도 & 가중치 설정")

    # ── 현재 보이는 값은 세션 prefs 값을 기본으로 사용
    p: Dict = st.session_state["prefs"]

    # 공통 파라미터 ------------------------------------------------------------
    crowd_weight   = st.slider("혼잡도 가중치", 0.0, 5.0, float(p.get("crowd_weight", 2.0)), 0.1)
    max_crowd      = st.slider("허용 최대 혼잡 레벨", 1, 4, int(p.get("max_crowd", 4)), 1)
    walk_limit_min = st.number_input("허용 최대 도보 (분)", 0, 60, int(p.get("walk_limit_min", 15)), 1)

    # 모드별 페널티 -------------------------------------------------------------
    st.subheader("모드별 페널티")
    mp_subway = st.number_input("지하철", 0.0, 10.0, float(p.get("mode_penalty", {}).get("SUBWAY", 0.0)), 0.5)
    mp_bus    = st.number_input("버스",   0.0, 10.0, float(p.get("mode_penalty", {}).get("BUS",    0.0)), 0.5)
    mp_walk   = st.number_input("도보",   0.0, 10.0, float(p.get("mode_penalty", {}).get("WALK",   0.0)), 0.5)

    # 모드별 선호도 -------------------------------------------------------------
    st.subheader("모드별 선호도")
    pref_subway = st.number_input("지하철 선호도", -10.0, 10.0, float(p.get("mode_preference", {}).get("SUBWAY", 0.0)), 0.5)
    pref_bus    = st.number_input("버스 선호도",   -10.0, 10.0, float(p.get("mode_preference", {}).get("BUS",    0.0)), 0.5)
    pref_walk   = st.number_input("도보 선호도",   -10.0, 10.0, float(p.get("mode_preference", {}).get("WALK",   0.0)), 0.5)

    # 저장 버튼 – 영구 저장이 필요할 때만 사용
    if st.button("💾  선호도 저장"):
        to_save: Dict = {
            "crowd_weight": crowd_weight,
            "max_crowd": max_crowd,
            "walk_limit_min": walk_limit_min,
            "mode_penalty": {
                "SUBWAY": mp_subway,
                "BUS": mp_bus,
                "WALK": mp_walk,
            },
            "mode_preference": {
                "SUBWAY": pref_subway,
                "BUS": pref_bus,
                "WALK": pref_walk,
            },
            "runs": p.get("runs", 0),
        }
        save_prefs(to_save)
        st.session_state["prefs"] = to_save  # 세션 상태도 동기화
        st.success("✅  선호도가 영구 저장되었습니다!")

    st.markdown("---")
    learn_mode = st.checkbox("🧠  학습 모드로 경로 기록", value=False)

# ──────────────────────────────────────────────────────────────────────────────
# ②  메인 영역 – 경로 탐색 ------------------------------------------------------
# ──────────────────────────────────────────────────────────────────────────────

st.title("🚍  ODsay 멀티모달 경로 플래너 · 개인화 UI")

col1, col2 = st.columns(2)
with col1:
    origin_input = st.text_input("출발지 (역명/주소/위도,경도)")
with col2:
    dest_input = st.text_input("도착지 (역명/주소/위도,경도)")

# 👉 버튼이 눌린 순간의 **위젯 값** 기준으로 prefs dict 를 구성
current_prefs: Dict = {
    "crowd_weight": crowd_weight,
    "max_crowd": max_crowd,
    "walk_limit_min": walk_limit_min,
    "mode_penalty": {
        "SUBWAY": mp_subway,
        "BUS": mp_bus,
        "WALK": mp_walk,
    },
    "mode_preference": {
        "SUBWAY": pref_subway,
        "BUS": pref_bus,
        "WALK": pref_walk,
    },
}

if st.button("🚀  경로 탐색"):
    if not origin_input or not dest_input:
        st.warning("출발지와 도착지를 모두 입력하세요.")
        st.stop()

    try:
        origin = parse_location(origin_input)
        dest = parse_location(dest_input)
    except ValueError as e:
        st.error(str(e))
        st.stop()

    # ── 경로 계산 & 선택 -------------------------------------------------------
    with st.spinner("경로 계산 중…"):

        def _call_with_prefs(func, *f_args):  # helper: 전달할 함수가 prefs 인자를 지원하면 넣어줌
            sig = inspect.signature(func)
            if "prefs" in sig.parameters:
                return func(*f_args, prefs=current_prefs)  # type: ignore[arg-type]
            return func(*f_args)

        routes: List[List[Dict]] = _call_with_prefs(odsay_all_routes, origin, dest)
        best_idx, segs = _call_with_prefs(choose_best_route, routes)

        if not segs:
            dist = haversine(origin, dest)
            segs = [{
                "mode": "WALK",
                "name": "직선도보",
                "distance_m": dist,
                "duration_min": round(dist / (AVG_WALK_SPEED * 60), 2),
                "crowd": 1,
                "best_car": None,
                "poly": [origin, dest],
            }]

    # ── 경로 요약 ------------------------------------------------------------- -------------------------------------------------------------
    total_min = sum(s.get("duration_min", 0) for s in segs)
    st.subheader("📝  경로 요약")
    for i, s in enumerate(segs, 1):
        car = f" | 추천칸 {s.get('best_car')}" if s.get("best_car") else ""
        st.write(f"{i}. {s.get('mode'):<6} | {s.get('name'):<10} | {s.get('duration_min',0):5.1f}분{car}")
    st.success(f"예상 총 소요 시간: {total_min:.1f}분")

    # ── 지도 -------------------------------------------------------------
    # 🌐 HTML 결과 파일 이름에 타임스탬프를 붙여 브라우저 캐싱 문제 방지
    html_path: Path = draw_map(segs, origin, dest)
    unique_path = html_path.with_stem(html_path.stem + f"_{int(datetime.now().timestamp())}")
    html_path.replace(unique_path)
    html_path = unique_path
    if st_folium:
        st.subheader("🗺️  경로 지도")
        st_folium(open(html_path, "r", encoding="utf-8").read(), width=900, height=600)
    else:
        import webbrowser
        webbrowser.open(html_path.as_uri())

    # ── 학습 모드 ----------------------------------------------------------
    if learn_mode:
        append_history({
            "datetime": datetime.now().isoformat(),
            "origin": origin_input,
            "dest": dest_input,
            "total_min": total_min,
            "modes": "/".join({s.get("mode") for s in segs}),
        })
        st.info("📚  경로 이용 기록이 저장되었습니다.")

# ──────────────────────────────────────────────────────────────────────────────
# 푸터 -----------------------------------------------------------------------
# ──────────────────────────────────────────────────────────────────────────────

st.markdown(
    "---\n"
    "<div style='text-align:center;'>ⓒ 2025 Multimodal Route Planner UI · 개발: ChatGPT</div>",
    unsafe_allow_html=True,
)
