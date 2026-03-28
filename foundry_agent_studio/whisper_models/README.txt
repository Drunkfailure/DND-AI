whisper.cpp-compatible weights for local STT.

These binary files are NOT committed to git (too large for GitHub). Download them into this folder after clone.

  ggml-small.bin  —  Whisper “small” GGML (~466 MiB). Download from:
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin

The app uses this file when “Whisper model file” in Settings is left empty.

whisper-cli: on Windows x64 the package includes ../bin/windows/ (see bin/windows/README.txt).
Other platforms: get whisper-cli from https://github.com/ggml-org/whisper.cpp/releases or build from source.

To use a different model, set an absolute path in the UI or replace this file (same name) after backup.
