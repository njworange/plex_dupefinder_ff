# PLEX DupeFinder FF

FlaskFarm에서 Plex 중복 영상을 점수화하고, **Dry Run으로 삭제 예정 목록을 확인하거나 Live Run으로 즉시 삭제**하는 플러그인입니다.

버전: **2.1.0**

> Live Run은 Plex Media DELETE를 호출하고 외부 자막을 영구 삭제합니다. quarantine, 백업, 승인 단계, 자동 복구 기능은 없습니다.

## 기능

- Plex가 중복으로 표시한 영화와 에피소드 검색
- 해상도·코덱·비트레이트·채널·파일 크기·파일명 규칙 기반 점수화
- 최고 점수 Media 하나 유지, 나머지 Media 자동 처리
- 동점이면 가장 작은 Plex Media ID 유지
- Dry Run: Plex와 파일시스템을 변경하지 않고 예정 목록과 예상 확보 용량 기록
- Live Run: Plex Media DELETE 직후 남아 있는 정확한 stem의 외부 자막 삭제
- multipart Media의 모든 Part와 자막 처리
- 수동 실행 및 FlaskFarm scheduler 실행
- 실행 및 항목별 최소 이력
- plex_mate 연결로 라이브러리를 조회하고 버튼으로 복수 선택

## 요구 사항

- FlaskFarm
- 설치 및 설정이 완료된 `plex_mate`
- Plex 서버에서 미디어 삭제 허용
- Live Run에서 외부 자막을 정리하려면 FlaskFarm 프로세스가 Plex가 반환한 영상 경로를 같은 경로로 읽고 삭제할 수 있어야 함
- Python 3.8 이상
- `requests>=2.25,<3`

Plex URL, Token, Machine ID는 이 플러그인에 저장하지 않습니다. 실행할 때마다 `plex_mate`의 연결 설정을 읽습니다.

## 대상 라이브러리

설정 화면에서 `라이브러리 조회`를 누르면 plex_mate에 저장된 Plex 연결로 현재 영화·TV 라이브러리를 읽습니다. 표시된 버튼을 누르면 해당 Library ID가 대상 목록에 추가되고, 선택된 버튼을 다시 누르면 해제됩니다. 음악 등 중복 영상 정리를 지원하지 않는 라이브러리는 표시하지 않습니다.

## 실행 모드

### Dry Run

실제 Live Run과 같은 중복 조회, 점수 계산, 유지본 선정 및 자막 탐색을 수행합니다. Plex DELETE와 `unlink`는 호출하지 않습니다.

각 삭제 후보는 `would_delete`로 기록되며 영상, 외부 자막, 점수와 예상 확보 용량을 확인할 수 있습니다.

### Live Run

중복 그룹을 하나씩 처리합니다.

```text
중복 조회
→ 최고 점수 Media 하나 선택
→ 삭제 후보의 외부 자막 목록 수집
→ Plex Media DELETE 한 번 호출
→ Plex metadata와 영상 경로 재확인
→ 영상이 사라졌으면 남은 외부 자막 삭제
→ 결과 기록 후 다음 후보 처리
```

미리보기 승인, 확인 문자열 또는 일괄 승인 단계는 없습니다. 한 항목이 실패해도 다음 중복으로 계속 진행합니다.

## 중복 기준

이 플러그인은 파일 해시를 비교하지 않습니다. Plex가 같은 metadata로 묶고 `duplicate=1` 검색에 반환한 항목 중 서로 다른 Media ID가 두 개 이상인 항목을 중복 그룹으로 취급합니다.

유지본과 삭제본이 같은 실제 영상 경로를 가리키면 해당 그룹은 건너뜁니다. 실행 직전 Media ID나 경로 구성이 달라진 후보도 건너뜁니다.

## 점수 설정

