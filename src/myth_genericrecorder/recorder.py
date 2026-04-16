#!/usr/bin/env python3
"""Recorder module for MythTV ExternalRecorder program."""
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

from importlib.metadata import version
__version__ = version("myth-genericrecorder")

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
        self.channel_iter        = None
        self.processes           = {}

        # Configuration
        self.config    = config or {}
        self.variables = variables or {}

        if command is not None and len(command) > 0:
            self.config['RECORDER']['command'] = command
        if tune_command is not None and len(tune_command) > 0:
            self.config['TUNER']['command'] = tune_command

        # Keep track of how many times XON is called
        self.variables['XONCOUNT'] = 0
        # XON/XOFF state
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
            "XOFF"                 : self.xoff,
            "LoadChannels"         : self.load_channels,
            "FirstChannel"         : self.first_channel,
            "NextChannel"          : self.next_channel
        }
        self.logger.debug(f"Recorder.__init__ called with config={config}")  # Debug line

    def __del__(self):
        # Terminate when the object is destroyed
        if self.streaming:
            self.stop_streaming()

        # Kill any subprocess that is still running
        for key,proc in self.processes.items():
            if proc['process'] and proc['process'].poll() is None:
                self.logger.info(f"Force terminating {key} process {proc['process'].pid}")
                try:
                    proc['process'].terminate()
                    proc['process'].wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc['process'].kill()
                    proc['process'].wait()

    # Handle Json message from mythbackend
    def process_command(self, message: Dict[str, Any]) -> None:
        """Process a command from stdin."""
        command = message.get("command", "")
        if not command:
            self.logger.warning("No command in message")
            return

        if command not in self.handlers:
            self.logger.warning(f"Unknown command: {command}")
            self.send_response(message, {"status": "error", "message": "Unknown command"})
            return

        try:
            # Execute the handler
            self.logger.debug(f"Executing handler for command: {command}")
            handler = self.handlers[command]
            handler(**message)
        except Exception as e:
            self.logger.exception(f"Error executing command {command}: {e}")
            self.send_response(message, {"status": "error", "message": str(e)})

    # Respond to mythbackend.
    def send_response(self, original_message: Dict[str, Any],
                      response_data: Dict[str, Any], level: int = logging.INFO) -> None:
        """Send a JSON response to stderr where mythbackend will read it."""

        # Echo original command and serial.
        keep_keys = ["command", "serial"]
        response = {key: original_message[key] for key in keep_keys
                    if key in original_message}
        response.update(response_data)

        # Write to stderr
        try:
            json.dump(response, sys.stderr)
            sys.stderr.write("\n")
            sys.stderr.flush()
            self.logger.log(level, f"Response: {response}")
        except Exception as e:
            self.logger.error(f"Failed to send response: {e}")

    def channel_override(self, variable : str, default : str):
        """Check [TUNER/channel] ini file for commands and values
        which override those in the main config file."""

        # Get the channum from variables
        channum = self.variables.get('CHANNUM', None)
        self.logger.debug(f"Looking for {variable} for channum {channum}")
        if not channum:
            self.logger.debug(f"Could not determine channum for {variable}")
            return default

        if 'CHANNELS' in self.config:
            self.logger.debug(f"Looking for {channum} in "
                              f"{self.config['TUNER']['CHANNELS']}")
            channel_config = self.config['CHANNELS'].get(channum, {})
            if variable in channel_config:
                value = channel_config[variable]
                self.logger.info(f"Using channel[{channum}] specific {variable}={value}")
                return value
        return default

    def api_version(self, **kwargs) -> None:
        """Handle APIVersion command."""
        self.logger.debug("APIVersion called")
        self.send_response(kwargs, {"status": "OK"})
