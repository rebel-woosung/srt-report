# SRT 리포트 분석 스크립트

SRT 리포트를 **다운로드**하고 **분석**하는 두 단계로 나뉩니다. 사이트(`--site`)별로 API·리포트 디렉터리·데이터 디렉터리가 완전히 분리됩니다.

## 1. 다운로드 — `download_srt_reports.py`

새 리포트가 올라왔을 때 실행합니다. mgmt-engine API에서 zip을 받아 `reports/<site>/` 아래에 풀고, 자동 분류합니다.

```bash
python download_srt_reports.py                       # pega (기본)
python download_srt_reports.py --site rtower
python download_srt_reports.py --site pega --since 2026-08-01
```

| 옵션 | 설명 |
|---|---|
| `--site` | `pega` / `rtower` (기본 `pega`) |
| `--since` | `YYYY-MM-DD` 이후 리포트만. 미지정 시 사이트 기본값 |
| `--url` | 사이트 기본 URL 대신 직접 지정 |
| `--keep-interrupted` | `f99-99`(중단 run) 리포트도 제외하지 않음 |
| `--json` | 표 대신 JSON 출력 |

분류 결과:
- `reports/<site>/` — 분석 대상 (round 1)
- `reports/<site>/_retest/` — retest run (`binning_test_round >= 2`) → round 2로 병합
- `reports/<site>/_excluded/` — 중단된 run 또는 유효성 미달 (빈 bin, workload_dir 불일치 등)

이미 받은 리포트는 다시 받지 않지만, **분류는 매번 재평가**되므로 규칙이 바뀌면 버킷 사이를 이동합니다.

## 2. 분석 — `main.py`

이미 받아둔 리포트로 분석 전체를 돌립니다 (다운로드는 안 함).

```bash
python main.py                    # pega (기본)
python main.py --site rtower
python main.py --site pega --no-open
```

| 옵션 | 설명 |
|---|---|
| `--site` | `pega` / `rtower` (기본 `pega`) |
| `--no-open` | 완료 후 브라우저 자동 실행 안 함 |

실행 순서와 산출물 (`data/<site>/`):
1. `build_fail_data.py` → `fail.csv` — fail 이벤트 (3개 버킷 전부)
2. `collect_srt_history.py` → `srt_history.csv` — run별 요약 (3개 버킷 전부)
3. `build_result_html.py` → `viewer/result.html` — 결과 요약 + fail 케이스 + run history
   (기본으로 브라우저 자동 오픈)
4. `build_fail_timing.py` → `viewer/fail_timing.html` — stage → workload별 fail 타이밍 차트
   (`result.html` 우측 상단 **Fail Timing ↗** 버튼으로 이동)

### `fail.csv`의 `source` 컬럼

`fail.csv`는 이제 세 버킷을 모두 담고, `source` 컬럼으로 구분합니다.

| `source` | 대상 | 쓰는 곳 |
|---|---|---|
| `1st` | `reports/<site>/` (round 1 정상 리포트) | 전부 |
| `retest` | `reports/<site>/_retest/` (재검사 run, round 2) | `result.html`(retest·2nd fail 집계), `fail_timing.html` |
| `excluded` | `reports/<site>/_excluded/` + `common.EXCLUDED_REPORTS` 등록 run | `fail_timing.html` **만** |

`round` 컬럼(`1st`/`2nd`)은 run 자체의 binning round라 그대로 유지되고, `run_bin`은 그 run에서
해당 카드의 최종 TEST_RESULT bin입니다 (`f99-99` = 중간에 끊긴 run).
`model` 컬럼은 그 카드의 제품명(`TEST_RESULT_*.json`의 `category` / `extra.model_name` —
`RBLN-CR13` / `RBLN-CR03`)입니다. 폴더 이름은 항상 CR13이라 이 컬럼이 유일한 구분 수단이고,
값을 걸러 쓰는 곳은 `fail_timing.html`뿐입니다 (아래 집계 규칙 참고).
`result.html` / `srt_history.csv` 등 나머지 산출물은 `source == "excluded"` 행을 버려서
집계 결과가 예전과 동일합니다. `fail_timing.html`만 셋 다 그려서 타이밍 표본을 최대로 씁니다
(마커는 전부 같은 점 — 출처/bin은 툴팁에만 표시).

