import streamlit as st
import google.generativeai as genai
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import re

# -----------------------------------------------------------------------------
# 1. 기본 설정 및 인증 (Secrets 관리)
# -----------------------------------------------------------------------------
st.set_page_config(page_title="YouTube AI Analyst Pro", layout="wide", page_icon="📺")

# Streamlit Secrets 로드
try:
    client_config = st.secrets["web"]
    gemini_key = st.secrets["GEMINI_API_KEY"]
except KeyError:
    st.error("🚨 Secrets 설정이 누락되었습니다.")
    st.stop()

# Gemini 설정 (요청하신 gemini-2.5-pro 적용)
# 만약 2.5 모델 접근 권한 문제 발생 시 2.0-flash 등으로 자동 변경 고려 가능
genai.configure(api_key=gemini_key)

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
# 2. 데이터 추출 및 정합성 체크 함수
# -----------------------------------------------------------------------------
def get_video_id(url):
    video_id = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', url)
    return video_id.group(1) if video_id else None

def get_video_data(creds, video_id):
    youtube = build('youtube', 'v3', credentials=creds)
    analytics = build('youtubeAnalytics', 'v2', credentials=creds)

    # [Step 1] Data API: 실시간 메타데이터 (가장 정확한 기준값)
    try:
        video_response = youtube.videos().list(
            part='snippet,statistics,contentDetails',
            id=video_id
        ).execute()
        
        if not video_response['items']: return None
        
        item = video_response['items'][0]
        snippet = item['snippet']
        stats = item['statistics']
        
        # 기본 정보
        data = {
            "id": video_id,
            "title": snippet['title'],
            "published_at": snippet['publishedAt'], # ISO 8601
            "publish_date_str": snippet['publishedAt'][:10], # YYYY-MM-DD
            "thumbnail": snippet['thumbnails']['maxres']['url'] if 'maxres' in snippet['thumbnails'] else snippet['thumbnails']['high']['url'],
            "channel_title": snippet['channelTitle'],
            # 실시간 수치
            "realtime_views": int(stats.get('viewCount', 0)),
            "likes": int(stats.get('likeCount', 0)),
            "comments": int(stats.get('commentCount', 0)),
        }
    except Exception as e:
        st.error(f"Data API 호출 오류: {e}")
        return None

    # [Step 2] Analytics API: 시청 시간 (지연 발생 가능)
    # 전략: 업로드 날짜부터 '오늘'까지 조회하되, 데이터가 없으면 0으로 받아옴
    start_date = data['publish_date_str']
    end_date = datetime.now().strftime('%Y-%m-%d')

    try:
        analytics_res = analytics.reports().query(
            ids='channel==MINE',
            startDate=start_date,
            endDate=end_date,
            metrics='views,estimatedMinutesWatched,averageViewDuration',
            filters=f'video=={video_id}'
        ).execute()
        
        # 트래픽 소스
        traffic_res = analytics.reports().query(
            ids='channel==MINE',
            startDate=start_date,
            endDate=end_date,
            metrics='views',
            dimensions='insightTrafficSourceType',
            filters=f'video=={video_id}',
            sort='-views'
        ).execute()

        # 데이터 파싱
        if analytics_res.get('rows'):
            row = analytics_res['rows'][0]
            data['analytics_views'] = row[0] # 집계된 조회수 (실시간보다 적음)
            data['watch_time_min'] = row[1]
            data['avg_duration_sec'] = row[2]
            data['has_analytics_data'] = True
        else:
            # 데이터가 아직 집계되지 않음
            data['analytics_views'] = 0
            data['watch_time_min'] = 0.0
            data['avg_duration_sec'] = 0.0
            data['has_analytics_data'] = False
            
        data['traffic_sources'] = traffic_res.get('rows', [])

    except Exception as e:
        # 권한 문제나 API 오류 시
        data['analytics_views'] = 0
        data['watch_time_min'] = 0.0
        data['avg_duration_sec'] = 0.0
        data['has_analytics_data'] = False
        data['traffic_sources'] = []
        
    return data

