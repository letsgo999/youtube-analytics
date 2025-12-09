import streamlit as st
import google.generativeai as genai
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import pandas as pd
from datetime import datetime, timedelta
import re

# -----------------------------------------------------------------------------
# 1. 기본 설정 및 인증 (Secrets 관리)
# -----------------------------------------------------------------------------
st.set_page_config(page_title="YouTube AI Analyst Pro", layout="wide", page_icon="📈")

# Streamlit Secrets에서 설정 불러오기
try:
    client_config = st.secrets["web"]
    gemini_key = st.secrets["GEMINI_API_KEY"]
except KeyError:
    st.error("🚨 Secrets 설정이 누락되었습니다. 스트림릿 대시보드에서 설정을 확인해주세요.")
    st.stop()

# Gemini 설정 (최신 모델 2.5 Pro 적용)
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
# 2. 데이터 추출 함수 (Data API + Analytics API 하이브리드)
# -----------------------------------------------------------------------------
def get_video_id(url):
    """URL에서 Video ID 추출"""
    video_id = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', url)
    return video_id.group(1) if video_id else None

def get_video_data(creds, video_id):
    """
    Data API(실시간)와 Analytics API(상세/지연)를 모두 사용하여 
    가장 정확한 데이터를 조합합니다.
    """
    youtube = build('youtube', 'v3', credentials=creds)
    analytics = build('youtubeAnalytics', 'v2', credentials=creds)

    # [Step 1] Data API: 실시간 기본 정보 조회 (조회수, 좋아요, 댓글 등)
    # 이 API는 지연 없이 현재 보이는 숫자를 그대로 가져옵니다.
    video_response = youtube.videos().list(
        part='snippet,statistics,contentDetails',
        id=video_id
    ).execute()

    if not video_response['items']:
        return None

    item = video_response['items'][0]
    snippet = item['snippet']
    stats = item['statistics']
    
    # 기본 메타데이터
    video_info = {
        "id": video_id,
        "title": snippet['title'],
        "published_at": snippet['publishedAt'], # ISO format
        "publish_date": snippet['publishedAt'][:10], # YYYY-MM-DD
        "channel_title": snippet['channelTitle'],
        "thumbnail": snippet['thumbnails']['high']['url'],
        # Data API 수치 (가장 정확한 현재 값)
        "view_count": int(stats.get('viewCount', 0)),
        "like_count": int(stats.get('likeCount', 0)),
        "comment_count": int(stats.get('commentCount', 0)),
    }

    # [Step 2] Analytics API: 시청 시간 및 상세 지표 조회
    # 주의: 이 API는 24~48시간 지연될 수 있습니다.
    
    # 조회 기간 설정 (게시일 ~ 어제)
    # 오늘 날짜를 endDate로 하면 데이터 집계 중이라 0이 나올 확률이 높음 -> 어제로 설정
    start_date = video_info['publish_date']
    end_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    
    # 만약 게시일이 오늘이면 start=오늘, end=오늘로 시도
    if start_date > end_date:
        end_date = start_date

    try:
        # 주요 지표 조회
        analytics_main = analytics.reports().query(
            ids='channel==MINE',
            startDate=start_date,
            endDate=end_date,
            metrics='views,estimatedMinutesWatched,averageViewDuration',
            filters=f'video=={video_id}'
        ).execute()

        # 트래픽 소스 조회
        analytics_traffic = analytics.reports().query(
            ids='channel==MINE',
            startDate=start_date,
            endDate=end_date,
            metrics='views',
            dimensions='insightTrafficSourceType',
            filters=f'video=={video_id}',
            sort='-views'
        ).execute()
        
        # Analytics 데이터 파싱 (데이터가 없으면 0 처리)
        if analytics_main.get('rows'):
            row = analytics_main['rows'][0]
            video_info['analytics_views'] = row[0] # 집계된 조회수 (Data API보다 적을 수 있음)
            video_info['watch_time_min'] = row[1]  # 분 단위
            video_info['avg_duration_sec'] = row[2] # 초 단위
        else:
            video_info['analytics_views'] = 0
            video_info['watch_time_min'] = 0
            video_info['avg_duration_sec'] = 0
            
        video_info['traffic_sources'] = analytics_traffic.get('rows', [])
        video_info['analysis_period'] = f"{start_date} ~ {end_date}"

    except Exception as e:
        st.error(f"Analytics API 호출 중 오류 발생: {e}")
        # 오류 발생 시 기본값 채움
        video_info['analytics_views'] = 0
        video_info['watch_time_min'] = 0
        video_info['avg_duration_sec'] = 0
        video_info['traffic_sources'] = []
        video_info['analysis_period'] = "데이터 없음"

    return video_info

