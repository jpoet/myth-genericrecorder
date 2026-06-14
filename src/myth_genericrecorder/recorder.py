#!/usr/bin/env python3
"""Recorder module for MythTV ExternalRecorder program."""
import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Any, Optional, Callable
import shlex
import re
import selectors


from importlib.metadata import version
__version__ = version("myth-genericrecorder")

class Recorder:
    """Recorder class to handle streaming and command execution."""

    def __init__(self,
                 logger: Optional[logging.Logger] = None,
                 config: Optional[Dict] = None,
                 variables: Dict = None,
                 block_size: int = 1048576,
                 event_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None):
        """Initialize the recorder."""
        self.log                 = logging.getLogger(__name__)
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
        self.recorder_tunes      = False
        self.tune_thread         = None
        self.tune_status         = "Idle"  # Idle, InProgress, Tuned
        self.channel_iter        = None
        self.processes           = {}
        self.main_event          = event_callback

        # Configuration
        self.config    = config or {}
        self.variables = variables or {}

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
        self.log.trace(f"Recorder.__init__ called with config={config}")

    def __del__(self):
        # Terminate when the object is destroyed
        if self.streaming:
            self.stop_streaming()

        # Kill any subprocess that is still running
        for key,proc in self.processes.items():
            if proc['process'] and proc['process'].poll() is None:
                self.log.info(f"Force terminating {key} process "
                              f"{proc['process'].pid}")
                try:
                    proc['process'].terminate()
                    proc['process'].wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc['process'].kill()
                    proc['process'].wait()


    def handle_touch_error(self, exit_code: int, error_msg: str) -> None:
        """Touch class calls this when a command fails and
        DAMAGED_ON_FAILURE is true
        """
        level = logging.WARN
        response = {
            "message": error_msg,
            "status": "DAMAGED"
        }
        self.send_response({"command": "STATUS"}, response, level)


    def getVariables(self):
        """ Allow Touch class access to variables for commands. """
        return self.variables

    # Handle Json message from mythbackend
    def process_command(self, message: Dict[str, Any]) -> None:
        """Process a command from stdin."""
        command = message.get("command", "")
        if not command:
            self.log.warning("No command in message")
            return

        if command not in self.handlers:
            self.log.warning(f"Unknown command: {command}")
            self.send_response(message,
                               {"status": "error",
                                "message": "Unknown command"})
            return

        try:
            # Execute the handler
            self.log.debug(f"Executing handler for command: {command}")
            handler = self.handlers[command]
            handler(**message)
        except Exception as e:
            self.log.exception(f"Error executing command {command}: {e}")
            self.send_response(message, {"status": "error", "message": str(e)})

    # Respond to mythbackend.
    def send_response(self, original_message: Dict[str, Any],
                      response_data: Dict[str, Any],
                      level: int = logging.INFO) -> None:
        """Send a JSON response to stderr where mythbackend will read it."""

        # Build the response object
        keep_keys = ["command", "serial", "value"]
        response = {key: original_message[key] for key in keep_keys
                    if key in original_message}
        response.update(response_data)

        # Can be called from different threads
        with self.stderr_lock:
            try:
                json.dump(response, sys.stderr)
                sys.stderr.write("\n")
                sys.stderr.flush()

                if response['command'] == 'STATUS':
                    self.log.log(level, f"> {response['message']}")
                else:
                    if 'message' in response:
                        self.log.log(level,
                                     f"'{response['command']}' → "
                                     f"'{response['message']}'")
                    elif 'value' in response:
                        self.log.log(level,
                                     f"'{response['command']}' ← "
                                     f"'{response['value']}'")
                    else:
                        self.log.log(level,
                                     f"> '{response['command']}'")
            except Exception as e:
                self.log.exception(f"Failed to send response: {e}\n{response}")


    def channel_override(self, variable : str, default : str):
        """Check [TUNER/channel] ini file for commands and values
        which override those in the main config file.
        """

        # Get the channum from variables
        channum = self.variables.get('CHANNUM', None)
        self.log.debug(f"Looking for {variable} for channum {channum}")
        if not channum:
            self.log.debug(f"Could not determine channum for {variable}")
            return default

        if 'CHANNELS' in self.config:
            self.log.debug(f"Looking for {channum} in "
                              f"{self.config['TUNER']['CHANNELS']}")
            channel_config = self.config['CHANNELS'].get(channum, {})
            if variable in channel_config:
                value = channel_config[variable]
                self.log.info(f"Using channel[{channum}] specific "
                              f"{variable}={value}")
                return value
        return default

    def api_version(self, **kwargs) -> None:
        """Handle APIVersion command."""
        """ Example query:
        {"command":"APIVersion","serial":1,"value":"3"}
        """
        """ Example response:
        {"command": "APIVersion", "serial": 1, "value": "3", "status": "OK"}
        """
        self.log.debug("APIVersion called")
        self.send_response(kwargs, {"status": "OK"})

    def version(self, **kwargs) -> None:
        """Handle Version? command."""
        """ Example query:
        {"command":"Version?","serial":2}
        """
        """ Example response:
        {"command":"APIVersion","message":"3","serial":"1","status":"OK"}
        """
        self.log.debug("Version? called")
        self.send_response(kwargs, {"status": "OK", "message": __version__})

    def description(self, **kwargs) -> None:
        """Handle Description? command."""
        """ Example query:
        {"command":"Description?","serial":3}
        """
        """ Example response:
        {"command":"Description?","message":"mag-1-2-3","serial":"3","status":"OK"}
        """
        self.log.debug(f"Variables:\n{self.variables}")

        self.log.debug("Description? called")
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
        self.log.debug("HasTuner? called")
        if 'TUNER' not in self.config:
            self.log.warn(f"Failed to find [TUNER] section in {self.config}")
            self.recorder_tunes = False
            msg = "No"
        elif ('COMMAND' in self.config['TUNER'] and
             len(self.config['TUNER']['COMMAND']) > 0):
            self.recorder_tunes = False
            msg = "Yes"
        elif ('CHANNELS' in self.config['TUNER'] and
              len(self.config['TUNER']['CHANNELS']) > 0):
            self.recorder_tunes = True
            msg = "Yes"
        else:
            msg = "No"
        self.send_response(kwargs, {"message":f"{msg}","status":"OK"})


    def has_picture_attributes(self, **kwargs) -> None:
        """Handle HasPictureAttributes? command."""
        """ Example query:
        {"command":"HasPictureAttributes?","serial":5}
        """
        """ Example response:
        {"command":"HasPictureAttributes","message":"No","serial":"4","status":"OK"}
        """
        self.log.debug("HasPictureAttributes? called")
        self.send_response(kwargs, {"status": "OK", "message": "No"})

    def flow_control(self, **kwargs) -> None:
        """Handle FlowControl? command."""
        """ Example query:
        {"command":"FlowControl?","serial":6}
        """
        """ Example response:
        {"command":"FlowControl?","message":"XON/XOFF","serial":"5","status":"OK"}
        """
        self.log.debug("FlowControl? called")
        self.send_response(kwargs, {"status": "OK", "message": "XON/XOFF"})

    def block_size_handler(self, **kwargs) -> None:
        """Handle BlockSize command."""
        """ Example query:
        {"command":"BlockSize","serial":7,"value":"3080192"}
        """
        """ Example response:
        {"command":"BlockSize","message":"Blocksize 3080192","serial":"6","status":"OK"}
        """
        self.log.debug("BlockSize called")
        self.send_response(kwargs, {"status": "OK", "message": f"Blocksize {self.block_size}"})

    def lock_timeout(self, **kwargs) -> None:
        """Handle LockTimeout? command."""
        """ Example query:
        {"command":"LockTimeout?","serial":8}
        """
        """ Example response:
        {"command":"LockTimeout","message":"30000","serial":"8","status":"OK"}
        """

        self.log.debug("LockTimeout? called")
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

        self.log.debug("SignalStrengthPercent? called")
        if self.recorder_tunes:
            message = "100"
        elif ('Tune' not in self.processes or
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

        self.log.debug("SignalStrengthPercent? called")
        if self.recorder_tunes:
            message = "Yes"
        elif ('Tune' not in self.processes or
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
        self.log.debug("TuneChannel called")
        self.log.debug(f"Received TuneChannel message: {kwargs}")

        # Update variables with message data
        if self.log.isEnabledFor(logging.DEBUG):
            for key, value in kwargs.items():
                self.log.debug(f"{key}: {value}")

        self.process_variables_in_message(kwargs)

        if self.recorder_tunes:
            self.send_response(kwargs,
                               {"status": "OK",
                                "message": "Tuned"
                                })
            return

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
        self.log.debug("CloseRecorder called")
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

        self.log.debug("IsOpen? called")
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
        self.log.debug("StartStreaming called")
        self.log.debug(f"Variables before command processing: {self.variables}")

        if self.streaming:
            self.log.warning("Already streaming")
            self.send_response(kwargs, {"status": "error", "message": "Already streaming"})
            return

        # Replace variables in the command
        self.variables['URL'] = self.channel_override("URL", "")

        if self.config["RECORDER"]["COMMAND"] is None:
            self.log.warning("No [RECORDER/command] specified")
            self.send_response(kwargs, {"status": "error",
                               "message": "No [RECORDER/command] specified"})
            return

        self.command = dequote(replace_variables_in_string
                               (self.config["RECORDER"]["COMMAND"],
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
        self.log.debug("StopStreaming called")

        if not self.streaming:
            self.log.warning("Not currently streaming")
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
        self.log.debug("XON called")
        self.send_response(kwargs, {"status": "OK",
                                    "message": "Started Streaming"})
        self.variables['XONCOUNT'] += 1

        xon_cmd = self.config.get('XON', {}).get('COMMAND', '')
        xon_cmd = self.channel_override("XON", xon_cmd)
        if xon_cmd and self.variables['XONCOUNT'] == 2:
            self._execute_command(xon_cmd, "XON", background=False)

        self.xon_state = True

        recstart_cmd = self.config.get('RECSTART', {}).get('COMMAND', '')
        recstart_cmd = self.channel_override("RECSTART", recstart_cmd)
        if recstart_cmd:
            self._execute_command(recstart_cmd, "RECSTART", background=False)

        self.signal_event("RECSTART")

    def xoff(self, **kwargs) -> None:
        """Handle XOFF command."""
        """When done streaming, or if data is too fast, mythbackend will issue XOFF"""

        """ Example query:
        {"command":"XOFF","serial":16}
        """
        """ Example response:
        {"command":"XOFF","message":"Stopped Streaming","serial":"13","status":"OK"}
        """
        self.log.debug("XOFF called")
        self.xon_state = False
        self.send_response(kwargs, {"status": "OK",
                                    "message": "Stopped Streaming"})

        if self.variables['XONCOUNT'] < 2:
            return

        recstop_cmd = self.config.get('RECSTOP', {}).get('COMMAND', '')
        recstop_cmd = self.channel_override("RECSTOP", recstop_cmd)
        if recstop_cmd:
            self._execute_command(recstop_cmd, "RECSTOP", background=False)


    def load_channels(self, **kwargs) -> None:
        """Handle LoadChannels command."""
        """ Example query:
        {"command":"LoadChannels","serial":19}
        """
        """ Example response:
        {"command":"LoadChannels","message":"52","serial":"19","status":"OK"}
        """
        self.log.debug("LoadChannels called")

        # Get channel count from CHANNELS section
        channel_count = 0
        if 'CHANNELS' in self.config:
            channel_count = len(self.config['CHANNELS'])

        if self.log.isEnabledFor(logging.DEBUG):
            for key,value in self.config['CHANNELS'].items():
                self.log.debug(f"{key} : {value}")

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
        self.log.debug("FirstChannel called")

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
        self.log.debug("NextChannel called")

        if self.channel_iter == None:
            self.send_response(kwargs, {"status": "error", "message": "first_channel not called yet."})
            return

        return self._channel_info(**kwargs)

    def tune_status_handler(self, **kwargs) -> None:
        """Handle TuneStatus? command."""
        self.log.debug("TuneStatus? called")

        if self.recorder_tunes:
            message = "Tuned"
        elif ('Tune' not in self.processes or
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
                self.log.error(f"Error reading stderr: {e}")
                break

    def _process_stderr_line(self, line: str) -> None:
        """Process a stderr line and send status message."""
        try:
            """Data may be in 'logfile' format, so handle loglevels"""
            # Determine status based on message prefix (case insensitive)
            status = "INFO"
            prefix  = ''
            sep     = ''
            message = line
            level = logging.INFO
            if line.lower().startswith("crit"):
                status = "CRIT"
                level = logging.CRITICAL
                prefix, sep, message = line.partition(':')
            if line.lower().startswith("err"):
                status = "ERROR"
                level = logging.ERROR
                prefix, sep, message = line.partition(':')
            elif line.lower().startswith("warn"):
                status = "WARN"
                level = logging.WARN
                prefix, sep, message = line.partition(':')
            elif line.lower().startswith("damage"):
                if self.xon_state:
                    status = "DAMAGED"
                else:
                    status = "LOST"
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

            if not self.xon_state and message.lower().startswith("damage"):
                prefix, sep, message = message.partition(':')
                if message[0] == ' ':
                    message = message[1:]

            # Send status message
            response = {
                "message": message,
                "status": status
            }
            self.send_response({"command": "STATUS"}, response, level)

        except Exception as e:
            self.log.exception(f"Error processing stderr line '{line}': {e}")

    def _stream_loop(self) -> None:
        """Main streaming loop."""
        """All 'stdout' data from the [RECORDER/command] application needs to be
           passed on to mythbackend. Data is raw so don't modify it"""

        if not self.command:
            self.log.error("No command specified for streaming")
            return

        self.log.info(f"Starting streaming: {self.command}")

        try:
            # Split command into args for subprocess
            cmd_args = shlex.split(self.command)
            self.log.debug(f"Splitting command into args: {cmd_args}")
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

            self.log.info(f"Reading from sub process")

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
                self.log.info("Already ran OnDataStart")
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
                            self.log.info("Process finished normally")
                        elif st < 0:
                            self.log.warn("Process killed")
                        else:
                            self.log.warn("Process failed")
                        break
                    continue

                # Only write to stdout if in XON state, otherwise drop it.
                if self.xon_state:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.flush()

        except Exception as e:
            self.log.exception(f"Error during streaming: {e}")
            self.streaming = False
        finally:
            if self.stream_process:
                self.stream_process.terminate()
                try:
                    self.stream_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.stream_process.kill()
                    self.stream_process.wait()
            self.log.info("Streaming stopped")

    def _execute_command(self, command: str, desc: str,
                         background: bool) -> str:
        """General subprocess executer"""

        if not command:
            return None

        if desc in self.processes and self.processes[desc]['status'] != "Idle":
            """A process with this name is already running, kill it"""
            proc = self.processes[desc]
            self.log.info(f"Force terminating {desc} process {proc['process'].pid}")
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
                self.log.info(f"Started background {desc} command ({process.pid}): {command}")

                monitor_thread = threading.Thread(target=self._monitor_process, args=(desc,))
                self.processes[desc] = {'process' : process,
                                        'command' : command,
                                        'status'  : 'InProgress',
                                        'monitor' : monitor_thread}
                monitor_thread.daemon = True
                monitor_thread.start()

            except Exception as e:
                self.log.error(f"Error starting background {desc} command: {e}")
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
                self.log.error(f"Error executing {desc} command: {e}")
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
            self.log.exception(f"Error monitoring {op} completion: {e}")

    def process_variables_in_message(self, message: Dict[str, Any]) -> None:
        """Process variables from the TuneChannel message."""
        self.log.debug(f"Processing message variables. Message: {message}")
        self.log.debug(f"Initial variables: {self.variables}")

        # Update with message data
        for key, value in message.items():
            if key in ['command', 'serial']:
                continue
            # Convert key to uppercase for consistent naming
            self.variables[key.upper()] = str(value)
            self.log.debug(f"Added message variable {key.upper()} = {value}")

        for key, value in self.variables.items():
            self.variables[key] = replace_variables_in_string(value, self.variables)

        self.log.debug(f"Final variables after message processing: {self.variables}")

    def signal_event(self, event_type: str,
                     details: Optional[Dict[str, Any]] = None) -> None:
        """Safely signals an operational event back to the main
        application context."""
        if self.main_event:
            try:
                # Fallback to an empty dictionary if no extra context
                # details are passed
                data = details or {}
                self.log.debug(f"Signaling event '{event_type}' to main...")

                # Execute the callback defined in main.py
                self.main_event(event_type, data)
            except Exception as e:
                self.log.exception("Exception encountered within main"
                                   f"event handler callback: {e}")


######## globals ########
def dequote(s):
    if len(s) >= 2 and s[0] == s[-1] and s.startswith(("'","\"")):
        return s[1:-1]
    return s


def replace_variables_in_string(value: str, variables: dict) -> str:
    """Variables can come from the [VARIABLES] section in ini file,
    or from data passed as part of the TuneChannel json
    message. Supports nested variables.
    """

    if not value:
        return value

    logger = logging.getLogger(__name__)

    try:
        # Pre-normalize dictionary keys to lowercase for efficiency
        # This eliminates the O(N) loop inside regex substitutions
        lower_vars = {k.lower(): str(v) for k, v in variables.items() if v}

        def replace_special_block(match):
            block_content = match.group(1)
            # Find all variables in the block
            variables_in_block = re.findall(r'\$\{([^}]+)\}', block_content)

            all_known_and_non_empty = True
            for var_name in variables_in_block:
                if var_name.lower() not in lower_vars:
                    all_known_and_non_empty = False
                    break

            return block_content if all_known_and_non_empty else ""

        def replace_variable(match):
            var_name = match.group(1).lower()
            if var_name in lower_vars:
                # Assuming dequote handles string cleanup
                return dequote(lower_vars[var_name])
            return match.group(0)

        current_value = value
        # Protection against circular references (e.g., A -> B -> A)
        max_depth = 10

        for depth in range(max_depth):
            previous_value = current_value

            # Process special conditional blocks first
            current_value = re.sub(
                r'\[\{([^}]*?\$\{[^}]+\}[^}]*?)\}\]',
                replace_special_block, current_value, flags=re.DOTALL
            )

            # Replace regular variables
            current_value = re.sub(r'\$\{([^}]+)\}', replace_variable, current_value)

            # If the string didn't change this iteration, all
            # variables are fully expanded.
            if current_value == previous_value:
                break
        else:
            logger.warning(f"Max variable substitution depth ({max_depth}) "
                           "reached. Possible circular reference in: {value}")

    except Exception as e:
        logger.error(f"Replacing in {value}")
        for key, data in variables.items():
            logger.error(f"{key} : {data}")
        logger.exception(f"Failed to replace variables: {e}")
        return None

    return current_value
