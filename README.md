# kilix-piper-tts

`kilix-piper-tts` is the isolated neural speech provider used by Kilix Voice.
It keeps one Piper model loaded in a short-lived local service, while callers
send only bounded text, a registered model ID, and a preset speaking rate.
Callers cannot supply executable paths, model paths, URLs, or Piper phonemes.

**Status:** the provider's code is not in the Plebian-OS image. Kilix fetches
and builds a pinned commit of it in the user's home the first time a Piper
voice is installed (`kilix tts --install=kilix-female-02`,
`kilix tts --enable-kristin` or `kilix tts --tier=neural`). Kilix 95 runs the
Female-02 install itself, without a question, when the desktop first starts
(`kilix wizard install-default-voice`). The 0.2.3 ISO also carries the
Female-02 files, as a Kilix Content asset in its offline default model bundle;
Kristin's files are downloaded only when that voice is installed.
Kilix for Plebian-OS 0.2.3 pins commit `ad84b296`
from the `work/0.2.2-female02` branch, which adds a download of Female-02 from
the kilix-voice release; this README describes `main`.

## What it does

- Runs Piper 1.7.0 (pinned in `uv.lock`) on the CPU in a worker process owned
  by a private per-user Unix-socket service.
- Offers a fixed catalog of two voices: `piper-en-us-kristin-medium` and
  `kilix-female-02` checkpoint 80.
- Downloads or imports voice files only on an explicit `install`, verifies
  every file against a pinned SHA-256 and publishes it atomically.
- Writes WAV files without overwriting, and streams raw PCM to Kilix Voice.

The catalog includes `kilix-female-02` checkpoint 80 and
`piper-en-us-kristin-medium`. The latter is Piper's single-speaker
`en_US-kristin-medium` model at 22,050 Hz. The model, configuration, and model
card are pinned to one `piper-voices` commit and exact SHA-256 digests. Piper
1.7.0 is pinned in `uv.lock`; inference is CPU-only.

## Install and try it

This repository expects the Kilix modules to be sibling checkouts so its
explicit model installer can reuse `kilix-content`'s checksum-enforcing
download primitive.

```sh
uv sync --locked
uv run kilix-piper-tts models
uv run kilix-piper-tts install piper-en-us-kristin-medium
uv run kilix-piper-tts synthesize \
  "Hello from Kristin, the first Piper voice for Kilix." \
  --output kristin.wav
```

`models`, `status`, and `verify` never download. Only the explicit `install`
action uses the network. A synthesis command starts the private provider on
demand; it exits after five idle minutes. The socket lives below
`$KILIX_SESSION_HOME/voice` (or the standard Kilix session directory), mode
0600, in a mode-0700 directory. Linux peer credentials restrict it to the
service owner's UID.

For Kilix Voice, install the command into its isolated uv tool environment.
The Make target includes the sibling `kilix-content` package in that
environment so future explicit installs retain Kilix's shared downloader:

```sh
make install
kilix-tts --install piper-en-us-kristin-medium
kilix-tts --speak "Kilix is speaking with Kristin." \
  --model piper-en-us-kristin-medium
```

`kilix-tts --output speech.wav` and `--output speech.mp3` continue to provide
private, no-overwrite file export for every registered engine.

## Usage

| Command | What it does |
|---|---|
| `kilix-piper-tts models [--json]` | List the voice catalog and what is installed. |
| `kilix-piper-tts install [MODEL] [--model-file F --config-file F]` | Download a voice, or import a pinned local export. |
| `kilix-piper-tts verify [MODEL]` | Hash every installed file of a voice. |
| `kilix-piper-tts status [--model MODEL] [--json]` | Installed, running and loaded state, and the socket path. |
| `kilix-piper-tts synthesize TEXT\|--stdin --output F.wav [--model M] [--rate R]` | Speak text into a new WAV file. Rates: 120, 150, 170, 200, 240 wpm. |
| `kilix-piper-tts serve [--idle-seconds N]` | Run the provider in the foreground (default idle exit 300 s). |

`make test` runs the unit tests and `make lint` byte-compiles the sources.

