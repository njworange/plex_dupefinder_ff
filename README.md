# Plex DupeFinder FF

현재 버전: `1.5.0`

`plex_dupefinder_ff`는 FlaskFarm에서 동작하는 Plex 중복 검사, **그룹별 수동 처리**, **일괄 승인형 반자동 처리** 플러그인입니다.

Plex 연결정보는 별도로 저장하지 않고, 작업을 시작할 때마다 [`plex_mate`](https://github.com/flaskfarm/plex_mate)의 `base_url`, `base_token`, `base_machine`을 읽습니다. 기본 `plex` 방식은 PMS에 Media 삭제를 요청합니다. `quarantine` 방식은 영상과 안전하게 귀속된 외부 자막을 격리 폴더로 이동합니다. 호환성을 위해 설정 키를 `direct`로 유지하는 **Plex Media DELETE + 외부 자막 정리** 방식은 영상 삭제를 PMS에 맡기고, 유지본 자막을 임시 보호한 뒤 삭제 대상에만 귀속된 외부 자막을 별도로 정리합니다. Plex SQLite DB를 직접 수정하지 않습니다.

> [!CAUTION]
> Plex Media DELETE는 해당 Media 버전의 실제 Part 파일과 연관 파일을 삭제할 수 있습니다. 안전 격리는 실제 파일을 이동합니다. 외부 자막 정리 방식도 영상을 PMS로 영구삭제하고 전용 자막을 정리하므로, 올바른 경로·권한과 별도 백업이 필요합니다. 이 방식의 FlaskFarm data 보호본은 PMS가 유지본 자막까지 지웠을 때 되돌리기 위한 트랜잭션용 임시본이며, 성공 후 사용자가 복구할 백업이나 격리본이 아닙니다. 그룹별 처리와 일괄 승인형 처리는 모두 기본적으로 꺼져 있으며, 사용자 검토 없이 실행되는 완전 자동 처리는 제공하지 않습니다.

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
- 선택형 안전 격리: 삭제 영상과 그 영상에만 대응하는 일반 외부 자막을 함께 격리
- 선택형 Plex Media DELETE + 외부 자막 정리: PMS로 영상을 삭제하고 대상 전용 자막만 별도 영구 정리
- PMS DELETE 전에 유지본·공유·모호 자막을 SHA-256 보호하고, 누락되면 원래 경로에 검증 복원
- 보호할 수 없는 링크·특수 자막을 PMS DELETE 전에 차단하고 전체 경로·사유 표시
- 결과와 감사 이력에서 함께 격리되거나 정리된 자막 및 보호·제외 대상 확인·필터
- 삭제 전후 Plex 재조회와 감사 로그
- 확인된 삭제 성공 후 선택 가능한 Plex 부분 스캔(`none`/Binary/Web, 기본 `none`)
- 작업 이력 화면에서 사후 부분 스캔의 상태·시도 횟수·대상 경로·오류 확인
- 최근 스캔에서 종료된 스캔의 그룹·후보 결과를 선택 삭제하고 Run ID tombstone과 감사·작업 journal은 보존
- Plex Token 비저장 및 로그·UI 미노출

## 요구사항

- FlaskFarm 4.1 계열 또는 호환되는 커스텀 빌드
- Python 3.8 이상
- [`plex_mate`](https://github.com/flaskfarm/plex_mate)가 설치되고 Plex URL, Token, Machine ID가 설정된 상태
- `requests` 2.25 이상
- 안전 격리 또는 외부 자막 정리 사용 시 Plex 미디어 경로를 FlaskFarm 프로세스가 직접 읽고 쓸 수 있는 실행환경(안전 격리는 격리 경로, 외부 자막 정리는 FlaskFarm data 보호본 경로 접근도 필요)

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
8. 외부 자막을 유지본과 분리해 정리하려면 `파일 처리 방식`에서 **안전 격리** 또는 **Plex Media DELETE + 외부 자막 정리**를 선택합니다. 안전 격리에만 FlaskFarm 호스트 기준 절대 `안전 격리 루트`가 필요합니다.
9. 안전 격리와 Plex Media DELETE + 외부 자막 정리는 PMS DB 반영과 사후 검증을 위해 `삭제 성공 후 Plex 부분 스캔 방식`을 Binary 또는 Web으로 반드시 선택해야 합니다.
10. 단건 처리라면 결과의 `개별 삭제`를 눌러 유지·처리 버전, 함께 처리될 자막과 보호·차단 검토 대상 자막의 전체 경로·사유를 확인한 뒤 화면의 정확한 확인 문구를 입력합니다. 실행 버튼 뒤에 같은 내용을 다시 묻는 브라우저 확인창은 표시하지 않습니다.
11. 결과 화면에서 `삭제 계획 만들기`를 누르고 각 그룹의 유지·처리 Media ID, 점수와 파일 경로를 모두 검토합니다.
12. 화면에 표시된 일괄 확인 문구를 정확히 한 번 입력하면 항목별 재검증과 파일 처리가 순차 실행됩니다.
13. 처리된 그룹은 부분 스캔 성공 여부와 관계없이 잠깁니다. 같은 스캔의 다른 미처리 그룹은 계속 처리할 수 있지만, 처리한 그룹을 최신 상태로 다시 판정하려면 DupeFinder 중복 스캔을 새로 실행합니다.

## 일괄 승인형 반자동 삭제

이 기능은 스케줄이나 스캔 완료를 계기로 자동 실행되지 않습니다. 사용자가 결과 화면에서 직접 계획을 만들고, 계획 전체를 검토하고, 짧게 유효한 일회용 nonce와 정확한 확인 문구로 승인해야 시작합니다.

계획에는 다음 조건을 모두 만족하는 그룹만 포함됩니다.

- 완료된 동일 스캔의 안전·미처리 그룹
- 활성 Media 버전이 정확히 2개인 그룹
- 점수 동점이 아니며 유지 추천이 단독 1위인 그룹
- 동일 스캔의 다른 활성 후보와 Part 파일 경로를 공유하지 않는 그룹
- 현재 `배치 계획 최대 항목 수` 안의 그룹

실행 시에는 기존 단건 삭제 엔진을 한 항목씩 호출합니다. 각 항목마다 Plex 연결, 서버 Machine ID, metadata, Media/Part 지문, 허용 경로와 삭제 후 상태를 다시 확인합니다. 실패·차단·결과 불명 항목이 하나라도 나오면 남은 항목은 실행하지 않습니다. 중단 버튼은 현재 처리 중인 한 항목을 강제로 끊지 않고, 그 항목의 검증이 끝난 뒤 다음 항목부터 중단합니다.

단건과 배치는 공용 DB 전역 삭제 lease를 사용하므로 전체 FlaskFarm worker에서 한 번에 하나의 삭제 트랜잭션만 실행됩니다. 배치는 승인 시 lease를 얻고 항목마다 갱신합니다. 유효한 lease는 다른 worker의 정상 작업으로 간주해 건드리지 않으며, 최소 20분의 만료 시간 뒤에도 갱신되지 않은 lease만 복구 CAS를 선점한 worker가 정리합니다. FlaskFarm 또는 플러그인을 재시작해도 진행 중이던 배치를 자동 재개하지 않으며, DELETE 경계를 확정할 수 없는 항목은 `unknown`으로 남깁니다.

## 안전 격리와 외부 자막

외부 자막 정리는 기본적으로 꺼져 있습니다. 설정의 `파일 처리 방식`을 `안전 격리 (영상 + 전용 외부 자막)`로 명시적으로 선택했을 때만 동작합니다. 이 방식은 Plex Media DELETE를 호출하지 않으며 파일을 영구삭제하지 않습니다. 삭제 대상으로 확정된 단일 Part 영상과 그 영상에만 대응한다고 검증된 외부 자막을 설정한 격리 루트 아래의 작업별 폴더로 이동합니다. 격리 파일의 확인과 최종 삭제는 운영자가 별도로 수행해야 하며, 플러그인은 자동 완전삭제 기능을 제공하지 않습니다.

일반 자막은 영상과 같은 폴더 또는 바로 아래 `Subs`/`Subtitles` 폴더에 있고, 영상의 전체 basename 뒤에 언어·속성 접미사가 이어지는 형태를 대상으로 합니다. 예를 들어 `Movie.1080p.mkv`에는 `Movie.1080p.ko.srt`, `Movie.1080p.en.forced.srt`, `Movie.1080p.sdh.ass` 등이 대응할 수 있습니다. 지원 확장자는 `.srt`, `.smi`, `.ssa`, `.ass`, `.vtt`입니다. VobSub처럼 쌍 파일인 `.idx/.sub`, `.sup`, 중첩 자막 폴더와 Plex 명명 규칙에 맞지 않는 파일은 자동 이동하지 않습니다.

다음과 같은 자막은 위험·모호한 예외로 분류해 원본 위치에 그대로 보존합니다.

- 유지 영상과 basename이 같아 어느 버전의 자막인지 독점 귀속을 증명할 수 없는 경우
- 유지본, 다른 Media Part 또는 다른 Plex 항목이 같은 경로를 참조할 가능성이 있는 경우
- 심볼릭 링크, hard link, 경로 이탈, 대상 변경 또는 파일시스템 경계를 안전하게 검증할 수 없는 경우
- 자막 경로나 파일 상태가 사전확인 이후 실행 직전 달라진 경우
- 내장 자막처럼 독립 외부 파일이 아니거나 지원 규칙에 맞지 않는 경우

사전확인은 함께 격리할 자막과 제외할 자막의 전체 경로 및 사유를 보여주며, 그 목록, 파일 identity, 관련 폴더 목록과 격리 루트 identity를 plan digest로 승인 정보에 바인딩합니다. 격리 루트를 처음 검증할 때는 루트 교체를 감지하는 32-byte identity marker 파일 `.plex_dupefinder_ff-root-id`를 한 번만 생성하며, 이 파일은 미디어나 격리 대상이 아니고 기존 파일을 덮어쓰지 않습니다. 실행 직전에 동일 계획을 다시 계산하고 파일 이동 전후에도 관련 폴더 목록을 확인합니다. 경로, 목록, 파일 stat 또는 같은 stem의 sibling 영상이 하나라도 달라지면 승인되지 않은 자막은 이동하지 않고 새 사전확인 또는 수동 복구를 요구합니다. 승인 없이 자막을 새로 포함하거나 제외한 채 진행하지 않습니다. 결과와 작업 이력에는 실제 자막의 원본·격리 경로, 보존된 위험 예외와 사유가 기록됩니다. 경로나 귀속이 조금이라도 모호하면 처리하지 않는 fail-closed 정책입니다.

첫 파일을 이동하기 전에 immutable manifest와 Plex snapshot을 DB에 기록하고, 유지 버전에 대응하는 외부 자막은 SHA-256 검증 보호본을 먼저 만듭니다. 영상부터 격리한 다음 승인된 전용 자막만 순서대로 이동하며 각 원자 rename을 즉시 저널에 기록합니다. 유지 자막이 사라진 경우 원본 경로가 비어 있고 보호본 hash가 정확할 때만 덮어쓰기 없이 복구합니다. 경로에 다른 파일이 생겼거나 파일 내용이 달라졌으면 자동 복구하지 않고 `manual_check_required`로 잠급니다. 중간 재시작이나 부분 이동도 자동 roll-forward/rollback하지 않으며 실제 파일과 감사 상세를 확인해야 합니다.

부분 스캔 뒤에는 삭제 대상 Media가 사라졌고 모든 유지 Media fingerprint가 같은지 다시 확인합니다. Plex의 자동 휴지통 비우기가 꺼져 Media가 남아 있더라도 대상 Part가 모두 `exists=false`이고 유지본이 그대로면 `trash_pending`으로 명시해 안전 완료 처리합니다. 유지 Media/파일 또는 격리 파일 identity가 달라지면 성공 처리하지 않습니다.

안전 격리는 FlaskFarm 실행 환경에서 Plex가 반환한 미디어 경로와 미리 생성한 격리 루트를 직접 읽고 쓸 수 있어야 합니다. 격리 루트는 모든 Plex library Location과 삭제 허용 미디어 루트 밖에 있어야 하며 symlink/reparse 경로일 수 없습니다. 영상·함께 이동할 자막·격리 루트가 서로 다른 파일시스템이면 원자 이동을 보장할 수 없어 해당 처리를 차단하거나 자막을 위험 예외로 제외합니다. 또한 PMS DB 반영과 유지본 사후 검증을 위해 Binary 또는 Web 부분 스캔 설정이 필수이며, 정확한 section과 부분 스캔 경로를 만들 수 없으면 파일 이동 전에 차단합니다.

## Plex Media DELETE + 외부 자막 정리

설정 값 `direct`는 기존 설치와 저장값 호환을 위해 유지하지만, 이제 영상을 FlaskFarm에서 직접 `rename`/`unlink`하지 않습니다. 단일 Part 삭제 영상은 PMS의 Media DELETE로 한 번만 요청하고, 외부 자막만 FlaskFarm이 별도로 보호·정리합니다. 이 구조는 mergerfs에서 새 handoff 이름이 다른 물리 branch에 배치되어 `EXDEV (errno 18)`가 발생하던 영상 rename 경로를 제거합니다. 안전 격리 루트는 사용하지 않습니다.

사전확인은 `.srt`, `.smi`, `.ssa`, `.ass`, `.vtt` 외부 자막을 두 종류로 나눕니다.

- **정리 대상**: 삭제할 영상에만 대응한다고 증명된 동일 stem 자막. PMS DELETE 뒤에도 남아 있으면 열린 파일 descriptor, identity와 전체 SHA-256을 다시 확인한 후 영구삭제합니다. PMS가 이미 함께 삭제했다면 다시 삭제하지 않습니다.
- **보호 대상**: 유지본 소유, 공유 또는 귀속이 모호하지만 regular-file snapshot을 안전하게 만들 수 있는 자막. PMS DELETE 전에 FlaskFarm `path_data` 아래 `plex_dupefinder_ff/direct-delete-backups/op-<operation-key>/sidecars`에 전체 SHA-256 보호본을 내구성 있게 기록합니다. PMS 처리 뒤 원본이 없어졌을 때만 SHA-256을 검증해 원래 경로에 배타적 생성(no-overwrite)으로 복원합니다.

관련 자막 중 symlink/reparse, 지원하지 않는 특수 파일 또는 그 밖에 snapshot을 안전하게 만들 수 없는 보호 대상이 하나라도 있으면 PMS DELETE를 보내기 전에 전체 작업을 차단합니다. 실행 직전에 Plex metadata, Media/Part fingerprint, 자막 목록·identity·SHA-256과 보호본을 다시 검증하며, 승인한 계획과 달라져도 PMS DELETE 전에 차단합니다.

화면에 표시된 영상·자막 계획과 `DELETE MEDIA {media_id} SUBTITLES {count} {digest12}` 문구를 정확히 승인하면 durable `direct_delete_journal`과 보호본을 먼저 확정한 뒤 PMS Media DELETE를 한 번 보냅니다. timeout 또는 응답 오류가 나도 DELETE를 자동 재시도하지 않습니다. 즉시 PMS를 정확히 재조회해 대상 Media가 사라졌고 유지 Media 집합과 fingerprint가 그대로인 경우에만 보호 자막 복원과 전용 자막 정리를 계속합니다. 대상 Media가 남았거나 결과를 확정할 수 없으면 전용 자막은 건드리지 않고 보호 자막만 안전하게 복원한 뒤 `unknown`/`manual_check_required`와 보호본을 남깁니다.

Binary 또는 Web 부분 스캔과 최종 유지본 검증까지 성공한 뒤에만 임시 보호본을 정리합니다. 보호본은 PMS의 연관 파일 삭제로부터 유지본 자막을 되돌리기 위한 트랜잭션용 임시 데이터이며, 성공 후 영상이나 전용 자막을 복구하는 사용자 백업이 아닙니다. 복원 경로에 다른 파일이 생겼거나 SHA-256이 일치하지 않으면 덮어쓰지 않고 수동 확인 상태로 남깁니다. 이전 버전에서 생성된 `recovery_required` 직접 파일삭제 journal은 읽기 전용 진단 대상으로 유지하며, 과거 source/handoff 파일을 자동 삭제하거나 복원하지 않습니다.

## 삭제 안전장치

삭제는 다음 조건을 모두 통과해야 합니다.

- 로그인된 FlaskFarm AJAX 요청
- Flask session CSRF 토큰
- 120초 동안 한 번만 사용할 수 있는 삭제 사전확인 nonce
- 기존 Plex 방식은 `DELETE {media_id}`, 안전 격리는 `QUARANTINE {media_id} SUBTITLES {count} {digest12}`, Plex DELETE + 자막 정리는 `DELETE MEDIA {media_id} SUBTITLES {count} {digest12}` 정확 확인 문구
- `plex_mate`의 현재 Machine ID와 스캔 당시 서버 일치
- ratingKey, GUID, 미디어 타입 및 영화/TV 식별정보 일치
- 스캔 당시와 현재의 Media ID 집합 일치
- 모든 Media/Part 경로, 크기, duration 및 품질 지문 일치
- 유지할 Media와 삭제할 Media가 모두 존재
- 삭제 후 최소 한 Media 버전 유지
- 삭제 후 Media ID 집합과 남은 버전의 지문이 정확히 예상 상태와 일치
- 모든 Part 경로가 설정한 허용 루트 안에 존재
- 동일 파일 경로 공유, 식별정보 누락 및 기본 설정상 multipart가 아님
- 삭제 시도 횟수의 DB 원자 증가와 감사 표시(스캔별 횟수 상한은 없음)
- 단건·배치 공용 전역 DB lease, 승인된 배치의 항목별 갱신 및 DB 원자 선점
- 일괄 계획 생성·승인·각 항목 시작 시 점수와 안전 설정 snapshot 일치
- 일괄 계획 내 다른 그룹을 포함한 동일 Part 경로 교차 참조 없음
- 사후 부분 스캔을 Binary/Web으로 선택한 경우 현재 PMS section/Location에서 정확한 영화 폴더 또는 TV 쇼 루트 산정 성공

개별 삭제에서는 정확한 확인 문구를 입력한 뒤 별도의 브라우저 최종 확인창을 띄우지 않습니다. 서버는 실행할 때마다 Flask session CSRF, 짧게 유효한 일회용 nonce, 승인된 plan digest와 정확한 확인 문구를 계속 검증하며, 일괄 승인 실행의 별도 브라우저 확인은 유지합니다.

DELETE 요청이 timeout되거나 연결 결과를 확정할 수 없으면 같은 요청을 자동 재시도하지 않습니다. Plex를 다시 조회해 결과를 확인하며, 확정할 수 없으면 감사 이력에 `unknown`으로 기록합니다. 결과가 `unknown`이어도 실제 삭제가 수행됐을 수 있으므로 삭제 시도 횟수에는 계속 기록됩니다. FlaskFarm이 작업 도중 재시작되면 `validating` 이력은 `blocked`, `deleting` 이력은 `unknown`으로 복구하고 해당 그룹을 수동 확인 상태로 잠급니다. 안전 격리의 `quarantining` 이력과 Plex DELETE + 자막 정리 journal의 진행 중 이력은 실제 PMS·원본·보호본 상태를 기준으로 `recovery_required`/`unknown`으로 보수 복구하며, DELETE나 전용 자막 삭제를 자동 재실행하지 않습니다.

스캔별 삭제 시도 횟수에는 상한이 없습니다. 결과 화면은 감사 목적으로 누적 시도 횟수를 표시하며, 개별 처리 수는 제한하지 않습니다. 배치 하나의 항목 수에는 별도 `배치 계획 최대 항목 수`가 계속 적용됩니다. 이전 버전에서 저장한 `setting_max_delete_per_run` 값은 읽거나 덮어쓰거나 마이그레이션하지 않고 무시합니다.

## 삭제 후 Plex 부분 스캔

`삭제 성공 후 Plex 부분 스캔 방식`의 호환 기본값은 `사용 안 함(none)`입니다. 기존 Plex 방식에서는 Plex DELETE가 완료되고 Media 사후 재조회로 삭제 결과가 정확히 확인된 경우에만 durable outbox에 비파괴 스캔 요청을 기록합니다. 안전 격리와 Plex DELETE + 외부 자막 정리 방식에서는 PMS DB 반영과 유지본 검증에 부분 스캔이 필수이므로 `none`을 허용하지 않습니다. 외부 자막 정리 방식은 PMS DELETE 결과를 정확히 확인하고 보호 자막을 복원하며 전용 자막을 정리한 뒤에만 부분 스캔을 요청합니다.

Binary 또는 Web을 선택하면 파일 처리 전에 현재 PMS에서 library section, 타입과 Location을 다시 읽고 모든 대상 Part에 대한 정확한 스캔 경로를 먼저 만듭니다. 영화 폴더나 TV 쇼 루트를 하나라도 안전하게 계산할 수 없거나 경로가 section/삭제 허용 루트를 벗어나면 DELETE, 격리 이동 또는 외부 자막 정리를 시작하지 않습니다. 기존 Plex 방식의 사후 스캔 전달은 best-effort지만 그 대상 경로를 정확히 준비하는 것은 파일 처리의 필수 사전조건입니다. 안전 격리와 Plex DELETE + 외부 자막 정리에서는 부분 스캔과 유지본 사후검증이 성공 확정의 일부입니다. 기존 Plex 방식에서만 이 추가 차단을 원하지 않을 때 기본값 `none`을 유지할 수 있습니다.

- `Binary`: 로드된 `plex_mate`의 `PlexBinaryScanner.scan_refresh(section_id, path)`를 사용합니다. FlaskFarm/PlexMate 실행 환경에서 Plex Media Scanner binary, 실행 사용자·권한과 Plex가 반환한 파일 경로에 접근할 수 있어야 합니다.
- `Web`: `plex_mate`에서 지연 조회한 URL·Token을 사용해 이 플러그인의 header-auth Plex client가 부분 스캔을 요청합니다. 대상 library section ID와 Plex 서버에서 보이는 경로가 필요합니다.
- 영화는 삭제된 Part가 속한 영화 폴더, TV 에피소드는 Plex 라이브러리 위치를 기준으로 계산한 해당 TV 쇼 루트를 대상으로 합니다. 안전하게 section과 경로를 계산할 수 없거나 경로가 비어 있으면 요청을 실행하지 않으며, 빈 경로로 전체 라이브러리 스캔을 시작하지 않습니다.

기존 Plex 방식에서 사후 스캔은 best-effort 후처리입니다. 일시적 실패는 outbox에서 최대 3회까지 제한적으로 지수 backoff 재시도할 수 있지만, Plex DELETE는 절대로 재시도하지 않습니다. 사후 스캔의 실패나 최종 포기는 이미 확인된 Plex 삭제 성공을 실패로 바꾸거나 삭제를 다시 실행하지 않습니다. 안전 격리와 Plex DELETE + 외부 자막 정리 방식은 다릅니다. 승인한 처리 뒤 부분 스캔과 유지본 사후검증까지 통과해야 성공이며, 외부 자막 정리의 임시 보호본도 그때까지 보존합니다. 최종 실패하면 `unknown`/`manual_check_required`로 남겨 운영자가 journal, Plex, 원본과 보호본 상태를 확인하도록 합니다. 또한 이 기능은 DupeFinder 중복 재검사를 실행하지 않고 `rescan_required` 잠금을 해제하지 않습니다. 처리한 그룹의 최신 중복 판정은 DupeFinder 스캔을 별도로 실행해야 반영됩니다. 백엔드는 각 항목을 실행 직전 다시 검증하므로 같은 스캔의 다른 미처리 그룹을 계속 처리할 수 있습니다.

Binary 프로세스에 종료를 요청한 뒤에도 실제 종료 여부를 확인할 수 없는 예외 상황에서는 자동 스캔 재실행을 영구 차단하고, 다른 삭제·부분 스캔과 겹치지 않도록 전역 작업 잠금을 1시간 유지합니다. 작업 이력에 이 상태가 표시되면 운영자는 1시간 잠금 만료 전후로 Plex Media Scanner 프로세스가 실제로 종료됐는지 확인해야 합니다. 1시간은 운영상 격리 시간이며, 만료 후에도 종료되지 않은 외부 프로세스를 이 플러그인이 증명하거나 계속 추적하지는 않습니다.

최근 outbox 작업은 **작업 이력 → 삭제 후 부분 스캔 작업**에서 확인할 수 있습니다. 상태, 방식, section, 대상 경로, 현재/최대 시도 횟수와 결과 또는 오류가 표시되며 새로고침 버튼은 상태 조회만 수행합니다.

## 점수 계산

기본 프로필은 원본 `plex_dupefinder`의 개념을 유지하되 다음 안전성 개선을 적용했습니다.

- 여러 오디오 트랙의 점수를 합산하지 않고 가장 높은 트랙 하나만 사용
- 멀티파트의 동일 파일명 규칙을 Part마다 중복 가산하지 않음
- 동점이면 유지 추천을 만들지 않음
- 총점과 구성요소를 함께 저장

점수는 유지 추천에만 사용되며 자동 삭제 판단으로 사용되지 않습니다.

## 데이터 저장

플러그인 전용 FlaskFarm DB에 다음 정보를 저장합니다.

- `scan_run`: 실행 상태와 서버 식별정보. 결과 삭제 뒤에는 Run ID 재사용을 막는 최소 `results_deleted` tombstone만 보존
- `duplicate_group`: Plex 작품 단위 중복 그룹과 안전성 판정
- `media_candidate`: Media/Part 스냅샷, 점수, 지문
- `action_log`: 삭제 시도, 차단, 응답 및 전후 검증 결과
- `post_delete_scan_job`: 확인된 삭제 뒤 부분 스캔을 전달하는 durable outbox 상태와 제한된 재시도 정보
- `quarantine_journal`: 승인 manifest, 파일 identity·hash, 보호본과 실제 격리 이동 경로 및 복구 상태
- `direct_delete_journal`: Plex DELETE + 외부 자막 정리 승인 manifest, PMS 결과, 자막 identity·hash, 임시 보호·복원·정리 상태 및 수동 확인 정보
- `batch_run`: 일괄 계획, 승인, 진행률, 중단 및 복구 상태
- `batch_item`: 계획에 포함된 유지·삭제 후보와 항목별 처리 결과
- `deletion_lease`: 단건과 배치를 직렬화하는 cross-worker 전역 lease

Plex Token은 어느 테이블에도 저장하지 않습니다.

**최근 스캔 → 스캔 결과 삭제**는 현재 실행 중이 아닌 종료 상태의 스캔 결과만 정리합니다. 연결된 `duplicate_group`, `media_candidate` 결과 행은 삭제하고, 원래 `scan_run`은 감사 기록의 Run ID가 미래 스캔에 재사용되지 않도록 식별정보를 비운 최소 `results_deleted` tombstone으로 남깁니다. tombstone은 최근 스캔 목록과 결과 API에서 숨겨집니다. 미디어·자막 파일이나 Plex 항목에는 어떤 명령도 보내지 않으며, `action_log`, `batch_run`/`batch_item`, `post_delete_scan_job`, 격리·직접 처리 journal 같은 삭제·감사 이력도 보존됩니다. 연결된 삭제·배치·부분 스캔 또는 미완료 journal이 아직 활성 상태라면 결과 삭제를 차단하므로, 작업 상태를 먼저 확인해야 합니다.

## 제한사항

- 하나의 `plex_mate` PMS 연결만 지원합니다.
- 음악 라이브러리는 지원하지 않습니다.
- 스케줄 자동 스캔과 사용자 승인 없는 완전 자동 삭제는 포함하지 않습니다.
- 동일 파일 경로가 Plex DB에 중복 등록된 DB 손상 유형은 삭제하지 않습니다.
- multipart Media는 기본적으로 삭제 차단됩니다.
- Plex 서버별 비공개 API 동작 차이가 있을 수 있으므로 실제 삭제는 폐기 가능한 테스트 라이브러리에서 먼저 검증해야 합니다.
- Binary 사후 스캔은 로컬 PMS scanner 실행환경과 Plex 파일 경로 접근이 필요합니다. Web 방식이 더 이식성이 높지만 section ID와 Plex 서버 기준 경로가 정확해야 합니다.
- Binary 종료 여부를 증명할 수 없는 작업은 자동 재실행하지 않고 1시간 동안 전역 격리합니다. 작업 이력에서 오류를 확인하고 Plex Media Scanner의 실제 종료 상태를 운영자가 점검해야 합니다.
- 연결 화면은 Web 방식의 header-auth Plex 연결과 Binary helper·Scanner 설정 여부만 비파괴로 확인합니다. 실제 scanner 실행, 경로 권한 또는 부분 스캔 성공은 시험하지 않습니다.
- 백그라운드 스캔 조정기는 프로세스 내부 thread를 사용합니다. 여러 web worker를 쓰는 커스텀 FlaskFarm에서는 스캔 시작을 담당하는 worker를 하나로 제한하거나 FlaskFarm 자체를 단일 worker로 실행하세요. 삭제 경로는 DB 원자 선점으로 별도 보호됩니다.
- 일괄 삭제 중에는 플러그인을 동적 재로드하지 마세요. 먼저 중단을 요청하고 현재 항목이 끝난 것을 확인한 뒤 재로드하세요. unload 시 worker를 기다리지만 Plex 요청 timeout이 더 길면 현재 검증이 잠시 계속될 수 있습니다.
- 삭제 실행 요청은 플러그인 자체 CSRF·nonce·확인문구로 보호하지만, 설정 저장은 FlaskFarm 공통 `setting_save` 라우트를 사용합니다. 커스텀 FlaskFarm에서는 session cookie의 SameSite 정책과 공통 설정 저장 CSRF 정책을 확인하세요.

## 개발 및 테스트

외부 Plex 서버 없이 코어 테스트를 실행할 수 있습니다.

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

테스트는 Plex JSON/XML 응답 파싱, 점수, 경로 정책, snapshot 변경 감지, Token 비노출, 스캔 상태 전이, 단건·배치 삭제의 원자 선점, 경로 충돌, 불확실 결과, 재시작 복구, 사후 스캔 outbox 및 FlaskFarm 계약을 다룹니다. 실제 SQLite/SQLAlchemy에서는 단건·배치 lease 동시 경쟁, 갱신·해제·만료 복구 CAS도 검증합니다. 실제 PMS 삭제 또는 부분 스캔 테스트는 포함하지 않습니다.

## 라이선스 및 원작 고지

이 프로젝트는 GPL-3.0으로 배포됩니다. 중복 탐색과 점수화 개념은 [`l3uddz/plex_dupefinder`](https://github.com/l3uddz/plex_dupefinder)에서 영감을 받았으며, FlaskFarm용 작업·UI·저장·안전 삭제 계층은 새로 작성했습니다.

자세한 조건은 [LICENSE](LICENSE)를 확인하세요.
