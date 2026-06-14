import logging
import subprocess
import threading
from typing import Optional, Callable

class Touch:
    def __init__(self, frequency: str, command: str,
                 on_error_callback: Optional[Callable[[int, str], None]] = None):
        self.frequency_str = frequency
        self.command = command
        self.on_error_callback = on_error_callback

        self.log = logging.getLogger(__name__)
        self.interval_seconds: Optional[float] = self._parse_frequency(frequency)
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

    def _parse_frequency(self, freq_str: str) -> Optional[float]:
        if not freq_str or not isinstance(freq_str, str): return None
        try:
            parts = [int(p) for p in freq_str.split(":")]
            num_parts = len(parts)
            if num_parts == 1:
                return float(parts[0])
            elif num_parts == 2:
                return float((parts[0] * 60) + parts[1])
            elif num_parts == 3:
                return float((parts[0] * 3600) + (parts[1] * 60) + parts[2])
            return None
        except ValueError: return None

    def _loop_executor(self) -> None:
        while not self._stop_event.is_set():
            stopped = self._stop_event.wait(self.interval_seconds)
            if stopped:
                break
            self._execute_command()

    def _execute_command(self) -> None:
        try:
            result = subprocess.run(
                self.command,
                shell=True,
                capture_output=True,
                text=True,
                check=False,
            )

            if result.returncode == 0:
                self.log.info("Command executed successfully (Exit 0): "
                              f"'{self.command}'")
                if result.stdout.strip():
                    self.log.debug(f"stdout: {result.stdout.strip()}")
            else:
                self.log.warning("Command returned non-zero exit code "
                                 f"{result.returncode}: '{self.command}'")
                if result.stdout.strip():
                    self.log.debug(f"stdout: {result.stdout.strip()}")
                if result.stderr.strip():
                    self.log.error(f"stderr: {result.stderr.strip()}")

                if self.on_error_callback:
                    self.on_error_callback(result.returncode,
                                           result.stderr.strip())

        except Exception as e:
            self.log.exception("Exception encountered while executing "
                               f"'{self.command}': {e}")
            if self.on_error_callback:
                self.on_error_callback(-1, str(e))

    def start(self) -> bool:
        if self.interval_seconds is None or self.interval_seconds <= 0:
            self.log.error(f"Invalid frequency format: '{self.frequency_str}'")
            return False
        if not self.command:
            self.log.error("Empty shell command string provided.")
            return False

        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._loop_executor,
                                               daemon=True)
        self._worker_thread.start()
        self.log.info("Started background keepalive loop every "
                      f"{self.interval_seconds}s: '{self.command}'")
        return True

    def stop(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            self._stop_event.set()
            self._worker_thread.join(timeout=2.0)
            self.log.info("Background keepalive loop stopped safely.")