### Kilix Female-02

Female-02 is imported from an existing local export. Its ONNX and JSON hashes
are pinned in the catalog; a different checkpoint is refused. Importing it
does not download or modify the training files:

```sh
kilix-piper-tts install kilix-female-02 \
  --model-file /absolute/export/voice.onnx \
  --config-file /absolute/export/voice.onnx.json
kilix-piper-tts status --model kilix-female-02 --json
kilix-piper-tts synthesize "Hello from Kilix." \
  --model kilix-female-02 --output female-02.wav
```

Female-02 preserves the exported speed scale and uses unity gain without
per-sentence peak normalization. The service keeps one selected model resident;
switching voices replaces that worker instead of retaining two models. Status
identifies the requested voice, checkpoint and model hash. Updating the catalog
and installed export can advance Female-02 without changing its callers.

## How it works

```
kilix-tts / kilix-piper-tts synthesize
        |  length-prefixed JSON frames, UID-checked AF_UNIX socket
        v
kilix-piper-tts serve  (one job at a time, exits after 5 idle minutes)
        |  multiprocessing pipe
        v
Piper worker process   (one voice loaded, CPU, killed on cancel or deadline)
```

The client (`client.py`) connects to `piper.sock` under the Kilix session
directory and starts `python -m kilix_piper_tts serve` in a new session if no
service is listening. The service (`service.py`) checks the caller's UID with
`SO_PEERCRED`, accepts one bounded request (`protocol.py`: at most 8,192 characters and
32 KiB of UTF-8 text, a catalog model ID and one of the preset rates) and hands it to the
worker (`runtime.py`), which loads the voice with `use_cuda=False`. A cancelled
or overdue job kills the worker, and the next request reloads it.

Voices install under `$KILIX_DATA_HOME/voice/models` (by default
`~/.local/gpu_terminal/kilix/data/voice/models`). The installer
(`installer.py`) borrows the checksum-enforcing download from `kilix-content`,
stages the files, checks size and SHA-256, and renames the directory into
place with a manifest.

## Safety and licensing

- Model IDs and artifact URLs are compiled into the provider catalog.
- Downloads are staged, checksum-verified, and atomically published.
- The service permits one bounded synthesis job at a time and kills/reloads
  its worker if the caller cancels or inference exceeds its deadline.
- Text containing Piper's raw `[[phoneme]]` syntax is rejected.
- Spoken text and generated samples are never logged or retained by the
  provider.

## Documentation

This repository has no separate `docs/` directory; this README and the module
docstrings under `src/kilix_piper_tts/` are its documentation.

## Related repositories

- [kilix-voice](https://github.com/itsmygithubacct/kilix-voice#readme): the
  read-aloud engine and `kilix-tts`, which calls this provider.
- [kilix-content](https://github.com/itsmygithubacct/kilix-content#readme): the
  catalog and checksum-enforcing downloader the installer reuses.
- [kilix](https://github.com/itsmygithubacct/kilix#readme): pins and installs
  this provider (`scripts/install-kilix-piper-tts.sh`).
- [kilix-whisper-stt](https://github.com/itsmygithubacct/kilix-whisper-stt#readme):
  the matching dictation provider, built the same way.

## Part of Plebian-OS

kilix-piper-tts is one of the repositories behind
[Plebian-OS](https://github.com/itsmygithubacct/plebian-os#readme), a Debian
install whose desktop is [Kilix](https://github.com/itsmygithubacct/kilix#readme)
running the [Kilix 95](https://github.com/itsmygithubacct/kilix-95#readme)
desktop through [Pleb](https://github.com/itsmygithubacct/pleb#readme).
Kilix installs a pinned commit of it on first use; on Plebian-OS that is the
first desktop start, when Kilix 95 installs the default Female-02 voice. The map of every repository and how it ships is on the
[components page](https://github.com/itsmygithubacct/plebian-os/blob/main/docs/components.md).

## License

This provider is GPL-3.0-or-later because it links to Piper. The Kristin model
card identifies the underlying LibriVox recording as public domain. The voice
artifacts are downloaded separately and retain their upstream terms.
