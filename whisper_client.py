import gc
import logging
import time

log = logging.getLogger("whisperclip")


class WhisperClient:
    def __init__(self, model_name="turbo", compute_type="int8"):
        self.model_name = model_name
        self.compute_type = compute_type
        self.model = None

    def load_model(self):
        if self.model is None:
            from faster_whisper import WhisperModel
            import ctranslate2

            cuda_types = ctranslate2.get_supported_compute_types("cuda")

            if len(cuda_types) > 0:
                log.info("Loading model '%s' on CUDA (compute_type=%s)",
                         self.model_name, self.compute_type)
                self.model = WhisperModel(
                    self.model_name,
                    device="cuda",
                    compute_type=self.compute_type,
                )
            else:
                log.info("Loading model '%s' on CPU (CUDA not available)", self.model_name)
                self.model = WhisperModel(
                    self.model_name,
                    device="cpu",
                    compute_type="int8",
                )

    def unload_model(self):
        if self.model is not None:
            del self.model
            self.model = None
            gc.collect()
            log.debug("Model unloaded")

            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                pass

    def transcribe(self, audio_path):
        if self.model is None:
            self.load_model()

        start = time.perf_counter()
        segments, _info = self.model.transcribe(
            audio_path,
            beam_size=5,
        )

        text = " ".join(segment.text.strip() for segment in segments)
        elapsed = time.perf_counter() - start
        log.info("Transcription took %.1fs (%d chars)", elapsed, len(text))
        return text
