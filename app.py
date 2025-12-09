# app.py
import streamlit as st
import google.generativeai as genai
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import pandas as pd
from datetime import datetime
import re

# 1. 페이지 설정
st.set_page_config(page_title="YouTube AI Analyst", layout="wide")
st.title("📺 YouTube AI 인사이트 분석기 (Web Ver.)")

# 2. 비밀 정보(Secrets) 불러오기
# Streamlit Cloud의 Secrets 관리 기능을 사용합니다.
try:
    client_config = st.secrets["web"]
    gemini_key = st.secrets["GEMINI_API_KEY"]
except:
    st.error("Secrets 설정이 필요합니다. 배포 가이드를 확인하세요.")
    st.stop()

genai.configure(api_key=gemini_key)

# 3. 인증 관련 설정
SCOPES = [
    'https://www.googleapis.com/auth/yt-analytics.readonly',
    'https://www.googleapis.com/auth/youtube.readonly'
]

def get_flow():
    # secrets에서 설정을 읽어와 Flow 객체 생성
    flow = Flow.from_client_config(
        {'web': client_config},
        scopes=SCOPES,
        redirect_uri=st.secrets["REDIRECT_URI"] 
    )
    return flow

# 4. 데이터 추출 및 분석 함수 (기존과 동일)
def get_video_id(url):
    video_id = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', url)
    return video_id.group(1) if video_id else None

def get_video_stats(creds, video_id):
    youtube = build('youtube', 'v3', credentials=creds)
    analytics = build('youtubeAnalytics', 'v2', credentials=creds)

    video_response = youtube.videos().list(part='snippet,statistics', id=video_id).execute()
    if not video_response['items']: return None
    
    snippet = video_response['items'][0]['snippet']
    publish_date = snippet['publishedAt'][:10]
    title = snippet['title']
    end_date = datetime.now().strftime('%Y-%m-%d')
    
    analytics_response = analytics.reports().query(
        ids='channel==MINE', startDate=publish_date, endDate=end_date,
        metrics='views,estimatedMinutesWatched,averageViewDuration',
        filters=f'video=={video_id}'
    ).execute()

    traffic_response = analytics.reports().query(
        ids='channel==MINE', startDate=publish_date, endDate=end_date,
        metrics='views', dimensions='insightTrafficSourceType',
        filters=f'video=={video_id}', sort='-views'
    ).execute()

    return {
        "title": title, "publish_date": publish_date,
        "basic_stats": analytics_response['rows'][0] if analytics_response.get('rows') else [0,0,0],
        "traffic_sources": traffic_response.get('rows', [])
    }

def analyze_with_gemini(data):
    model = genai.GenerativeModel('gemini-1.5-pro-latest')
    prompt = f"""
    영상 제목: {data['title']} (게시일: {data['publish_date']})
    조회수: {data['basic_stats'][0]}, 총 시청시간(분): {data['basic_stats'][1]}
    유입 경로: {data['traffic_sources']}
    
    위 데이터를 바탕으로 조회수와 시청 지속 시간을 늘리기 위한 구체적인 개선 전략과
    클릭을 부르는 제목/썸네일 아이디어를 제안해줘. (마크다운 형식)
    """
    with st.spinner('Gemini가 분석 중입니다... 🧠'):
        response = model.generate_content(prompt)
    return response.text

# 5. 메인 로직 (인증 흐름 변경)
if "creds" not in st.session_state:
    st.session_state.creds = None

# URL에 'code'가 있으면 인증 완료 후 돌아온 상태임
if st.query_params.get("code"):
    try:
        flow = get_flow()
        flow.fetch_token(code=st.query_params.get("code"))
        st.session_state.creds = flow.credentials
        st.query_params.clear() # URL 깔끔하게 정리
    except Exception as e:
        st.error(f"인증 오류: {e}")

# 로그인 안 된 상태면 로그인 버튼 표시
if not st.session_state.creds:
    st.info("YouTube 데이터를 분석하려면 로그인이 필요합니다.")
    flow = get_flow()
    auth_url, _ = flow.authorization_url(prompt='consent')
    st.link_button("Google 계정으로 로그인", auth_url)

# 로그인 된 상태면 분석기 표시
else:
    st.success("로그인 완료! 👋")
    if st.button("로그아웃"):
        st.session_state.creds = None
        st.rerun()
        
    video_url = st.text_input("분석할 YouTube 영상 URL", placeholder="https://youtube.com/...")
    if video_url and st.button("분석 시작"):
        try:
            vid = get_video_id(video_url)
            if vid:
                stats = get_video_stats(st.session_state.creds, vid)
                if stats:
                    st.subheader(f"📊 {stats['title']}")
                    st.markdown(analyze_with_gemini(stats))
                else:
                    st.error("데이터를 가져올 수 없습니다. 본인 채널 영상이 맞나요?")
            else:
                st.error("URL 형식이 올바르지 않습니다.")
        except Exception as e:
            st.error(f"오류: {e}")
