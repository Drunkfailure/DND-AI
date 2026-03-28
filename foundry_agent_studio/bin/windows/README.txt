Windows x64 whisper.cpp CLI (v1.8.4) from:
  https://github.com/ggml-org/whisper.cpp/releases/download/v1.8.4/whisper-bin-x64.zip

Files here are the minimal set needed to run whisper-cli:
  whisper-cli-x86_64-pc-windows-msvc.exe
  whisper.dll, ggml.dll, ggml-base.dll, ggml-cpu.dll, SDL2.dll

Non-Windows (Linux, macOS, or Windows ARM):
  — Delete this entire "windows" folder (it is useless on those platforms).
  — Download the matching archive for your OS from the whisper.cpp releases page:
    https://github.com/ggml-org/whisper.cpp/releases
  — Build from source if your platform has no binary:
    https://github.com/ggml-org/whisper.cpp
  — Set "whisper-cli executable" in the app to your whisper-cli binary, or place it next to python.exe
    using the name pattern whisper-cli-<triple>.exe / whisper-cli-<triple>.