### Fail Cases의 펼치기

`result.html` → Fail Cases 탭에서 **펼치기**를 하면 round 2(재검사) 이벤트와 함께
`elapsed_s` / `card_temp` / `hbm_temp` 세 컬럼이 추가로 보입니다 (접으면 다시 숨습니다).

`elapsed_s`는 **워크로드 시작부터 fail까지 걸린 초**입니다 (`fail.csv`의 같은 컬럼 =
`fail_at - workload_start_at`). iteration stage는 **그 iteration 안에서의 경과 시간**이라
옆 `stage` 셀의 `iteration(10/15)`와 같이 읽어야 합니다. 워크로드 수행 시간 대비 %로 보려면
`fail_timing.html`을 보세요 — 그쪽은 리포트의 `CR13.json` 값이 아니라 stage별 고정 상수로
%를 계산하므로, 여기 초값을 `workload_duration_s`로 직접 나눈 값과는 다릅니다.

`card_temp` / `hbm_temp`는 fail 시점 ±`MONITOR_WINDOW_S` 구간의 카드 MAX 온도라 옆의
`inlet_temp`(서버 흡기)와 같은 창에서 비교됩니다. `card_temp`는 `fail.csv`의 같은 이름 컬럼
그대로이고, `hbm_temp`는 `fail.csv`가 chiplet별로 갖고 있는 `hbm_cl0_temp`~`hbm_cl3_temp`
**4개 중 MAX**입니다 (제일 뜨거운 chiplet이 CL0이라는 보장이 없어서 4개를 다 기록하고 볼 때만
하나로 줄입니다).

또 재검사 후 최종 Pass한 카드(`test_count >= 2` + `result = Pass`)마다 얇은 `PASS` 행이
하나 붙습니다. Pass한 run은 fail 이벤트가 없어 온도가 아예 안 보이기 때문에,
**inlet_temp만** 채워 넣습니다. 값은 그 카드가 **가장 최근에 fail 났던 test unit(=같은
workload)** 을 Pass run에서 돌 때의 inlet 온도 MAX라, fail run 값과 바로 비교됩니다.
`elapsed_s`와 `card_temp`/`hbm_temp`는 이 행에서 비워둡니다 — Pass run에는 fail 시점이 없고,
카드 온도는 슬롯별 npu 로그에서 와야 하는데 Pass run에서의 슬롯을 여기서 알 수 없어(재장착
가능) 잘못 짚을 수 있습니다.

### Historys 탭 (run history)

`result.html` → **Historys** 탭은 `srt_history.csv`를 그대로 그린 **SRT run 단위 로그**입니다.
다른 탭과 달리 **excluded / retest run도 전부 포함**합니다 — 나머지 산출물은 집계가 흔들리지 않게
excluded를 버리지만, 여기는 "실제로 뭘 돌렸나"를 보는 페이지라 하나도 빼지 않습니다.

출처는 `type` 컬럼 하나에 배지로 표시됩니다.

| 배지 | 뜻 |
|---|---|
| `1st` | round 1 정상 run (`reports/<site>/`) |
| `retest` | `_retest` 버킷 = binning round 2 |
| `excluded` | 분석에서 빠진 run. 배지에 마우스를 올리면 **이유**가 뜹니다 (`srt_history.csv`의 `exclude_reason`: 수동 제외 / `interrupted, bin f99-99` / `report_reject_reason`). 그 run이 retest였으면 `2nd` 배지가 같이 붙고, 행 전체가 옅게 칠해집니다 |

표 위 탭(`All` / `1st` / `retest` / `excluded`)으로 한 종류만 걸러 볼 수 있습니다.

컬럼은 **그 run을 돌린 조건**(start/end, cp_fw, srt_ver)과 **결과**입니다.
(`dcl_clock`은 `srt_history.csv`에는 남아 있지만 표에서는 뺐습니다.)

- `total` / `pass` / `fail` — 그 run의 TEST_RESULT 기준 디바이스 수. `fail`은 spurious(가짜 fail)까지
  포함한 그 run의 FAIL 대수이고, spurious가 섞여 있으면 `3 (false 3)`처럼 표시합니다 — fail이 전부
  false면 실제로는 real fail이 0인 run이라는 뜻입니다.