#                                    "message":f"{kwargs['value']}"})

    def version(self, **kwargs) -> None:
        """Handle Version? command."""
        """ Example query:
        {"command":"Version?","serial":2}
        """
        """ Example response:
        {"command":"APIVersion","message":"3","serial":"1","status":"OK"}
        """
        self.logger.debug("Version? called")
        self.send_response(kwargs, {"status": "OK", "message": __version__})

    def description(self, **kwargs) -> None:
        """Handle Description? command."""
        """ Example query:
        {"command":"Description?","serial":3}
        """
        """ Example response:
        {"command":"Description?","message":"mag-1-2-3","serial":"3","status":"OK"}
        """
        self.logger.debug(f"Variables:\n{self.variables}")

        self.logger.debug("Description? called")
        # Get description from config if available
        desc = self.config.get('RECORDER', {}).get('DESC', 'External Recorder')
        desc = dequote(replace_variables_in_string(desc, self.variables))
        self.send_response(kwargs, {"status": "OK", "message": desc})

    def has_tuner(self, **kwargs) -> None:
        """Handle HasTuner? command."""
        """ Example query:
        {"command":"HasTuner?","serial":4}
        """
        """ Example response:
        {"command":"HasTuner","message":"Yes","serial":"2","status":"OK"}
        """
        self.logger.debug("HasTuner? called")
        msg = ("Yes" if self.config['TUNER']['COMMAND'] is not None and
               len(self.config['TUNER']['COMMAND']) > 0 else "No")
        self.send_response(kwargs, {"message":f"{msg}","status":"OK"})


    def has_picture_attributes(self, **kwargs) -> None:
        """Handle HasPictureAttributes? command."""
        """ Example query:
        {"command":"HasPictureAttributes?","serial":5}
        """
        """ Example response:
        {"command":"HasPictureAttributes","message":"No","serial":"4","status":"OK"}
        """
        self.logger.debug("HasPictureAttributes? called")
        self.send_response(kwargs, {"status": "OK", "message": "No"})

    def flow_control(self, **kwargs) -> None:
        """Handle FlowControl? command."""
        """ Example query:
        {"command":"FlowControl?","serial":6}
        """
        """ Example response:
        {"command":"FlowControl?","message":"XON/XOFF","serial":"5","status":"OK"}
        """
        self.logger.debug("FlowControl? called")
        self.send_response(kwargs, {"status": "OK", "message": "XON/XOFF"})

    def block_size_handler(self, **kwargs) -> None:
        """Handle BlockSize command."""
        """ Example query:
        {"command":"BlockSize","serial":7,"value":"3080192"}
        """
        """ Example response:
        {"command":"BlockSize","message":"Blocksize 3080192","serial":"6","status":"OK"}
        """
        self.logger.debug("BlockSize called")
        self.send_response(kwargs, {"status": "OK", "message": f"Blocksize {self.block_size}"})

    def lock_timeout(self, **kwargs) -> None:
        """Handle LockTimeout? command."""
        """ Example query:
        {"command":"LockTimeout?","serial":8}
        """
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
        """ Example query:
        {"command":"SignalStrengthPercent?","serial":11}
        """
        """ Example response:
        {"command":"SignalStrengthPercent?","serial":11}
        """

        self.logger.debug("SignalStrengthPercent? called")
        if ('Tune' not in self.processes or
            # Tuner command has not been run yet.
            self.processes['Tune'] is None or
            self.processes['Tune']['process'] is None):
            message = "0"
        else:
            status = self.processes['Tune']['status']
            if status == "InProgress":
                # Tuner command is still running.
                message = "25"
            elif status == "Finished":
                # Tuner command has finished
                message = "100"
            else:
                # Catchall. Should never get here.
                message = "0"
        self.send_response(kwargs, {"status": "OK", "message": message})

    def has_lock(self, **kwargs) -> None:
        """Handle HasLock? command."""
        """ Example query:
        {"command":"HasLock?","serial":12}
        """
        """ Example response:
        {"command":"HasLock?","serial":12}
        """

        self.logger.debug("SignalStrengthPercent? called")
        if ('Tune' not in self.processes or
            # Tuner command has not been run yet.
            self.processes['Tune'] is None or
            self.processes['Tune']['process'] is None):
            message = "No"
        else:
            # If Tuner has finished, then we are "locked"
            status = self.processes['Tune']['status']
            message = "Yes" if status == "Finished" else "No"
        self.send_response(kwargs, {"status": "OK", "message": message})

    def tune_channel(self, **kwargs) -> None:
        """Handle TuneChannel command."""
        """ Example query:
        {"atsc_major":0,"atsc_minor":0,"callsign":"CALLSIGN","chanid":100,"channum":"100","command":"TuneChannel","description":"","duration":1923,"freqid":"","inputid":16,"mplexid":0,"name":"Station Name","programid":"","recordid":4165,"serial":9,"seriesid":"","sourceid":4,"subtitle":"Subtitle","title":"Title","value":"96"}
        """
        """ Example response:
        {"command":"TuneChannel","message":"InProgress `/usr/local/bin/roku-control --channum 318"`","serial":"9","status":"OK"}
        """
        self.logger.debug("TuneChannel called")
        self.logger.debug(f"Received TuneChannel message: {kwargs}")

        # Update variables with message data
        if self.logger.isEnabledFor(logging.DEBUG):
            for key, value in kwargs.items():
                self.logger.debug(f"{key}: {value}")

        self.process_variables_in_message(kwargs)

        # Get the tune command from config
        tune_cmd = self.config.get('TUNER', {}).get('COMMAND', '')
        tune_cmd = self.channel_override("TUNE", tune_cmd)

        """ Already handled in send_response(), right?
        keys_to_keep = ['command', 'serial']
        kwargs = {k: kwargs[k] for k in keys_to_keep if k in kwargs}
        """

        if not tune_cmd:
            self.send_response(kwargs,
                               {"status": "error",
                                "message": "No tune command provided"
                                })
            return

        tune_cmd = self._execute_command(tune_cmd, "Tune",
                                         background=True)

        self.send_response(kwargs, {
            "status": "OK",
            "message": f"InProgress `{tune_cmd}`"
        })

    def close_recorder(self, **kwargs) -> None:
        """Handle CloseRecorder command."""
        """ Example query:
        {"command":"CloseRecorder","serial":18}
        """
        """ Example response:
        {"command":"CloseRecorder","message":"Terminating","serial":"9","status":"OK"}
        """
        self.streaming = False
        self.logger.debug("CloseRecorder called")
        self.send_response(kwargs, {"status": "OK", "message": "Terminating"})
        sys.exit(0)

    def is_open(self, **kwargs) -> None:
        """Handle IsOpen? command."""
        """ Example query:
        {"command":"IsOpen?","serial":13}
        """
        """ Example response:
        {"command":"IsOpen?","message":"Not Open yet","serial":"13","status":"WARN"}
        """

        self.logger.debug("IsOpen? called")
        msg = "Open" if self.stream_process else "No"
        self.send_response(kwargs, {"status": "OK", "message": msg})

    def start_streaming(self, **kwargs) -> None:
        """Handle StartStreaming command."""
        """ Example query:
        {"command":"StartStreaming","serial":14}
        """
        """ Example response:
        {"command":"StartStreaming","message":"Streaming Started","serial":"11","status":"OK"}
        """
        self.logger.debug("StartStreaming called")
        self.logger.debug(f"Variables before command processing: {self.variables}")

        if self.streaming:
            self.logger.warning("Already streaming")
            self.send_response(kwargs, {"status": "error", "message": "Already streaming"})
            return

        # Replace variables in the command
        if self.config["RECORDER"]["COMMAND"] is None:
            self.logger.warning("No [RECORDER/command] specified")
            self.send_response(kwargs, {"status": "error",
                               "message": "No [RECORDER/command] specified"})
            return

        self.command = dequote(replace_variables_in_string(self.config["RECORDER"]["COMMAND"],
                                                                self.variables))
        self.streaming = True
        self.stream_thread = threading.Thread(target=self._stream_loop)
        self.stream_thread.daemon = True
        self.stream_thread.start()

        self.send_response(kwargs, {"status": "OK", "message": "Streaming Started"})

    def stop_streaming(self, **kwargs) -> None:
        """Handle StopStreaming command."""
        """ Example query:
        {"command":"StopStreaming","serial":17}
        """
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
                self.stream_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.stream_process.kill()
                self.stream_process.wait()

        self.send_response(kwargs, {"status": "OK", "message": "Streaming Stopped"})

    def xon(self, **kwargs) -> None:
        """Handle XON command."""
        """When mythbackend is actually ready to receive data it will issue XON"""

        """ Example query:
        {"command":"XON","serial":15}
        """
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
            self._execute_command(xon_cmd, "XON", background=False)

        self.xon_state = True

    def xoff(self, **kwargs) -> None:
        """Handle XOFF command."""
        """When done streaming, or if data is too fast, mythbackend will issue XOFF"""

        """ Example query:
        {"command":"XOFF","serial":16}
        """
        """ Example response:
        {"command":"XOFF","message":"Stopped Streaming","serial":"13","status":"OK"}
        """
        self.logger.debug("XOFF called")
        self.xon_state = False
        self.send_response(kwargs, {"status": "OK",
                                    "message": "Stopped Streaming"})

    def load_channels(self, **kwargs) -> None:
        """Handle LoadChannels command."""
        """ Example query:
        {"command":"LoadChannels","serial":19}
        """
        """ Example response:
        {"command":"LoadChannels","message":"52","serial":"19","status":"OK"}
        """
        self.logger.debug("LoadChannels called")

        # Get channel count from CHANNELS section
        channel_count = 0
        if 'CHANNELS' in self.config:
            channel_count = len(self.config['CHANNELS'])

        for key,value in self.config['CHANNELS'].items():
            self.logger.info(f"{key} : {value}")

        self.send_response(kwargs, {"status": "OK", "message": str(channel_count)})

    def _channel_info(self, **kwargs):
        key = next(self.channel_iter, None)
        if key == None:
            return self.send_response(kwargs, {"status": "WARN", "message": "DONE"})

        channel_data = self.config['CHANNELS'][key]

        # Format response as comma-separated values
        # Use channel key as ChanNum, and other fields from channel data
        response_parts = [
            key,
            channel_data.get('NAME', ''),
            channel_data.get('CALLSIGN', ''),
            channel_data.get('XMLTVID', ''),
            channel_data.get('ICON', '')
        ]

        response = ','.join(response_parts)
        self.send_response(kwargs, {"status": "OK", "message": response})

    def first_channel(self, **kwargs) -> None:
        """Handle FirstChannel command."""
        """ Example query:
        {"command":"FirstChannel","serial":20}
        """
        """ Example response:
        {"command":"FirstChannel","message":"ChanNum,ChanName,Callsign,xmltvid,icon","serial":"20","status":"OK"}
        """
        self.logger.debug("FirstChannel called")

        if 'CHANNELS' not in self.config or not self.config['CHANNELS']:
            self.send_response(kwargs, {"status": "error", "message": "No channels available"})
            return

        # Set iterator to first channel
        self.channel_iter = iter(self.config['CHANNELS'])
        return self._channel_info(**kwargs)

    def next_channel(self, **kwargs) -> None:
        """Handle NextChannel command."""
        """ Example query:
        {"command":"NextChannel","serial":21}
        """
        """ Example response:
        {"command":"NextChannel","message":"ChanNum,ChanName,Callsign,xmltvid,icon","serial":"21","status":"OK"}
        """
        self.logger.debug("NextChannel called")

        if self.channel_iter == None:
            self.send_response(kwargs, {"status": "error", "message": "first_channel not called yet."})
            return

        return self._channel_info(**kwargs)

    def tune_status_handler(self, **kwargs) -> None:
        """Handle TuneStatus? command."""
        self.logger.debug("TuneStatus? called")

        if ('Tune' not in self.processes or
            # Tuner has not been called yet.
            self.processes['Tune'] is None or
            self.processes['Tune']['process'] is None):
            message = "Idle"
        else:
            status = self.processes['Tune']['status']
            if status == "InProgress":
                message = "InProgress"
            elif status == "Finished":
                message = "Tuned"
            else:
                message = "Idle"

        self.send_response(kwargs,
                           {"status": "OK",
                            "message": message},
                           logging.DEBUG
                           )

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
            """Data may be in 'logfile' format, so handle loglevels"""
            # Determine status based on message prefix (case insensitive)
            status = "INFO"
            message = line
            level = logging.INFO
            if line.lower().startswith("crit"):
                status = "CRIT"
                level = logging.CRITICAL
                prefix, sep, message = line.partition(':')
            if line.lower().startswith("err"):
                status = "ERR"
                level = logging.ERR
                prefix, sep, message = line.partition(':')
            elif line.lower().startswith("warn"):
                status = "WARN"
                level = logging.WARN
                prefix, sep, message = line.partition(':')
            elif line.lower().startswith("damage"):
                status = "DAMAGED"
                level = logging.WARN
                prefix, sep, message = line.partition(':')
            elif line.lower().startswith("info"):
                status = "INFO"
                level = logging.INFO
                prefix, sep, message = line.partition(':')
            elif line.lower().startswith("debug"):
                status = "DEBUG"
                level = logging.DEBUG
                prefix, sep, message = line.partition(':')
            elif line.lower().startswith("trace"):
                status = "TRACE"
                level = logging.TRACE
                prefix, sep, message = line.partition(':')

            if len(message) == 0:
                message = prefix

            # If the message has 'LEVEL: MSG', then we have stripped
            # the 'LEVEL:', but there still may be an unwanted space.
            if message[0] == ' ':
                message = message[1:]

            # Send status message
            response = {
                "message": message,
                "status": status
            }
            self.send_response({"command": "STATUS"}, response, level)

        except Exception as e:
            self.logger.exception(f"Error processing stderr line '{line}': {e}")

    def _stream_loop(self) -> None:
        """Main streaming loop."""
        """All 'stdout' data from the [RECORDER/command] application needs to be
           passed on to mythbackend. Data is raw so don't modify it"""

        if not self.command:
            self.logger.error("No command specified for streaming")
            return

        self.logger.info(f"Starting streaming: {self.command}")

        try:
            # Split command into args for subprocess
            cmd_args = shlex.split(self.command)
            self.logger.debug(f"Splitting command into args: {cmd_args}")
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
                    self._execute_command(data_cmd,
                                          "ONDATA",
                                          background=False)

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

                # Only write to stdout if in XON state, otherwise drop it.
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

    def _execute_command(self, command: str, desc: str,
                         background: bool) -> str:
        """General subprocess executer"""

        if not command:
            return None

        if desc in self.processes and self.processes[desc]['status'] != "Idle":
            """A process with this name is already running, kill it"""
            proc = self.processes[desc]
            self.logger.info(f"Force terminating {desc} process {proc['process'].pid}")
            try:
                proc['process'].terminate()
                proc['process'].wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc['process'].kill()
                proc['process'].wait()
            self.processes[desc]['status'] = "Idle"

        command = dequote(replace_variables_in_string(command, self.variables))

        # Check if command should run in background (has trailing &)
        ampersand = command.endswith('&')
        if ampersand:
            command = command[:-1].strip()
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
                self.logger.info(f"Started background {desc} command ({process.pid}): {command}")

                monitor_thread = threading.Thread(target=self._monitor_process, args=(desc,))
                self.processes[desc] = {'process' : process,
                                        'command' : command,
                                        'status' : 'InProgress',
                                        'monitor' : monitor_thread}
                monitor_thread.daemon = True
                monitor_thread.start()

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

                ret = process.returncode
                if ret == 0:
                    msg = f"`{command}` completed succesfully"
                    level = logging.INFO
                    status = "OK"
                else:
                    msg = f"`{command}` completed with code {ret}"
                    level = logging.WARN
                    status = "WARN"
                response = {"message" : msg, "status": status}
                self.send_response({"command": "STATUS"}, response, level)

            except Exception as e:
                self.logger.error(f"Error executing {desc} command: {e}")
                return None

        return command

    def _monitor_process(self, op) -> None:
        """Monitor subprocess completion."""
        if op not in self.processes:
            return
        if self.processes[op] is None:
            return
        if self.processes[op]['process'] is None:
            return

        command = self.processes[op]['command']
        try:
            self.processes[op]['process'].wait()
            self.processes[op]['status'] = "Finished"
            ret = self.processes[op]['process'].returncode
            if ret == 0:
                msg = f"`{command}` completed succesfully"
                level = logging.INFO
                status = "OK"
            else:
                msg = f"`{command}` completed with code {ret}"
                level = logging.WARN
                status = "WARN"
            response = {"message" : msg, "status": status}
            self.send_response({"command": "STATUS"}, response, level)
        except Exception as e:
            self.processes[op]["status"] = "Error"
            self.logger.exception(f"Error monitoring {op} completion: {e}")

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
            self.variables[key] = replace_variables_in_string(value, self.variables)

        self.logger.debug(f"Final variables after message processing: {self.variables}")


