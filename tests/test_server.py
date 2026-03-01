"""Tests for the WhisperClip FastAPI transcription server.

Tests cover API key auth, file upload, transcription via the HTTP API,
and error handling. Uses the same audio fixtures from conftest.py.

Run with: pytest tests/test_server.py -v
"""
import os
import pytest

from fastapi.testclient import TestClient
from server import create_app
from whisper_client import WhisperClient

TEST_API_KEY = "test-secret-key-for-tests"


@pytest.fixture
def whisper_client_server():
    """A WhisperClient instance for server tests (tiny model)."""
    client = WhisperClient(model_name="tiny", compute_type="int8")
    yield client
    client.unload_model()


@pytest.fixture
def app(whisper_client_server):
    """FastAPI app instance with test dependencies."""
    return create_app(whisper_client_server, TEST_API_KEY, llm_context_prefix_default=True)


@pytest.fixture
def client(app):
    """FastAPI TestClient for making HTTP requests."""
    return TestClient(app)


class TestHealthEndpoint:
    """Test GET /api/v1/health."""

    def test_health_returns_ok(self, client):
        """Health endpoint should return status ok."""
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["model"] == "tiny"
        assert data["compute_type"] == "int8"

    def test_health_no_auth_required(self, client):
        """Health endpoint should not require API key."""
        response = client.get("/api/v1/health")
        assert response.status_code == 200


class TestAuthenticaton:
    """Test API key authentication on /api/v1/transcribe."""

    def test_missing_api_key_returns_401(self, client, small_audio):
        """Request without API key should be rejected."""
        with open(small_audio, "rb") as f:
            response = client.post("/api/v1/transcribe", files={"file": ("test.wav", f, "audio/wav")})
        assert response.status_code == 401
        assert "Invalid or missing API key" in response.json()["detail"]

    def test_wrong_api_key_returns_401(self, client, small_audio):
        """Request with wrong API key should be rejected."""
        with open(small_audio, "rb") as f:
            response = client.post(
                "/api/v1/transcribe",
                files={"file": ("test.wav", f, "audio/wav")},
                headers={"X-API-Key": "wrong-key"},
            )
        assert response.status_code == 401

    def test_valid_header_api_key(self, client, small_audio):
        """Request with correct X-API-Key header should succeed."""
        with open(small_audio, "rb") as f:
            response = client.post(
                "/api/v1/transcribe",
                files={"file": ("test.wav", f, "audio/wav")},
                headers={"X-API-Key": TEST_API_KEY},
            )
        assert response.status_code == 200

    def test_valid_query_param_api_key(self, client, small_audio):
        """Request with correct ?api_key= query param should succeed."""
        with open(small_audio, "rb") as f:
            response = client.post(
                f"/api/v1/transcribe?api_key={TEST_API_KEY}",
                files={"file": ("test.wav", f, "audio/wav")},
            )
        assert response.status_code == 200


