#!/usr/bin/env python3
"""Main module for myth-genericrecorder program."""
from importlib.metadata import version

import argparse
import json

from myth_genericrecorder.recorder import Recorder, replace_variables_in_string, dequote
from myth_genericrecorder.touch import Touch
from myth_genericrecorder.logger import setup_logging, log
import logging

import os
import sys
import threading
from pathlib import Path
from typing import Dict, Any, Optional
import configparser
import re


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Generic recorder that processes JSON commands "
                    "and executes external programs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Usage examples:
  %(prog)s --conf "/home/myth/etc/magewell-1-4.conf"
        """
    )

    LOG_LEVELS = ['TRACE', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

    parser.add_argument(
        "--verbose",
        help="MythTV verbose categories (ignored)",
        type=str,
        required=False
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Program version"
    )
    parser.add_argument(
        '-l', '--loglevel',
        choices=LOG_LEVELS,
        type=lambda s: s.upper(), # Folds any input to upper case
        default='INFO',
        help="Set the logging level (default: INFO)"
    )
    parser.add_argument(
        "--inputid",
        help="InputID used to enforce unique command",
        default=0,
        required=False
    )
    parser.add_argument(
        "--conf",
        help="Configuration file path",
        type=Path,
        required=False
    )
    parser.add_argument(
        "--logpath",
        help="Path to MythTV logging directory",
        type=Path,
        default=Path.home() / 'log',
        required=False
    )
    parser.add_argument(
        '--debug', action='store_true',
        help='turn on debug messages (%(default)s)'
    )
    parser.add_argument(
        '--quiet', action='store_true',
        help='suppress progress messages (%(default)s)'
    )

    return parser.parse_args()


#################
# 'TOUCH' threads
PREPARED_TOUCHES = []
ACTIVE_TOUCHES = []

def handle_recorder_event(event_type: str, data: dict) -> None:
    global PREPARED_TOUCHES, ACTIVE_TOUCHES

    if len(PREPARED_TOUCHES) == 0:
        return

    if event_type == "RECSTART":
        logging.info("Starting Touch loops...")
        for keepalive in PREPARED_TOUCHES:
            keepalive.start()
            # Move the reference to the active list instead of deleting it
            ACTIVE_TOUCHES.append(keepalive)
        PREPARED_TOUCHES.clear()

    elif event_type == "STREAM_STOPPED":
        logging.warning("Stopping Touch loops...")
        for keepalive in ACTIVE_TOUCHES:
            keepalive.stop()
        ACTIVE_TOUCHES.clear()


#####################
# Read config file(s)
def process_config_section(config: configparser.ConfigParser,
                           section_name: str) -> Dict[str, str]:
    """Process a configuration section from a ConfigParser instance.

    Extracts all key/value pairs, filters out empty entries, and normalizes
    all dictionary keys to UPPERCASE for uniform downstream variable lookups.
    """
    if section_name not in config:
        return {}

    result = {}

    # config[section_name].items() automatically handles
    # case-insensitive key retrieval from the source file, but
    # force uppercase on the resulting dictionary output.
    for key, value in config[section_name].items():
        if value is not None:
            result[key.upper()] = value

    return result


def process_config_section(config: configparser.ConfigParser,
                           section_name: str) -> Dict[str, str]:
    """Process a configuration section with variable replacement."""
    if section_name not in config:
        return {}

    result = {}
    for key, value in config[section_name].items():
        if value is not None:
            result[key.upper()] = value
    return result


def parse_config_file(config_path: Path) -> tuple[Dict[str, Any],
                                                  Dict[str, str]]:
    """Parse the configuration file and return a dictionary of settings.

    Normalizes all section names and inner variable keys to UPPERCASE
    to prevent case-mismatch errors down the line during execution
    lookups.
    """
    config = configparser.ConfigParser(allow_no_value=True)

    # Read the main configuration file
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    config.read(config_path)

    # Process VARIABLES section first to set up replacement variables
    variables = {}
    if 'VARIABLES' in config:
        for key, value in config['VARIABLES'].items():
            if value is not None:  # Skip keys without values
                variables[key.upper()] = value
                log.debug(f"Loaded variable {key.upper()} = {value}")

    # Process INCLUDE section and merge options
    if 'INCLUDE' in config:
        # include_files will iterate over the options/keys under the
        # [INCLUDE] header
        for include_file, _ in config.items('INCLUDE'):
            include_path = Path(replace_variables_in_string(include_file,
                                                            variables))

            # Resolve relative paths relative to the main config file
            if not include_path.is_absolute():
                include_path = config_path.parent / include_path

            if include_path.exists():
                log.debug(f"Loading included config: {include_path}")
                include_config = configparser.ConfigParser(allow_no_value=True)
                include_config.read(include_path)

                # Safe Explicit Merge: Build configuration data nodes correctly
                for section_name in include_config.sections():
                    if section_name == 'DEFAULT':
                        continue

                    if not config.has_section(section_name):
                        config.add_section(section_name)

                    for key, value in include_config.items(section_name):
                        config.set(section_name, key, value)
            else:
                log.warning(f"Include file not found: {include_path}")

    # Process all parsed sections with uppercase header normalization
    processed_config = {}
    for section_name, section_data in config.items():
        if section_name == 'DEFAULT':
            continue

        processed_config[section_name.upper()] = process_config_section(config, section_name)

    # Load external channel configuration if provided
    tuner_section = processed_config.get('TUNER', {})
    if 'CHANNELS' in tuner_section:
        channel_file = tuner_section['CHANNELS']
        channel_path = Path(channel_file)

        if channel_path.exists():
            log.debug(f"Processing channels from {channel_path}")
            channel_config = configparser.ConfigParser(allow_no_value=True)
            channel_config.read(channel_path)

            # Process channel configurations with variable replacement mapping
            processed_config['CHANNELS'] = {}
            for section_name, section_data in channel_config.items():
                if section_name == 'DEFAULT':
                    continue
                # Normalize sub-channel blocks to uppercase
                processed_config['CHANNELS'][section_name.upper()] = process_config_section(channel_config, section_name)
        else:
            log.debug(f"Channel layout file not found: {channel_path}")
            processed_config['CHANNELS'] = {}
    else:
        processed_config['CHANNELS'] = {}

    # Output debugging traces if enabled
    for key, data in processed_config.items():
        log.debug(f"Configuration Map -> [{key}] = {data}")

    return processed_config, variables


############
# Main entry
def main():
    """Main entry point."""
    args = parse_arguments()
    variables = None

    __version__ = version("myth-genericrecorder")

    if args.version:
        print(__version__)
        sys.exit(0)

    # Setup logging
    if args.logpath:
        log_dir = args.logpath
    else:
        log_dir = Path.home() / "log"

    # Ensure directory exists
    log_dir.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"myth-genericrecorder-{args.inputid}.log"

    setup_logging(log_file, args.debug, args.quiet,
                  default_level=args.loglevel)
    if args.debug:
        log.setLevel(logging.DEBUG)

    log.critical("Starting myth-genericrecorder")
    log.debug(f"Command line arguments: {args}")
    log.debug(f"Log file path: {log_file}")

    # Load configuration
    config = {}
    if args.conf:
        try:
            config, variables = parse_config_file(args.conf)
            log.info(f"Configuration loaded from {args.conf}")
        except Exception as e:
            log.exception(f"Failed to load configuration: {e}")
            sys.exit(1)

    if 'RECORDER' not in config:
        log.error("No [RECORDER] section found in configuration.")
        return False;

    # Create recorder with configuration
    recorder = Recorder(
        logger=log,
        config=config,
        variables=variables,
        event_callback=handle_recorder_event
    )

    # Look for any configuration sections starting with "TOUCH"
    touch_sections = [sec for sec in config.keys() if sec.startswith('TOUCH')]

    for section in touch_sections:
        touch_config = config[section]

        command = touch_config.get('COMMAND')
        delay = touch_config.get('DELAY')
        frequency = touch_config.get('FREQUENCY')

        # Pull the new field out (will safely return None if omitted)
        damaged_on_failure_str = touch_config.get('DAMAGED_ON_FAILURE')

        if command:
            log.debug("Preparing background Touch instance "
                      f"for section: [{section}]")

            keepalive = Touch(
                frequency=frequency,
                delay=delay,
                command=command,
                recorder_instance=recorder,
                damaged_on_failure_str=damaged_on_failure_str # Pass it here!
            )

            PREPARED_TOUCHES.append(keepalive)
        else:
            log.warning(f"Section [{section}] found, but 'COMMAND' "
                        "is missing. Skipping.")


    ######
    # Main processing loop.
    # Receives JSON message on stdin and process them.
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                if line == "APIVersion?":
                    sys.stderr.write("OK:3\n")
                    continue
                message = json.loads(line)
                log.debug(f"Raw JSON message: {line}")

                # Process the command
                recorder.process_command(message)

            except json.JSONDecodeError as e:
                log.error(f"Invalid JSON message: {line}")
                log.error(f"Error: {e}")
                continue
            except Exception as e:
                log.error(f"Error processing message: {line}")
                log.error(f"Error details: {e}")
                continue

    except KeyboardInterrupt:
        log.info("Received interrupt signal")
        sys.exit(0)
    except Exception as e:
        log.error("Unexpected error in main loop")
        log.error(f"Error details: {e}")
        sys.exit(1)

    for keepalive in ACTIVE_TOUCHES:
        keepalive.stop()

    ACTIVE_TOUCHES.clear()


if __name__ == "__main__":
    main()