# -----------------------------------------------------------------------------
# 3. Gemini 분석 요청 함수 (Gemini 2.5 Pro)
# -----------------------------------------------------------------------------
def analyze_with_gemini(data):
    # Gemini 2.5 Pro 모델 사용
    model = genai.GenerativeModel('gemini-2.5-pro')
    
    # 조회수 불일치에 대한 맥락 설명 추가
    view_context = ""
    if data['view_count'] > 0 and data['watch_time_min'] == 0:
        view_context = "(참고: 현재 누적 조회수는 있으나, 유튜브 상세 통계 집계 지연으로 인해 시청 시간 데이터가 아직 0으로 잡히는 상황일 수 있습니다. 이 점을 감안하여 분석해주세요.)"

    prompt = f"""
    당신은 유튜브 알고리즘 및 데이터 분석 전문가입니다. 
    아래 제공된 유튜브 동영상 데이터를 바탕으로 심층 분석을 수행하고, 성과 개선을 위한 구체적인 전략을 제안해주세요.

    [영상 기본 정보]
    - 제목: {data['title']}
    - 게시일: {data['publish_date']}
    - 분석 시점: {datetime.now().strftime('%Y-%m-%d %H:%M')}
    
    [핵심 성과 데이터]
    - 누적 조회수 (실시간): {data['view_count']}회
    - 누적 시청 시간: {data['watch_time_min']:.1f}분 {view_context}
    - 평균 시청 지속 시간: {data['avg_duration_sec']:.1f}초
    - 좋아요 수: {data['like_count']}개
    - 댓글 수: {data['comment_count']}개
    
    [트래픽 소스 (유입 경로)]
    - {data['traffic_sources']}

    [요청 사항]
    1. **데이터 진단**: 위 수치를 바탕으로 현재 영상의 성과를 냉정하게 평가해주세요. (조회수 대비 반응률, 시청 지속 시간의 적절성 등)
    2. **문제점 발견**: 왜 조회수나 시청 시간이 이 수준인지, 트래픽 소스를 근거로 분석해주세요.
    3. **개선 솔루션**:
       - 클릭률(CTR)을 높이기 위한 **제목 및 썸네일 개선안** 3가지 (구체적인 카피라이팅 포함)
       - 시청 지속 시간을 늘리기 위한 **영상 내 구성/편집 제안**
       - 댓글 등 참여를 유도하기 위한 **구체적인 행동 지침(Call to Action)**
    
    분석 결과는 가독성 좋은 마크다운 형식으로 작성해주시고, 중요한 부분은 볼드체로 강조해주세요.
    """
    
    with st.spinner('Gemini 2.5 Pro가 데이터를 깊이 있게 분석 중입니다... 🧠'):
        try:
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            return f"Gemini 분석 중 오류가 발생했습니다: {e}"

