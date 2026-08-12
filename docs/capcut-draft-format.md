# The CapCut desktop draft format

Notes from reverse-engineering CapCut 9.x project files on macOS, gathered while
building [b-roll](https://github.com/june1ee/b-roll). CapCut publishes no schema, so
everything here is measured against real projects rather than documented behaviour.

Verified against CapCut 9.1.4443 and 9.2.0 on macOS, draft `new_version` 173 to 181.

## Where projects live

```
~/Movies/CapCut/User Data/Projects/com.lveditor.draft/
├ root_meta_info.json          index of every project
└ <project name>/
    ├ draft_info.json          the timeline document (plain JSON)
    ├ draft_meta_info.json     name, duration, paths (used by the project list)
    ├ Timelines/
    │   ├ project.json         main_timeline_id and the timeline list
    │   └ <timeline id>/
    │       └ draft_info.json  a second copy of the same document
    ├ audio_record/*.aac       in-app voice recordings (raw ADTS)
    ├ textReading/*.wav        text-to-speech output
    └ draft_cover.jpg, key_value.json, attachment_*.json, ...
```

Two things surprise people coming from older write-ups:

- **CapCut 9.x uses `draft_info.json`, not `draft_content.json`.** Tools written against
  the older schema (pyJianYingDraft, pyCapCut) will not find the file they expect.
- **It is plain JSON, not encrypted.** JianYing (the Chinese build) encrypts drafts from
  6.0 onward; the international CapCut build does not, at least through 9.2.

## The timeline document

```jsonc
{
  "id": "46A2CA2E-...",          // this is the TIMELINE id, not the project id
  "duration": 21633333,          // microseconds, everywhere
  "fps": 30.0,
  "canvas_config": { "width": 1080, "height": 1920 },
  "tracks": [
    {
      "type": "video",           // video | text | audio
      "segments": [
        {
          "id": "...",
          "material_id": "...",              // -> materials.videos[].id
          "target_timerange": { "start": 0, "duration": 2970000 },
          "source_timerange": { "start": 14800000, "duration": 2970000 },
          "speed": 1.0,
          "extra_material_refs": ["...", "..."],
          "clip": { "transform": {...}, "scale": {...} }
        }
      ]
    }
  ],
  "materials": {
    "videos": [], "texts": [], "audios": [],
    "speeds": [], "canvases": [], "effects": [], "hsl": [],
    "material_animations": [], "placeholder_infos": [],
    "sound_channel_mappings": [], "vocal_separations": [],
    "audio_fades": [], "beats": [], "material_colors": []
  }
}
```

**Time is in microseconds.** Divide by 1,000,000.

**`target_timerange` is where a segment sits on the timeline. `source_timerange` is what
it consumes from the source file.** With a speed change the two differ:

```
source_duration = target_duration x speed
```

Measured: a segment with `target 1.567s` and `speed 1.4` has `source 2.193s`.

## Five things that will break your writer

Each of these produced a specific, confusing failure. They are the reason this document
exists.

### 1. Every segment must own its extra materials

`extra_material_refs` points at entries in `materials.speeds`, `canvases`, `effects` and
friends. In a real project **no two segments ever share one**. If you clone a segment as a
prototype and keep its refs, several segments end up pointing at the same speed or canvas
material, and **CapCut opens the project but the preview will not play at all** - no error,
just a dead player.

Clone the referenced materials too and rebind the refs to fresh ids.

### 2. The timeline id chain

```
draft_info.id  ==  Timelines/project.json → main_timeline_id  ==  Timelines/<that id>/
```

This holds in every project. Mint a new UUID for `draft_info.id` and CapCut cannot resolve
the main timeline: it shows **an extra empty timeline tab and a player stuck at
00:00:00:00**, even though all the tracks are visibly there.

The project identifiers you *should* regenerate when copying a project are
`Timelines/project.json → id` and `draft_meta_info.json → draft_id`.

### 3. There are two copies of the document

`draft_info.json` at the project root and `Timelines/<main timeline id>/draft_info.json`
are separate files with the same content. Write only the root and CapCut may open the
stale copy.

### 4. Text style ranges are counted in UTF-16 code units

A text material's `content` is a JSON string:

```jsonc
{
  "text": "여기서도 방해하는 보안프로그램😡",
  "styles": [
    { "range": [0, 18], "size": 13.2, "fill": {...}, "strokes": [...] }
  ]
}
```

That caption is 17 Python characters but **18 UTF-16 code units** - the emoji is a
surrogate pair. Write `[0, 17]` and the trailing unit falls outside the styled range,
renders with default styling, and **that part of the line jumps in size and shifts the
caption**.

```python
def u16len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2
```

### 5. Positions are relative to half the frame height

A text segment's `clip.transform.y` is normalised, negative meaning downward, but the
divisor is **half** the canvas height:

```
y_pixels_from_top = height / 2 * (1 - transform.y)
```

Confirmed by checking three caption layers at `-0.272 / -0.442 / -0.589` against where they
actually render in a 1080x1920 frame. Note that the number CapCut shows in its own UI is
`transform.y * height`, twice the actual pixel offset, so do not take the UI value as pixels.

`clip.scale.x` and `.y` are independent; captions are often squeezed horizontally
(`x: 0.84, y: 1.08`).

## Materials worth knowing

### `audios[].type`

| type | what it is |
|---|---|
| `record` | recorded in CapCut, file under `audio_record/`, raw ADTS AAC |
| `text_to_audio` | TTS, file under `textReading/*.wav`, carries `tone_speaker` and related voice metadata |
| `music` | from the CapCut library |
| `sound` | sound effect |

TTS output is written to disk locally, so a project where you only generated speech is a
complete source of timings and audio.

**Recordings are raw ADTS AAC and their container duration disagrees with the decoded
length.** Trimming them directly with `ffmpeg -ss/-t` silently loses up to 0.9 seconds in
measurements. Decode to wav first.

### Path placeholders

Files inside the project folder are referenced with a placeholder prefix:

```
##_draftpath_placeholder_0E685133-18CE-45ED-8CB8-2904A212EC80_##/textReading/6a5b66.wav
```

Everything after `_##/` is relative to the project folder. External media (camera files and
so on) is stored as a plain absolute path.

### Text tracks are layers, not roles

A project can have several text tracks, and CapCut does not keep a fixed meaning per track.
In one 8-cut reel, four text tracks held a mix of clock overlays, main captions and
parenthetical asides, with the same track holding different roles at different times.
Classify by content, not by track index.

## The project index

`root_meta_info.json` at the drafts root lists every project. **You do not need to edit
it** - while CapCut is running it picks up a new project folder within seconds. Worse, if
CapCut is open it will rewrite that file from its own cache, so your edits are lost and the
duration shown in the list can go stale even when the project on disk is correct.

Close a project in CapCut before writing to it. An open project may be saved back over your
changes.

## Reading a draft

```python
import json
from pathlib import Path

US = 1_000_000
root = Path.home() / "Movies/CapCut/User Data/Projects/com.lveditor.draft" / "my-project"
draft = json.loads((root / "draft_info.json").read_text(encoding="utf-8"))

videos = {v["id"]: v for v in draft["materials"]["videos"]}
for track in draft["tracks"]:
    if track["type"] != "video":
        continue
    for seg in track["segments"]:
        tr, sr = seg["target_timerange"], seg["source_timerange"]
        print(f"{tr['start']/US:6.2f}s +{tr['duration']/US:5.2f}s "
              f"from {sr['start']/US:6.2f}s at {seg['speed']}x "
              f"{videos[seg['material_id']]['material_name']}")
```

## Writing a draft

Generating a project from scratch means reproducing fonts, strokes, shadows, animations,
effects and HSL settings by hand, and getting all of them right. A far shorter path is to
**copy an existing project and replace only the segments**, using each existing segment and
material as a prototype. Styling then carries over for free.

If you do that, the five failures above are exactly what you will hit. b-roll checks all of
them after every write:

| Invariant | Symptom when broken |
|---|---|
| Extra materials owned per segment | Preview will not play |
| Timeline id chain intact | Empty extra tab, player at 00:00:00:00 |
| Root and sub timeline documents agree | CapCut opens stale content |
| Video track contiguous | Black frames between cuts |
| Style range counted in UTF-16 | Emoji lines oversized and shifted |
| Referenced media exists | Missing-media errors |

## Corrections welcome

This is measured behaviour from a handful of projects on one machine, not a specification.
If something differs on your CapCut version, an issue with the version number and the
relevant JSON fragment is very welcome.
