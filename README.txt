진주시청 고시/공고 감시 - 클라우드판 (PC 꺼도 동작 / Gmail 알림 / 10분 간격 24시간)
===================================================================================

[중요] 이 저장소는 Public(공개)으로 만듭니다. 그래서
 - 이메일 주소와 앱 비밀번호는 코드/설정 파일에 넣지 않고 'Secrets'에만 넣습니다.
 - 저장소에는 키워드(keywords.txt)와 확인한 글 목록(seen.json)이 공개됩니다.
 - 이 폴더에 개인 정보가 들어 있는 파일을 추가로 올리지 마세요.

1) GitHub 저장소 만들기
   - github.com 가입 > New repository > 이름 예: jinju-notice > Public 선택 > Create
   - "uploading an existing file" 로 이 폴더의 파일을 전부 끌어다 올리기
     (숨김 폴더 .github 포함! .github/workflows/monitor.yml 경로가 그대로여야 함)
     * 윈도우 탐색기에서 숨김 폴더가 안 보이면: 보기 > 표시 > 숨긴 항목 체크

2) Secrets 등록 (저장소 Settings > Secrets and variables > Actions > New repository secret)
   - GMAIL_APP_PASSWORD : 구글 앱 비밀번호 16자리
       (구글 계정 2단계 인증 켜기 > https://myaccount.google.com/apppasswords 에서 발급)
   - GMAIL_ADDRESS      : 보내는 Gmail 주소
   - TO_EMAIL           : (선택) 알림 받을 주소. 비우면 GMAIL_ADDRESS 로 받음

3) 테스트 (저장소 > Actions 탭 > 왼쪽 "진주시 고시공고 감시" > Run workflow)
   a. mode = test_mail   -> Gmail 로 테스트 메일이 오는지 확인
   b. mode = replay (replay_count = 10) -> 최근 글 10건 중 키워드에 걸린 글이 실제로 메일로 오는지 확인
        * 이 단계에서 "게시글을 하나도 찾지 못했습니다" 오류면, GitHub 서버에서 진주시청 접속이 막힌 것일 수 있음
   c. mode = check -> 초록 체크 확인. 첫 실행은 현재 글을 '기준'으로만 저장(알림 없음)

4) 이후 자동 실행: 매시 2,12,22,32,42,52분. 새 글 제목에 키워드가 있으면 Gmail 발송

키워드 추가/삭제
   - 저장소의 keywords.txt 파일 > 연필 아이콘 > 한 줄에 하나씩 수정 > Commit changes
   - 다음 실행부터 바로 반영

알아둘 점
   - GitHub 예약 실행은 부하에 따라 몇 분~십여 분 늦어질 수 있습니다.
   - 저장소에 오래 활동이 없으면 GitHub 가 예약 실행을 멈출 수 있습니다.
     Actions 탭에 안내가 보이면 "Enable workflow" 를 누르세요.
   - 실행이 실패하면 Actions 탭에 빨간 X 가 뜨고, GitHub 가 가입 이메일로도 알려줍니다.