class TestTranscription:
    """Test POST /api/v1/transcribe with actual audio files."""

    def test_transcribe_returns_text(self, client, small_audio):
        """Transcription should return non-empty text."""
        with open(small_audio, "rb") as f:
            response = client.post(
                "/api/v1/transcribe",
                files={"file": ("test.wav", f, "audio/wav")},
                headers={"X-API-Key": TEST_API_KEY},
            )
        assert response.status_code == 200
        data = response.json()
        assert "text" in data
        assert len(data["text"].strip()) > 0

    def test_transcribe_returns_metadata(self, client, small_audio):
        """Response should include language, duration, and timing metadata."""
        with open(small_audio, "rb") as f:
            response = client.post(
                "/api/v1/transcribe?llm_context_prefix=false",
                files={"file": ("test.wav", f, "audio/wav")},
                headers={"X-API-Key": TEST_API_KEY},
            )
        assert response.status_code == 200
        data = response.json()
        assert "language" in data
        assert "language_probability" in data
        assert "audio_duration" in data
        assert "processing_time" in data
        assert isinstance(data["audio_duration"], float)
        assert isinstance(data["processing_time"], float)

    def test_llm_prefix_enabled_by_default(self, client, small_audio):
        """When llm_context_prefix is not specified, default (True) should apply."""
        with open(small_audio, "rb") as f:
            response = client.post(
                "/api/v1/transcribe",
                files={"file": ("test.wav", f, "audio/wav")},
                headers={"X-API-Key": TEST_API_KEY},
            )
        data = response.json()
        assert data["text"].startswith("[Transcribed via speech-to-text")

    def test_llm_prefix_disabled(self, client, small_audio):
        """When llm_context_prefix=false, raw text should be returned."""
        with open(small_audio, "rb") as f:
            response = client.post(
                "/api/v1/transcribe?llm_context_prefix=false",
                files={"file": ("test.wav", f, "audio/wav")},
                headers={"X-API-Key": TEST_API_KEY},
            )
        data = response.json()
        assert not data["text"].startswith("[Transcribed via speech-to-text")

    def test_llm_prefix_explicitly_enabled(self, client, small_audio):
        """When llm_context_prefix=true, prefix should be present."""
        with open(small_audio, "rb") as f:
            response = client.post(
                "/api/v1/transcribe?llm_context_prefix=true",
                files={"file": ("test.wav", f, "audio/wav")},
                headers={"X-API-Key": TEST_API_KEY},
            )
        data = response.json()
        assert data["text"].startswith("[Transcribed via speech-to-text")


class TestTranscribeWithInfo:
    """Test the WhisperClient.transcribe_with_info method directly."""

    def test_returns_text_and_metadata(self, small_audio):
        """transcribe_with_info should return a (text, metadata) tuple."""
        client = WhisperClient(model_name="tiny", compute_type="int8")
        text, metadata = client.transcribe_with_info(small_audio)

        assert isinstance(text, str)
        assert len(text.strip()) > 0
        assert isinstance(metadata, dict)
        assert "language" in metadata
        assert "language_probability" in metadata
        assert "audio_duration" in metadata
        assert "processing_time" in metadata
        client.unload_model()

    def test_metadata_values_are_reasonable(self, small_audio):
        """Metadata values should be within expected ranges."""
        client = WhisperClient(model_name="tiny", compute_type="int8")
        _, metadata = client.transcribe_with_info(small_audio)

        assert 0 < metadata["language_probability"] <= 1.0
        assert metadata["audio_duration"] > 0
        assert metadata["processing_time"] > 0
        client.unload_model()


class TestErrorHandling:
    """Test error handling in the server."""

    def test_no_file_returns_422(self, client):
        """Missing file field should return 422 validation error."""
        response = client.post(
            "/api/v1/transcribe",
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert response.status_code == 422

    def test_invalid_audio_returns_500(self, client):
        """Uploading non-audio data should return 500."""
        response = client.post(
            "/api/v1/transcribe",
            files={"file": ("test.txt", b"this is not audio", "text/plain")},
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert response.status_code == 500


class TestTempFileCleanup:
    """Test that temp files are cleaned up after transcription."""

    def test_temp_dir_cleaned_after_success(self, client, small_audio):
        """Temp files should be removed after successful transcription."""
        tmp_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "tmp")

        # Count files before
        files_before = set(os.listdir(tmp_dir)) if os.path.isdir(tmp_dir) else set()

        with open(small_audio, "rb") as f:
            response = client.post(
                "/api/v1/transcribe",
                files={"file": ("test.wav", f, "audio/wav")},
                headers={"X-API-Key": TEST_API_KEY},
            )
        assert response.status_code == 200

        # Count files after — should be the same (temp was cleaned up)
        files_after = set(os.listdir(tmp_dir)) if os.path.isdir(tmp_dir) else set()
        new_files = files_after - files_before
        assert len(new_files) == 0, f"Temp files not cleaned up: {new_files}"
