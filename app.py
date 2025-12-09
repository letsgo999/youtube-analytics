import streamlit as st
import google.generativeai as genai
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import re

# -----------------------------------------------------------------------------
# 1. 기본 설정 및 인증 (Secrets 관리)
# -----------------------------------------------------------------------------
st.set_page_config(page_title="YouTube AI Analyst Pro", layout="wide", page_icon="📈")

# Streamlit Secrets 로드
try:
    client_config = st.secrets["web"]
    gemini_key = st.secrets["GEMINI_API_KEY"]
except KeyError:
    st.error("🚨 Secrets 설정이 누락되었습니다.")
    st.stop()

# Gemini 설정 (gemini-2.0-flash 사용 - 속도 및 안정성 최적화)
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
# 2. 데이터 추출 및 정합성 체크 함수
# -----------------------------------------------------------------------------
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
        st.error(f"채널 정보 조회 실패: {e}")
    return None

def get_video_id(url):
    video_id = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', url)
    return video_id.group(1) if video_id else None

def get_video_data(creds, video_id):
    youtube = build('youtube', 'v3', credentials=creds)
    analytics = build('youtubeAnalytics', 'v2', credentials=creds)

    # [Step 1] Data API: 실시간 메타데이터 (공개 정보)
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
            "channel_id": snippet['channelId'], # 영상 소유 채널 ID
            # 실시간 수치
            "realtime_views": int(stats.get('viewCount', 0)),
            "likes": int(stats.get('likeCount', 0)),
            "comments": int(stats.get('commentCount', 0)),
        }
    except Exception as e:
        st.error(f"Data API 호출 오류: {e}")
        return None

    # [Step 2] Analytics API: 시청 시간 (비공개 통계)
    # *중요*: 게시일부터 '어제'까지 조회 (오늘 데이터는 미확정일 수 있음)
    start_date = data['publish_date_str']
    end_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    
    # 게시일이 오늘이면 오늘 날짜 사용
    if start_date > end_date:
        end_date = start_date

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
            data['analytics_views'] = row[0] # Analytics 기준 조회수
            data['watch_time_min'] = row[1]
            data['avg_duration_sec'] = row[2]
            data['has_analytics_data'] = True
        else:
            # 데이터가 비어있음 (권한 문제 or 진짜 데이터 없음)
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
        data['error_msg'] = str(e)
        
    return data