기본 점수는 원본 [l3uddz/plex_dupefinder](https://github.com/l3uddz/plex_dupefinder)의 `config_sample.json` 예시를 사용합니다. 2.0.0에서 `{}`였던 설정은 2.1.0으로 처음 로드할 때 이 값으로 자동 변경됩니다.

`Score JSON`에서 필요한 값만 덮어쓸 수 있습니다.

```json
{
  "video_codec_scores": {
    "Unknown": 0,
    "h264": 10000,
    "h265": 5000,
    "hevc": 5000,
    "mpeg1video": 250,
    "mpeg2video": 250,
    "mpeg4": 500,
    "msmpeg4": 100,
    "msmpeg4v2": 100,
    "msmpeg4v3": 100,
    "vc1": 3000,
    "vp9": 1000,
    "wmv2": 250,
    "wmv3": 250
  },
  "audio_codec_scores": {
    "Unknown": 0,
    "aac": 1000,
    "ac3": 1000,
    "dca": 2000,
    "dca-ma": 4000,
    "eac3": 1250,
    "flac": 2500,
    "mp2": 500,
    "mp3": 1000,
    "pcm": 2500,
    "truehd": 4500,
    "wmapro": 200
  },
  "resolution_scores": {
    "1080": 10000,
    "480": 3000,
    "4k": 20000,
    "720": 5000,
    "Unknown": 0,
    "sd": 1000
  },
  "bitrate_weight": 2,
  "duration_divisor": 300,
  "dimensions_weight": 2,
  "audio_channels_weight": 1000,
  "size_divisor": 100000
}
```

파일명 점수는 glob과 점수의 JSON 객체입니다.

```json
{
  "*.avi": -1000,
  "*.ts": -1000,
  "*.vob": -5000,
  "*1080p*BluRay*": 15000,
  "*720p*BluRay*": 10000,
  "*HDTV*": -1000,
  "*PROPER*": 1500,
  "*REPACK*": 1500,
  "*Remux*": 20000,
  "*WEB*CasStudio*": 5000,
  "*WEB*KINGS*": 5000,
  "*WEB*NTB*": 5000,
  "*WEB*QOQ*": 5000,
  "*WEB*SiGMA*": 5000,
  "*WEB*TBS*": -1000,
  "*WEB*TROLLHD*": 2500,
  "*WEB*VISUM*": 5000,
  "*dvd*": -1000
}
```

원본 예시와 동일하게 파일 크기 점수도 기본으로 사용합니다. 설정하지 않은 값은 위 내장 기본값을 사용합니다.

## 외부 자막

기본 검색 위치:

- 영상과 같은 폴더
- 영상 폴더 바로 아래의 `Subs`
- 영상 폴더 바로 아래의 `Subtitles`

기본 확장자:

```text
.srt, .smi, .ass, .ssa, .vtt, .sub, .idx, .sup
```

다음처럼 영상의 정확한 전체 stem과 일치하거나 점으로 이어지는 언어·forced·SDH 표기가 있는 파일만 대상으로 삼습니다.

```text
Movie.1080p.mkv
Movie.1080p.srt
Movie.1080p.ko.srt
Movie.1080p.ko.forced.ass
```

유지본과 삭제본이 같은 자막 경로를 공유하면 그 자막은 남깁니다. 영상은 Plex가 삭제하며, Plex 처리 후에도 남아 있는 대상 자막만 플러그인이 직접 삭제합니다. 영상 삭제를 확인할 수 없으면 자막은 삭제하지 않습니다.

## 상태

Action 상태:

- `would_delete`: Dry Run 삭제 예정
- `deleted`: 영상과 예정 자막 처리 완료
- `partial`: 영상은 삭제됐지만 일부 자막 처리 실패
- `failed`: 삭제 실패
- `unknown`: Plex DELETE 결과를 확정할 수 없음
- `skipped`: 같은 경로 또는 실행 직전 변경으로 건너뜀

중지 버튼은 진행 중인 HTTP 요청을 취소하지 않습니다. 현재 항목이 끝난 뒤 다음 삭제부터 중지합니다.

## Scheduler

Scheduler 모드는 다음 중 하나입니다.

- `off`: 사용 안 함
- `dry_run`: 설정한 간격마다 Dry Run
- `live`: 설정한 간격마다 Live Run

이미 실행 중이면 새 scheduler 실행은 시작하지 않습니다.

진행 상태와 worker 잠금은 프로세스 로컬이므로 FlaskFarm은 이 플러그인에
대해 단일 web worker로 실행하는 구성을 권장합니다.

## 개발

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
python -m compileall -q .
```

CI는 Python 3.8과 3.12에서 실행합니다.

## 라이선스

GNU General Public License v3.0. 원본 프로젝트와 저작권 고지는 [NOTICE](NOTICE)를 참고하세요.
