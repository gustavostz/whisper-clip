"""FastAPI transcription server for WhisperClip.

Started as a daemon thread from main.py when server_enabled=True.
Shares a WhisperClient instance with the desktop app.
"""
import os
import secrets
import tempfile
import logging

from fastapi import FastAPI, UploadFile, File, Header, Query, HTTPException

log = logging.getLogger("whisperclip")

LLM_PREFIX = (
    "[Transcribed via speech-to-text (Whisper). "
    "Some words may be inaccurate \u2014 please interpret based on context.]\n\n"
)


def create_app(whisper_client, api_key, llm_context_prefix_default=True):
    """Create the FastAPI app with injected dependencies."""
    app = FastAPI(title="WhisperClip API", version="1.0.0", docs_url="/docs", redoc_url=None)

    def _verify_api_key(x_api_key=None, api_key_param=None):
        provided = x_api_key or api_key_param
        if not provided or not secrets.compare_digest(provided, api_key):
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

    @app.get("/api/v1/health")
    def health():
        return {
            "status": "ok",
            "model": whisper_client.model_name,
            "compute_type": whisper_client.compute_type,
        }

    @app.post("/api/v1/transcribe")
    def transcribe(
        file: UploadFile = File(...),
        llm_context_prefix: bool | None = Query(None),
        x_api_key: str | None = Header(None),
        api_key_param: str | None = Query(None, alias="api_key"),
    ):
        _verify_api_key(x_api_key, api_key_param)

        # Save uploaded file to temp location
        suffix = _get_suffix(file.filename)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir=_get_temp_dir()) as tmp:
                tmp_path = tmp.name
                contents = file.file.read()
                tmp.write(contents)

            log.info("Server: transcribing upload (%d bytes, %s)", len(contents), file.filename)

            text, metadata = whisper_client.transcribe_with_info(tmp_path)

            # Unload model after request (same pattern as desktop)
            whisper_client.unload_model()

            # Apply LLM prefix based on request param or config default
            use_prefix = llm_context_prefix if llm_context_prefix is not None else llm_context_prefix_default
            if use_prefix:
                text = LLM_PREFIX + text

            return {
                "text": text,
                "language": metadata["language"],
                "language_probability": metadata["language_probability"],
                "audio_duration": metadata["audio_duration"],
                "processing_time": metadata["processing_time"],
            }

        except HTTPException:
            raise
        except Exception as e:
            log.error("Server transcription error: %s", e, exc_info=True)
            try:
                whisper_client.unload_model()
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    return app


def _get_suffix(filename):
    """Extract file extension from upload filename, defaulting to .wav."""
    if filename and "." in filename:
        return "." + filename.rsplit(".", 1)[1].lower()
    return ".wav"


def _get_temp_dir():
    """Return temp directory for uploaded files, creating if needed."""
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "tmp")
    os.makedirs(tmp, exist_ok=True)
    return tmp
