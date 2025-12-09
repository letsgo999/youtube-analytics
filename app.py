import streamlit as st
import google.generativeai as genai
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import re
import time

# -----------------------------------------------------------------------------
# 1. 기본 설정 및 인증 (Secrets 관리)
# -----------------------------------------------------------------------------
st.set_page_config(page_title="YouTube AI Analyst Pro", layout="wide", page_icon="📺")

try:
    client_config = st.secrets["web"]
    gemini_key = st.secrets["GEMINI_API_KEY"]
except KeyError:
    st.error("🚨 Secrets 설정이 누락되었습니다. (GEMINI_API_KEY, web, REDIRECT_URI 확인 필요)")
    st.stop()

# Gemini 설정
try:
    genai.configure(api_key=gemini_key)
except Exception as e:
    st.error(f"Gemini API 설정 오류: {e}")

SCOPES = [
    'https://www.googleapis.com/auth/yt-analytics.readonly',
    'https://www.googleapis.com/auth/youtube.readonly'
]

def get_flow():
    flow = Flow.from_client_config(
        {'web': client_config},
        scopes=SCOPES,
        redirect_uri=st.secrets["REDIRECT_URI"]
    )
    return flow

# -----------------------------------------------------------------------------
# 2. 데이터 추출 함수
# -----------------------------------------------------------------------------
def get_video_id(url):
    video_id = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', url)
    return video_id.group(1) if video_id else None

def get_authenticated_channel_info(creds):
    """현재 로그인된 계정의 채널 ID와 이름을 가져옵니다."""
    try:
        youtube = build('youtube', 'v3', credentials=creds)
        response = youtube.channels().list(mine=True, part='id,snippet').execute()
        if response['items']:
            item = response['items'][0]
            return {
                'id': item['id'],
                'title': item['snippet']['title'],
                'thumbnail': item['snippet']['thumbnails']['default']['url']
            }
    except:
        return None
    return None

def get_video_basic_info(creds, video_id):
    """기본 정보(제목, 채널ID)만 빠르게 조회 (소유권 확인용)"""
    try:
        youtube = build('youtube', 'v3', credentials=creds)
        response = youtube.videos().list(part='snippet,statistics', id=video_id).execute()
        
        if not response['items']: return None
        item = response['items'][0]
        snippet = item['snippet']
        return {
            "id": video_id,
            "title": snippet['title'],
            "channel_title": snippet['channelTitle'],
            "channel_id": snippet['channelId'], # 영상 소유 채널 ID
            "published_at": snippet['publishedAt'],
            "realtime_views": int(item['statistics'].get('viewCount', 0)),
            "likes": int(item['statistics'].get('likeCount', 0)),
            "comments": int(item['statistics'].get('commentCount', 0))
        }
    except Exception as e:
        st.error(f"영상 정보 조회 실패: {e}")
        return None

def get_analytics_data_safe(creds, video_data):
    """
    [핵심 변경] ID 일치 여부와 상관없이 일단 API 호출을 시도합니다.
    성공하면 권한이 있는 것이고, 실패하면 권한이 없는 것입니다.
    """
    analytics = build('youtubeAnalytics', 'v2', credentials=creds)
    
    start_date = video_data['published_at'][:10]
    end_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    if start_date > end_date: end_date = start_date

    try:
        # 주요 지표 조회 시도
        analytics_res = analytics.reports().query(
            ids='channel==MINE', # 현재 토큰의 권한으로 조회
            startDate=start_date,
            endDate=end_date,
            metrics='views,estimatedMinutesWatched,averageViewDuration',
            filters=f'video=={video_data["id"]}'
        ).execute()
        
        # 트래픽 소스
        traffic_res = analytics.reports().query(
            ids='channel==MINE',
            startDate=start_date,
            endDate=end_date,
            metrics='views',
            dimensions='insightTrafficSourceType',
            filters=f'video=={video_data["id"]}'
        ).execute()

        if analytics_res.get('rows'):
            row = analytics_res['rows'][0]
            video_data['analytics_views'] = row[0]
            video_data['watch_time_min'] = row[1]
            video_data['avg_duration_sec'] = row[2]
        else:
            # 데이터가 0인 경우 (집계 전)
            video_data['analytics_views'] = 0
            video_data['watch_time_min'] = 0.0
            video_data['avg_duration_sec'] = 0.0
            
        video_data['traffic_sources'] = traffic_res.get('rows', [])
        video_data['permission_ok'] = True # 성공적으로 호출됨

    except Exception as e:
        # API 호출 자체가 실패한 경우 (권한 없음 403 등)
        video_data['permission_ok'] = False
        video_data['error_detail'] = str(e)
        # 기본값 채움
        video_data['analytics_views'] = 0
        video_data['watch_time_min'] = 0.0
        video_data['avg_duration_sec'] = 0.0
        video_data['traffic_sources'] = []

    return video_data

