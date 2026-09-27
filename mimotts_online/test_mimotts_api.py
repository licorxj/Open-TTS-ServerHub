#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
"""
Test script for MiMo TTS API (Online Version).

This script tests the MiMo TTS API functionality including:
- Preset voice synthesis (mimo-v2.5-tts)
- Voice design synthesis (mimo-v2.5-tts-voicedesign)
- Voice clone synthesis (mimo-v2.5-tts-voiceclone)
- Batch processing
- Multi-threading
"""

import logging
import os
import sys
import time


# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
import json

from mimotts_online.mimotts_api import (
    MiMoTTS,
    MiMoTTSAPI,
    MiMoTTSConfig,
    TTSResult,
    PRESET_VOICES,
    quick_synthesize,
    quick_design,
    quick_clone,
    CONFIG,
)

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


def get_api_key():
    """Load API key from config.json or environment variable."""
    # Try config.json first
    api_key = CONFIG.get("api_key")
    if api_key:
        return api_key
    
    # Try environment variable
    api_key = os.environ.get("MIMO_API_KEY")
    if api_key:
        return api_key
    
    return None


def test_config_creation():
    """Test configuration object creation."""
    print("\n" + "=" * 60)
    print("Test: Configuration Creation")
    print("=" * 60)
    
    try:
        config = MiMoTTSConfig(
            api_key="test_key",
            api_base="https://api.xiaomimimo.com/v1",
            timeout=120,
            max_workers=5,
        )
        
        print(f"✓ Config created successfully")
        print(f"  api_base: {config.api_base}")
        print(f"  timeout: {config.timeout}")
        print(f"  max_workers: {config.max_workers}")
        
        return True
        
    except Exception as e:
        print(f"✗ Test failed: {e}")
        return False


def test_tts_initialization(api_key):
    """Test TTS initialization."""
    print("\n" + "=" * 60)
    print("Test: TTS Initialization")
    print("=" * 60)
    
    try:
        # Test with api_key parameter
        tts1 = MiMoTTS(api_key=api_key, max_workers=5)
        print(f"✓ TTS initialized with api_key parameter")
        print(f"  api_base: {tts1.api_base}")
        print(f"  max_workers: {tts1.max_workers}")
        
        # Show available voices
        voices = tts1.get_preset_voices()
        print(f"  Available voices: {', '.join(voices.keys())}")
        
        tts1.shutdown()
        
        # Test with config object
        config = MiMoTTSConfig(api_key=api_key)
        tts2 = MiMoTTS(config=config)
        print(f"✓ TTS initialized with config object")
        tts2.shutdown()
        
        # Test context manager
        with MiMoTTS(api_key=api_key) as tts3:
            print(f"✓ TTS initialized with context manager")
        
        return True
        
    except Exception as e:
        print(f"✗ Test failed: {e}")
        return False


def test_preset_voice_synthesis(api_key):
    """Test preset voice synthesis."""
    print("\n" + "=" * 60)
    print("Test: Preset Voice Synthesis")
    print("=" * 60)
    
    if not api_key:
        print("⚠ Skipping: No API key provided")
        return None
    
    try:
        tts = MiMoTTS(api_key=api_key, max_workers=5)
        
        # Test with different voices
        test_voices = ["冰糖", "茉莉", "苏打"]
        
        for voice in test_voices:
            print(f"\n  Testing voice: {voice}")
            
            result = tts.synthesize_with_preset(
                text="你好，欢迎使用小米MiMo语音合成系统！",
                voice=voice,
                style_description="用温柔亲切的语气，语速适中"
            )
            
            if result.success:
                print(f"    ✓ Success - Audio size: {len(result.audio_data)} bytes")
                
                # Save audio
                output_path = f"test_preset_{voice}.wav"
                if tts.save_audio(result, output_path):
                    print(f"    Saved to: {output_path}")
            else:
                print(f"    ✗ Failed: {result.error_message}")
        
        tts.shutdown()
        return True
        
    except Exception as e:
        print(f"✗ Test failed: {e}")
        return False


