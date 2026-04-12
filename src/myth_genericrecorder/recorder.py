#!/usr/bin/env python3
"""Recorder module for ExternalRecorder program."""
import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Any, Optional
import shlex
import re
import selectors

class Recorder:
    """Recorder class to handle streaming and command execution."""

    def __init__(self, command: Optional[str] = None,
                 logger: Optional[logging.Logger] = None,
                 tune_command: Optional[str] = None,
                 config: Optional[Dict] = None,
                 variables: Dict = None,
                 block_size: int = 65536):
        """Initialize the recorder."""
        self.command             = None
        self.logger              = logger or logging.getLogger(__name__)
        self.streaming           = False
        self.stream_thread       = None
        self.stderr_thread       = None
        self.stream_process      = None
        self.xon_process         = None
        self.stdout_lock         = threading.Lock()
        self.stderr_lock         = threading.Lock()
        self.tune_process        = None
        self.ondatastart_process = None
        self.ondatastart_done    = False
        self.tune_thread         = None
        self.tune_status         = "Idle"  # Idle, InProgress, Tuned

        # Configuration
        self.config    = config or {}
        self.variables = variables

        """
        if variables is not None:
            for key, value in self.variables.items():
                self.variables[key] = self.replace_variables_in_string(value)
        """

        if command is not None and len(command) > 0:
            self.config['RECORDER']['command'] = command
        if tune_command is not None and len(tune_command) > 0:
            self.config['TUNER']['command'] = tune_command

        # XON/XOFF state
        self.variables['XONCOUNT'] = 0
        self.xon_state = False  # Start in XOFF state

        # Block size
        self.block_size = block_size

        # Available command handlers
        self.handlers = {
            "APIVersion"           : self.api_version,
            "Version?"             : self.version,
            "Description?"         : self.description,
            "HasTuner?"            : self.has_tuner,
            "HasPictureAttributes?": self.has_picture_attributes,
            "FlowControl?"         : self.flow_control,
            "BlockSize"            : self.block_size_handler,
            "LockTimeout?"         : self.lock_timeout,
            "SignalStrengthPercent?": self.signal_strength,
            "HasLock?"             : self.has_lock,
            "TuneChannel"          : self.tune_channel,
            "TuneStatus?"          : self.tune_status_handler,
            "IsOpen?"              : self.is_open,
            "CloseRecorder"        : self.close_recorder,
            "StartStreaming"       : self.start_streaming,
            "StopStreaming"        : self.stop_streaming,
            "XON"                  : self.xon,
            "XOFF"                 : self.xoff
        }
        self.logger.debug(f"Recorder.__init__ called with config={config}")  # Debug line

    def process_command(self, message: Dict[str, Any]) -> None:
        """Process a command from stdin."""
        command = message.get("command", "")
        if not command:
            self.logger.warning("No command in message")
            return

        # Clean command name (remove invalid characters)
        clean_command = "".join(c for c in command if c.isalnum() or c in "_?")

        if clean_command not in self.handlers:
            self.logger.warning(f"Unknown command: {command}")
            self.send_response(message, {"status": "error", "message": "Unknown command"})
            return

        try:
            # Execute the handler
            self.logger.debug(f"Executing handler for command: {command}")
            handler = self.handlers[clean_command]
            handler(**message)
        except Exception as e:
            self.logger.exception(f"Error executing command {command}: {e}")
            self.send_response(message, {"status": "error", "message": str(e)})

    def send_response(self, original_message: Dict[str, Any],
                     response_data: Dict[str, Any]) -> None:
        """Send a JSON response to stderr."""
        keep_keys = ["command", "serial"]
        response = {key: original_message[key] for key in keep_keys
                    if key in original_message}
        response.update(response_data)

        # Write to stderr
        try:
            json.dump(response, sys.stderr)
            sys.stderr.write("\n")
            sys.stderr.flush()
            self.logger.debug(f"Sent response: {response}")
        except Exception as e:
            self.logger.error(f"Failed to send response: {e}")

    def channel_override(self, variable : str, default : str):
        # Get the channum from variables
        channum = self.variables.get('CHANNUM', None)
        self.logger.debug(f"Looking for {variable} for channum {channum}")
        if not channum:
            self.logger.debug(f"Could not determine channum for {variable}")
            return default

        if 'CHANNELS' in self.config:
            self.logger.info(f"Looking for {channum} in "
                             f"{self.config['TUNER']['CHANNELS']}")
            channel_config = self.config['CHANNELS'].get(channum, {})
            if variable in channel_config:
                value = channel_config[variable]
                self.logger.info(f"Using channel-specific {variable}={value}")
                return value
        return default

    def api_version(self, **kwargs) -> None:
        """Handle APIVersion command."""
        self.logger.debug("APIVersion called")
        self.send_response(kwargs, {"status": "OK"})
