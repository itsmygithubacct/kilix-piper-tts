# kilix-piper-tts

`kilix-piper-tts` is the isolated neural speech provider used by Kilix Voice.
It keeps one Piper model loaded in a short-lived local service, while callers
send only bounded text, a registered model ID, and a preset speaking rate.
Callers cannot supply executable paths, model paths, URLs, or Piper phonemes.

The initial catalog deliberately contains one voice:
`piper-en-us-kristin-medium`. It is Piper's single-speaker
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

## Safety and licensing

- Model IDs and artifact URLs are compiled into the provider catalog.
- Downloads are staged, checksum-verified, and atomically published.
- The service permits one bounded synthesis job at a time and kills/reloads
  its worker if the caller cancels or inference exceeds its deadline.
- Text containing Piper's raw `[[phoneme]]` syntax is rejected.
- Spoken text and generated samples are never logged or retained by the
  provider.

This provider is GPL-3.0-or-later because it links to Piper. The Kristin model
card identifies the underlying LibriVox recording as public domain. The voice
artifacts are downloaded separately and retain their upstream terms.
