# Changelog

## 2.1.1

- 중지 요청과 중지 완료 상태를 UI에서 명확하게 구분
- 삭제 직전 중지 신호를 재확인하여 새 Plex DELETE 시작 방지
- 종료와 중지 요청이 겹쳐 DB 상태가 `stopping`에 남는 경합 방지
- 실제 중지 타이밍을 검증하는 회귀 테스트 추가

## 2.1.0

- plex_mate 연결을 사용한 영화·TV 라이브러리 조회 및 클릭 선택 UI
- 기존 2.0.0의 빈 점수 설정을 원본 dupefinder 예시값으로 자동 마이그레이션
- 오디오·비디오·해상도·파일명·파일 크기 기본 점수를 원본 `config_sample.json`과 통일

## 2.0.0

전체 구조를 단순한 자동 정리 실행기로 다시 작성했습니다.

- Plex 중복 그룹 조회 및 점수 기반 유지본 선택
- Dry Run과 즉시 자동 삭제 실행 모드
- Plex Media DELETE 후 외부 자막 정리
- multipart Media 및 언어·forced·SDH 자막 이름 지원
- 실행·항목별 최소 이력과 중지 기능
- 기존 preview 승인, quarantine, batch, lease, journal, outbox 제거
