import logging
import subprocess
import threading
from typing import Optional

from myth_genericrecorder.recorder import Recorder, replace_variables_in_string, dequote


class Touch:
    def __init__(self,
                 frequency: Optional[str],
                 command: Optional[str],
                 recorder_instance: Recorder,
                 delay: Optional[str] = None,
                 damaged_on_failure_str: Optional[str] = None):

        self.log = logging.getLogger(__name__)
        self.recorder = recorder_instance
        self.raw_command: str = command or ""
        self.frequency_str = frequency
        self.delay_str = delay

        self.damaged_on_failure: bool = str(damaged_on_failure_str).strip().lower() == 'true'

        # Parse intervals into total float seconds
        self.interval_seconds: Optional[float] = self._parse_time_string(frequency)
        self.delay_seconds: Optional[float] = self._parse_time_string(delay)

        # Thread management control signals
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

    def _parse_time_string(self, time_str: Optional[str]) -> Optional[float]:
        if not time_str or not isinstance(time_str, str): return None
        try:
            parts = [int(p) for p in time_str.split(":")]
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
        if self.delay_seconds is not None and self.delay_seconds > 0:
            self.log.debug("Touch thread idling for initial delay of "
                           f"{self.delay_seconds}s...")
            if self._stop_event.wait(self.delay_seconds):
                return

        self._execute_command()

        if self.interval_seconds is None:
            self.log.info("Single-shot Touch execution complete. "
                          "Exiting thread gracefully.")
            return

        while not self._stop_event.is_set():
            stopped = self._stop_event.wait(self.interval_seconds)
            if stopped:
                break
            self._execute_command()

    def _execute_command(self) -> None:
        if not self.raw_command:
            self.log.error("Touch execution aborted: No base command provided.")
            return

        try:
            current_variables = self.recorder.getVariables()
            expanded = replace_variables_in_string(self.raw_command,
                                                   current_variables)
            if not expanded:
                self.log.error("Touch command generation yielded an "
                               "empty string using current variables.")
                return

            active_command = dequote(expanded)

            result = subprocess.run(
                active_command,
                shell=True,
                capture_output=True,
                text=True,
                check=False,
            )

            if result.returncode == 0:
                self.log.info("Command executed successfully (Exit 0): "
                              f"'{active_command}'")
                if result.stdout.strip():
                    self.log.debug(f"stdout: {result.stdout.strip()}")
            else:
                self.log.warning("Command returned non-zero exit code "
                                 f"{result.returncode}: '{active_command}'")
                if result.stdout.strip():
                    self.log.debug(f"stdout: {result.stdout.strip()}")
                if result.stderr.strip():
                    self.log.error(f"stderr: {result.stderr.strip()}")

                if self.damaged_on_failure:
                    self.recorder.handle_touch_error(
                        result.returncode, result.stderr.strip()
                    )

        except Exception as e:
            self.log.exception("Exception encountered during dynamic "
                               f"Touch execution workflow: {e}")
            error_type = ("TOUCH_CRITICAL_FAILURE"
                          if self.damaged_on_failure
                          else "TOUCH_NONCRITICAL_FAILURE")
            self.recorder.signal_event(error_type, {"exit_code": -1,
                                                    "error": str(e),
                                                    "command": self.raw_command})

    def start(self) -> bool:
        has_valid_frequency = (self.interval_seconds is not None
                               and self.interval_seconds > 0)
        has_valid_delay = (self.delay_seconds is not None
                           and self.delay_seconds > 0)

        if not has_valid_frequency and not has_valid_delay:
            self.log.error("Touch parameters invalid. Must provide a "
                           f"valid frequency ('{self.frequency_str}') or "
                           f"delay ('{self.delay_str}')")
            return False

        if not self.raw_command:
            self.log.error("Touch validation aborted: Base raw command "
                           "missing from config.")
            return False

        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._loop_executor,
                                               daemon=True)
        self._worker_thread.start()

        if has_valid_frequency:
            delay_msg = (f" after an initial delay of {self.delay_seconds}s"
                         if has_valid_delay else "")
            self.log.info("Started tracking loop every "
                          f"{self.interval_seconds}s{delay_msg} "
                          f"(DamagedOnFailure={self.damaged_on_failure}): "
                          f"'{self.raw_command}'")
        else:
            self.log.info("Started single-shot tracking task scheduled "
                          f"once in {self.delay_seconds}s "
                          f"(DamagedOnFailure={self.damaged_on_failure}): "
                          f"'{self.raw_command}'")
        return True

    def stop(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            self._stop_event.set()
            self._worker_thread.join(timeout=2.0)
            self.log.info("Touch background thread stopped safely.")