# -----------------------------------------------------------------------------
# 3. Gemini 분석
# -----------------------------------------------------------------------------
def analyze_with_gemini(data):
    model = genai.GenerativeModel('gemini-2.0-flash')
    
    # 권한은 있는데 데이터가 0인 경우 vs 권한이 아예 없는 경우
    is_permission_error = not data.get('permission_ok', False)
    
    base_info = f"""
    [영상 정보]
    - 제목: {data['title']}
    - 채널명: {data['channel_title']}
    - 조회수(Data API): {data['realtime_views']}회
    - 좋아요: {data['likes']}개
    """
    
    if is_permission_error:
        prompt = f"""
        당신은 유튜브 컨설턴트입니다.
        현재 **계정 권한 문제(로그인된 계정과 채널 불일치)**로 인해 상세 시청 시간 데이터를 불러오지 못했습니다.
        
        {base_info}
        
        [지시사항]
        '시청 시간' 분석은 생략하고, **현재 확보된 조회수, 좋아요, 제목**을 바탕으로 다음을 분석해주세요.
        1. **초기 반응 분석**: 조회수와 좋아요 수를 기반으로 시청자 호응도 평가.
        2. **제목 매력도 진단**: 제목이 클릭을 유도하는지 분석하고 개선 아이디어 3가지 제안.
        3. **브랜드 계정 전환 안내**: 분석 마지막에 "정확한 시청 시간 분석을 위해 영상 소유 계정으로 로그인해주세요"라는 멘트 추가.
        """
    elif data['watch_time_min'] == 0:
        # 권한은 있으나 데이터가 아직 집계 안 된 경우
        prompt = f"""
        당신은 유튜브 컨설턴트입니다.
        현재 상세 데이터가 유튜브 서버에서 집계 중(지연)인 상태입니다.
        
        {base_info}
        
        [지시사항]
        시청 시간 0분은 데이터 지연 때문이므로 부정적으로 평가하지 마십시오.
        대신 **제목 키워드 분석, 썸네일(제목 기반 유추) 개선 전략, 초기 홍보 방안** 위주로 제안해주세요.
        """
    else:
        # 정상 데이터
        prompt = f"""
        당신은 유튜브 데이터 분석가입니다.
        
        {base_info}
        [상세 통계]
        - 시청 시간: {data['watch_time_min']:.1f}분
        - 평균 지속 시간: {data['avg_duration_sec']:.1f}초
        - 유입 경로: {data['traffic_sources']}
        
        [지시사항]
        1. **몰입도 진단**: 평균 지속 시간을 평가하고 이탈을 막을 편집 전략 제안.
        2. **알고리즘 분석**: 트래픽 소스를 분석하여 노출 확대 전략 제안.
        3. **액션 플랜**: 채널 성장을 위한 구체적 실행 방안 3가지.
        """

    with st.spinner('Gemini 2.0 Flash가 분석 중입니다... 🧠'):
        try:
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            return f"AI 분석 중 오류: {e}"

