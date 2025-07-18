import os
from datetime import datetime
from pathlib import Path
from threading import Thread, Timer

import numpy as np
import sounddevice as sd
import soundfile as sf
from goofi.manager import Manager
from oscpy.client import OSCClient
from oscpy.server import OSCThreadServer
from pylsl import resolve_streams
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from adalog.base_modality import BaseModalityPlay
from adalog.utils import get_asset_path, play_audio_file


class DreamIncubator(BaseModalityPlay):
    def __init__(self):
        super().__init__()
        self.goofi_thread = None
        self.goofi_manager = None
        self.osc_server = OSCThreadServer()
        self.osc_server.listen("127.0.0.1", 5009, default=True)
        self.osc_server.bind(b"/goofi/theta_alpha", self.update_alpha_theta_ratio)
        self.osc_server.bind(b"/goofi/lziv", self.update_lziv_complexity)
        self.osc_server.bind(b"/goofi/incubation_triggered", self.handle_incubation_triggered)
        self.osc_server.bind(b"/goofi/baseline_done", self.handle_baseline_done)
        self.osc_client = OSCClient("127.0.0.1", 5010)  # OSC client to send messages to Goofi

        self.is_recording_audio = False
        self.audio_frames = []
        self.audio_samplerate = 44100  # Default sample rate
        self.audio_channels = 1  # Default channels
        self.audio_stream = None
        self.recorded_audio_path = None
        self.wakeup_audio_path = None  # To store the selected wakeup audio file path
        self.wakeup_timer = QTimer()
        self.duration_timer = None
        self.start_time = None
        self.incubation_start_time = None
        self.incubation_timer = None
        self.incubation_duration_label = QLabel("Incubation Duration: 00:00")
        


        # Start Goofi patch immediately in a separate thread
        patch_path = Path(__file__).parent / "dream_incubator.gfi"
        if not patch_path.exists():
            print(f"Error: Goofi patch not found at {patch_path}")
        else:
            self.goofi_thread = Thread(
                target=Manager,
                kwargs=dict(filepath=patch_path, headless=True),
                daemon=True,
            )
            self.goofi_thread.start()
            print(f"Started Goofi with patch: {patch_path}")

        self.setup_ui()
        self.refresh_streams()  # Initial refresh

    def setup_ui(self):
        layout = QVBoxLayout()

        self.incubation_trigger_count = 0
        self.incubation_trigger_count_label = QLabel("Incubation Triggered: 0 times")

        self.start_button = QPushButton("Start Dream Incubation")
        self.start_button.clicked.connect(self.start_dream_incubation)
        layout.addWidget(self.start_button)

        self.reset_button = QPushButton("Reset Dream Incubation")
        self.reset_button.clicked.connect(self.reset_dream_incubation)
        self.reset_button.setEnabled(False)  # Disable until started
        layout.addWidget(self.reset_button)

        self.alpha_theta_label = QLabel("Alpha/Theta Ratio: N/A")
        layout.addWidget(self.alpha_theta_label)

        self.lziv_complexity_label = QLabel("LZiv Complexity: N/A")
        layout.addWidget(self.lziv_complexity_label)

        self.duration_label = QLabel("Duration: 00:00")
        layout.addWidget(self.duration_label)

        # Incubation Audio Selection and Recording
        incubation_audio_row_layout = QHBoxLayout()
        incubation_audio_row_layout.addWidget(QLabel("Incubation Audio:"))
        self.select_incubation_audio_btn = QPushButton("Select File")
        self.select_incubation_audio_btn.clicked.connect(self.select_incubation_audio_file)
        incubation_audio_row_layout.addWidget(self.select_incubation_audio_btn)

        self.record_audio_btn = QPushButton("Start Recording")
        self.record_audio_btn.clicked.connect(self.toggle_audio_recording)
        incubation_audio_row_layout.addWidget(self.record_audio_btn)

        self.current_incubation_audio_label = QLabel("Current: None")
        incubation_audio_row_layout.addWidget(self.current_incubation_audio_label)
        incubation_audio_row_layout.addStretch(1)
        layout.addLayout(incubation_audio_row_layout)

        # Wake Up Audio Selection
        wake_up_audio_row_layout = QHBoxLayout()
        wake_up_audio_row_layout.addWidget(QLabel("Wake Up Audio:"))
        self.select_wake_up_audio_btn = QPushButton("Select File")
        self.select_wake_up_audio_btn.clicked.connect(self.select_wake_up_audio_file)
        wake_up_audio_row_layout.addWidget(self.select_wake_up_audio_btn)

        self.current_wake_up_audio_label = QLabel("Current: None")
        wake_up_audio_row_layout.addWidget(self.current_wake_up_audio_label)
        wake_up_audio_row_layout.addStretch(1)
        layout.addLayout(wake_up_audio_row_layout)

        # Wakeup Audio Delay
        wakeup_delay_row_layout = QHBoxLayout()
        wakeup_delay_row_layout.addWidget(QLabel("Wakeup Audio Delay (minutes):"))
        self.wakeup_delay_spinbox = QSpinBox()
        self.wakeup_delay_spinbox.setRange(1, 120)  # 1 to 120 minutes
        self.wakeup_delay_spinbox.setValue(10)  # Default to 10 minutes
        self.wakeup_delay_spinbox.valueChanged.connect(self.update_wakeup_delay)
        wakeup_delay_row_layout.addWidget(self.wakeup_delay_spinbox)
        wakeup_delay_row_layout.addStretch(1)
        layout.addLayout(wakeup_delay_row_layout)
        layout.addWidget(self.incubation_duration_label)  # Add this label somewhere in your UI
        layout.addWidget(self.incubation_trigger_count_label)

        # LSL Stream Selector
        row = QHBoxLayout()
        row.addWidget(QLabel("LSL Stream:"))
        self.device_dropdown = QComboBox()
        self.device_dropdown.setFixedWidth(150)
        self.device_dropdown.currentTextChanged.connect(self.send_selected_stream)

        row.addWidget(self.device_dropdown)
        self.refresh_btn = QPushButton("🔄 Refresh Streams")
        self.refresh_btn.clicked.connect(self.refresh_streams)
        row.addWidget(self.refresh_btn)
        layout.addLayout(row)

        # Audio Output Device Selector
        audio_out_row = QHBoxLayout()
        audio_out_row.addWidget(QLabel("Audio Output Device:"))
        self.audio_output_device_dropdown = QComboBox()
        self.audio_output_device_dropdown.setFixedWidth(250)
        self.audio_output_device_dropdown.currentTextChanged.connect(self.send_audio_output_device)
        audio_out_row.addWidget(self.audio_output_device_dropdown)

        self.refresh_audio_out_btn = QPushButton("🔄 Refresh Devices")
        self.refresh_audio_out_btn.clicked.connect(self.refresh_audio_output_devices)
        audio_out_row.addWidget(self.refresh_audio_out_btn)
        layout.addLayout(audio_out_row)

        


        layout.addStretch(1)
        self.setLayout(layout)

        self.refresh_streams()  # Initial refresh

    def start_dream_incubation(self):
        self.osc_client.send_message(b"/start_incubation", [1])
        self.start_button.setEnabled(False)
        self.reset_button.setEnabled(True)
        self.alpha_theta_label.setText("Alpha/Theta Ratio: Running...")
        self.lziv_complexity_label.setText("LZiv Complexity: Running...")
        self.duration_label.setText("Accumulating baseline EEG")
        print("Sent OSC message to start incubation.")

    def play_wakeup_audio(self):
        print("Playing wakeup audio.")
        audio_to_play = self.wakeup_audio_path
        if not audio_to_play:
            audio_to_play = get_asset_path("default_wakeup_audio.wav")  # Assuming a default audio file
            print(f"No wakeup audio selected, using default: {audio_to_play}")

        if audio_to_play and Path(audio_to_play).exists():
            play_audio_file(audio_to_play)
        else:
            print(f"Error: Wakeup audio file not found at {audio_to_play}")

    def reset_dream_incubation(self):
        self.osc_client.send_message(b"/reset_incubation", [1])
        self.start_button.setEnabled(True)
        self.reset_button.setEnabled(False)
        self.alpha_theta_label.setText("Alpha/Theta Ratio: N/A")
        self.lziv_complexity_label.setText("LZiv Complexity: N/A")
        self.duration_label.setText("Duration: 00:00")
        
        # Reset and stop the baseline duration timer
        if self.duration_timer is not None:
            self.duration_timer.cancel()
            self.duration_timer = None
        self.start_time = None
        
        # Stop the wakeup QTimer
        self.wakeup_timer.stop()
        
        # Reset and stop the incubation duration timer
        if hasattr(self, 'incubation_timer') and self.incubation_timer is not None:
            self.incubation_timer.cancel()
            self.incubation_timer = None
        if hasattr(self, 'incubation_start_time'):
            self.incubation_start_time = None
        if hasattr(self, 'incubation_duration_label'):
            self.incubation_duration_label.setText("Incubation Duration: 00:00")
        
        # Reset the incubation trigger counter
        if hasattr(self, 'incubation_trigger_count'):
            self.incubation_trigger_count = 0
        if hasattr(self, 'incubation_trigger_count_label'):
            self.incubation_trigger_count_label.setText("Incubation Triggered: 0 times")
        
        print("Sent OSC message to reset incubation.")


    def update_alpha_theta_ratio(self, value):
        if isinstance(value, (bytes, bytearray)):
            value = float(value.decode())
        self.alpha_theta_label.setText(f"Alpha/Theta Ratio: {value:.2f}")

    def update_lziv_complexity(self, value):
        if isinstance(value, (bytes, bytearray)):
            value = float(value.decode())
        self.lziv_complexity_label.setText(f"LZiv Complexity: {value:.2f}")

    def update_incubation_duration_display(self):
        if self.incubation_start_time:
            elapsed_time = datetime.now() - self.incubation_start_time
            minutes, seconds = divmod(int(elapsed_time.total_seconds()), 60)
            self.incubation_duration_label.setText(f"Incubation Duration: {minutes:02d}:{seconds:02d}")

            self.incubation_timer = Timer(1, self.update_incubation_duration_display)
            self.incubation_timer.start()

    def update_duration_display(self):
        if self.start_time:
            elapsed_time = datetime.now() - self.start_time
            minutes, seconds = divmod(int(elapsed_time.total_seconds()), 60)
            self.duration_label.setText(f"Duration: {minutes:02d}:{seconds:02d}")

            self.duration_timer = Timer(1, self.update_duration_display)
            self.duration_timer.start()

    def select_incubation_audio_file(self):
        file_dialog = QFileDialog()
        file_path, _ = file_dialog.getOpenFileName(
            self, "Select Incubation Audio File", "", "Audio Files (*.wav *.flac *.ogg)"
        )
        if file_path:
            self.current_incubation_audio_label.setText(f"Current: {Path(file_path).name}")
            self.send_audio_path_to_goofi(file_path, "incubation")

    def select_wake_up_audio_file(self):
        file_dialog = QFileDialog()
        file_path, _ = file_dialog.getOpenFileName(self, "Select Wake Up Audio File", "", "Audio Files (*.wav *.flac *.ogg)")
        if file_path:
            self.current_wake_up_audio_label.setText(f"Current: {Path(file_path).name}")
            self.wakeup_audio_path = file_path
            self.send_audio_path_to_goofi(file_path, "wakeup")

    def handle_incubation_triggered(self, value):
        if value == 1:
            self.incubation_trigger_count += 1
            self.incubation_trigger_count_label.setText(f"Incubation Triggered: {self.incubation_trigger_count} times")
            print("Incubation triggered. Starting wakeup timer and incubation timer.")

            # Start incubation timer
            self.incubation_start_time = datetime.now()
            if self.incubation_timer is not None:
                self.incubation_timer.cancel()
            self.incubation_timer = Timer(1, self.update_incubation_duration_display)
            self.incubation_timer.start()

            # Existing wakeup timer logic
            delay_ms = self.wakeup_delay_spinbox.value() * 60 * 1000  # Convert minutes to milliseconds
            self.wakeup_timer.singleShot(delay_ms, self.play_wakeup_audio)



    def handle_baseline_done(self, value):
        if value == 1:
            print("Baseline done. Starting duration timer.")
            self.start_time = datetime.now()
            self.duration_timer = Timer(1, self.update_duration_display)
            self.duration_timer.start()

    def toggle_audio_recording(self):
        if not self.is_recording_audio:
            self.audio_frames = []
            self.audio_stream = sd.InputStream(
                samplerate=self.audio_samplerate, channels=self.audio_channels, callback=self.audio_callback
            )
            self.audio_stream.start()
            self.is_recording_audio = True
            self.record_audio_btn.setText("Stop Recording")
            self.current_incubation_audio_label.setText("Current: Recording...")
        else:
            self.audio_stream.stop()
            self.audio_stream.close()
            self.is_recording_audio = False
            self.record_audio_btn.setText("Start Recording")

            # Save the recorded audio
            output_dir = Path.home() / ".adalog_audio_recordings"
            output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            file_name = f"recorded_audio_{timestamp}.wav"
            self.recorded_audio_path = str(output_dir / file_name)
            sf.write(self.recorded_audio_path, np.concatenate(self.audio_frames), self.audio_samplerate)
            self.current_incubation_audio_label.setText(f"Current: {file_name}")
            self.send_audio_path_to_goofi(self.recorded_audio_path, "incubation")

    def audio_callback(self, indata, frames, time, status):
        self.audio_frames.append(indata.copy())

    def send_audio_path_to_goofi(self, audio_path, audio_type):
        if audio_type == "incubation":
            self.osc_client.send_message(b"/audio_file_path", [audio_path.encode()])
        elif audio_type == "wakeup":
            self.osc_client.send_message(b"/wakeup_audio_file_path", [audio_path.encode()])

    def update_wakeup_delay(self, value):
        self.wakeup_delay_minutes = value
        print(f"Wakeup audio delay set to {self.wakeup_delay_minutes} minutes.")

    def send_selected_stream(self, stream_name):
        if stream_name and "No streams" not in stream_name:
            self.osc_client.send_message(b"/lsl_stream_selected", [stream_name.encode()])

    def refresh_streams(self):
        self.device_dropdown.clear()
        streams = resolve_streams()
        if not streams:
            self.device_dropdown.addItem("No streams available")
        else:
            for stream in streams:
                label = f"{stream.source_id()}"
                self.device_dropdown.addItem(label)

    def send_audio_output_device(self, device_name):
        if device_name and "No devices" not in device_name:
            self.osc_client.send_message(b"/audio_out_device", [device_name.encode()])

    def refresh_audio_output_devices(self):
        self.audio_output_device_dropdown.clear()
        devices = sd.query_devices()
        output_devices = [d["name"] for d in devices if d["max_output_channels"] > 0]
        if not output_devices:
            self.audio_output_device_dropdown.addItem("No devices available")
        else:
            self.audio_output_device_dropdown.addItems(output_devices)

    def start(self):
        # This method is called when the main system starts
        # We can potentially auto-start the dream incubation here or just leave it to the user button
        print("DreamIncubator panel received start signal from main system.")

    def stop(self):
        # This method is called when the main system stops
        self.reset_dream_incubation()
        print("DreamIncubator panel received stop signal from main system.")

    def closeEvent(self, event):
        self.osc_server.terminate_server()
        self.osc_server.join_server()
        if self.is_recording_audio and self.audio_stream:
            self.audio_stream.stop()
            self.audio_stream.close()
        if self.duration_timer is not None:
            self.duration_timer.cancel()
            self.duration_timer = None
        if hasattr(self, 'incubation_timer') and self.incubation_timer is not None:
            self.incubation_timer.cancel()
            self.incubation_timer = None
        if self.goofi_manager:
            self.goofi_manager.stop()
        if self.goofi_thread and self.goofi_thread.is_alive():
            self.goofi_thread.join(timeout=1)
        super().closeEvent(event)
        print("DreamIncubator panel received stop signal from main system.")

