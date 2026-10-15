import os
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

APP_NAME = "VexoTTS"
SINK = "vexotts_output"
SOURCE = "vexotts_mic"

root = None
status = None
speak_button = None
closing = False
player_lock = threading.Lock()
speak_lock = threading.Lock()
stop_event = threading.Event()
active_processes = set()


def run_command(args):
    """Run a short system command and return its completed result."""
    try:
        return subprocess.run(args, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RuntimeError(f"Could not run {args[0]}: {exc}") from exc


def entity_exists(kind, name):
    result = run_command(["pactl", "list", "short", kind])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"Could not list audio {kind}.")
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == name:
            return True
    return False


def setup_virtual_microphone():
    """Create a virtual sink and a source connected to its monitor."""
    if not entity_exists("sinks", SINK):
        result = run_command([
            "pactl", "load-module", "module-null-sink",
            f"sink_name={SINK}",
            "sink_properties=device.description=VexoTTS_Output",
        ])
        if result.returncode != 0:
            raise RuntimeError(
                "Could not create the VexoTTS virtual output.\n"
                + (result.stderr.strip() or "PipeWire/PulseAudio returned an error.")
            )

    if not entity_exists("sources", SOURCE):
        result = run_command([
            "pactl", "load-module", "module-remap-source",
            f"master={SINK}.monitor",
            f"source_name={SOURCE}",
            "source_properties=device.description=VexoTTS_Microphone",
        ])
        if result.returncode != 0:
            raise RuntimeError(
                "The VexoTTS output exists, but its virtual microphone could not be created.\n"
                + (result.stderr.strip() or "PipeWire/PulseAudio returned an error.")
            )

    if not entity_exists("sources", SOURCE):
        raise RuntimeError("The VexoTTS microphone source was not found after setup.")


def get_voices():
    """Get usable English voice IDs, rather than the display-name column."""
    preferred = [
        "en-us+m3", "en-us+f3", "en-us+m1", "en-us+f1", "en-us", "en-gb", "en"
    ]
    result = run_command(["espeak-ng", "--voices"])
    discovered = []
    if result.returncode == 0:
        for line in result.stdout.splitlines()[1:]:
            fields = line.split()
            # Column 2 is the language/voice ID (e.g. en-us), not VoiceName.
            if len(fields) >= 2 and fields[1].lower().startswith("en"):
                discovered.append(fields[1])
    ordered = []
    for item in preferred + discovered:
        if item not in ordered:
            ordered.append(item)
    return ordered


def schedule_ui(callback):
    """Safely schedule a UI update from a worker thread."""
    if closing or root is None:
        return
    try:
        root.after(0, callback)
    except tk.TclError:
        pass


def set_status(text):
    schedule_ui(lambda text=text: status.set(text) if status is not None else None)


def start_tracked_process(args, **kwargs):
    """Start a process and register it so STOP can terminate it."""
    try:
        proc = subprocess.Popen(args, **kwargs)
    except OSError as exc:
        raise RuntimeError(f"Could not start {args[0]}: {exc}") from exc

    with player_lock:
        active_processes.add(proc)
        should_stop = stop_event.is_set() or closing

    if should_stop and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
    return proc


def forget_process(proc):
    with player_lock:
        active_processes.discard(proc)


def terminate_active_processes():
    with player_lock:
        processes = list(active_processes)
    for proc in processes:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass


def terminate_and_reap(processes):
    for proc in processes:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
    for proc in processes:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                pass
        except OSError:
            pass
        forget_process(proc)


def show_error(text):
    if not closing:
        schedule_ui(lambda text=text: messagebox.showerror(APP_NAME, text))


