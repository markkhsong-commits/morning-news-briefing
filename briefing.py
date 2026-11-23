name: Morning Briefing

on:
  schedule:
    # 밴쿠버 오전 6시 17분 (겨울/표준시 PST = UTC-8 → UTC 14:17)
    # ★ 서머타임(PDT, 3월 둘째 일요일~11월 첫째 일요일)에는 "17 13 * * *" 로 바꾸세요.
    #   2027년 서머타임 시작: 3월 14일 / 종료: 11월 7일
    # 정각(분=0)은 GitHub이 가장 붐벼서 일부러 17분으로 잡았습니다.
    - cron: "17 14 * * *"
  workflow_dispatch:

permissions:
  contents: read

jobs:
  build:
    runs-on: ubuntu-latest
    env:
      TZ_NAME: America/Vancouver
      CITY_NAME: 코퀴틀람
      LAT: "49.2838"
      LON: "-122.7932"
      PAGE_URL: https://${{ github.repository_owner }}.github.io/${{ github.event.repository.name }}/
      GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: 패키지 설치
        run: pip install -r requirements.txt

      - name: 브리핑 만들기
        run: python briefing.py build

      - name: 웹페이지 파일 업로드
        uses: actions/upload-pages-artifact@v4
        with:
          path: site

      - name: 이메일 내용 보관
        uses: actions/upload-artifact@v4
        with:
          name: email-content
          path: build

  deploy:
    needs: build
    runs-on: ubuntu-latest
    permissions:
      pages: write
      id-token: write
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - name: 웹페이지 배포
        id: deployment
        uses: actions/deploy-pages@v4

  send:
    needs: deploy
    runs-on: ubuntu-latest
    env:
      PAGE_URL: https://${{ github.repository_owner }}.github.io/${{ github.event.repository.name }}/
      GMAIL_ADDRESS: ${{ secrets.GMAIL_ADDRESS }}
      GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: 패키지 설치
        run: pip install -r requirements.txt

      - name: 이메일 내용 내려받기
        uses: actions/download-artifact@v4
        with:
          name: email-content
          path: build

      - name: 이메일 보내기
        run: python briefing.py send
