#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test script for ASR module.

This script tests the ASR class functionality.
"""

import sys
import os
import logging

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.asr import ASR, transcribe_audio

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_asr_init():
    """Test ASR initialization."""
    print("Testing ASR initialization...")
    try:
        asr = ASR()
        print(f"✓ ASR initialized on device: {asr.device}")
        print(f"✓ Model loaded: {asr.is_loaded}")
        return True
    except Exception as e:
        print(f"✗ ASR initialization failed: {e}")
        return False


def test_transcribe_file():
    """Test transcription with file path."""
    print("\nTesting transcription with file path...")
    
    # Check if test audio file exists
    test_audio = "test_audio.wav"
    if not os.path.exists(test_audio):
        print(f"⚠ Test audio file not found: {test_audio}")
        print("  Skipping file transcription test")
        return None
    
    try:
        asr = ASR()
        text = asr.transcribe(test_audio)
        print(f"✓ Transcription result: {text}")
        return True
    except Exception as e:
        print(f"✗ Transcription failed: {e}")
        return False


def test_transcribe_waveform():
    """Test transcription with waveform tuple."""
    print("\nTesting transcription with waveform tuple...")
    
    try:
        import numpy as np
        
        # Create dummy waveform (silence)
        sample_rate = 16000
        duration = 1.0  # 1 second
        waveform = np.zeros(int(sample_rate * duration), dtype=np.float32)
        
        asr = ASR()
        # Note: This will produce empty or nonsense output since it's silence
        text = asr.transcribe((waveform, sample_rate))
        print(f"✓ Transcription completed (result: '{text}')")
        return True
    except Exception as e:
        print(f"✗ Transcription failed: {e}")
        return False


def test_transcribe_with_timestamps():
    """Test transcription with timestamps."""
    print("\nTesting transcription with timestamps...")
    
    # Check if test audio file exists
    test_audio = "test_audio.wav"
    if not os.path.exists(test_audio):
        print(f"⚠ Test audio file not found: {test_audio}")
        print("  Skipping timestamp test")
        return None
    
    try:
        asr = ASR()
        result = asr.transcribe_with_timestamps(test_audio)
        print(f"✓ Transcription with timestamps:")
        print(f"  Text: {result.get('text', 'N/A')}")
        if 'chunks' in result:
            print(f"  Chunks: {len(result['chunks'])}")
        return True
    except Exception as e:
        print(f"✗ Transcription with timestamps failed: {e}")
        return False


def test_convenience_function():
    """Test convenience function."""
    print("\nTesting convenience function...")
    
    # Check if test audio file exists
    test_audio = "test_audio.wav"
    if not os.path.exists(test_audio):
        print(f"⚠ Test audio file not found: {test_audio}")
        print("  Skipping convenience function test")
        return None
    
    try:
        text = transcribe_audio(test_audio)
        print(f"✓ Convenience function result: {text}")
        return True
    except Exception as e:
        print(f"✗ Convenience function failed: {e}")
        return False


def main():
    """Run all tests."""
    print("=" * 60)
    print("ASR Module Test Suite")
    print("=" * 60)
    
    results = []
    
    # Test 1: Initialization
    results.append(("Initialization", test_asr_init()))
    
    # Test 2: File transcription
    results.append(("File Transcription", test_transcribe_file()))
    
    # Test 3: Waveform transcription
    results.append(("Waveform Transcription", test_transcribe_waveform()))
    
    # Test 4: Timestamps
    results.append(("Timestamps", test_transcribe_with_timestamps()))
    
    # Test 5: Convenience function
    results.append(("Convenience Function", test_convenience_function()))
    
    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    
    passed = 0
    failed = 0
    skipped = 0
    
    for name, result in results:
        if result is True:
            status = "✓ PASSED"
            passed += 1
        elif result is False:
            status = "✗ FAILED"
            failed += 1
        else:
            status = "⚠ SKIPPED"
            skipped += 1
        print(f"{name}: {status}")
    
    print(f"\nTotal: {passed} passed, {failed} failed, {skipped} skipped")
    
    if failed > 0:
        print("\n⚠ Some tests failed!")
        return 1
    else:
        print("\n✓ All tests passed!")
        return 0


if __name__ == "__main__":
    sys.exit(main())