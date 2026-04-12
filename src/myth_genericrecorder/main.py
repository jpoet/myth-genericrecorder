#!/usr/bin/env python3
"""Main module for myth-genericrecorder program."""
import argparse
import json
import logging
import logging.config
import os
import sys
import threading
from pathlib import Path
from typing import Dict, Any, Optional
import configparser
import re

from myth_genericrecorder.recorder import Recorder

# Setup logging
def setup_logging(log_file: Path, quiet: bool = True) -> None:
    """Setup logging configuration."""
    dict_conf = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s.%(msecs)-4d %(levelname)-8s "
                          "[%(filename)s:%(lineno)d] %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S"
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": "INFO",
                "formatter": "standard",
                "stream": sys.stderr  # Always log to stderr
            },
            "file": {
                "class": "logging.FileHandler",
                "level": "DEBUG",
                "formatter": "standard",
                "filename": str(log_file),
                "mode": "a"
            }
        },
        "root": {
            "level": "DEBUG",
            "handlers": ["console", "file"] if not quiet else ["file"]
        }
    }
    logging.config.dictConfig(dict_conf)

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
    parser.add_argument(
        "--verbose",
        help="MythTV verbose categories (ignored)",
        type=str,
        required=False
    )
    parser.add_argument(
        "--loglevel",
        help="Default log level",
        type=str,
        required=False
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
        "--quiet",
        action="store_true",
        help="Suppress console output"
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
                logging.getLogger(__name__).debug(f"Loaded variable {key} = {value}")

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
#            logging.getLogger(__name__).error(f"Processing channels from {channel_path}")
            channel_config = configparser.ConfigParser(allow_no_value=True)
            channel_config.read(channel_path)

            # Process channel configurations with variable replacement
            processed_config['CHANNELS'] = {}
            for section_name, section_data in channel_config.items():
                if section_name == 'DEFAULT':
                    continue
                processed_config['CHANNELS'][section_name] = process_config_section(channel_config, section_name)
#            logging.getLogger(__name__).error(f"Channels: {processed_config['CHANNELS']}")
        else:
#            logging.getLogger(__name__).error(f"No channels processed")
            processed_config['CHANNELS'] = {}
#    else:
#        logging.getLogger(__name__).error(f"'channels' not in {processed_config['TUNER']}")

    return processed_config, variables

def main():
    """Main entry point."""
    args = parse_arguments()
    variables = None

    # Setup logging
    if args.logpath:
        log_dir = args.logpath
    else:
        log_dir = Path.home() / "log"

    # Ensure directory exists
    log_dir.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"myth-genericrecorder-{args.inputid}.log"

    setup_logging(log_file, args.quiet)

    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)

    logger.critical("Starting myth-genericrecorder")
    logger.debug(f"Command line arguments: {args}")
    logger.debug(f"Log file path: {log_file}")

    # Load configuration if provided
    config = {}
    if args.conf:
        try:
            config, variables = parse_config_file(args.conf)
            logger.info(f"Configuration loaded from {args.conf}")
        except Exception as e:
            logger.critical(f"Failed to load configuration: {e}")
            sys.exit(1)

    # Create recorder with configuration
    recorder = Recorder(
        command=args.command,
        logger=logger,
        tune_command=args.tune,
        config=config,
        variables=variables,
        block_size=args.blocksize
    )

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
                logger.debug(f"Raw JSON message: {line}")

                # Process the command
                recorder.process_command(message)

            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON message: {line}")
                logger.error(f"Error: {e}")
                continue
            except Exception as e:
                logger.error(f"Error processing message: {line}")
                logger.error(f"Error details: {e}")
                continue

    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
        recorder.stop_streaming()
        sys.exit(0)
    except Exception as e:
        logger.error("Unexpected error in main loop")
        logger.error(f"Error details: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
