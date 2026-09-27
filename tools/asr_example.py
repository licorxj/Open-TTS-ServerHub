#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ASR Usage Examples.

This script demonstrates how to use the ASR class for audio transcription.
"""

import sys
import os
import logging

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.asr import ASR, transcribe_audio

logging.basicConfig(level=logging.INFO)


def example_1_basic_usage():
    """Example 1: Basic usage with file path."""
    print("\n" + "=" * 60)
    print("Example 1: Basic usage with file path")
    print("=" * 60)
    
    # Initialize ASR model
    asr = ASR(model_name="openai/whisper-large-v3-turbo")
    
    # Transcribe audio file
    audio_path = "path/to/your/audio.wav"
    
    if os.path.exists(audio_path):
        text = asr.transcribe(audio_path)
        print(f"Transcription: {text}")
    else:
        print(f"Audio file not found: {audio_path}")
        print("Please provide a valid audio file path.")


def example_2_with_waveform():
    """Example 2: Transcription with waveform tuple."""
    print("\n" + "=" * 60)
    print("Example 2: Transcription with waveform tuple")
    print("=" * 60)
    
    try:
        import torchaudio
        import torch
        
        # Initialize ASR model
        asr = ASR()
        
        # Load audio file
        audio_path = "path/to/your/audio.wav"
        
        if os.path.exists(audio_path):
            # Load audio with torchaudio
            waveform, sample_rate = torchaudio.load(audio_path)
            
            # Transcribe
            text = asr.transcribe((waveform, sample_rate))
            print(f"Transcription: {text}")
        else:
            print(f"Audio file not found: {audio_path}")
            
    except ImportError:
        print("torchaudio not installed. Skipping this example.")


def example_3_with_timestamps():
    """Example 3: Transcription with timestamps."""
    print("\n" + "=" * 60)
    print("Example 3: Transcription with timestamps")
    print("=" * 60)
    
    # Initialize ASR model
    asr = ASR()
    
    # Transcribe with timestamps
    audio_path = "path/to/your/audio.wav"
    
    if os.path.exists(audio_path):
        result = asr.transcribe_with_timestamps(audio_path)
        
        print(f"Full text: {result.get('text', 'N/A')}")
        
        if 'chunks' in result:
            print("\nTimestamps:")
            for chunk in result['chunks']:
                start = chunk['timestamp'][0]
                end = chunk['timestamp'][1]
                text = chunk['text']
                print(f"  [{start:.2f}s - {end:.2f}s]: {text}")
    else:
        print(f"Audio file not found: {audio_path}")


def example_4_convenience_function():
    """Example 4: Using convenience function."""
    print("\n" + "=" * 60)
    print("Example 4: Using convenience function")
    print("=" * 60)
    
    # Quick transcription without class instantiation
    audio_path = "path/to/your/audio.wav"
    
    if os.path.exists(audio_path):
        text = transcribe_audio(audio_path)
        print(f"Transcription: {text}")
    else:
        print(f"Audio file not found: {audio_path}")


def example_5_custom_model():
    """Example 5: Using custom model."""
    print("\n" + "=" * 60)
    print("Example 5: Using custom model")
    print("=" * 60)
    
    # Use a smaller model for faster inference
    asr = ASR(model_name="openai/whisper-medium")
    
    audio_path = "path/to/your/audio.wav"
    
    if os.path.exists(audio_path):
        text = asr.transcribe(audio_path)
        print(f"Transcription: {text}")
    else:
        print(f"Audio file not found: {audio_path}")


def example_6_batch_transcription():
    """Example 6: Batch transcription."""
    print("\n" + "=" * 60)
    print("Example 6: Batch transcription")
    print("=" * 60)
    
    # Initialize ASR model once
    asr = ASR()
    
    # List of audio files
    audio_files = [
        "audio1.wav",
        "audio2.wav",
        "audio3.wav",
    ]
    
    # Transcribe all files
    for audio_path in audio_files:
        if os.path.exists(audio_path):
            text = asr.transcribe(audio_path)
            print(f"{audio_path}: {text}")
        else:
            print(f"{audio_path}: File not found")


def main():
    """Run all examples."""
    print("ASR Module Usage Examples")
    print("=" * 60)
    print("\nNote: These examples require actual audio files.")
    print("Please modify the audio_path variable to point to your audio files.")
    
    # Run examples
    example_1_basic_usage()
    example_2_with_waveform()
    example_3_with_timestamps()
    example_4_convenience_function()
    example_5_custom_model()
    example_6_batch_transcription()
    
    print("\n" + "=" * 60)
    print("All examples completed!")
    print("=" * 60)


if __name__ == "__main__":
    main()