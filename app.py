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

# Streamlit Secrets 로드
try:
    client_config = st.secrets["web"]
    gemini_key = st.secrets["GEMINI_API_KEY"]
except KeyError:
    st.error("🚨 Secrets 설정이 누락되었습니다. 스트림릿 대시보드에서 설정을 확인해주세요.")
    st.stop()

# Gemini 설정 (최신 모델: gemini-2.0-flash 권장)
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
# 2. 데이터 추출 및 검증 함수
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
                'title': item['snippet']['title']
            }
    except Exception as e:
        return None
    return None

def get_video_basic_info(creds, video_id):
    """
    영상 소유권 확인을 위해 '기본 정보(제목, 채널ID)'만 빠르게 먼저 조회합니다.
    """
    try:
        youtube = build('youtube', 'v3', credentials=creds)
        response = youtube.videos().list(
            part='snippet,statistics',
            id=video_id
        ).execute()
        
        if not response['items']: return None
        
        item = response['items'][0]
        snippet = item['snippet']
        return {
            "id": video_id,
            "title": snippet['title'],
            "channel_title": snippet['channelTitle'],
            "channel_id": snippet['channelId'], # 소유주 ID
            "published_at": snippet['publishedAt'],
            "thumbnail": snippet['thumbnails']['maxres']['url'] if 'maxres' in snippet['thumbnails'] else snippet['thumbnails']['high']['url'],
            "realtime_views": int(item['statistics'].get('viewCount', 0)),
            "likes": int(item['statistics'].get('likeCount', 0)),
            "comments": int(item['statistics'].get('commentCount', 0))
        }
    except Exception as e:
        st.error(f"영상 정보 조회 실패: {e}")
        return None