def test_voice_design_synthesis(api_key):
    """Test voice design synthesis."""
    print("\n" + "=" * 60)
    print("Test: Voice Design Synthesis")
    print("=" * 60)
    
    if not api_key:
        print("⚠ Skipping: No API key provided")
        return None
    
    try:
        tts = MiMoTTS(api_key=api_key, max_workers=5)
        
        # Test with different voice descriptions
        test_cases = [
            {
                "description": "一位温柔的年轻女性，声音甜美，语速稍慢",
                "text": "你好，这是一段测试语音。"
            },
            {
                "description": "Young male, energetic and cheerful, speaking quickly",
                "text": "Hello, this is a test voice."
            },
        ]
        
        for i, case in enumerate(test_cases):
            print(f"\n  Test case {i+1}: {case['description'][:30]}...")
            
            result = tts.synthesize_with_design(
                text=case["text"],
                voice_description=case["description"]
            )
            
            if result.success:
                print(f"    ✓ Success - Audio size: {len(result.audio_data)} bytes")
                
                # Save audio
                output_path = f"test_design_{i+1}.wav"
                if tts.save_audio(result, output_path):
                    print(f"    Saved to: {output_path}")
            else:
                print(f"    ✗ Failed: {result.error_message}")
        
        tts.shutdown()
        return True
        
    except Exception as e:
        print(f"✗ Test failed: {e}")
        return False


def test_voice_clone_synthesis(api_key):
    """Test voice clone synthesis."""
    print("\n" + "=" * 60)
    print("Test: Voice Clone Synthesis")
    print("=" * 60)
    
    if not api_key:
        print("⚠ Skipping: No API key provided")
        return None
    
    # Check for test audio file
    test_audio = "reference.wav"
    if not os.path.exists(test_audio):
        print(f"⚠ Reference audio not found: {test_audio}")
        print("  Skipping voice clone test")
        print("  To test voice clone, place a reference audio file named 'reference.wav' in the current directory")
        return None
    
    try:
        tts = MiMoTTS(api_key=api_key, max_workers=5)
        
        result = tts.synthesize_with_clone(
            text="你好，这是使用克隆声音合成的语音。",
            reference_audio_path=test_audio
        )
        
        if result.success:
            print(f"✓ Voice clone synthesis successful")
            print(f"  Audio size: {len(result.audio_data)} bytes")
            
            # Save audio
            output_path = "test_clone.wav"
            if tts.save_audio(result, output_path):
                print(f"  Saved to: {output_path}")
        else:
            print(f"✗ Voice clone synthesis failed: {result.error_message}")
        
        tts.shutdown()
        return result.success
        
    except Exception as e:
        print(f"✗ Test failed: {e}")
        return False


def test_batch_synthesis(api_key):
    """Test batch synthesis."""
    print("\n" + "=" * 60)
    print("Test: Batch Synthesis")
    print("=" * 60)
    
    if not api_key:
        print("⚠ Skipping: No API key provided")
        return None
    
    try:
        tts = MiMoTTS(api_key=api_key, max_workers=3)
        
        # Create batch requests
        requests = [
            {
                "mode": "preset",
                "text": "第一段文本",
                "voice": "冰糖",
            },
            {
                "mode": "preset",
                "text": "第二段文本",
                "voice": "茉莉",
            },
            {
                "mode": "design",
                "text": "第三段文本",
                "voice_description": "一位温柔的女性，声音甜美",
            },
        ]
        
        start_time = time.time()
        results = tts.batch_synthesize(requests, max_workers=3)
        elapsed = time.time() - start_time
        
        print(f"✓ Batch processing completed in {elapsed:.2f}s")
        
        success_count = 0
        for i, result in enumerate(results):
            if result.success:
                print(f"  Request {i+1}: Success (size: {len(result.audio_data)} bytes)")
                success_count += 1
                
                # Save audio
                tts.save_audio(result, f"test_batch_{i+1}.wav")
            else:
                print(f"  Request {i+1}: Failed - {result.error_message}")
        
        print(f"  Success rate: {success_count}/{len(requests)}")
        
        tts.shutdown()
        return success_count > 0
        
    except Exception as e:
        print(f"✗ Test failed: {e}")
        return False


