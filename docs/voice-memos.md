# Voice memos

Optional. Needs your own transcription service — no model ships with the app. The [README](../README.md) has the overview.

## Voice memos (optional)

Open the microphone button in the header, hold the record button, speak, release.
For anything longer than a sentence, swipe up while holding: the recording locks
and keeps running with your thumb off the button, and the next tap stops it. The
recording is transcribed, cleaned up by whichever chat model you have selected —
filler removed, bullet lists and tables where the content calls for them, nothing
added and nothing dropped — and shown to you for review. Save, and it becomes one
Markdown file per memo in `PaperlessBrain Memos/` inside your vault, named
`26-08-08 Topic.md`, searchable in chat like any other note.

The dialog also takes an **existing audio file** — wav, m4a, mp3, ogg, flac —
which is the way in for a recording made on your phone's own voice recorder or a
call you already captured. And with no microphone at all (or no HTTPS) you can
simply type the memo. All three routes end in the same review-then-file step.

It is **not** a chat feature. Pressing the button is what tells the app you mean
a memo, so nothing has to guess whether "remind me to…" was meant as a memory, a
deadline or a note to yourself.

**You need a transcription service.** PaperlessBrain doesn't ship one — no
bundled model, no extra dependency. Anything with an OpenAI-compatible
`/v1/audio/transcriptions` endpoint works:

| Option | Notes | Speaker labels |
|---|---|---|
| [hwdsl2/docker-whisper](https://github.com/hwdsl2/docker-whisper) | faster-whisper in one container, API key generated for you, CPU and CUDA images | yes — set `WHISPER_DIARIZATION=true` |
| [Speaches](https://github.com/speaches-ai/speaches) | loads models on demand, also does TTS; no auth by default | no |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) | no container needed; run `whisper-server --inference-path /v1/audio/transcriptions --convert` | no |
| Groq / OpenAI | hosted; your dictation leaves the house | no |

The last column is what conversation mode needs. Without it everything still
works — you get an ordinary unlabelled transcript.

```bash
WHISPER_URL=http://127.0.0.1:9000/v1      # empty = feature hidden everywhere
WHISPER_API_KEY=
WHISPER_MODEL=large-v3-turbo              # what this project runs; see below
WHISPER_LANGUAGE=de                       # empty = auto-detect
```

`large-v3-turbo` is the model this project runs day to day: near large-v3
accuracy at a fraction of the cost, and good enough on German that it rarely
needs correcting. Use it if your service can load it. The code default is
`whisper-1` instead, because that is OpenAI's model id and the safe answer for a
hosted endpoint — and most self-hosted servers ignore the field entirely and
serve whatever they already loaded, so it does no harm there.

Then switch it on in **Settings > Voice memos** (on by default once the URL is
set). When it's configured, the chat mic switches to the same service too —
noticeably better than the browser's built-in speech recognition, at the cost of
no live preview while you talk.

### Conversation mode

The dialog has a **Memo / Conversation** switch. In conversation mode the
transcript comes back as speaker turns:

```markdown
**Speaker 1:** The quote lands at 14,200 including the worktop.

**Speaker 2:** And the deadline?

**Speaker 1:** End of March.
```

Three things worth knowing before you rely on it:

- **Your transcription service has to do the diarization.** PaperlessBrain asks
  for `response_format=verbose_json` and reads the per-segment `speaker` field;
  a service without a diarizer returns no such field and you get an ordinary
  unlabelled transcript, no error. For `hwdsl2/whisper-server` set
  `WHISPER_DIARIZATION=true` (it bundles a sherpa-onnx diarizer, no GPU needed).
- **Speakers are numbered, never named.** Diarization is unsupervised: it can
  tell two voices apart but has no idea whose they are. You get `Speaker 1` /
  `Speaker 2`, renumbered by who speaks first, and you rename them yourself in
  the review step if you want real names.
- **Attribution is segment-level.** A Whisper segment spanning a speaker change
  is assigned wholesale to whoever talked most in it, so quick interjections and
  cross-talk land on the wrong person. Good for "who said roughly what", not a
  verbatim record.

Conversation mode also runs a different rewrite prompt — one that preserves turn
order and never merges speakers — and uses the larger `CONVERSATION_MAX_*` caps,
since a meeting is not a memo. If only one voice is detected, the labels are
dropped and you get ordinary prose.

CPU is fine. On 8 cores with `large-v3-turbo`, 30 seconds of German speech
transcribes in about 6 seconds. Don't put it on a machine the
[power management](operations.md#power-management-optional) watchdog shuts down — waiting for
a wake-on-LAN boot defeats the point of quick capture.

Two things to know:

- **Recording needs HTTPS.** Browsers only grant microphone access over HTTPS or
  on `localhost`. Over plain `http://192.168.x.x:8080` the record button is
  disabled and says so. You can still type a memo.
- **Silence produces text.** Whisper answers non-speech with a confident stock
  phrase rather than nothing, so an accidental press would otherwise file a
  convincing memo about nothing. Recordings that are too short, or that come
  back as one of the known filler phrases, are rejected before anything is
  written.
