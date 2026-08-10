# sauger

릴스 조립기. 자막 줄 + 줄별 녹음 + 영상 소스를 주면 타이밍을 맞춰 조립하고 미리보기 mp4를 뽑는다.
계획과 배경은 [`docs/PLAN.md`](docs/PLAN.md), 자막 스타일은 [`style.yml`](style.yml).

## 지금 되는 것 (M1)

```bash
uv sync
uv run sauger build reels/0806.yml            # 타임라인 + 미리보기 mp4 + 스토리보드
uv run sauger build reels/0806.yml --no-render # 타이밍만 계산
```

- 줄별 녹음에서 **실제 말한 구간만** 잘라 이어 붙인다 (앞뒤 무음·군더더기 테이크 제거)
- 자막을 번인한다 — 프리텐다드 흰색, 얇은 테두리 + 연한 그림자, `*강조*` 는 연핑크
- 컷마다 한 프레임씩 뽑은 **스토리보드 PNG** 로 한눈에 검수

출력은 `reels/.sauger/<이름>/` 아래 (`timeline.json`, `storyboard.png`, 중간 세그먼트).

## 아직 안 되는 것

- **M2** 캡컷 드래프트 출력 (`src/sauger/capcut.py` 는 읽기만 구현)
- **M3** 소스 안에서 컷 구간 자동 선택 — 지금은 `from:` 지정값 또는 0초부터
- **M4** `--from-capcut` 역방향 (캡컷 TTS 먼저 만들고 컷 채우기)

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
curl -L --fail -o ~/.cache/sauger/models/ggml-large-v3-turbo.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
```

`ffmpeg-full` 은 keg-only라 PATH에 안 잡히지만 sauger가 알아서 찾는다 (`SAUGER_FFMPEG_DIR` 로 지정 가능).
