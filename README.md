# Plex DupeFinder FF

`plex_dupefinder_ff`는 FlaskFarm에서 동작하는 Plex 중복 검사, **그룹별 수동 삭제**, **일괄 승인형 반자동 삭제** 플러그인입니다.

Plex 연결정보는 별도로 저장하지 않고, 작업을 시작할 때마다 [`plex_mate`](https://github.com/flaskfarm/plex_mate)의 `base_url`, `base_token`, `base_machine`을 읽습니다. Plex SQLite DB를 직접 수정하거나 운영체제에서 파일을 직접 삭제하지 않습니다.

> [!CAUTION]
> Plex Media 삭제는 해당 Media 버전의 실제 Part 파일을 삭제할 수 있습니다. 백업과 Plex의 `Allow media deletion` 설정을 먼저 확인하세요. 그룹별 삭제와 일괄 승인형 삭제는 모두 기본적으로 꺼져 있으며, 사용자 검토 없이 실행되는 완전 자동 삭제는 제공하지 않습니다.

## 주요 기능

- 영화 및 TV 에피소드의 Plex `duplicate` 검색
- Media/Part/오디오 스트림 단위 품질정보 수집
- 해상도, 코덱, bitrate, 크기, 파일명 패턴 기반 유지 점수
- 항목별 점수 근거 표시
- 백그라운드 단일 스캔, 진행률, 취소, 실행 이력
- 안전 조건에 맞는 그룹만 단건 수동 삭제
- 결과 목록에서 안전 그룹의 개별 삭제 화면 바로 열기
- 라이브러리 전체 선택 및 전체 선택 해제
- 안전한 2개 버전 그룹을 계획으로 묶어 한 번 승인한 뒤 순차 처리
- 삭제 전후 Plex 재조회와 감사 로그
- Plex Token 비저장 및 로그·UI 미노출

## 요구사항

- FlaskFarm 4.1 계열 또는 호환되는 커스텀 빌드
- Python 3.8 이상
- [`plex_mate`](https://github.com/flaskfarm/plex_mate)가 설치되고 Plex URL, Token, Machine ID가 설정된 상태
- `requests` 2.25 이상

현재 공개 FlaskFarm의 `require_plugin` 자동 설치 코드에는 호환성 문제가 있어 `info.yaml`에서 자동 의존성 설치를 선언하지 않습니다. `plex_mate`를 먼저 설치해야 하며, 플러그인도 실행 시 이를 다시 확인합니다.

## 설치

FlaskFarm 플러그인 디렉터리에서 다음과 같이 설치합니다.

```bash
git clone https://github.com/njworange/plex_dupefinder_ff.git
```

FlaskFarm을 재시작한 뒤 `PLEX DupeFinder` 메뉴를 엽니다.

## 처음 사용하는 순서

1. `plex_mate`에서 URL, Token, Machine ID가 올바른지 확인합니다.
2. DupeFinder의 **설정** 화면에서 `Plex 연결 확인`을 실행합니다.
3. 삭제 기능은 끈 상태로 작은 테스트 라이브러리를 스캔합니다.
4. **결과** 화면에서 Plex의 그룹과 실제 파일 경로가 올바른지 확인합니다.
5. 삭제가 필요하면 **삭제 허용 미디어 루트**를 한 줄에 하나씩 설정합니다.
6. 허용 루트를 적용한 상태로 다시 스캔해 그룹의 안전성 판정을 갱신합니다.
7. 백업을 확인한 뒤 `그룹별 수동 삭제 활성화`를 켭니다.
8. 단건 처리라면 결과의 `개별 삭제`를 눌러 유지·삭제 버전을 확인하고 정확한 확인 문구를 입력합니다.
9. 여러 그룹을 처리하려면 `일괄 승인형 반자동 삭제 활성화`를 추가로 켜고 `스캔 1회당 전역 삭제 시도 한도`를 필요한 범위만큼 올립니다.
10. 결과 화면에서 `삭제 계획 만들기`를 누르고 각 그룹의 유지·삭제 Media ID, 점수와 파일 경로를 모두 검토합니다.
11. 화면에 표시된 일괄 확인 문구를 정확히 한 번 입력하면 항목별 재검증과 삭제가 순차 실행됩니다.
12. 삭제 후 처리된 그룹은 잠기며, 다음 작업 전에 다시 스캔해야 합니다.

## 일괄 승인형 반자동 삭제

이 기능은 스케줄이나 스캔 완료를 계기로 자동 실행되지 않습니다. 사용자가 결과 화면에서 직접 계획을 만들고, 계획 전체를 검토하고, 짧게 유효한 일회용 nonce와 정확한 확인 문구로 승인해야 시작합니다.

계획에는 다음 조건을 모두 만족하는 그룹만 포함됩니다.

- 완료된 동일 스캔의 안전·미처리 그룹
- 활성 Media 버전이 정확히 2개인 그룹
- 점수 동점이 아니며 유지 추천이 단독 1위인 그룹
- 동일 스캔의 다른 활성 후보와 Part 파일 경로를 공유하지 않는 그룹
- 현재 `배치 계획 최대 항목 수`와 남은 `스캔 1회당 전역 삭제 시도 한도` 안의 그룹

실행 시에는 기존 단건 삭제 엔진을 한 항목씩 호출합니다. 각 항목마다 Plex 연결, 서버 Machine ID, metadata, Media/Part 지문, 허용 경로와 삭제 후 상태를 다시 확인합니다. 실패·차단·결과 불명 항목이 하나라도 나오면 남은 항목은 실행하지 않습니다. 중단 버튼은 현재 처리 중인 한 항목을 강제로 끊지 않고, 그 항목의 검증이 끝난 뒤 다음 항목부터 중단합니다.

단건과 배치는 공용 DB 전역 삭제 lease를 사용하므로 전체 FlaskFarm worker에서 한 번에 하나의 삭제 트랜잭션만 실행됩니다. 배치는 승인 시 lease를 얻고 항목마다 갱신합니다. 유효한 lease는 다른 worker의 정상 작업으로 간주해 건드리지 않으며, 최소 20분의 만료 시간 뒤에도 갱신되지 않은 lease만 복구 CAS를 선점한 worker가 정리합니다. FlaskFarm 또는 플러그인을 재시작해도 진행 중이던 배치를 자동 재개하지 않으며, DELETE 경계를 확정할 수 없는 항목은 `unknown`으로 남깁니다.

## 삭제 안전장치

삭제는 다음 조건을 모두 통과해야 합니다.

- 로그인된 FlaskFarm AJAX 요청
- Flask session CSRF 토큰
- 120초 동안 한 번만 사용할 수 있는 삭제 사전확인 nonce
- `DELETE {media_id}` 확인 문구
- `plex_mate`의 현재 Machine ID와 스캔 당시 서버 일치
- ratingKey, GUID, 미디어 타입 및 영화/TV 식별정보 일치
- 스캔 당시와 현재의 Media ID 집합 일치
- 모든 Media/Part 경로, 크기, duration 및 품질 지문 일치
- 유지할 Media와 삭제할 Media가 모두 존재
- 삭제 후 최소 한 Media 버전 유지
- 삭제 후 Media ID 집합과 남은 버전의 지문이 정확히 예상 상태와 일치
- 모든 Part 경로가 설정한 허용 루트 안에 존재
- 동일 파일 경로 공유, 식별정보 누락 및 기본 설정상 multipart가 아님
- 스캔별 삭제 **시도** 개수 상한 미도달(동시 요청도 DB에서 원자 차단)
- 단건·배치 공용 전역 DB lease, 승인된 배치의 항목별 갱신 및 DB 원자 선점
- 일괄 계획 생성·승인·각 항목 시작 시 점수와 안전 설정 snapshot 일치
- 일괄 계획 내 다른 그룹을 포함한 동일 Part 경로 교차 참조 없음

DELETE 요청이 timeout되거나 연결 결과를 확정할 수 없으면 같은 요청을 자동 재시도하지 않습니다. Plex를 다시 조회해 결과를 확인하며, 확정할 수 없으면 감사 이력에 `unknown`으로 기록합니다. 결과가 `unknown`이어도 실제 삭제가 수행됐을 수 있으므로 스캔별 삭제 시도 상한을 하나 소비합니다. FlaskFarm이 작업 도중 재시작되면 `validating` 이력은 `blocked`, `deleting` 이력은 `unknown`으로 복구하고 해당 그룹을 수동 확인 상태로 잠급니다.

## 점수 계산

기본 프로필은 원본 `plex_dupefinder`의 개념을 유지하되 다음 안전성 개선을 적용했습니다.

- 여러 오디오 트랙의 점수를 합산하지 않고 가장 높은 트랙 하나만 사용
- 멀티파트의 동일 파일명 규칙을 Part마다 중복 가산하지 않음
- 동점이면 유지 추천을 만들지 않음
- 총점과 구성요소를 함께 저장

점수는 유지 추천에만 사용되며 자동 삭제 판단으로 사용되지 않습니다.

## 데이터 저장

플러그인 전용 FlaskFarm DB에 다음 정보를 저장합니다.

- `scan_run`: 실행 상태와 서버 식별정보
- `duplicate_group`: Plex 작품 단위 중복 그룹과 안전성 판정
- `media_candidate`: Media/Part 스냅샷, 점수, 지문
- `action_log`: 삭제 시도, 차단, 응답 및 전후 검증 결과
- `batch_run`: 일괄 계획, 승인, 진행률, 중단 및 복구 상태
- `batch_item`: 계획에 포함된 유지·삭제 후보와 항목별 처리 결과
- `deletion_lease`: 단건과 배치를 직렬화하는 cross-worker 전역 lease

Plex Token은 어느 테이블에도 저장하지 않습니다.

## 제한사항

- 하나의 `plex_mate` PMS 연결만 지원합니다.
- 음악 라이브러리는 지원하지 않습니다.
- 스케줄 자동 스캔과 사용자 승인 없는 완전 자동 삭제는 포함하지 않습니다.
- 동일 파일 경로가 Plex DB에 중복 등록된 DB 손상 유형은 삭제하지 않습니다.
- multipart Media는 기본적으로 삭제 차단됩니다.
- Plex 서버별 비공개 API 동작 차이가 있을 수 있으므로 실제 삭제는 폐기 가능한 테스트 라이브러리에서 먼저 검증해야 합니다.
- 백그라운드 스캔 조정기는 프로세스 내부 thread를 사용합니다. 여러 web worker를 쓰는 커스텀 FlaskFarm에서는 스캔 시작을 담당하는 worker를 하나로 제한하거나 FlaskFarm 자체를 단일 worker로 실행하세요. 삭제 경로는 DB 원자 선점으로 별도 보호됩니다.
- 일괄 삭제 중에는 플러그인을 동적 재로드하지 마세요. 먼저 중단을 요청하고 현재 항목이 끝난 것을 확인한 뒤 재로드하세요. unload 시 worker를 기다리지만 Plex 요청 timeout이 더 길면 현재 검증이 잠시 계속될 수 있습니다.
- 삭제 실행 요청은 플러그인 자체 CSRF·nonce·확인문구로 보호하지만, 설정 저장은 FlaskFarm 공통 `setting_save` 라우트를 사용합니다. 커스텀 FlaskFarm에서는 session cookie의 SameSite 정책과 공통 설정 저장 CSRF 정책을 확인하세요.

## 개발 및 테스트

외부 Plex 서버 없이 코어 테스트를 실행할 수 있습니다.

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

테스트는 Plex JSON/XML 응답 파싱, 점수, 경로 정책, snapshot 변경 감지, Token 비노출, 스캔 상태 전이, 단건·배치 삭제의 원자 선점, 경로 충돌, 불확실 결과, 재시작 복구 및 FlaskFarm 계약을 다룹니다. 실제 SQLite/SQLAlchemy에서는 단건·배치 lease 동시 경쟁, 갱신·해제·만료 복구 CAS도 검증합니다. 실제 PMS 삭제 테스트는 포함하지 않습니다.

## 라이선스 및 원작 고지

이 프로젝트는 GPL-3.0으로 배포됩니다. 중복 탐색과 점수화 개념은 [`l3uddz/plex_dupefinder`](https://github.com/l3uddz/plex_dupefinder)에서 영감을 받았으며, FlaskFarm용 작업·UI·저장·안전 삭제 계층은 새로 작성했습니다.

자세한 조건은 [LICENSE](LICENSE)를 확인하세요.
