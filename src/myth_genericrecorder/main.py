#!/usr/bin/env python3
"""Main module for myth-genericrecorder program."""
from importlib.metadata import version

import argparse
import json

from myth_genericrecorder.logger import setup_logging, log
import logging

import os
import sys
import threading
from pathlib import Path
from typing import Dict, Any, Optional
import configparser
import re

from myth_genericrecorder.recorder import Recorder, replace_variables_in_string, dequote
from myth_genericrecorder.touch import Touch

def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Generic recorder that processes JSON commands "
                    "and executes external commands",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Usage examples:
  %(prog)s --conf "/home/myth/etc/magewell-1-4.conf"
  %(prog)s --command "vlc --demux=mp4 input.mp4"
  %(prog)s --command "streamlink twitch.tv/channel best"
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
        "--command",
        help="External command to execute during streaming",
        type=str,
        required=False
    )
    parser.add_argument(
        "--tune",
        help="Tune command to execute in background",
        type=str,
        required=False
    )
    parser.add_argument(
        "--conf",
        help="Configuration file path",
        type=Path,
        required=False
    )
    parser.add_argument(
        "--blocksize",
        help="Block size for streaming (default: 64k)",
        type=int,
        default=65536,
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

def process_config_section(config: configparser.ConfigParser, section_name: str) -> Dict[str, str]:
    """Process a configuration section with variable replacement."""
    if section_name not in config:
        return {}

    result = {}
    for key, value in config[section_name].items():
        if value is not None:
            result[key.upper()] = value
    return result

def parse_config_file(config_path: Path) -> Dict[str, Any]:
    """Parse the configuration file and return a dictionary of settings."""
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
                log.debug(f"Loaded variable {key} = {value}")

    # Process INCLUDE section
    if 'INCLUDE' in config:
        include_files = config['INCLUDE']
        for include_file in include_files:
            include_path = Path(replace_variables_in_string(include_file, variables))
            # Resolve relative paths relative to the main config file
            if not include_path.is_absolute():
                include_path = config_path.parent / include_path
            if include_path.exists():
                log.debug(f"Loading included config: {include_path}")
                include_config = configparser.ConfigParser(allow_no_value=True)
                include_config.read(include_path)

                # Merge included config into main config
                for section_name, section_data in include_config.items():
                    if section_name == 'DEFAULT':
                        continue
                    config[section_name] = process_config_section(include_config, section_name)
                    """
                    if section_name not in config:
                        config[section_name] = {}
                    for key, value in section_data.items():
                        config[section_name][key.upper()] = value
                    """
            else:
                log.warning(f"Include file not found: {include_path}")

    # Process all sections
    processed_config = {}
    for section_name, section_data in config.items():
        if section_name == 'DEFAULT':
            continue
        processed_config[section_name] = process_config_section(config, section_name)

    # Load channel configuration if specified
    if 'TUNER' in processed_config and 'CHANNELS' in processed_config['TUNER']:
        channel_file = processed_config['TUNER']['CHANNELS']
        channel_path = Path(channel_file)
        if channel_path.exists():
            log.debug(f"Processing channels from {channel_path}")
            channel_config = configparser.ConfigParser(allow_no_value=True)
            channel_config.read(channel_path)

            # Process channel configurations with variable replacement
            processed_config['CHANNELS'] = {}
            for section_name, section_data in channel_config.items():
                if section_name == 'DEFAULT':
                    continue
                processed_config['CHANNELS'][section_name] = process_config_section(channel_config, section_name)
            log.trace(f"Channels: {processed_config['CHANNELS']}")
        else:
            log.debug(f"No channels processed")
            processed_config['CHANNELS'] = {}
#    else:
#        log.error(f"'channels' not in {processed_config['TUNER']}")

    if log.isEnabledFor(logging.DEBUG):
        for key,data in processed_config.items():
            log.trace(f"{key}={data}")

    return processed_config, variables


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

    print(f"log level: {args.loglevel}")

    setup_logging(log_file, args.debug, args.quiet,
                  default_level=args.loglevel)
    if args.debug:
        log.setLevel(logging.DEBUG)

    log.critical("Starting myth-genericrecorder")
    log.debug(f"Command line arguments: {args}")
    log.debug(f"Log file path: {log_file}")

    # Load configuration if provided
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
        command=args.command,
        logger=log,
        tune_command=args.tune,
        config=config,
        variables=variables,
        block_size=args.blocksize
    )

    if 'TOUCH' in config:
        frequency = config['TOUCH'].get('FREQUENCY')
        command   = replace_variables_in_string(config['TOUCH'].get('COMMAND'),
                                                variables)
        if frequency and command:
            keepalive = Touch(frequency=frequency,
                              command=dequote(command),
                              on_error_callback=recorder.handle_touch_error)
            keepalive.start()

    # Process stdin messages
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

    if keepalive:
        keepalive.stop()

if __name__ == "__main__":
    main()