# -----------------------------------------------------------------------------
# 4. 메인 UI 로직
# -----------------------------------------------------------------------------
def main():
    st.title("📊 YouTube AI 인사이트 분석기 Pro")
    st.markdown("---")

    # 세션 상태 초기화
    if "creds" not in st.session_state:
        st.session_state.creds = None

    # A. 인증 처리 (OAuth)
    if st.query_params.get("code"):
        try:
            flow = get_flow()
            flow.fetch_token(code=st.query_params.get("code"))
            st.session_state.creds = flow.credentials
            st.query_params.clear()
        except Exception as e:
            st.error(f"로그인 처리 중 오류 발생: {e}")

    # B. 로그인 버튼 표시
    if not st.session_state.creds:
        col1, col2 = st.columns([1, 2])
        with col1:
            st.info("👋 먼저 Google 계정으로 로그인해주세요.")
            flow = get_flow()
            auth_url, _ = flow.authorization_url(prompt='consent')
            st.link_button("Google 계정으로 로그인", auth_url, type="primary")
        return

    # C. 메인 분석 화면
    with st.sidebar:
        st.success("로그인 완료! ✅")
        if st.button("로그아웃"):
            st.session_state.creds = None
            st.rerun()
    
    st.write("분석할 내 채널의 동영상 URL을 입력하세요.")
    video_url = st.text_input("Video URL", placeholder="https://www.youtube.com/watch?v=...")

    if video_url:
        video_id = get_video_id(video_url)
        if not video_id:
            st.error("올바르지 않은 YouTube URL입니다.")
            return

        if st.button("데이터 호출 및 분석 시작", type="primary"):
            try:
                # 1. 데이터 가져오기
                with st.status("YouTube API에서 데이터를 불러오는 중...", expanded=True) as status:
                    st.write("📡 Data API 접속 중... (실시간 조회수)")
                    video_data = get_video_data(st.session_state.creds, video_id)
                    
                    if not video_data:
                        status.update(label="데이터를 찾을 수 없습니다.", state="error")
                        st.error("본인 채널의 영상이 맞는지, 혹은 영상이 공개 상태인지 확인해주세요.")
                        return
                    
                    st.write("📊 Analytics API 접속 중... (시청 시간 및 트래픽)")
                    status.update(label="데이터 로드 완료!", state="complete")

                # 2. 데이터 검증 대시보드 (사용자 요청 사항 반영)
                st.markdown("### 1️⃣ 데이터 정합성 확인 (Data Check)")
                st.info("분석 전, 아래 데이터가 유튜브 스튜디오와 일치하는지 먼저 확인하세요.")
                
                # 메타데이터 표시
                meta_col1, meta_col2 = st.columns(2)
                with meta_col1:
                    st.image(video_data['thumbnail'], use_container_width=True)
                with meta_col2:
                    st.subheader(video_data['title'])
                    st.caption(f"채널명: {video_data['channel_title']}")
                    st.text(f"📅 업로드 날짜: {video_data['published_at']}")
                    st.text(f"🕒 분석 대상 기간: {video_data['analysis_period']}")
                    st.text(f"🔍 분석 실행 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

                st.markdown("#### 📈 핵심 지표 (Key Metrics)")
                metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)
                
                # 누적 조회수 (Data API - 가장 정확)
                metric_col1.metric(
                    label="누적 조회수 (실시간)", 
                    value=f"{video_data['view_count']:,}회",
                    help="YouTube Data API 기준 현재 외부 노출 조회수입니다."
                )
                
                # 누적 시청 시간 (Analytics API)
                watch_time_display = f"{video_data['watch_time_min']/60:.1f}시간" if video_data['watch_time_min'] > 0 else "집계 중 (0시간)"
                metric_col2.metric(
                    label="누적 시청 시간", 
                    value=watch_time_display,
                    help="Analytics API 기준. 최근 48시간 데이터는 아직 반영되지 않았을 수 있습니다."
                )
                
                # 평균 시청 시간
                avg_duration_display = f"{video_data['avg_duration_sec']:.0f}초" if video_data['avg_duration_sec'] > 0 else "집계 중"
                metric_col3.metric(
                    label="평균 시청 지속 시간", 
                    value=avg_duration_display
                )

                # 지난 48시간 조회수 (대체 지표)
                # API 제한으로 인해 '지난 48시간' 전용 데이터는 못 가져오지만, 
                # 현재 누적 조회수가 0이 아니라는 점으로 데이터 연결 상태를 보여줍니다.
                metric_col4.metric(
                    label="데이터 연결 상태", 
                    value="정상" if video_data['view_count'] > 0 else "대기 중",
                    delta="API 연결됨",
                    help="공식 API는 '지난 48시간 조회수' 그래프 데이터를 제공하지 않습니다. 대신 실시간 누적 조회수로 연결을 확인합니다."
                )

                # 데이터가 너무 적을 경우 경고
                if video_data['view_count'] > 0 and video_data['analytics_views'] == 0:
                    st.warning("⚠️ 알림: 현재 '누적 조회수'는 확인되나, 상세 통계(시청 시간 등)는 유튜브 서버에서 아직 집계 중입니다. (보통 업로드 후 24~48시간 소요) \n\nGemini가 현재 확인된 조회수 정보를 바탕으로 최대한 분석을 진행합니다.")

                st.markdown("---")

                # 3. Gemini 분석 결과
                st.markdown("### 2️⃣ Gemini 2.5 Pro 심층 분석 리포트")
                result = analyze_with_gemini(video_data)
                st.markdown(result)

            except Exception as e:
                st.error(f"시스템 오류 발생: {e}")

if __name__ == "__main__":
    main()