def speak():
    text = text_box.get("1.0", "end-1c").strip()
    if not text:
        status.set("Type something first!")
        return

    if not speak_lock.acquire(blocking=False):
        status.set("Already speaking. Press STOP first.")
        return

    stop_event.clear()
    speak_button.config(state="disabled")
    selected_voice = voice.get().strip() or "en-us+m3"
    selected_speed = str(int(speed.get()))
    selected_pitch = str(int(pitch.get()))

    def worker():
        temp_path = None
        own_processes = []
        try:
            set_status("Preparing virtual microphone…")
            setup_virtual_microphone()
            if stop_event.is_set():
                return

            # Generate a WAV file once so both outputs receive identical audio.
            temp = tempfile.NamedTemporaryFile(prefix="vexotts_", suffix=".wav", delete=False)
            temp_path = temp.name
            temp.close()

            set_status("Generating speech…")
            with open(temp_path, "wb") as audio_file:
                generator = start_tracked_process(
                    [
                        "espeak-ng", "-v", selected_voice,
                        "-s", selected_speed,
                        "-p", selected_pitch,
                        "--stdout", text,
                    ],
                    stdout=audio_file,
                    stderr=subprocess.PIPE,
                )
                own_processes.append(generator)
                _, generation_error = generator.communicate()
                forget_process(generator)

            if stop_event.is_set():
                return
            if generator.returncode != 0:
                detail = generation_error.decode("utf-8", errors="replace").strip()
                raise RuntimeError(detail or "eSpeak NG could not generate the speech.")
            if not os.path.exists(temp_path) or os.path.getsize(temp_path) < 44:
                raise RuntimeError("eSpeak NG produced an empty or invalid audio file.")

            set_status("Speaking through headphones and virtual microphone…")

            # Play locally through the normal default output AND into VexoTTS's virtual sink.
            local_player = start_tracked_process(
                ["paplay", temp_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            own_processes.append(local_player)

            virtual_player = start_tracked_process(
                ["paplay", f"--device={SINK}", temp_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            own_processes.append(virtual_player)

            failure = None
            while True:
                if stop_event.is_set():
                    terminate_and_reap(own_processes)
                    break

                running = [proc for proc in own_processes if proc.poll() is None]
                failed = [proc for proc in own_processes if proc.poll() is not None and proc.returncode != 0]
                if failed:
                    # Stop the other output too if either audio route fails.
                    terminate_and_reap(own_processes)
                    failure = failed[0]
                    break
                if not running:
                    break
                time.sleep(0.04)

            errors = []
            for proc in own_processes:
                if proc.poll() is None:
                    try:
                        proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        terminate_and_reap([proc])
                try:
                    _, err = proc.communicate(timeout=0.5)
                except subprocess.TimeoutExpired:
                    terminate_and_reap([proc])
                    try:
                        _, err = proc.communicate(timeout=0.5)
                    except (subprocess.TimeoutExpired, OSError):
                        err = b""
                except OSError:
                    err = b""
                forget_process(proc)
                if proc.returncode not in (0, None) and not stop_event.is_set():
                    errors.append(err.decode("utf-8", errors="replace").strip())

            if failure is not None and not stop_event.is_set():
                detail = next((item for item in errors if item), "")
                raise RuntimeError(detail or "An audio playback route failed.")
            if errors and not stop_event.is_set():
                raise RuntimeError(next((item for item in errors if item), "Audio playback failed."))

            if stop_event.is_set():
                set_status("Stopped.")
            else:
                set_status("Finished! Ready for more.")

        except Exception as exc:
            if not stop_event.is_set():
                show_error(str(exc))
                set_status("An error occurred.")
        finally:
            terminate_and_reap(own_processes)
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            speak_lock.release()
            schedule_ui(lambda: speak_button.config(state="normal") if speak_button is not None else None)

    threading.Thread(target=worker, daemon=True).start()


def stop():
    stop_event.set()
    terminate_active_processes()
    status.set("Stopping…")


def close_app():
    global closing
    closing = True
    stop_event.set()
    terminate_active_processes()
    if root is not None:
        root.destroy()


for program in ("espeak-ng", "paplay", "pactl"):
    if not shutil.which(program):
        raise SystemExit(
            f"Missing required program: {program}\n\n"
            "Install everything with:\n"
            "sudo apt install espeak-ng pulseaudio-utils python3-tk"
        )


root = tk.Tk()
root.title(APP_NAME)
root.geometry("620x520")
root.minsize(480, 420)

header = ttk.Label(root, text=APP_NAME, font=("Sans", 26, "bold"))
header.pack(pady=(18, 2))
ttk.Label(root, text="Your text. Your voice.", font=("Sans", 11)).pack(pady=(0, 16))

ttk.Label(root, text="Enter your text:").pack(anchor="w", padx=24)
text_box = tk.Text(root, height=7, wrap="word", font=("Sans", 12))
text_box.pack(fill="both", expand=True, padx=24, pady=8)
text_box.insert("1.0", "Hello everyone! This is VexoTTS.")

settings = ttk.Frame(root)
settings.pack(fill="x", padx=24, pady=10)

ttk.Label(settings, text="Voice:").grid(row=0, column=0, sticky="w", pady=5)
voice = tk.StringVar(value="en-us+m3")
voice_box = ttk.Combobox(
    settings, textvariable=voice, values=get_voices(), width=24, state="normal"
)
voice_box.grid(row=0, column=1, sticky="ew", padx=12, pady=5)
voice_box.set("en-us+m3")

ttk.Label(settings, text="Speed:").grid(row=1, column=0, sticky="w", pady=5)
speed = tk.IntVar(value=150)
ttk.Scale(settings, from_=80, to=250, variable=speed, orient="horizontal").grid(
    row=1, column=1, sticky="ew", padx=12, pady=5
)

ttk.Label(settings, text="Pitch:").grid(row=2, column=0, sticky="w", pady=5)
pitch = tk.IntVar(value=50)
ttk.Scale(settings, from_=0, to=99, variable=pitch, orient="horizontal").grid(
    row=2, column=1, sticky="ew", padx=12, pady=5
)
settings.columnconfigure(1, weight=1)

button_frame = ttk.Frame(root)
button_frame.pack(pady=10)
speak_button = ttk.Button(button_frame, text="▶  SPEAK", command=speak)
speak_button.pack(side="left", padx=6)
ttk.Button(button_frame, text="■  STOP", command=stop).pack(side="left", padx=6)

status = tk.StringVar(value="Ready!")
ttk.Label(root, textvariable=status, wraplength=560).pack(pady=(5, 16))
text_box.bind("<Control-Return>", lambda event: (speak(), "break"))
root.protocol("WM_DELETE_WINDOW", close_app)
root.mainloop()
