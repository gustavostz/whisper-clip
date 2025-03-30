
# WhisperClip: One-Click Audio Transcription

![Example using WhisperClip](assets/readme/example-of-usage.gif)

WhisperClip simplifies your life by automatically transcribing audio recordings and saving the text directly to your clipboard. With just a click of a button, you can effortlessly convert spoken words into written text, ready to be pasted wherever you need it. This application harnesses the power of OpenAI's Whisper for free, making transcription more accessible and convenient. This is a fork from the original [repo](https://github.com/gustavostz/whisper-clip/tree/main) that aims to support Linux as an operating system.

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

- Record audio with a simple click.
- Automatically transcribe audio using Whisper (free).
- Option to save transcriptions directly to the clipboard.

## Installation

### Prerequisites

- Python 3.8 or higher
- [CUDA](https://developer.nvidia.com/cuda-downloads) is highly recommended for better performance but not necessary. WhisperClip can also run on a CPU.

### Setting Up the Environment

1. Clone the repository:
   ```
   git clone https://github.com/gustavostz/whisper-clip.git
   cd whisper-clip
   ```

2. Install PyTorch if you don't have it already. Refer to [PyTorch's website](https://pytorch.org/get-started/locally/) for installation instructions.

3. Install the required dependencies:
   ```
   pip install -r requirements.txt
   ```

### Choosing the Right Model

Based on your GPU's VRAM, choose the appropriate Whisper model for optimal performance. Below is a table of available models with their required VRAM and relative speed:

|  Size  | Required VRAM | Relative speed |
|:------:|:-------------:|:--------------:|
|  tiny  |     ~1 GB     |      ~32x      |
|  base  |     ~1 GB     |      ~16x      |
| small  |     ~2 GB     |      ~6x       |
| medium |     ~5 GB     |      ~2x       |
| large  |    ~10 GB     |       1x       |

For English-only applications, `.en` models (e.g., `tiny.en`, `base.en`) tend to perform better.

To change the model, modify the `model_name` variable in `config.json` to the desired model name.

## Usage

Run the application:

```
python main.py
```

- Click the microphone button to start and stop recording.
- If "Save to Clipboard" is checked, the transcription will be copied to your clipboard automatically.

## Configuration

- The default shortcut for toggling recording is `Alt+Shift+R`. You can modify this in the `config.json` file.
- You can also change the Whisper model used for transcription in the `config.json` file.

## Feedback

If there's interest in a more user-friendly, executable version of WhisperClip, I'd be happy to consider creating one. Your feedback and suggestions are welcome! Just let me know through the [GitHub issues](https://github.com/gustavostz/whisper-clip/issues).

## Acknowledgments

This project uses [OpenAI's Whisper](https://github.com/openai/whisper) for audio transcription.
This project is a fork from [the original whisper-clip](https://github.com/gustavostz/whisper-clip/tree/main)