- `A1/B1/C1/D1` — pass 디바이스의 grade 분포.
- `inlet min` / `inlet max` — 그 run **전체 구간**의 서버 흡기 온도 min/max
  (`server_monitoring_combined.csv`의 `INLET Temp`; 없으면 test unit별 로그로 폴백).
  Fail Cases의 `inlet_temp`가 **fail 순간 ±30초** 창인 것과 달리 여기는 run 전 구간이라,
  그 run이 어떤 열 조건에서 돌았는지를 봅니다. install 단계에서 죽어 모니터링 로그가 아예 없는
  run은 비어 있습니다(`–`).

`run` 셀은 호스트명 + run 타임스탬프이고, 마우스를 올리면 리포트 폴더 이름 전체가 보입니다.

`fail_timing.html`은 **1차로 stage별로 나누고, 각 stage 안에서 workload별로** fail 위치를
워크로드 수행 시간 대비 %로 한 장의 차트에 찍습니다 (stage는 차트 왼쪽 거터).

행(workload) 목록은 **현재 pega SRT 설정**(`reports/pega/<최신 run>/.../workloads/CR13.json`)을
`build_fail_timing.WORKLOADS` 상수로 박아둔 것이고, 이 목록에 있는 것만 그립니다. fail은
`(stage, test_unit_no)` **슬롯**으로 배치하므로, 예전 run이 다른 .bin을 돌렸어도 (rtower의
`ucie_max_48` → iteration/1) 해당 슬롯 행에 모입니다 — 행 이름이 버전마다 갈라지지 않습니다.
실제로 돌린 .bin은 점 툴팁에 `[원래이름.bin]`으로 표시됩니다. 워크로드가 바뀌면 `WORKLOADS`만
갱신하면 됩니다.

**차트**에는 fail이 한 건이라도 있는 workload만 행이 생기지만(점이 없는 행은 노이즈),
**아래 표**는 `WORKLOADS` 전체 슬롯을 그대로 나열합니다. fail 0건인 슬롯은 `fails 0` /
`latest –`로 흐리게 찍히고 span은 그 stage의 고정 구간이 들어갑니다. 그래서 표에 이름이
없으면 "fail이 없다"가 아니라 "현재 설정에 없는 workload다"라는 뜻입니다.

집계 규칙 (페이지 상단에 주석으로도 붙습니다):

- **워크로드 수행 중에 난 fail만** 셉니다. 진입 전에 죽은 `ERR_FAIL_HW_CFG` /
  `ERR_BOOT_FAIL` / `ERR_CARD_NOT_PRESENT`(`EXCLUDE_REASONS`)는 위치가 의미 없는 0%라 제외.
- **중단된 run은 제외**합니다. `fail.csv`의 `run_bin`(그 run에서 그 카드의 TEST_RESULT bin)이
  `f99-99`면 완주하지 못한 테스트라 빼고 셉니다.
- **워크로드 파일 누락 run도 제외**합니다. 호스트에 `.bin`이 없어 retrace가 시작조차 못 하면
  (`rbln_file_open: failed to open file ... (rc -2)`) 그 test unit의 카드 전부가 40~50초 만에
  같이 떨어집니다. 디바이스 fail이 아니라 설비 문제라, build_fail_data가 `wl_missing` 컬럼으로
  표시해두고 타이밍 차트에서 뺍니다.
- **`RBLN-CR13` 카드만** 그립니다 (`build_fail_timing.MODEL`, `fail.csv`의 `model` 컬럼 기준).
  리포트 폴더 이름은 전부 `RBLN-CR13_SRT_...`이지만 실제로는 CR03 카드를 돌린 run이 섞여 있고
  (rtower의 `shw-suma-06-amd` 호스트), CR03은 워크로드 구성 자체가 이 `WORKLOADS`/stage 고정
  시간과 다르므로 위치 %를 같은 잣대로 볼 수 없습니다. `model`이 빈 행(필드가 없던 옛 리포트)은
  통과시킵니다. **이 필터는 `fail_timing.html`에만** 적용되고, `fail.csv`·`result.html`·
  `srt_history.csv`는 CR03을 그대로 포함합니다.

호스트가 `server.log`를 run마다 로테이트하지 않아서 뒤 리포트가 앞 세션의 `[fail_binning]` 줄을
다시 담는 경우가 있고, 그러면 같은 fail이 리포트 수만큼 점으로 찍힙니다. 이건 리포트에 기록된
그대로 두고 별도 병합은 하지 않습니다.

