#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ASR (Automatic Speech Recognition) module.

Provides a simple class interface for speech-to-text transcription
using Whisper models from HuggingFace Transformers.
"""

import logging
from typing import Optional, Tuple, Union

import numpy as np
import torch
import transformers

logger = logging.getLogger(__name__)


class ASR:
    """Automatic Speech Recognition class using Whisper models.
    
    This class provides a simple interface for transcribing audio
    to text using HuggingFace's Whisper pipeline.
    
    Example:
        >>> asr = ASR(model_name="openai/whisper-large-v3-turbo")
        >>> text = asr.transcribe("audio.wav")
        >>> print(text)
        "Hello, how are you?"
    
    Example with waveform:
        >>> import torchaudio
        >>> waveform, sr = torchaudio.load("audio.wav")
        >>> text = asr.transcribe((waveform, sr))
        >>> print(text)
        "Hello, how are you?"
    """
    
    def __init__(
        self,
        model_name: str = "openai/whisper-large-v3-turbo",
        device: Optional[str] = None,
    ):
        """Initialize the ASR model.
        
        Args:
            model_name: HuggingFace model name or local path for the Whisper model.
                Default is "openai/whisper-large-v3-turbo".
                Other options: "openai/whisper-large-v3", "openai/whisper-medium", etc.
            device: Device to run the model on. If None, auto-detect CUDA availability.
                Options: "cuda", "cpu", "mps", etc.
        """
        from transformers import pipeline as hf_pipeline
        
        # Auto-detect device if not specified
        if device is None:
            if torch.cuda.is_available():
                self.device = "cuda"
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                self.device = "mps"
            else:
                self.device = "cpu"
        else:
            self.device = device
        
        logger.info(f"Initializing ASR model: {model_name} on {self.device}")
        
        # Determine dtype based on device
        asr_dtype = torch.float16 if self.device.startswith("cuda") else torch.float32
        
        # Load the ASR pipeline
        # transformers 4.x 使用 torch_dtype 参数，5.x 更名为 dtype，按版本兼容
        if int(transformers.__version__.split(".")[0]) >= 5:
            self._asr_pipe = hf_pipeline(
                "automatic-speech-recognition",
                model=model_name,
                device=self.device,
                dtype=asr_dtype,
            )
        else:
            self._asr_pipe = hf_pipeline(
                "automatic-speech-recognition",
                model=model_name,
                device=self.device,
                torch_dtype=asr_dtype,
            )
        
        logger.info(f"ASR model loaded successfully on {self.device}")
    
    @torch.inference_mode()
    def transcribe(
        self,
        audio: Union[str, Tuple[np.ndarray, int], Tuple[torch.Tensor, int]],
    ) -> str:
        """Transcribe audio to text.
        
        Args:
            audio: Input audio, can be one of:
                - str: Path to audio file
                - Tuple[np.ndarray, int]: (waveform, sample_rate) where waveform
                  is a numpy array of shape (1, T) or (T,)
                - Tuple[torch.Tensor, int]: (waveform, sample_rate) where waveform
                  is a torch.Tensor of shape (1, T) or (T,)
        
        Returns:
            Transcribed text string.
        
        Raises:
            RuntimeError: If the ASR model is not loaded.
        """
        if self._asr_pipe is None:
            raise RuntimeError("ASR model is not loaded. Initialize the ASR class first.")
        
        if isinstance(audio, str):
            # Audio file path
            result = self._asr_pipe(audio)
            return result["text"].strip()
        else:
            # (waveform, sample_rate) tuple
            waveform, sr = audio
            
            # Convert torch.Tensor to numpy if needed
            if isinstance(waveform, torch.Tensor):
                waveform = waveform.cpu().numpy()
            
            # Ensure waveform is 1D: (T,)
            waveform = np.squeeze(waveform)
            
            # Create audio input dict for the pipeline
            audio_input = {
                "array": waveform,
                "sampling_rate": sr,
            }
            
            result = self._asr_pipe(audio_input)
            return result["text"].strip()
    
    def transcribe_with_timestamps(
        self,
        audio: Union[str, Tuple[np.ndarray, int], Tuple[torch.Tensor, int]],
        return_timestamps: bool = True,
    ) -> dict:
        """Transcribe audio with timestamps.
        
        Args:
            audio: Input audio (same format as transcribe method).
            return_timestamps: Whether to include timestamps in output.
        
        Returns:
            Dictionary containing 'text' and optionally 'chunks' with timestamps.
        """
        if self._asr_pipe is None:
            raise RuntimeError("ASR model is not loaded.")
        
        if isinstance(audio, str):
            result = self._asr_pipe(audio, return_timestamps=return_timestamps)
        else:
            waveform, sr = audio
            if isinstance(waveform, torch.Tensor):
                waveform = waveform.cpu().numpy()
            waveform = np.squeeze(waveform)
            
            audio_input = {
                "array": waveform,
                "sampling_rate": sr,
            }
            result = self._asr_pipe(audio_input, return_timestamps=return_timestamps)
        
        return result
    
    @property
    def is_loaded(self) -> bool:
        """Check if the ASR model is loaded."""
        return self._asr_pipe is not None


# Convenience function for quick transcription
def transcribe_audio(
    audio: Union[str, Tuple[np.ndarray, int], Tuple[torch.Tensor, int]],
    model_name: str = "openai/whisper-large-v3-turbo",
    device: Optional[str] = None,
) -> str:
    """Quick transcription function without class instantiation.
    
    Note: This creates a new ASR instance each time, which is inefficient
    for multiple transcriptions. Use the ASR class for batch processing.
    
    Args:
        audio: Input audio (file path or (waveform, sample_rate) tuple).
        model_name: Whisper model name.
        device: Device to use.
    
    Returns:
        Transcribed text string.
    """
    asr = ASR(model_name=model_name, device=device)
    return asr.transcribe(audio)


if __name__ == "__main__":
    # Example usage
    import sys
    
    if len(sys.argv) > 1:
        audio_path = sys.argv[1]
        print(f"Transcribing: {audio_path}")
        
        asr = ASR()
        text = asr.transcribe(audio_path)
        print(f"Transcription: {text}")
    else:
        print("Usage: python asr.py <audio_file_path>")
        print("\nExample:")
        print("  python asr.py audio.wav")