# -----------------------------------------------------------------------------
# 4. 메인 로직
# -----------------------------------------------------------------------------
def main():
    st.title("📊 YouTube AI 인사이트 분석기 Pro")

    if "creds" not in st.session_state:
        st.session_state.creds = None

    # A. OAuth 콜백 처리
    if st.query_params.get("code"):
        flow = get_flow()
        flow.fetch_token(code=st.query_params.get("code"))
        st.session_state.creds = flow.credentials
        st.query_params.clear()

    # B. 로그인/로그아웃 관리
    if not st.session_state.creds:
        st.info("분석할 영상의 소유 계정(브랜드 채널)으로 로그인해주세요.")
        # [핵심] prompt='select_account'를 추가하여 매번 계정 선택창 강제 호출
        auth_url, _ = get_flow().authorization_url(prompt='consent select_account')
        st.link_button("Google 계정으로 로그인", auth_url, type="primary")
        return

    # 로그인 정보 표시
    user_channel = get_authenticated_channel_info(st.session_state.creds)
    with st.sidebar:
        if user_channel:
            st.image(user_channel['thumbnail'], width=50)
            st.success(f"로그인: **{user_channel['title']}**")
        
        # 계정 변경 버튼 (강제로 선택창 띄우기)
        auth_url_switch, _ = get_flow().authorization_url(prompt='consent select_account')
        st.link_button("🔄 다른 계정으로 전환", auth_url_switch)

    # C. 분석 시작
    video_url = st.text_input("분석할 내 영상 URL", placeholder="https://youtube.com/watch?v=...")
    
    if video_url and st.button("분석 시작", type="primary"):
        video_id = get_video_id(video_url)
        if not video_id:
            st.error("올바르지 않은 URL입니다.")
            return

        # 1. 기본 정보 조회 (제목, 소유자ID 확인)
        basic_data = get_video_basic_info(st.session_state.creds, video_id)
        if not basic_data:
            st.error("영상을 찾을 수 없습니다.")
            return

        # 2. Analytics 데이터 조회 시도 (ID 체크 없이 일단 시도!)
        full_data = get_analytics_data_safe(st.session_state.creds, basic_data)

        # 3. 결과에 따른 UI 분기 처리
        st.divider()
        st.subheader(f"🎬 {full_data['title']}")

        # Case 1: 데이터 호출 성공 (ID가 달라도 권한이 있어서 가져온 경우 포함)
        if full_data.get('permission_ok'):
            # 계정 일치 확인 메시지 (성공했으면 일치하는 것으로 간주)
            st.toast(f"✅ 인증 성공! '{full_data['channel_title']}' 채널 데이터를 불러왔습니다.", icon="🎉")
            
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("조회수", f"{full_data['realtime_views']:,}")
            m2.metric("총 시청 시간", f"{full_data['watch_time_min']:.1f}분")
            m3.metric("평균 지속 시간", f"{full_data['avg_duration_sec']:.0f}초")
            m4.metric("좋아요", f"{full_data['likes']}")

            # 데이터가 0이면(집계 중) 안내
            if full_data['watch_time_min'] == 0:
                 st.info("ℹ️ 권한은 확인되었으나, 유튜브 서버에서 아직 시청 시간 데이터를 집계 중입니다. (업로드 직후 or 조회수 저조 시 발생)")

        # Case 2: 권한 없음 (403 Error) - 진짜 계정 불일치
        else:
            st.error("🚫 **데이터 접근 권한 없음 (계정 불일치)**")
            
            err_col1, err_col2 = st.columns(2)
            with err_col1:
                st.warning(f"현재 로그인:\n**{user_channel['title'] if user_channel else '확인 불가'}**")
            with err_col2:
                st.error(f"영상 소유 계정:\n**{basic_data['channel_title']}**")

            st.markdown(f"""
            ---
            **[해결 방법]**
            현재 로그인된 계정으로는 이 영상의 통계를 볼 수 없습니다.
            아래 버튼을 눌러 **'{basic_data['channel_title']}'** 브랜드 계정을 정확히 선택하여 다시 로그인하세요.
            """)
            
            # 재로그인 유도 버튼
            auth_url_retry, _ = get_flow().authorization_url(prompt='consent select_account')
            st.link_button(f"🔄 '{basic_data['channel_title']}' 계정으로 다시 로그인", auth_url_retry, type="primary")

            # 기본 데이터만 보여줌
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("조회수", f"{full_data['realtime_views']:,}")
            m2.metric("총 시청 시간", "권한 없음", delta="계정 확인", delta_color="off")
            m3.metric("평균 지속 시간", "권한 없음", delta="계정 확인", delta_color="off")
            m4.metric("좋아요", f"{full_data['likes']}")

        # 4. AI 분석 실행
        st.divider()
        st.markdown("### 🤖 Gemini 2.0 Flash 분석 리포트")
        result = analyze_with_gemini(full_data)
        st.markdown(result)

if __name__ == "__main__":
    main()