점의 색·모양은 fail 종류를 나타냅니다 — **f31(HBM)** 빨강 원, **f22(HIGH_TEMP)** 청록 원,
**Golden mismatch** 보라 마름모, **ERR_INVALID_CMD** 초록 삼각형, 나머지는 중립 회색 원. 앞의
둘은 bin 코드(`DOT_BINS`)로 잡고, 뒤의 둘은 자기 bin이 없어서 — Golden mismatch는 test unit별로
f171/f174/f192/f234 … 로 흩어져 binning되고, invalid cmd는 reason에만 이름이 있습니다 — 각각
`fail_detail`(`DOT_DETAILS`) / `reason`(`DOT_REASONS`)으로 잡습니다. 범례에 넷 다 이름이 적혀
있어 색만으로 판단하지 않아도 되고, 뒤의 둘은 모양까지 다릅니다 (라이트의 초록↔빨강, 다크의
보라↔청록이 CVD 분리 6~8 구간이라 모양이 보조 인코딩으로 필요합니다). 점 툴팁에는
`reason bin (fail_detail)`이 그대로 들어가서, 색으로 구분하지 않는 나머지 fail의 실제 내용도
확인할 수 있습니다.

workload는 **이름 글자 자체의 색**으로 구분합니다 (차트 왼쪽 거터 + 아래 표 모두. 검증된 8색
categorical 팔레트, 라이트/다크 각각 별도 스텝). 같은 workload는 stage가 달라도 같은 색이라
prestress ↔ poststress, enhanced ↔ baseline을 눈으로 바로 대응시킬 수 있습니다.

글자에 쓰는 색은 점·칩에 쓰는 **mark 스텝이 아니라 별도의 text 스텝**(`--t1`~`--t9`)입니다.
mark 스텝을 13px 글자에 그대로 쓰면 대비가 최저 2.1:1(c4)까지 떨어지므로, 색상(hue)은 유지하고
명도만 내려/올려 **9색 전부 각자 배경에서 4.5:1 이상**이 되는 값으로 다시 잡았습니다
(라이트 4.50~8.33:1, 다크 4.52~5.67:1). 인접 슬롯 c4↔c3의 CVD 분리는 7.8(protan)로 "secondary
encoding 필수" 구간인데, 여기서는 **색이 얹혀 있는 그 이름 자체**가 secondary encoding이라
색만으로 workload를 구분해야 하는 상황이 생기지 않습니다.

기준 시간은 **stage별 고정 상수**입니다 (`build_fail_timing.STAGE_DURATION_S`). 리포트의
`CR13.json`이 400s / 1000s / 3550s로 잡혀 있어도 무시하고 아래 값으로 계산합니다 — 그래야 run이
달라도 %가 같은 잣대로 비교됩니다.

| stage | 고정 구간 |
|---|---|
| `iteration` | 180s × 15 = **2700s** |
| `prestress` / `poststress` | **1580s** |
| `enhanced_stress` / `baseline_stress` | **7100s** |

`iteration` stage만 반복 실행되므로 몇 번째 iteration에서 났는지까지 반영해
`(iteration-1) × 180 + elapsed` 위치에 찍습니다 (차트의 점선 눈금 = iteration 경계).
나머지 stage는 워크로드를 한 번만 돌리므로 위치가 곧 `elapsed`입니다.
차트에서 빼고 싶은 워크로드는 `EXCLUDE_WORKLOADS`에 이름(`.bin` 제외)을 추가하면 됩니다.

## `--site` 설정

[common.py](common.py)의 `SITES`에서 관리합니다.

| site | API URL | reports | data | 기본 since |
|---|---|---|---|---|
| `pega` (기본) | `http://192.168.101.100` | `reports/pega` | `data/pega` | 2026-07-09 |
| `rtower` | `http://192.168.7.151` | `reports/rtower` | `data/rtower` | 전체 |

사이트를 추가하려면 `SITES`에 항목 하나만 넣으면 됩니다. 두 스크립트 모두 여기서 URL과 경로를 읽습니다.

## 전형적인 사용 흐름

```bash
python download_srt_reports.py --site rtower   # 새 리포트 수집
python main.py --site rtower                   # 분석 + result.html 열기
```