def test_async_requests(api_key):
    """Test async request handling."""
    print("\n" + "=" * 60)
    print("Test: Async Requests")
    print("=" * 60)
    
    if not api_key:
        print("⚠ Skipping: No API key provided")
        return None
    
    try:
        tts = MiMoTTS(api_key=api_key, max_workers=5)
        
        # Submit async requests
        futures = []
        for i in range(3):
            future = tts.synthesize_with_preset_async(
                text=f"异步请求测试 {i}",
                voice="冰糖" if i % 2 == 0 else "茉莉",
            )
            futures.append(future)
        
        # Wait for results
        results = []
        for i, future in enumerate(futures):
            result = future.result()
            results.append(result)
            status = "Success" if result.success else "Failed"
            print(f"  Request {i+1}: {status}")
        
        success_count = sum(1 for r in results if r.success)
        print(f"✓ Async requests completed: {success_count}/{len(futures)} successful")
        
        tts.shutdown()
        return success_count > 0
        
    except Exception as e:
        print(f"✗ Test failed: {e}")
        return False


def test_high_level_api(api_key):
    """Test high-level API wrapper."""
    print("\n" + "=" * 60)
    print("Test: High-Level API")
    print("=" * 60)
    
    if not api_key:
        print("⚠ Skipping: No API key provided")
        return None
    
    try:
        api = MiMoTTSAPI(api_key=api_key, max_workers=5)
        
        # Show available voices
        voices = api.get_voices()
        print(f"  Available voices: {', '.join(voices.keys())}")
        
        # Test preset voice
        result = api.synthesize(
            text="你好，这是使用高级API合成的语音。",
            voice="冰糖",
            style="用温柔亲切的语气"
        )
        
        if result.success:
            print(f"✓ High-level API synthesis successful")
            print(f"  Audio size: {len(result.audio_data)} bytes")
            
            # Save audio
            output_path = "test_highlevel.wav"
            if api.save_audio(result, output_path):
                print(f"  Saved to: {output_path}")
        else:
            print(f"✗ High-level API failed: {result.error_message}")
        
        api.shutdown()
        return result.success
        
    except Exception as e:
        print(f"✗ Test failed: {e}")
        return False


def test_concurrent_requests(api_key):
    """Test concurrent request handling."""
    print("\n" + "=" * 60)
    print("Test: Concurrent Requests")
    print("=" * 60)
    
    if not api_key:
        print("⚠ Skipping: No API key provided")
        return None
    
    try:
        tts = MiMoTTS(api_key=api_key, max_workers=5)
        
        import threading
        
        results = []
        errors = []
        
        def worker(text_id):
            try:
                result = tts.synthesize_with_preset(
                    text=f"并发请求测试 {text_id}",
                    voice="冰糖" if text_id % 2 == 0 else "茉莉",
                )
                results.append((text_id, result.success))
            except Exception as e:
                errors.append((text_id, str(e)))
        
        # Create multiple threads
        threads = []
        for i in range(5):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()
        
        # Wait for all threads
        for t in threads:
            t.join()
        
        success_count = sum(1 for _, success in results if success)
        print(f"✓ Concurrent requests completed")
        print(f"  Successful: {success_count}")
        print(f"  Failed: {len(errors)}")
        
        tts.shutdown()
        return len(errors) == 0
        
    except Exception as e:
        print(f"✗ Test failed: {e}")
        return False


def main():
    """Run all tests."""
    print("=" * 60)
    print("MiMo TTS API Test Suite (Online Version)")
    print("=" * 60)
    
    # Get API key
    api_key = get_api_key()
    
    if api_key:
        print(f"✓ API key configured")
    else:
        print("⚠ No API key - will skip API call tests")
    
    tests = [
        ("Configuration Creation", test_config_creation, False),
        ("TTS Initialization", lambda: test_tts_initialization(api_key), False),
        ("Preset Voice Synthesis", lambda: test_preset_voice_synthesis(api_key), True),
        ("Voice Design Synthesis", lambda: test_voice_design_synthesis(api_key), True),
        ("Voice Clone Synthesis", lambda: test_voice_clone_synthesis(api_key), True),
        ("Batch Synthesis", lambda: test_batch_synthesis(api_key), True),
        ("Async Requests", lambda: test_async_requests(api_key), True),
        ("High-Level API", lambda: test_high_level_api(api_key), True),
        ("Concurrent Requests", lambda: test_concurrent_requests(api_key), True),
    ]
    
    results = []
    for name, test_func, requires_key in tests:
        try:
            if requires_key and not api_key:
                results.append((name, None))
            else:
                passed = test_func()
                results.append((name, passed))
        except Exception as e:
            print(f"✗ {name} failed with exception: {e}")
            results.append((name, False))
    
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
    
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())