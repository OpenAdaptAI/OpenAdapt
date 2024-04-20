"""Allows for capturing the screen and audio on Windows."""
from datetime import datetime
from sys import platform
import os
import wave

from screen_recorder_sdk import screen_recorder
import pyaudio

from openadapt.config import config


class Capture:
    """Capture the screen video and audio on Windows."""

    def __init__(self, pid: int = 0) -> None:
        """Initialize the capture object.

        Args:
            pid (int, optional): The process ID of the window to capture.
            Defaults to 0 (the entire screen)
        """
        if platform != "win32":
            raise NotImplementedError(
                "This is the Windows implementation, please use the macOS version"
            )
        self.is_recording = False
        self.video_out = None
        self.audio_out = None
        self.pid = pid

        screen_recorder.init_resources(screen_recorder.RecorderParams(pid=self.pid))

        # Initialize PyAudio
        self.audio = pyaudio.PyAudio()
        self.audio_stream = None
        self.audio_frames = []

    def start(self, audio: bool = True) -> None:
        """Start capturing the screen video and audio.

        Args:
            audio (bool): Whether to capture audio.
        """
        if self.is_recording:
            raise RuntimeError("Recording is already in progress")
        self.is_recording = True

        # Start video recording
        self.video_out = os.path.join(
            config.CAPTURE_DIR_PATH,
            datetime.now().strftime("%Y-%m-%d-%H-%M-%S") + ".mp4",
        )
        if not os.path.exists(config.CAPTURE_DIR_PATH):
            os.mkdir(config.CAPTURE_DIR_PATH)
        screen_recorder.start_video_recording(self.video_out, 30, 8000000, True)

        # Start audio recording
        if audio:
            self.audio_out = os.path.join(
                config.CAPTURE_DIR_PATH,
                datetime.now().strftime("%Y-%m-%d-%H-%M-%S") + ".wav",
            )
            self.audio_stream = self.audio.open(
                format=pyaudio.paInt16,
                channels=2,
                rate=44100,
                input=True,
                frames_per_buffer=1024,
                stream_callback=self._audio_callback,
            )
            self.audio_frames = []

    def _audio_callback(
        self, in_data: bytes, frame_count: int, time_info: dict, status: int
    ) -> tuple:
        self.audio_frames.append(in_data)
        return (None, pyaudio.paContinue)

    def stop(self) -> None:
        """Stop capturing the screen video and audio."""
        if self.is_recording:
            screen_recorder.stop_video_recording()
            if self.audio_stream:
                self.audio_stream.stop_stream()
                self.audio_stream.close()
                self.audio.terminate()
                self.save_audio()
            self.is_recording = False
            screen_recorder.free_resources()

    def save_audio(self) -> None:
        """Save the captured audio to a WAV file."""
        with wave.open(self.audio_out, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(self.audio.get_sample_size(pyaudio.paInt16))
            wf.setframerate(44100)
            wf.writeframes(b"".join(self.audio_frames))


if __name__ == "__main__":
    capture = Capture()
    capture.start()
    input("Press enter to stop")
    capture.stop()
