# broll

*[English](README.md)*

릴스 조립기. 자막 줄 + 줄별 녹음 + 영상 소스를 주면 타이밍을 맞춰 조립하고 미리보기 mp4를 뽑는다.
계획과 배경은 [`docs/PLAN.md`](docs/PLAN.md), 자막 스타일은 [`style.yml`](style.yml).

## 쓰는 법

### 기본 - 캡컷에서 녹음까지 하고 넘기기 (권장)

```bash
uv run broll build reels/내릴스.yml --from-capcut 0811
```

1. 캡컷에서 녹음하고 속도를 맞춘다 (TTS 를 써도 된다)
2. yml 에 자막과 줄마다 쓸 영상 소스만 적는다
3. 위 명령 → `0811-broll` 프로젝트가 생긴다. 음악 얹고 마감.

타이밍을 추정하지 않는다 - 캡컷에서 확정한 값을 그대로 쓰므로 정렬 오차가 아예 없다.
n번째 목소리 세그먼트 ↔ n번째 줄로 1:1 짝짓는다.

### 대안 - 녹음 파일을 주고 정렬을 맡기기

```bash
uv run broll build reels/내릴스.yml --capcut 내릴스-out
```

줄마다 `rec:` 로 녹음을 주면 whisper + 무음 검출로 말한 구간을 찾아 트리밍하고,
템플릿이 쓰던 배속을 적용한다. 캡컷에서 녹음하지 않은 경우에만 쓴다.

### 공통

- **러프컷 다듬기** - 넉넉히 잘라 준 클립을 목소리 길이에 맞게 줄인다 (기본 가운데 정렬, `fit: start|end`, `from:` 으로 못 박기)
- **캡컷 드래프트 출력** - `template:` 프로젝트를 복제하고 세그먼트만 교체한다.
  자막 층(메인 / 시각 / 괄호 부연)의 폰트·크기·테두리·그림자·위치가 그대로 따라온다.
  캡컷이 켜져 있으면 몇 초 안에 목록에 뜬다.
- 미리보기 mp4 + 컷별 **스토리보드 PNG** - 타이밍 확인용 러프 프록시다.
  자막의 정확한 생김새는 캡컷이 렌더하는 쪽이 정답이다.

**포맷 = 캡컷 프로젝트 하나.** 새 포맷을 쓰려면 캡컷에서 그 스타일로 프로젝트를 하나
만들어두고 `template:` 에 이름만 적으면 된다. 코드에 박아둔 스타일 값은 없다.

출력은 `reels/.broll/<이름>/` 아래 (`timeline.json`, `storyboard.png`, 중간 세그먼트).

## 설계 메모

**정렬은 두 신호를 나눠 쓴다.** whisper.cpp 타임스탬프는 경계가 0.3~1.3초씩 밀린다(실측).
그래서 *무엇을 말했나*만 whisper에서 가져오고, *어디서 시작하고 끝났나*는 `silencedetect`로 잡는다.

**자막은 대사의 전사가 아니라 요약이다.** June의 실제 릴스에서 8줄 중 4줄이 그랬다. 그래서
텍스트 일치도가 높고(≥0.80) 들린 말의 60% 이상을 덮을 때만 텍스트로 구간을 좁히고,
아니면 첫 테이크를 통째로 쓴다. 뒤에 긴 침묵을 두고 붙은 덩어리는 실측상 군더더기였다.

**녹음은 wav로 디코드해 캐시한다.** 캡컷 녹음은 raw ADTS AAC라 컨테이너가 보고하는 길이가
실제와 달라서, 그대로 트림하면 구간이 조용히 최대 0.9초까지 짧게 잘린다.

## 필요한 것

```bash
brew install whisper-cpp ffmpeg-full   # ffmpeg 기본 병에는 libass 가 없어 자막을 못 굽는다
curl -L --fail -o ~/.cache/broll/models/ggml-large-v3-turbo.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
```

`ffmpeg-full` 은 keg-only라 PATH에 안 잡히지만 broll가 알아서 찾는다 (`BROLL_FFMPEG_DIR` 로 지정 가능).
