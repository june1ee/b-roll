# sauger

릴스 조립기. 자막 줄 + 줄별 녹음 + 영상 소스를 주면 타이밍을 맞춰 조립하고 미리보기 mp4를 뽑는다.
계획과 배경은 [`docs/PLAN.md`](docs/PLAN.md), 자막 스타일은 [`style.yml`](style.yml).

## 지금 되는 것 (M1 + M2)

```bash
uv sync
uv run sauger build reels/0806.yml --capcut 0806-sauger --no-render   # 캡컷 드래프트만
uv run sauger build reels/0806.yml --capcut 0806-sauger               # 드래프트 + 미리보기 mp4
```

- 줄별 녹음에서 **실제 말한 구간만** 잘라 이어 붙인다 (앞뒤 무음·군더더기 테이크 제거)
- **러프컷 다듬기** — 넉넉히 잘라 준 클립을 자막·오디오 길이에 맞게 줄인다 (기본 가운데 정렬, `fit: start|end`)
- **캡컷 드래프트 출력** — `template:` 프로젝트를 복제하고 세그먼트만 교체한다.
  자막 3층(메인 / 시각 / 괄호 부연)의 폰트·크기·테두리·그림자·위치가 그대로 따라온다.
  캡컷이 켜져 있으면 몇 초 안에 목록에 뜬다. 음악·TTS 는 거기서 얹으면 된다.
- 미리보기 mp4 + 컷별 **스토리보드 PNG** — 타이밍 확인용 러프 프록시다.
  자막의 정확한 생김새는 캡컷이 렌더하는 쪽이 정답이고, 여기 번인 자막은 근사치일 뿐이다.

출력은 `reels/.sauger/<이름>/` 아래 (`timeline.json`, `storyboard.png`, 중간 세그먼트).

## 아직 안 되는 것

- **M4** `--from-capcut` 역방향 (캡컷 TTS 먼저 만들고 컷 채우기)
- 컷 구간 자동 선택 — 러프컷을 받아 다듬는 쪽으로 방향을 잡아서 당분간 안 한다

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