# -----------------------------------------------------------------------------
# 3. Gemini 분석 요청 함수
# -----------------------------------------------------------------------------
def analyze_with_gemini(data):
    # 최신 모델 사용 (Gemini 2.0 Flash 권장)
    model = genai.GenerativeModel('gemini-2.0-flash')

    # 데이터 상태에 따른 프롬프트 분기
    is_missing_data = not data['has_analytics_data']
    
    # 기본 정보
    base_info = f"""
    [영상 정보]
    - 제목: {data['title']}
    - 게시일: {data['published_at'][:10]}
    - 채널명: {data['channel_title']}
    
    [확정 지표 (Data API)]
    - 누적 조회수: {data['realtime_views']}회
    - 좋아요: {data['likes']}개
    - 댓글: {data['comments']}개
    """
    
    if is_missing_data:
        # 시나리오 A: 상세 데이터 누락 (권한 문제 등)
        prompt = f"""
        당신은 유튜브 컨설턴트입니다. 
        현재 이 영상은 상세 통계(시청 시간 등)를 불러올 수 없는 상태입니다. (권한 문제 또는 데이터 집계 중)
        
        {base_info}
        
        [지시 사항]
        '시청 시간' 데이터가 없음을 비판하지 마십시오.
        대신 **현재 보이는 조회수, 좋아요 수, 그리고 영상의 제목/썸네일(텍스트)**을 중심으로 다음을 분석해주세요.
        
        1. **초기 성과 진단**: 조회수 대비 좋아요 비율({(data['likes']/data['realtime_views']*100) if data['realtime_views'] > 0 else 0:.1f}%)이 4% 이상인지 확인하여 콘텐츠 만족도를 예측.
        2. **제목/기획 피드백**: 제목이 타겟 시청자의 호기심을 자극하는지, 키워드는 적절한지 구체적으로 조언.
        3. **액션 플랜**: 조회수를 더 끌어올리기 위한 외부 홍보 및 썸네일 개선 전략 제안.
        """
    else:
        # 시나리오 B: 정상 분석
        prompt = f"""
        당신은 전문 유튜브 데이터 분석가입니다. 아래 데이터를 바탕으로 성과를 분석하고 구체적인 개선안을 제안해주세요.
        
        {base_info}
        
        [상세 통계 (Analytics API)]
        - 총 시청 시간: {data['watch_time_min']:.1f}분
        - 평균 시청 지속 시간: {data['avg_duration_sec']:.1f}초
        - 유입 경로: {data['traffic_sources']}
        
        [지시 사항]
        1. **성과 진단**: 조회수 대비 시청 지속 시간을 평가하여 영상의 몰입도(Retention)를 진단해주세요.
        2. **유입 분석**: 어떤 경로(검색, 추천, 외부 등)로 들어오고 있는지 파악하고, 이를 강화할 전략을 제시해주세요.
        3. **개선 솔루션**: 클릭률(CTR)과 시청 시간을 동시에 높일 수 있는 3가지 구체적인 액션 플랜을 제시해주세요.
        """

    with st.spinner('Gemini가 데이터를 분석 중입니다... 🧠'):
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

    # 사이드바: 로그인 정보 확인
    with st.sidebar:
        user_channel = get_authenticated_channel_info(st.session_state.creds)
        if user_channel:
            st.success(f"로그인 됨: {user_channel['title']}")
            st.caption(f"Channel ID: {user_channel['id']}")
        else:
            st.warning("채널 정보를 가져올 수 없습니다.")
            
        if st.button("로그아웃"):
            st.session_state.creds = None
            st.rerun()

    # 메인 화면
    video_url = st.text_input("분석할 내 영상 URL 입력", placeholder="https://youtube.com/watch?v=...")

    if video_url and st.button("분석 시작", type="primary"):
        video_id = get_video_id(video_url)
        if not video_id:
            st.error("올바르지 않은 URL입니다.")
            return

        with st.status("데이터 조회 중...", expanded=True) as status:
            data = get_video_data(st.session_state.creds, video_id)
            
            if not data:
                st.error("데이터 조회 실패. 영상이 존재하지 않거나 공개되지 않았습니다.")
                status.update(state="error")
                return
            
            # [핵심] 계정 권한 불일치 감지 및 경고
            if user_channel and data['channel_id'] != user_channel['id']:
                st.error(f"🚨 **계정 불일치 경고**")
                st.markdown(f"""
                * **로그인된 계정:** `{user_channel['title']}`
                * **영상 소유 계정:** `{data['channel_title']}`
                
                **다른 계정(채널)으로 로그인하셨습니다!** 유튜브 Analytics API는 본인 채널의 통계만 보여주므로, 현재 상태에서는 **조회수 외의 시청 시간 데이터가 '0'으로 나옵니다.**
                
                👉 **해결 방법:** 로그아웃 후, `{data['channel_title']}` 브랜드 계정을 선택하여 다시 로그인해주세요.
                """)
                status.update(label="계정 권한 불일치", state="error")
                # 분석 중단하지 않고 기본 데이터로만 진행 여부는 선택, 여기선 경고만 줌
            
            else:
                status.update(label="데이터 수집 완료!", state="complete")

        # --- 대시보드 표시 ---
        st.divider()
        st.subheader(f"🎬 {data['title']}")
        
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("실시간 조회수", f"{data['realtime_views']:,}회")
        
        if data['has_analytics_data']:
            col2.metric("총 시청 시간", f"{data['watch_time_min']:.1f}분")
            col3.metric("평균 지속 시간", f"{data['avg_duration_sec']:.0f}초")
            status_text = "✅ 정상 연결"
        else:
            col2.metric("총 시청 시간", "데이터 없음 (0)", delta="권한/집계 확인", delta_color="off")
            col3.metric("평균 지속 시간", "데이터 없음 (0)", delta="권한/집계 확인", delta_color="off")
            status_text = "⚠️ 확인 필요"

        col4.metric("좋아요", f"{data['likes']}개")
        
        st.divider()
        
        # 권한 문제 시 추가 안내
        if not data['has_analytics_data']:
            if user_channel and data['channel_id'] != user_channel['id']:
                st.warning("⚠️ **상세 데이터가 보이지 않는 이유:** 로그인된 채널과 영상의 소유주가 다르기 때문입니다. (위의 붉은색 경고 확인)")
            else:
                st.info("ℹ️ 영상이 너무 최신이거나(48시간 이내), 시청 데이터가 집계되지 않았습니다.")

        # Gemini 분석 결과
        st.markdown("### 🤖 Gemini 2.0 Flash 분석 리포트")
        result = analyze_with_gemini(data)
        st.markdown(result)

if __name__ == "__main__":
    main()