def get_analytics_data(creds, video_data):
    """
    검증이 끝난 후, 실제 Analytics API(시청 시간 등)를 조회합니다.
    """
    analytics = build('youtubeAnalytics', 'v2', credentials=creds)
    
    start_date = video_data['published_at'][:10]
    end_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    if start_date > end_date: end_date = start_date

    try:
        # 주요 지표
        analytics_res = analytics.reports().query(
            ids='channel==MINE',
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
            video_data['has_analytics_data'] = True
        else:
            video_data['analytics_views'] = 0
            video_data['watch_time_min'] = 0.0
            video_data['avg_duration_sec'] = 0.0
            video_data['has_analytics_data'] = False
            
        video_data['traffic_sources'] = traffic_res.get('rows', [])

    except Exception as e:
        # 권한 오류 또는 데이터 없음
        video_data['has_analytics_data'] = False
        video_data['error_msg'] = str(e)
        video_data['traffic_sources'] = []

    return video_data

# -----------------------------------------------------------------------------
# 3. Gemini 분석 (gemini-2.0-flash)
# -----------------------------------------------------------------------------
def analyze_with_gemini(data):
    model = genai.GenerativeModel('gemini-2.0-flash')
    
    # 데이터 상태에 따른 프롬프트 분기
    is_missing = not data.get('has_analytics_data', False)
    
    base_info = f"""
    [영상 정보]
    - 제목: {data['title']}
    - 게시일: {data['published_at'][:10]}
    - 채널명: {data['channel_title']}
    - 조회수(Data API): {data['realtime_views']}회
    - 좋아요: {data['likes']}개
    """
    
    if is_missing:
        prompt = f"""
        당신은 유튜브 컨설턴트입니다. 
        현재 이 영상은 상세 통계(시청 시간)가 집계되지 않았거나 지연 중인 상태입니다.
        
        {base_info}
        
        [지시사항]
        시청 시간 데이터가 없으므로 '이탈률'이나 '지속 시간'에 대한 비판은 하지 마십시오.
        대신 **조회수, 좋아요 수, 제목의 매력도**를 중심으로 아래 내용을 분석해주세요.
        1. **초기 반응**: 조회수 대비 좋아요 비율을 분석하여 시청자 만족도 추정.
        2. **제목/썸네일 진단**: 제목이 클릭을 유도하는지, 키워드는 적절한지 피드백.
        3. **확산 전략**: 초기 노출을 늘리기 위해 지금 당장 할 수 있는 홍보 전략 3가지.
        """
    else:
        prompt = f"""
        당신은 유튜브 데이터 분석가입니다. 상세 데이터를 바탕으로 분석해주세요.
        
        {base_info}
        
        [상세 통계]
        - 총 시청 시간: {data['watch_time_min']:.1f}분
        - 평균 시청 지속 시간: {data['avg_duration_sec']:.1f}초
        - 유입 경로: {data['traffic_sources']}
        
        [지시사항]
        1. **성과 진단**: 시청 지속 시간을 기반으로 영상의 몰입도(Retention) 평가.
        2. **유입 분석**: 트래픽 소스를 분석하여 현재 알고리즘의 평가 진단.
        3. **액션 플랜**: 조회수와 시청 시간을 동시에 늘릴 수 있는 구체적 개선안 제안.
        """

    with st.spinner('Gemini 2.0 Flash가 데이터를 분석 중입니다... 🧠'):
        try:
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            return f"분석 중 오류 발생: {e}"

# -----------------------------------------------------------------------------
# 4. 메인 UI 및 로직
# -----------------------------------------------------------------------------
def main():
    st.title("📊 YouTube AI 인사이트 분석기 Pro")
    
    if "creds" not in st.session_state:
        st.session_state.creds = None

    # A. OAuth 인증 처리
    if st.query_params.get("code"):
        flow = get_flow()
        flow.fetch_token(code=st.query_params.get("code"))
        st.session_state.creds = flow.credentials
        st.query_params.clear()

    # B. 로그인 전 화면
    if not st.session_state.creds:
        st.info("👋 분석을 시작하려면 YouTube 계정으로 로그인하세요.")
        auth_url, _ = get_flow().authorization_url(prompt='consent')
        st.link_button("Google 계정으로 로그인", auth_url, type="primary")
        return

    # C. 로그인 후 - 사용자 정보 확인
    user_channel = get_authenticated_channel_info(st.session_state.creds)
    
    with st.sidebar:
        if user_channel:
            st.success(f"로그인 됨: **{user_channel['title']}**")
        else:
            st.error("채널 정보를 불러올 수 없음")
            
        if st.button("로그아웃 (계정 변경)"):
            st.session_state.creds = None
            st.rerun()

    # D. URL 입력 및 분석 시작
    video_url = st.text_input("분석할 영상 URL 입력", placeholder="https://youtube.com/watch?v=...")
    
    if video_url and st.button("분석 시작", type="primary"):
        video_id = get_video_id(video_url)
        if not video_id:
            st.error("올바르지 않은 URL입니다.")
            return

        # 1. 영상 기본 정보 확인 (소유권 검증용)
        basic_data = get_video_basic_info(st.session_state.creds, video_id)
        
        if not basic_data:
            st.error("영상을 찾을 수 없습니다.")
            return

        # ---------------------------------------------------------
        # 🚨 [핵심] 계정 불일치 검증 로직
        # ---------------------------------------------------------
        is_owner = False
        if user_channel and basic_data['channel_id'] == user_channel['id']:
            is_owner = True
            # ✅ 일치 시 팝업 알림 (Toast)
            st.toast(f"✅ 확인 완료! 영상 소유 계정과 일치합니다.\n({user_channel['title']})", icon="🎉")
            time.sleep(1) # 사용자가 메시지를 볼 수 있게 찰나의 대기
        else:
            # ❌ 불일치 시 경고 및 재로그인 유도
            st.error("🚨 **계정 불일치 경고**")
            
            # 비교 UI
            col_err1, col_err2 = st.columns(2)
            col_err1.warning(f"현재 로그인된 계정:\n**{user_channel['title'] if user_channel else '확인 불가'}**")
            col_err2.error(f"영상 소유 계정:\n**{basic_data['channel_title']}**")
            
            st.markdown(f"""
            ---
            **[문제 해결 방법]**
            현재 로그인된 계정으로는 **'{basic_data['title']}'** 영상의 상세 통계(시청 시간 등)를 볼 권한이 없습니다.
            아래 버튼을 눌러 **'{basic_data['channel_title']}'** 브랜드 계정으로 다시 로그인해주세요.
            """)
            
            # 재로그인(계정 변경) 버튼
            auth_url_retry, _ = get_flow().authorization_url(prompt='consent')
            st.link_button(f"🔄 '{basic_data['channel_title']}' 계정으로 다시 로그인하기", auth_url_retry, type="primary")
            
            # 불일치 상태에서는 더 이상 진행하지 않음 (또는 제한적 분석만 허용)
            st.warning("⚠️ 현재 상태에서는 '조회수' 외의 핵심 데이터(시청 시간)가 0으로 표시됩니다.")
            # 여기서 return을 하면 분석 중단, 아래로 흘려보내면 제한적 분석 수행.
            # 사용자 경험상 멈추고 로그인을 유도하는게 낫지만, 요청하신대로 '데이터를 불러와서 수행'하려면 진행시킴.
            
        
        # 2. 상세 Analytics 데이터 호출 (계정이 맞을 때만 유효한 값이 옴)
        full_data = get_analytics_data(st.session_state.creds, basic_data)
        
        # 3. 결과 대시보드 출력
        st.divider()
        st.subheader(f"🎬 {full_data['title']}")
        
        m_col1, m_col2, m_col3, m_col4 = st.columns(4)
        m_col1.metric("실시간 조회수", f"{full_data['realtime_views']:,}")
        
        if full_data.get('has_analytics_data'):
            m_col2.metric("총 시청 시간", f"{full_data['watch_time_min']:.1f}분")
            m_col3.metric("평균 지속 시간", f"{full_data['avg_duration_sec']:.0f}초")
            status_Badge = "🟢 데이터 정상"
        else:
            m_col2.metric("총 시청 시간", "권한 없음 (0)", delta="계정 불일치", delta_color="off")
            m_col3.metric("평균 지속 시간", "권한 없음 (0)", delta="계정 불일치", delta_color="off")
            status_Badge = "🔴 데이터 누락"
            
        m_col4.metric("좋아요", f"{full_data['likes']}")
        
        # 4. Gemini 분석 실행
        st.divider()
        st.markdown(f"### 🤖 Gemini 2.0 Flash 분석 ({status_Badge})")
        
        result_text = analyze_with_gemini(full_data)
        st.markdown(result_text)

if __name__ == "__main__":
    main()
