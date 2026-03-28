"""Piper and whisper.cpp subprocess helpers and sidecar path resolution."""

from __future__ import annotations

import platform
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


def current_target_triple() -> str:
    machine = platform.machine().lower()
    sys_p = sys.platform
    if sys_p == "win32":
        if machine in ("amd64", "x86_64"):
            return "x86_64-pc-windows-msvc"
        if "arm" in machine:
            return "aarch64-pc-windows-msvc"
    if sys_p == "linux":
        if machine in ("x86_64", "amd64"):
            return "x86_64-unknown-linux-gnu"
        if "arm" in machine or machine == "aarch64":
            return "aarch64-unknown-linux-gnu"
    if sys_p == "darwin":
        if machine == "arm64":
            return "aarch64-apple-darwin"
        return "x86_64-apple-darwin"
    return "x86_64-pc-windows-msvc"


def bundled_whisper_cli_windows() -> Path | None:
    """Prebuilt whisper-cli + DLLs shipped for Windows x64 only (see bin/windows/README.txt)."""
    if sys.platform != "win32":
        return None
    here = Path(__file__).resolve().parent
    p = here / "bin" / "windows" / f"whisper-cli-{current_target_triple()}.exe"
    return p if p.is_file() else None


def resolve_whisper_cli_exe(user_path: str) -> Path:
    """User path, else Windows bundled binary, else sidecar next to python.exe."""
    p = user_path.strip()
    if p:
        pb = Path(p)
        if pb.is_file():
            return pb
        raise FileNotFoundError(f"Binary not found: {pb}")
    b = bundled_whisper_cli_windows()
    if b is not None:
        return b
    return resolve_sidecar_exe("whisper-cli", "")


def resolve_sidecar_exe(sidecar_base: str, user_path: str) -> Path:
    p = user_path.strip()
    if p:
        pb = Path(p)
        if pb.is_file():
            return pb
        raise FileNotFoundError(f"Binary not found: {pb}")
    exe_dir = Path(sys.executable).resolve().parent
    triple = current_target_triple()
    fname = f"{sidecar_base}-{triple}"
    if sys.platform == "win32":
        fname = f"{fname}.exe"
    candidate = exe_dir / fname
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(
        f"Missing sidecar at {candidate}. Set an absolute path in Settings or place the binary next to Python."
    )


def is_wav_header(buf: bytes) -> bool:
    return len(buf) >= 12 and buf[:4] == b"RIFF" and buf[8:12] == b"WAVE"


def run_piper_to_wav(piper_exe: Path, model: Path, text: str) -> bytes:
    if not piper_exe.is_file():
        raise FileNotFoundError(f"piper not found: {piper_exe}")
    if not model.is_file():
        raise FileNotFoundError(f"Piper model not found: {model}")
    uid = uuid.uuid4().hex
    out = Path(tempfile.gettempdir()) / f"fas-piper-{uid}.wav"
    txt = Path(tempfile.gettempdir()) / f"fas-piper-{uid}.txt"
    txt.write_text(text, encoding="utf-8")
    try:
        proc = subprocess.run(
            [str(piper_exe), "--model", str(model), "--output_file", str(out), "--input_file", str(txt)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode == 0 and out.is_file():
            data = out.read_bytes()
            out.unlink(missing_ok=True)
            txt.unlink(missing_ok=True)
            return data
        out.unlink(missing_ok=True)
        err_in = proc.stderr or ""
        return run_piper_stdin(piper_exe, model, text, err_in)
    finally:
        txt.unlink(missing_ok=True)


def run_piper_stdin(piper_exe: Path, model: Path, text: str, prev_err: str = "") -> bytes:
    uid = uuid.uuid4().hex
    out = Path(tempfile.gettempdir()) / f"fas-piper-stdin-{uid}.wav"
    proc = subprocess.run(
        [str(piper_exe), "--model", str(model), "--output_file", str(out)],
        input=text + "\n",
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"piper exited {proc.returncode}: {prev_err}; {proc.stderr}")
    if not out.is_file():
        raise RuntimeError("piper produced no wav")
    data = out.read_bytes()
    out.unlink(missing_ok=True)
    return data


def run_whisper_cli_to_text(whisper_exe: Path, model: Path, wav: Path) -> str:
    if not whisper_exe.is_file():
        raise FileNotFoundError(f"whisper binary not found: {whisper_exe}")
    if not model.is_file():
        raise FileNotFoundError(f"Whisper model not found: {model}")
    if not wav.is_file():
        raise FileNotFoundError(f"WAV not found: {wav}")
    proc = subprocess.run(
        [str(whisper_exe), "-m", str(model), "-f", str(wav), "-otxt"],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(whisper_exe.resolve().parent),
    )
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or (proc.stdout or "").strip() or "whisper failed"
        raise RuntimeError(f"whisper-cli exited {proc.returncode}: {err}")
    # whisper.cpp writes "file.wav.txt", not "file.txt" (Path.with_suffix would replace .wav).
    txt_path = Path(str(wav) + ".txt")
    if txt_path.is_file():
        return txt_path.read_text(encoding="utf-8", errors="replace")
    raise FileNotFoundError(f"Expected transcript at {txt_path}")


def write_temp_wav(bytes_data: bytes) -> Path:
    p = Path(tempfile.gettempdir()) / f"fas-stt-{uuid.uuid4().hex}.wav"
    p.write_bytes(bytes_data)
    return p
