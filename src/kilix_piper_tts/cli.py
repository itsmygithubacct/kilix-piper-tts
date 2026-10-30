"""Command-line interface for the Kilix Piper provider."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import wave

from . import __version__, catalog, client, installer, paths, protocol, service


def _model_record(model: catalog.ModelSpec, *, verify: bool = False) -> dict:
    installed, detail = installer.inspect_install(
        model.catalog_id, verify_hashes=verify
    )
    return {
        "id": model.catalog_id,
        "voice": model.voice,
        "language": model.language,
        "quality": model.quality,
        "sample_rate": model.sample_rate,
        "checkpoint": model.checkpoint,
        "local_import": model.checkpoint is not None,
        "download_bytes": model.download_size,
        "installed": installed,
        "detail": detail,
        "summary": model.summary,
    }


def _wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()


def _write_new(path: str, data: bytes) -> None:
    target = os.path.abspath(os.path.expanduser(path))
    descriptor = -1
    created = False
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        if created:
            try:
                os.unlink(target)
            except FileNotFoundError:
                pass
        raise


def _stdin_text() -> str:
    raw = sys.stdin.buffer.read(protocol.MAX_TEXT_BYTES + 1)
    if len(raw) > protocol.MAX_TEXT_BYTES:
        raise ValueError(f"standard input exceeds {protocol.MAX_TEXT_BYTES} bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"standard input is not valid UTF-8: {error}") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kilix-piper-tts",
        description="Install and run the sandboxed persistent Piper provider for Kilix.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    models = commands.add_parser("models", help="list the immutable voice catalog")
    models.add_argument("--json", action="store_true", dest="as_json")

    install = commands.add_parser("install", help="explicitly download one voice")
    install.add_argument("model", nargs="?", default=catalog.MODEL_ID, choices=catalog.MODEL_IDS)
    install.add_argument("--model-file", help="import a checksum-pinned local ONNX export")
    install.add_argument("--config-file", help="matching local ONNX JSON configuration")

    verify = commands.add_parser("verify", help="hash every installed voice artifact")
    verify.add_argument("model", nargs="?", default=catalog.MODEL_ID, choices=catalog.MODEL_IDS)

    status = commands.add_parser("status", help="show installed and running state")
    status.add_argument("--json", action="store_true", dest="as_json")
    status.add_argument("--model", default=catalog.MODEL_ID, choices=catalog.MODEL_IDS)

    serve = commands.add_parser("serve", help="run the private provider in the foreground")
    serve.add_argument("--idle-seconds", type=float, default=300.0)

    synth = commands.add_parser("synthesize", help="synthesize arbitrary ordinary text")
    synth.add_argument("text", nargs="?")
    synth.add_argument("--stdin", action="store_true")
    synth.add_argument("--model", default=catalog.MODEL_ID, choices=catalog.MODEL_IDS)
    synth.add_argument("--rate", type=int, default=170, choices=protocol.RATE_CHOICES)
    synth.add_argument("--output", metavar="WAV")
    synth.add_argument("--raw", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "models":
            records = [_model_record(model) for model in catalog.MODELS]
            if args.as_json:
                print(
                    json.dumps(
                        {"schema": catalog.CATALOG_SCHEMA, "models": records},
                        sort_keys=True,
                    )
                )
            else:
                for record in records:
                    print(
                        f"model={record['id']} voice={record['voice']} "
                        f"installed={'yes' if record['installed'] else 'no'} "
                        f"download_bytes={record['download_bytes']}"
                    )
                    print(f"  {record['summary']}; {record['detail']}")
            return 0
        if args.command == "install":
            directory, created = installer.install_model(args.model, report=print,
                model_file=args.model_file, config_file=args.config_file)
            action = "Installed" if created else "Already installed and verified"
            print(f"{action}: {args.model} at {directory}")
            return 0
        if args.command == "verify":
            ok, detail = installer.inspect_install(args.model, verify_hashes=True)
            if not ok:
                raise installer.InstallError(detail)
            print(f"Verified {args.model}: {detail}")
            return 0
        if args.command == "status":
            model = catalog.get_model(args.model)
            installed, detail = installer.inspect_install(args.model)
            running: dict | None = None
            try:
                endpoint = client.connect(autostart=False)
            except client.ClientError:
                pass
            else:
                with endpoint:
                    protocol.send_frame(endpoint, {"op": "status", "model": args.model})
                    running = protocol.recv_frame(endpoint)
            payload = {
                "version": __version__,
                "model": model.catalog_id,
                "voice": model.voice,
                "checkpoint": model.checkpoint,
                "model_sha256": model.artifacts[0].sha256,
                "installed": installed,
                "detail": detail,
                "running": bool(running and running.get("ok")),
                "loaded": bool(running and running.get("model") == args.model and running.get("loaded")),
                "socket": paths.socket_path(),
            }
            if args.as_json:
                print(json.dumps(payload, sort_keys=True))
            else:
                print(
                    f"model={payload['model']} voice={payload['voice']} "
                    f"installed={'yes' if installed else 'no'} "
                    f"running={'yes' if payload['running'] else 'no'} "
                    f"loaded={'yes' if payload['loaded'] else 'no'}"
                )
                print(f"  {detail}")
            return 0
        if args.command == "serve":
            service.run_service(idle_seconds=args.idle_seconds)
            return 0
        if args.command == "synthesize":
            if args.stdin == (args.text is not None):
                parser.error("synthesize needs exactly one of TEXT or --stdin")
            if not args.raw and not args.output:
                parser.error("synthesize needs --output FILE (or the private --raw mode)")
            text = _stdin_text() if args.stdin else args.text
            pcm, metadata = client.synthesize(text, model=args.model, rate=args.rate)
            if args.raw:
                sys.stdout.buffer.write(pcm)
                sys.stdout.buffer.flush()
            else:
                if not args.output.lower().endswith(".wav"):
                    parser.error("Piper provider output must end in .wav")
                _write_new(args.output, _wav_bytes(pcm, metadata["sample_rate"]))
                print(
                    f"Saved {metadata['voice']} at {metadata['rate']} wpm to "
                    f"{os.path.abspath(os.path.expanduser(args.output))}"
                )
            return 0
        raise AssertionError(f"unhandled command {args.command}")
    except (ValueError, OSError, client.ClientError, installer.InstallError,
            paths.PathError, service.ServiceError) as error:
        parser.exit(1, f"kilix-piper-tts: {error}\n")


__all__ = ["build_parser", "main"]
