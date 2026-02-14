
# WhisperClip: One-Click Audio Transcription

![Example using WhisperClip](assets/readme/example-of-usage.gif)

WhisperClip simplifies your life by automatically transcribing audio recordings and saving the text directly to your clipboard. With just a click of a button, you can effortlessly convert spoken words into written text, ready to be pasted wherever you need it. Powered by OpenAI's Whisper model via [faster-whisper](https://github.com/SYSTRAN/faster-whisper), it provides fast, free, and fully local transcription — your audio never leaves your machine.

## Table of Contents

- [Features](#features)
- [Installation](#installation)
  - [Prerequisites](#prerequisites)
  - [Setting Up the Environment](#setting-up-the-environment)
  - [Choosing the Right Model](#choosing-the-right-model)
- [Usage](#usage)
- [Configuration](#configuration)
- [Feedback](#feedback)
- [Acknowledgments](#acknowledgments)

## Features

- Record audio with a simple click or global hotkey (`Alt+Shift+R`).
- Fast, local transcription using OpenAI's Whisper model with GPU acceleration (CUDA).
- Option to save transcriptions directly to the clipboard.
- Transcribe existing audio files via the file picker.
- Optional LLM context prefix — prepends a note explaining the text was generated via speech-to-text.
- Real-time audio visualizer showing recording and transcription states.

## Installation

### Prerequisites

- Python 3.10 or higher
- [CUDA](https://developer.nvidia.com/cuda-downloads) is highly recommended for better performance but not necessary. WhisperClip can also run on a CPU.

### Setting Up the Environment

1. Clone the repository:
   ```
   git clone https://github.com/gustavostz/whisper-clip.git
   cd whisper-clip
   ```

2. Create and activate a virtual environment:
   ```
   python -m venv .venv
   .venv\Scripts\activate        # Windows
   source .venv/bin/activate     # Linux/macOS
   ```

3. Install the required dependencies:
   ```
   pip install -r requirements.txt
   ```

### Choosing the Right Model

The default model is `turbo` (large-v3-turbo), which offers the best balance of speed and accuracy at ~1.5 GB VRAM with int8 quantization. Available models:

|  Size  | Required VRAM (int8) | Relative speed |
|:------:|:--------------------:|:--------------:|
|  tiny  |       ~0.5 GB        |    fastest     |
|  base  |       ~0.5 GB        |      fast      |
| small  |       ~1 GB          |    moderate    |
| medium |       ~2.5 GB        |     slower     |
| large-v3 |     ~3 GB          |    slowest     |
| turbo  |       ~1.5 GB        |  fast + accurate (recommended) |

To change the model, modify `model_name` in `config.json`. You can also change `compute_type` (default: `int8`) — options include `float16`, `int8_float16`, `int8`.

## Usage

Run the application:

```
python main.py
```

- Click the microphone button to start and stop recording.
- If "Save to Clipboard" is checked, the transcription will be copied to your clipboard automatically.

## Configuration

All settings are in `config.json`:

| Setting | Default | Description |
|---------|---------|-------------|
| `model_name` | `"turbo"` | Whisper model to use (see table above) |
| `compute_type` | `"int8"` | Quantization type (`int8`, `float16`, `int8_float16`) |
| `shortcut` | `"alt+shift+r"` | Global hotkey for toggling recording |
| `notify_clipboard_saving` | `true` | Play a sound when transcription is copied to clipboard |
| `llm_context_prefix` | `true` | Prepend a note to transcriptions explaining they were generated via speech-to-text |

## Feedback

If there's interest in a more user-friendly, executable version of WhisperClip, I'd be happy to consider creating one. Your feedback and suggestions are welcome! Just let me know through the [GitHub issues](https://github.com/gustavostz/whisper-clip/issues).

## Acknowledgments

This project uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (a CTranslate2-based reimplementation of OpenAI's [Whisper](https://github.com/openai/whisper)) for audio transcription.
