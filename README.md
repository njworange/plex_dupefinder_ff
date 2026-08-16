# Plex DupeFinder FF

`plex_dupefinder_ff`는 FlaskFarm에서 동작하는 Plex 중복 검사 및 **그룹별 수동 삭제** 플러그인입니다.

Plex 연결정보는 별도로 저장하지 않고, 작업을 시작할 때마다 [`plex_mate`](https://github.com/flaskfarm/plex_mate)의 `base_url`, `base_token`, `base_machine`을 읽습니다. Plex SQLite DB를 직접 수정하거나 운영체제에서 파일을 직접 삭제하지 않습니다.

> [!CAUTION]
> Plex Media 삭제는 해당 Media 버전의 실제 Part 파일을 삭제할 수 있습니다. 백업과 Plex의 `Allow media deletion` 설정을 먼저 확인하세요. 이 프로젝트는 기본적으로 삭제 기능이 꺼져 있으며 자동 삭제와 일괄 삭제를 제공하지 않습니다.

## 주요 기능

- 영화 및 TV 에피소드의 Plex `duplicate` 검색
- Media/Part/오디오 스트림 단위 품질정보 수집
- 해상도, 코덱, bitrate, 크기, 파일명 패턴 기반 유지 점수
- 항목별 점수 근거 표시
- 백그라운드 단일 스캔, 진행률, 취소, 실행 이력
- 안전 조건에 맞는 그룹만 단건 수동 삭제
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
8. 유지할 버전과 삭제할 버전을 선택하고 정확한 확인 문구를 입력합니다.
9. 삭제 후 해당 그룹은 잠기며, 다음 작업 전에 다시 스캔해야 합니다.

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

Plex Token은 어느 테이블에도 저장하지 않습니다.

## 제한사항

- 하나의 `plex_mate` PMS 연결만 지원합니다.
- 음악 라이브러리는 지원하지 않습니다.
- 스케줄 자동 스캔과 자동 삭제는 포함하지 않습니다.
- 동일 파일 경로가 Plex DB에 중복 등록된 DB 손상 유형은 삭제하지 않습니다.
- multipart Media는 기본적으로 삭제 차단됩니다.
- Plex 서버별 비공개 API 동작 차이가 있을 수 있으므로 실제 삭제는 폐기 가능한 테스트 라이브러리에서 먼저 검증해야 합니다.
- 백그라운드 스캔 조정기는 프로세스 내부 thread를 사용합니다. 여러 web worker를 쓰는 커스텀 FlaskFarm에서는 스캔 시작을 담당하는 worker를 하나로 제한하거나 FlaskFarm 자체를 단일 worker로 실행하세요. 삭제 경로는 DB 원자 선점으로 별도 보호됩니다.

## 개발 및 테스트

외부 Plex 서버 없이 코어 테스트를 실행할 수 있습니다.

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

테스트는 Plex JSON/XML 응답 파싱, 점수, 경로 정책, snapshot 변경 감지, Token 비노출, 스캔 상태 전이, 삭제의 원자 선점·불확실 결과·재시작 복구 및 FlaskFarm 계약을 다룹니다. 실제 PMS 삭제 테스트는 포함하지 않습니다.

## 라이선스 및 원작 고지

이 프로젝트는 GPL-3.0으로 배포됩니다. 중복 탐색과 점수화 개념은 [`l3uddz/plex_dupefinder`](https://github.com/l3uddz/plex_dupefinder)에서 영감을 받았으며, FlaskFarm용 작업·UI·저장·안전 삭제 계층은 새로 작성했습니다.

자세한 조건은 [LICENSE](LICENSE)를 확인하세요.
