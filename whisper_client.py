import logging
import time
import threading

log = logging.getLogger("whisperclip")


class WhisperClient:
    def __init__(self, model_name="turbo", compute_type="int8"):
        self.model_name = model_name
        self.compute_type = compute_type
        self.model = None
        self._lock = threading.Lock()

    def load_model(self):
        with self._lock:
            if self.model is not None:
                # Model object exists — reload weights to device if previously unloaded
                if not self.model.model.model_is_loaded:
                    log.info("Reloading model weights to device")
                    self.model.model.load_model()
                return

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
        with self._lock:
            if self.model is not None:
                # Use CTranslate2's built-in unload to release GPU memory without
                # destroying the C++ object. Calling `del self.model` triggers C++
                # destructors that segfault during CUDA resource cleanup — a known
                # unresolved bug (CTranslate2 #1782, faster-whisper #71).
                # to_cpu=True moves weights to RAM so reload is a fast GPU memcpy
                # instead of slow disk I/O (which would block the GIL and freeze the app).
                self.model.model.unload_model(to_cpu=True)
                log.debug("Model unloaded from GPU")

    def transcribe(self, audio_path):
        if self.model is None or not self.model.model.model_is_loaded:
            self.load_model()

        start = time.perf_counter()
        segments, _info = self.model.transcribe(
            audio_path,
            beam_size=5,
            condition_on_previous_text=False,
            repetition_penalty=1.2,
            no_repeat_ngram_size=3,
        )

        text = " ".join(segment.text.strip() for segment in segments)
        elapsed = time.perf_counter() - start
        log.info("Transcription took %.1fs (%d chars)", elapsed, len(text))
        return text
