import os
import sys
import site
from pathlib import Path
import pytest

# Add project root to path so we can import whisper_client
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Register NVIDIA DLL paths on Windows (same as main.py)
if sys.platform == "win32":
    _nvidia_dirs = [
        Path(sys.prefix) / "Lib" / "site-packages" / "nvidia",
        Path(site.getusersitepackages()) / "nvidia",
    ]
    for _nvidia_dir in _nvidia_dirs:
        if _nvidia_dir.is_dir():
            for _pkg in _nvidia_dir.iterdir():
                _bin = _pkg / "bin"
                if _bin.is_dir():
                    os.environ["PATH"] = str(_bin) + os.pathsep + os.environ.get("PATH", "")
                    if hasattr(os, "add_dll_directory"):
                        os.add_dll_directory(str(_bin))

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")


def _get_wav_files_by_size():
    """Return list of (filepath, size) tuples sorted by size ascending."""
    if not os.path.isdir(OUTPUT_DIR):
        return []
    files = []
    for f in os.listdir(OUTPUT_DIR):
        if f.endswith(".wav"):
            path = os.path.join(OUTPUT_DIR, f)
            files.append((path, os.path.getsize(path)))
    files.sort(key=lambda x: x[1])
    return files


@pytest.fixture(scope="session")
def audio_files_by_size():
    """All wav files in output/ sorted by size (smallest first)."""
    files = _get_wav_files_by_size()
    if not files:
        pytest.skip("No audio files in output/ — run the app first to generate test data")
    return files


@pytest.fixture(scope="session")
def small_audio(audio_files_by_size):
    """A small audio file with enough content to transcribe (~500KB-2MB)."""
    for path, size in audio_files_by_size:
        if size >= 500_000:
            return path
    # Fallback: pick the largest available
    return audio_files_by_size[-1][0]


@pytest.fixture(scope="session")
def medium_audio(audio_files_by_size):
    """A medium audio file (~1MB-10MB)."""
    for path, size in audio_files_by_size:
        if 1_000_000 <= size <= 10_000_000:
            return path
    # Fallback: pick something in the middle
    mid = len(audio_files_by_size) // 2
    return audio_files_by_size[mid][0]


@pytest.fixture(scope="session")
def tiny_audio(audio_files_by_size):
    """The smallest audio file available."""
    return audio_files_by_size[0][0]


@pytest.fixture
def whisper_client():
    """A fresh WhisperClient instance (uses tiny model for fast tests)."""
    from whisper_client import WhisperClient
    client = WhisperClient(model_name="tiny", compute_type="int8")
    yield client
    client.unload_model()