# -----------------------------------------------------------------------------
# 3. Gemini 분석 요청 함수 (상황별 프롬프트 분기 처리)
# -----------------------------------------------------------------------------
def analyze_with_gemini(data):
    # 모델 설정 (Gemini 2.5 Pro)
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
    except:
        # 2.5가 아직 정식 배포 전 지역이거나 권한 없을 경우 2.0 Flash로 폴백
        model = genai.GenerativeModel('gemini-2.0-flash')

    # [핵심 로직] 데이터 상태에 따른 프롬프트 분기
    is_early_stage = False
    
    # 조회수는 있는데 시청 시간이 0이거나, 집계된 조회수가 실시간의 10% 미만인 경우 -> "집계 중"으로 판단
    if data['realtime_views'] > 0 and (not data['has_analytics_data'] or data['analytics_views'] < data['realtime_views'] * 0.1):
        is_early_stage = True
        
    # --- 프롬프트 구성 ---
    base_info = f"""
    [영상 정보]
    - 제목: {data['title']}
    - 게시일: {data['published_at']}
    - 채널명: {data['channel_title']}
    
    [확정된 실시간 지표 (Data API)]
    - 누적 조회수: {data['realtime_views']}회
    - 좋아요: {data['likes']}개
    - 댓글: {data['comments']}개
    """
    
    if is_early_stage:
        # 시나리오 A: 데이터 집계 지연 상태 (초기 영상)
        prompt = f"""
        당신은 유튜브 전문 컨설턴트입니다. 
        현재 이 영상은 **게시된 지 얼마 되지 않아 상세 통계(시청 시간, 평균 지속 시간)가 유튜브 서버에서 집계 중인 상태**입니다.
        
        따라서 `시청 시간`이나 `이탈률`이 0이거나 매우 낮게 표시될 수 있는데, **이것은 성과가 나쁜 것이 아니라 데이터 집계 지연 때문입니다.**
        
        {base_info}
        
        [지시 사항]
        위 상황을 인지하고, **'시청 지속 시간'이 낮다는 비판은 절대 하지 마십시오.** (데이터가 없기 때문입니다.)
        대신 현재 확보된 `조회수`, `좋아요`, `제목/썸네일(텍스트)` 정보를 바탕으로 다음 내용을 분석해주세요.
        
        1. **초기 반응 분석**: 조회수 {data['realtime_views']}회 대비 좋아요 {data['likes']}개 ({data['likes']/data['realtime_views']*100:.1f}%)의 비율이 적절한지 평가.
        2. **매력도 진단**: 제목 "{data['title']}"이 클릭을 유도하기에 충분히 매력적인지, 키워드는 적절한지 분석.
        3. **확산 전략**: 상세 데이터가 잡히기 전인 지금(골든타임)에 외부 유입을 늘리기 위해 무엇을 해야 할지 구체적인 홍보/공유 팁 제공.
        """
    else:
        # 시나리오 B: 데이터가 충분한 상태
        prompt = f"""
        당신은 유튜브 전문 데이터 분석가입니다. 상세 데이터가 확보된 영상을 분석합니다.
        
        {base_info}
        
        [상세 통계 (Analytics API)]
        - 총 시청 시간: {data['watch_time_min']:.1f}분
        - 평균 시청 지속 시간: {data['avg_duration_sec']:.1f}초
        - 유입 경로: {data['traffic_sources']}
        
        [지시 사항]
        1. **성과 진단**: 조회수와 시청 지속 시간의 상관관계를 분석하고, 영상의 몰입도를 평가해주세요.
        2. **유입 분석**: 트래픽 소스를 기반으로 현재 알고리즘의 선택을 받고 있는지 진단해주세요.
        3. **개선 솔루션**: 클릭률(CTR)과 시청 지속 시간(Retention)을 동시에 높일 수 있는 구체적인 편집/기획 조언을 해주세요.
        """

    with st.spinner('Gemini가 데이터의 유효성을 검토하고 사고 모드로 분석 중입니다... 🧠'):
        try:
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            return f"분석 중 오류 발생: {e}"

# -----------------------------------------------------------------------------
# 4. 메인 UI
# -----------------------------------------------------------------------------
def main():
    st.title("📊 YouTube AI 인사이트 분석기 Pro")
    
    if "creds" not in st.session_state:
        st.session_state.creds = None

    # 로그인 처리
    if st.query_params.get("code"):
        flow = get_flow()
        flow.fetch_token(code=st.query_params.get("code"))
        st.session_state.creds = flow.credentials
        st.query_params.clear()

    if not st.session_state.creds:
        auth_url, _ = get_flow().authorization_url(prompt='consent')
        st.info("로그인이 필요합니다.")
        st.link_button("구글 계정으로 로그인", auth_url, type="primary")
        return

    # 메인 화면
    with st.sidebar:
        st.success("로그인 완료 ✅")
        if st.button("로그아웃"):
            st.session_state.creds = None
            st.rerun()

    video_url = st.text_input("분석할 YouTube 영상 URL", placeholder="https://youtube.com/watch?v=...")

    if video_url and st.button("분석 시작", type="primary"):
        video_id = get_video_id(video_url)
        if not video_id:
            st.error("올바르지 않은 URL입니다.")
            return

        with st.status("데이터를 수집 및 검증 중입니다...", expanded=True) as status:
            data = get_video_data(st.session_state.creds, video_id)
            
            if not data:
                st.error("데이터 조회 실패. 본인 채널 영상이 맞나요?")
                status.update(state="error")
                return
            
            status.update(label="데이터 수집 완료!", state="complete")

        # --- 데이터 대시보드 (사용자가 직관적으로 데이터 상태 확인) ---
        st.divider()
        st.subheader(f"🎬 {data['title']}")
        
        col1, col2, col3, col4 = st.columns(4)
        
        # 1. 실시간 조회수 (가장 신뢰)
        col1.metric("실시간 조회수", f"{data['realtime_views']}회")
        
        # 2. 시청 시간 (상태에 따라 표시 변경)
        if data['has_analytics_data'] and data['watch_time_min'] > 0:
            col2.metric("총 시청 시간", f"{data['watch_time_min']:.1f}분")
            col3.metric("평균 지속 시간", f"{data['avg_duration_sec']:.0f}초")
            data_status = "✅ 분석 가능"
        else:
            col2.metric("총 시청 시간", "집계 중 (대기)", delta="API 지연", delta_color="off")
            col3.metric("평균 지속 시간", "집계 중 (대기)", delta="API 지연", delta_color="off")
            data_status = "⚠️ 상세 데이터 지연"

        col4.metric("좋아요", f"{data['likes']}개")

        # 경고 메시지 표시
        if data_status == "⚠️ 상세 데이터 지연":
            st.warning("""
            **📢 데이터 집계 알림:**
            현재 유튜브 API에서 상세 통계(시청 시간 등)가 아직 넘어오지 않았습니다. (보통 업로드 후 24~48시간 소요)
            
            👉 **따라서 Gemini가 '시청 시간 0분'을 '성과 부족'으로 오해하지 않도록, 
            현재 확인된 '조회수/좋아요/제목' 위주로 초기 반응 전략을 분석하도록 지시했습니다.**
            """)

        st.divider()
        st.markdown("### 🤖 Gemini 2.5 Pro 분석 리포트")
        result = analyze_with_gemini(data)
        st.markdown(result)

if __name__ == "__main__":
    main()