#                                    "message":f"{kwargs['value']}"})

    def version(self, **kwargs) -> None:
        """Handle Version? command."""
        self.logger.debug("Version? called")
        self.send_response(kwargs, {"status": "OK", "message": "0.5"})

    def description(self, **kwargs) -> None:
        """Handle Description? command."""
        """ Example response:
        {"command":"Version?","message":"2.0","serial":"3","status":"OK"}
        """
        self.logger.debug(f"Variables:\n{self.variables}")

        self.logger.debug("Description? called")
        # Get description from config if available
        desc = self.config.get('RECORDER', {}).get('DESC', 'External Recorder')
        desc = dequote(self.replace_variables_in_string(desc))
        self.send_response(kwargs, {"status": "OK", "message": desc})

    def has_tuner(self, **kwargs) -> None:
        """Handle HasTuner? command."""
        """ Example response:
        {"command":"HasTuner","message":"Yes","serial":"2","status":"OK"}
        """
        self.logger.debug("HasTuner? called")
        msg = ("Yes" if self.config['TUNER']['COMMAND'] is not None and
               len(self.config['TUNER']['COMMAND']) > 0 else "No")
        self.send_response(kwargs, {"message":f"{msg}","status":"OK"})


    def has_picture_attributes(self, **kwargs) -> None:
        """Handle HasPictureAttributes? command."""
        """ Example response:
        {"command":"HasPictureAttributes","message":"No","serial":"4","status":"OK"}
        """
        self.logger.debug("HasPictureAttributes? called")
        self.send_response(kwargs, {"status": "OK", "message": "No"})

    def flow_control(self, **kwargs) -> None:
        """Handle FlowControl? command."""
        """ Example response:
        {"command":"FlowControl?","message":"XON/XOFF","serial":"5","status":"OK"}
        """
        self.logger.debug("FlowControl? called")
        self.send_response(kwargs, {"status": "OK", "message": "XON/XOFF"})

    def block_size_handler(self, **kwargs) -> None:
        """Handle BlockSize command."""
        """ Example response:
        {"command":"BlockSize","message":"Blocksize 3080192","serial":"6","status":"OK"}
        """
        self.logger.debug("BlockSize called")
        self.send_response(kwargs, {"status": "OK", "message": f"Blocksize {self.block_size}"})

    def lock_timeout(self, **kwargs) -> None:
        """Handle LockTimeout? command."""
        """ Example response:
        {"command":"LockTimeout","message":"30000","serial":"8","status":"OK"}
        """

        self.logger.debug("LockTimeout? called")
        # Get timeout from config if available
        timeout = self.config.get('TUNER', {}).get('TIMEOUT', '30000')
        timeout = self.channel_override("TIMEOUT", timeout)

        self.send_response(kwargs, {"status": "OK", "message": timeout})

    def signal_strength(self, **kwargs) -> None:
        """Handle SignalStrengthPercent? command."""

        self.logger.debug("SignalStrengthPercent? called")
        if self.tune_status == "InProgress":
            msg = "25"
        elif self.tune_status == "Idle":
            msg = "0";
        else:
            msg = "100";
        self.send_response(kwargs, {"status": "OK", "message": msg})

    def has_lock(self, **kwargs) -> None:
        """Handle SignalStrengthPercent? command."""

        self.logger.debug("SignalStrengthPercent? called")
        msg = "Yes" if self.tune_status == "Tuned" else "No"
        self.send_response(kwargs, {"status": "OK", "message": msg})

    def tune_channel(self, **kwargs) -> None:
        """Handle TuneChannel command."""
        """ Example response:
        {"command":"TuneChannel","message":"InProgress `/usr/local/bin/rOKu-control --device roku9 --link \"[aivod://B0D6ZCZQVH]\" --prologue amazon`","serial":"9","status":"OK"}
        """
        self.logger.debug("TuneChannel called")
        self.logger.debug(f"Received TuneChannel message: {kwargs}")

        if self.tune_status != "Idle":
            self.tune_process.kill()
            self.tune_process.wait()
            self.tune_status = "Idle";

        # Update variables with message data
        self.process_variables_in_message(kwargs)
        # Get the tune command from config
        tune_cmd = self.config.get('TUNER', {}).get('COMMAND', '')
        tune_cmd = self.channel_override("TUNE", tune_cmd)

        keys_to_keep = ['command', 'serial']
        kwargs = {k: kwargs[k] for k in keys_to_keep if k in kwargs}

        if not tune_cmd:
            self.send_response(kwargs,
                               {
                "status": "error",
                "message": "No tune command provided"
            })
            return

        self.tune_status = "InProgress"
        self.tune_process = self._execute_command(tune_cmd, "tune",
                                                  background=True)

        # Start thread to monitor completion
        self.tune_thread = threading.Thread(target=self._monitor_tune_completion)
        self.tune_thread.daemon = True
        self.tune_thread.start()

        self.send_response(kwargs, {
            "status": "OK",
            "message": f"InProgress `{tune_cmd}`"
        })

    def _monitor_tune_completion(self) -> None:
        """Monitor tune command completion."""
        if not self.tune_process:
            return

        try:
            self.tune_process.wait()
            self.tune_status = "Tuned"
            self.logger.info("Tune command completed successfully")
        except Exception as e:
            self.logger.error(f"Error monitoring tune completion: {e}")
            self.tune_status = "Error"

    def tune_status_handler(self, **kwargs) -> None:
        """Handle TuneStatus? command."""
        self.logger.debug("TuneStatus? called")

        if self.tune_status == "InProgress":
            message = "InProgress"
        elif self.tune_status == "Tuned":
            message = "Tuned"
        else:
            message = "Idle"

        self.send_response(kwargs,
                           {"status": "OK",
                            "message": message}
                           )

    def close_recorder(self, **kwargs) -> None:
        """Handle CloseRecorder command."""
        """ Example response:
        {"command":"CloseRecorder","message":"Terminating","serial":"9","status":"OK"}
        """
        self.streaming = False
        self.logger.debug("CloseRecorder called")
        self.send_response(kwargs, {"status": "OK", "message": "Terminating"})
        sys.exit(0)

    def is_open(self, **kwargs) -> None:
        self.logger.debug("IsOpen? called")
        msg = "Open" if self.stream_process else "No"
        self.send_response(kwargs, {"status": "OK", "message": msg})

    def start_streaming(self, **kwargs) -> None:
        """Handle StartStreaming command."""
        """ Example response:
        {"command":"StartStreaming","message":"Streaming Started","serial":"11","status":"OK"}
        """
        self.logger.debug("StartStreaming called")
        self.logger.debug(f"Variables before command processing: {self.variables}")

        if self.streaming:
            self.logger.warning("Already streaming")
            self.send_response(kwargs, {"status": "error", "message": "Already streaming"})
            return

        self.logger.warning(f"RECORDER/command: {self.config['RECORDER']}")

        # Replace variables in the command
        if self.config["RECORDER"]["COMMAND"] is None:
            self.logger.warning("No [RECORDER/command] specified")
            self.send_response(kwargs, {"status": "error",
                               "message": "No [RECORDER/command] specified"})
            return

        self.command = dequote(self.replace_variables_in_string(self.config["RECORDER"]["COMMAND"]))
        self.logger.info(f"Final streaming command after variable replacement: {self.command}")

        self.streaming = True
        self.stream_thread = threading.Thread(target=self._stream_loop)
        self.stream_thread.daemon = True
        self.stream_thread.start()

        self.send_response(kwargs, {"status": "OK", "message": "Streaming Started"})

    def stop_streaming(self, **kwargs) -> None:
        """Handle StopStreaming command."""
        """ Example response:
        {"command":"StopStreaming","message":"Streaming Stopped","serial":"12","status":"OK"}
        """
        self.logger.debug("StopStreaming called")

        if not self.streaming:
            self.logger.warning("Not currently streaming")
            self.send_response(kwargs, {"status": "error", "message": "Not streaming"})
            return

        self.streaming = False

        if self.stream_process and self.stream_process.poll() is None:
            self.stream_process.terminate()
            try:
                self.stream_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.stream_process.kill()
                self.stream_process.wait()

        self.send_response(kwargs, {"status": "OK", "message": "Streaming Stopped"})

    def xon(self, **kwargs) -> None:
        """Handle XON command."""
        """ Example response:
        {"command":"XON","message":"Started Streaming","serial":"12","status":"OK"}
        """
        self.logger.debug("XON called")
        self.send_response(kwargs, {"status": "OK",
                                    "message": "Started Streaming"})
        self.variables['XONCOUNT'] += 1

        xon_cmd = self.config.get('XON', {}).get('COMMAND', '')
        xon_cmd = self.channel_override("XON", xon_cmd)
        if xon_cmd:
            self.xon_process = self._execute_command(xon_cmd, "XON",
                                                     background=False)

        self.xon_state = True

    def xoff(self, **kwargs) -> None:
        """Handle XOFF command."""
        """ Example response:
        {"command":"XOFF","message":"Stopped Streaming","serial":"13","status":"OK"}
        """
        self.logger.debug("XOFF called")
        self.xon_state = False
        self.send_response(kwargs, {"status": "OK",
                                    "message": "Stopped Streaming"})

    def _read_stderr(self) -> None:
        """Read stderr from the stream subprocess and send status messages."""
        while self.streaming and self.stream_process:
            try:
                # Read stderr line by line
                line = self.stream_process.stderr.readline()
                if not line:
                    # Check if process is still running
                    if self.stream_process.poll() is not None:
                        break
                    continue

                # Decode line to string
                line_str = line.decode('utf-8', errors='ignore').strip()
                if not line_str:
                    continue

                # Process stderr message
                self._process_stderr_line(line_str)

            except Exception as e:
                self.logger.error(f"Error reading stderr: {e}")
                break

    def _process_stderr_line(self, line: str) -> None:
        """Process a stderr line and send status message."""
        try:
            # Determine status based on message prefix (case insensitive)
            status = "INFO"
            message = line

            if line.lower().startswith("err"):
                status = "ERR"
                message = line[3:].strip()
            elif line.lower().startswith("warn"):
                status = "WARN"
                message = line[4:].strip()
            elif line.lower().startswith("damage"):
                status = "DAMAGED"
                message = line[6:].strip()
            elif line.lower().startswith("info"):
                status = "INFO"
                message = line[4:].strip()

            # Send status message
            response = {
                "message": message,
                "status": status
            }
            self.send_response({"command": "status"}, response)

        except Exception as e:
            self.logger.error(f"Error processing stderr line '{line}': {e}")

    def _stream_loop(self) -> None:
        """Main streaming loop."""
        if not self.command:
            self.logger.error("No command specified for streaming")
            return

        self.logger.info(f"Starting streaming with command: {self.command}")

        try:
            # Split command into args for subprocess
            cmd_args = shlex.split(self.command)
            self.logger.info(f"Splitting command into args: {cmd_args}")
            self.stream_process = subprocess.Popen(
                cmd_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,  # Unbuffered for raw read1 performance
                universal_newlines=False  # Ensure binary mode for stdout
            )

            # Start stderr reading thread
            self.stderr_thread = threading.Thread(target=self._read_stderr)
            self.stderr_thread.daemon = True
            self.stderr_thread.start()

            self.logger.info(f"Reading from sub process")

            # Wait until initial data is available
            sel = selectors.DefaultSelector()
            sel.register(self.stream_process.stdout, selectors.EVENT_READ)
            while True:
                # Check for events (0.1s timeout so loop remains responsive)
                events = sel.select(timeout=0.1)
                if events or self.stream_process.poll() is not None:
                    break

            sel.unregister(self.stream_process.stdout)
            sel.close()

            if self.ondatastart_done:
                self.logger.info("Already ran OnDataStart")
            else:
                # Execute ondatastart command if available
                data_cmd = self.config.get('TUNER', {}).get('ONDATASTART', "")
                data_cmd = self.channel_override("ONSTART", data_cmd)
                if data_cmd:
                    self.ondatastart_process = self._execute_command(data_cmd,
                                                                "ONDATA",
                                                                background=True)

            while self.streaming:
                # Read up to block_size at a time
                chunk = self.stream_process.stdout.read(self.block_size)

                if not chunk:
                    st = self.stream_process.poll()
                    if st is not None:
                        if st == 0:
                            self.logger.info("Process finished normally")
                        elif st < 0:
                            self.logger.warn("Process killed")
                        else:
                            self.logger.warn("Process failed")
                        break
                    continue

                # Only write to stdout if in XON state
                if self.xon_state:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.flush()

        except Exception as e:
            self.logger.exception(f"Error during streaming: {e}")
            self.streaming = False
        finally:
            if self.stream_process:
                self.stream_process.terminate()
                try:
                    self.stream_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.stream_process.kill()
                    self.stream_process.wait()
            self.logger.info("Streaming stopped")

    def _execute_command(self, command: str, desc: str, background: bool) -> None:
        if not command:
            return None

        command = dequote(self.replace_variables_in_string(command))

        # Check if command should run in background (has trailing &)
        ampersand = command.endswith(' &')
        if ampersand:
            command = command[:-2].strip()
        background |= ampersand

        if background:
            # Execute in background
            try:
                cmd_args = shlex.split(command)
                process = subprocess.Popen(
                    cmd_args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                self.logger.info(f"Started background {desc} command: {command}")
            except Exception as e:
                self.logger.error(f"Error starting background {desc} command: {e}")
                return None
        else:
            # Execute in foreground
            try:
                cmd_args = shlex.split(command)
                process = subprocess.Popen(
                    cmd_args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                process.wait()
                self.logger.info(f"Completed {desc} command: {command}")
            except Exception as e:
                self.logger.error(f"Error executing {desc} command: {e}")
                return None

        return process

    def replace_variables_in_string(self, value: str) -> str:
        if not value:
            return value

        """
        self.logger.info(f"Replacing in {value}")
        for key,data in self.variables.items():
            self.logger.info(f"{key} : {data}")
        """

        # First, handle the special blocks that need to be removed if any variables are unknown
        def replace_special_block(match):
            block_content = match.group(1)
            # Find all variables in the block
            variables_in_block = re.findall(r'\$\{([^}]+)\}', block_content)

            # Check if all variables in the block are known and not empty
            all_known_and_non_empty = True
            for var_name in variables_in_block:
                # Case insensitive lookup
                found = False
                for key, val in self.variables.items():
                    if key.lower() == var_name.lower() and val:
                        found = True
                        break
                if not found:
                    all_known_and_non_empty = False
                    break

            # If all variables are known and non-empty, return the block content
            # Otherwise, return empty string (remove the block)
            return block_content if all_known_and_non_empty else ""

        # Process special blocks first
        processed_value = re.sub(r'\[\{([^}]*?\$\{[^}]+\}[^}]*?)\}\]', replace_special_block, value, flags=re.DOTALL)

        # Now replace regular variables
        def replace_variable(match):
            var_name = match.group(1)
            # Case insensitive lookup
            for key, val in self.variables.items():
                if key.lower() == var_name.lower():
                    return dequote(str(val))
            # If variable not found, leave it alone
            return match.group(0)

        # Replace all variables in the processed string
        result = re.sub(r'\$\{([^}]+)\}', replace_variable, processed_value)

        return result

    def process_variables_in_message(self, message: Dict[str, Any]) -> None:
        """Process variables from the TuneChannel message."""
        self.logger.debug(f"Processing message variables. Message: {message}")
        self.logger.debug(f"Initial variables: {self.variables}")

        # Update with message data
        for key, value in message.items():
            if key in ['command', 'serial']:
                continue
            # Convert key to uppercase for consistent naming
            self.variables[key.upper()] = str(value)
            self.logger.debug(f"Added message variable {key.upper()} = {value}")

        for key, value in self.variables.items():
            self.variables[key] = self.replace_variables_in_string(value)

        self.logger.debug(f"Final variables after message processing: {self.variables}")


def dequote(s):
    if len(s) >= 2 and s[0] == s[-1] and s.startswith(("'","\"")):
        return s[1:-1]
    return s
