"""Integration tests for WhisperClip transcription pipeline.

These tests use audio files from the local output/ directory.
Run with: pytest tests/ -v
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from whisper_client import WhisperClient


class TestSingleTranscription:
    """Test basic transcription with a single file."""

    def test_transcribe_small_file(self, whisper_client, small_audio):
        """Transcribe a small audio file and verify we get text back."""
        result = whisper_client.transcribe(small_audio)
        assert isinstance(result, str)
        assert len(result.strip()) > 0, "Transcription should not be empty"

    def test_transcribe_returns_stripped_text(self, whisper_client, small_audio):
        """Transcription should not have leading/trailing whitespace."""
        result = whisper_client.transcribe(small_audio)
        assert result == result.strip()


class TestBackToBackTranscriptions:
    """Test sequential transcriptions — the scenario that was crashing."""

    def test_three_sequential_transcriptions(self, small_audio):
        """Transcribe the same file 3 times in sequence with load/unload cycles."""
        client = WhisperClient(model_name="tiny", compute_type="int8")

        for i in range(3):
            result = client.transcribe(small_audio)
            assert len(result.strip()) > 0, f"Transcription #{i+1} should not be empty"
            client.unload_model()

    def test_back_to_back_different_files(self, audio_files_by_size):
        """Transcribe multiple different files sequentially."""
        client = WhisperClient(model_name="tiny", compute_type="int8")

        # Pick 3 files of varying sizes
        indices = [0, len(audio_files_by_size) // 4, len(audio_files_by_size) // 2]
        files = [audio_files_by_size[i][0] for i in indices if i < len(audio_files_by_size)]

        for filepath in files:
            result = client.transcribe(filepath)
            assert isinstance(result, str)
            client.unload_model()


class TestModelLifecycle:
    """Test model loading, unloading, and reloading."""

    def test_load_unload_cycle(self):
        """Model should load and unload cleanly."""
        client = WhisperClient(model_name="tiny", compute_type="int8")
        assert client.model is None

        client.load_model()
        assert client.model is not None

        client.unload_model()
        assert client.model is None

    def test_double_load_is_noop(self):
        """Loading an already-loaded model should be safe."""
        client = WhisperClient(model_name="tiny", compute_type="int8")
        client.load_model()
        model_ref = client.model

        client.load_model()  # Should not crash or reload
        assert client.model is model_ref

        client.unload_model()

    def test_double_unload_is_noop(self):
        """Unloading an already-unloaded model should be safe."""
        client = WhisperClient(model_name="tiny", compute_type="int8")
        client.load_model()

        client.unload_model()
        assert client.model is None

        client.unload_model()  # Should not crash
        assert client.model is None

    def test_rapid_load_unload_cycles(self, small_audio):
        """Rapid load/unload cycles should not corrupt CUDA state."""
        client = WhisperClient(model_name="tiny", compute_type="int8")

        for _ in range(5):
            client.load_model()
            client.unload_model()

        # After rapid cycles, transcription should still work
        result = client.transcribe(small_audio)
        assert len(result.strip()) > 0
        client.unload_model()

    def test_transcribe_auto_loads_model(self, small_audio):
        """Calling transcribe() without loading first should auto-load."""
        client = WhisperClient(model_name="tiny", compute_type="int8")
        assert client.model is None

        result = client.transcribe(small_audio)
        assert len(result.strip()) > 0
        assert client.model is not None

        client.unload_model()


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_transcribe_tiny_file(self, whisper_client, tiny_audio):
        """The smallest audio file should transcribe without crashing."""
        result = whisper_client.transcribe(tiny_audio)
        assert isinstance(result, str)

    def test_transcribe_medium_file(self, medium_audio):
        """A medium-sized audio file should transcribe successfully."""
        client = WhisperClient(model_name="tiny", compute_type="int8")
        result = client.transcribe(medium_audio)
        assert isinstance(result, str)
        assert len(result.strip()) > 0
        client.unload_model()

    def test_invalid_audio_path(self, whisper_client):
        """Transcribing a nonexistent file should raise an error, not crash."""
        with pytest.raises(Exception):
            whisper_client.transcribe("nonexistent_file.wav")


class TestThreadSafety:
    """Test that WhisperClient is thread-safe."""

    def test_concurrent_load_calls(self):
        """Two threads calling load_model simultaneously should be safe."""
        import threading

        client = WhisperClient(model_name="tiny", compute_type="int8")
        errors = []

        def load():
            try:
                client.load_model()
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=load)
        t2 = threading.Thread(target=load)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(errors) == 0, f"Concurrent loads failed: {errors}"
        assert client.model is not None
        client.unload_model()

    def test_load_while_transcribing(self, small_audio):
        """Loading in one thread while transcribing in another should be safe."""
        import threading

        client = WhisperClient(model_name="tiny", compute_type="int8")
        client.load_model()
        errors = []

        def transcribe():
            try:
                client.transcribe(small_audio)
            except Exception as e:
                errors.append(e)

        def load():
            try:
                client.load_model()  # Should be a no-op (already loaded)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=transcribe)
        t2 = threading.Thread(target=load)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(errors) == 0, f"Concurrent load+transcribe failed: {errors}"
        client.unload_model()


class TestConfig:
    """Test WhisperClient configuration."""

    def test_default_config(self):
        """Default config should be turbo with int8."""
        client = WhisperClient()
        assert client.model_name == "turbo"
        assert client.compute_type == "int8"
        assert client.model is None

    def test_custom_model_name(self):
        """Custom model name should be stored."""
        client = WhisperClient(model_name="tiny")
        assert client.model_name == "tiny"

    def test_custom_compute_type(self):
        """Custom compute type should be stored."""
        client = WhisperClient(compute_type="float16")
        assert client.compute_type == "float16"

    def test_tiny_model_loads(self):
        """The tiny model should load successfully."""
        client = WhisperClient(model_name="tiny", compute_type="int8")
        client.load_model()
        assert client.model is not None
        client.unload_model()
