import torch
import whisper
import os

# flag to control deleting of newly created files
DELETE_FILE_AFTER_TRANSCRIPTION = os.getenv("DELETE_FILE_AFTER_TRANSCRIPTION", 'true').lower() == 'true'

class WhisperClient:
    def __init__(self, model_name="medium.en"):
        self.model_name = model_name
        self.model = None

    def load_model(self):
        if self.model is None:
            self.model = whisper.load_model(self.model_name)

    def unload_model(self):
        if self.model is not None:
            # Clear CUDA cache
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            
            # Delete model and clear from memory
            del self.model
            self.model = None
            
            # Force garbage collection
            import gc
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    def transcribe(self, audio_path):
        if self.model is None:
            self.load_model()
        result = self.model.transcribe(audio_path)
        print(DELETE_FILE_AFTER_TRANSCRIPTION)
        # delete file if flag is set
        if DELETE_FILE_AFTER_TRANSCRIPTION:
            os.remove(audio_path)

        return result["text"]
