"""Allows for capturing the screen and audio on Linux.

usage: see bottom of file
"""

import os
import subprocess
from datetime import datetime
from sys import platform
import wave
import pyaudio

from openadapt.config import CAPTURE_DIR_PATH


class Capture:
    """Capture the screen, audio, and camera on Linux."""

    def __init__(self) -> None:
        """Initialize the capture object."""
        assert platform == "linux", platform

        self.is_recording = False
        self.audio_out = None
        self.video_out = None
        self.audio_stream = None
        self.audio_frames = []

        # Initialize PyAudio
        self.audio = pyaudio.PyAudio()

    def start(self, audio: bool = True, camera: bool = False) -> None:
        """Start capturing the screen, audio, and camera.

        Args:
            audio (bool, optional): Whether to capture audio (default: True).
            camera (bool, optional): Whether to capture the camera (default: False).
        """
        if self.is_recording:
            raise RuntimeError("Recording is already in progress")

        self.is_recording = True
        capture_dir = CAPTURE_DIR_PATH
        if not os.path.exists(capture_dir):
            os.mkdir(capture_dir)

        # Start video capture using ffmpeg
        video_filename = datetime.now().strftime("%Y-%m-%d-%H-%M-%S") + ".mp4"
        self.video_out = os.path.join(capture_dir, video_filename)
        self._start_video_capture()

        # Start audio capture
        if audio:
            audio_filename = datetime.now().strftime("%Y-%m-%d-%H-%M-%S") + ".wav"
            self.audio_out = os.path.join(capture_dir, audio_filename)
            self._start_audio_capture()

    def _start_video_capture(self) -> None:
        """Start capturing the screen using ffmpeg."""
        cmd = [
            "ffmpeg",
            "-f", "x11grab",  # Capture X11 display
            "-video_size", "1920x1080",  # Set resolution
            "-framerate", "30",  # Set frame rate
            "-i", ":0.0",  # Capture from display 0
            "-c:v", "libx264",  # Video codec
            "-preset", "ultrafast",  # Speed/quality tradeoff
            "-y", self.video_out  # Output file
        ]
        self.video_proc = subprocess.Popen(cmd)

    def _start_audio_capture(self) -> None:
        """Start capturing audio using PyAudio."""
        self.audio_stream = self.audio.open(
            format=pyaudio.paInt16,
            channels=2,
            rate=44100,
            input=True,
            frames_per_buffer=1024,
            stream_callback=self._audio_callback
        )
        self.audio_frames = []
        self.audio_stream.start_stream()

    def _audio_callback(self, in_data: bytes, frame_count: int, time_info: dict, status: int) -> tuple:
        """Callback function to process audio data."""
        self.audio_frames.append(in_data)
        return (None, pyaudio.paContinue)

    def stop(self) -> None:
        """Stop capturing the screen, audio, and camera."""
        if self.is_recording:
            # Stop the video capture
            self.video_proc.terminate()

            # Stop audio capture
            if self.audio_stream:
                self.audio_stream.stop_stream()
                self.audio_stream.close()
                self.audio.terminate()
                self.save_audio()

            self.is_recording = False

    def save_audio(self) -> None:
        """Save the captured audio to a WAV file."""
        if self.audio_out:
            with wave.open(self.audio_out, "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(self.audio.get_sample_size(pyaudio.paInt16))
                wf.setframerate(44100)
                wf.writeframes(b"".join(self.audio_frames))


if __name__ == "__main__":
    capture = Capture()
    capture.start(audio=True, camera=False)
    input("Press enter to stop")
    capture.stop()