def dequote(s):
    if len(s) >= 2 and s[0] == s[-1] and s.startswith(("'","\"")):
        return s[1:-1]
    return s

def replace_variables_in_string(value: str, variables: dict) -> str:
    """Variables can come from the [VARIABLES] section in ini file, or from
       data passed as part of the TuneChannel json message"""

    if not value:
        return value

    logger = logging.getLogger(__name__)

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"Replacing in {value}")
        for key,data in variables.items():
            logger.debug(f"{key} : {data}")

    # First, handle the special blocks that need to be removed if any variables are unknown
    """ If '[{...}] is seen, remove that block of code unless ALL of the variables (e.g. ${VERSION})
        are known."""
    def replace_special_block(match):
        block_content = match.group(1)
        # Find all variables in the block
        variables_in_block = re.findall(r'\$\{([^}]+)\}', block_content)

        # Check if all variables in the block are known and not empty
        all_known_and_non_empty = True
        for var_name in variables_in_block:
            # Case insensitive lookup
            found = False
            for key, val in variables.items():
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
        for key, val in variables.items():
            if key.lower() == var_name.lower():
                return dequote(str(val))
        # If variable not found, leave it alone
        return match.group(0)

    # Replace all variables in the processed string
    """Variables are in 'shell' style of ${VARNAME}"""
    result = re.sub(r'\$\{([^}]+)\}', replace_variable, processed_value)

    return result
