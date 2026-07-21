import glob
import logging
import logging.config
import sys
import os
from pathlib import Path

# ==============================================================================
# TRACE LEVEL INJECTION & LIVE PROXY
TRACE_LEVEL_NUM = 5
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")

# Inject the .trace() method into the core Logger class architecture
def trace(self, message, *args, **kws):
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)
logging.Logger.trace = trace

class LiveLogger:
    def __getattr__(self, name):
        return getattr(logging.getLogger(), name)

log = LiveLogger()
# ==============================================================================

def setup_logging(filename, debug=False, quiet=False, default_level='INFO'):
    # Convert filename string to a pathlib Path object
    log_path = Path(filename).resolve()

    # Pre-flight check: Verify write permissions for the file or its parent directory
    if log_path.exists():
        if not os.access(log_path, os.W_OK):
            raise PermissionError(f"Cannot write to existing log file: {log_path}")
    else:
        # Create missing parent directories if necessary, then check write access
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if not os.access(log_path.parent, os.W_OK):
            raise PermissionError(f"Cannot create log file. Write permission denied in: {log_path.parent}")

    # Evaluate levels from command line arguments or old flags
    if quiet:
        file_level = 'DEBUG' if debug else 'INFO'
        console_level = 60  # Complete console mute
        optstr = '--quiet' + (' --debug' if debug else '')
    elif debug:
        file_level = 'DEBUG'
        console_level = 'INFO'  # Never send debug to mythbackend
        optstr = '--debug'
    else:
        # Accepts 'TRACE', 'DEBUG', 'INFO', etc. straight from your CLI args
        file_level = default_level.upper()
        console_level = 'INFO'  # Never send debug to mythbackend
        optstr = f'--log-level {default_level}'

    dict_conf = {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'standard': {
                'format': '%(asctime)s.%(msecs)-4d %(levelname)-8s '
                          '[%(filename)s:%(lineno)d] %(message)s',
                'datefmt': "%Y-%m-%dT%H:%M:%S",
            },
        },
        'handlers': {
            'default': {
                'level': console_level,
                'class': 'logging.StreamHandler',
                'formatter': 'standard',
                'stream': sys.stderr,
            },
            'rotating_to_file': {
                'level': file_level,
                'class': "logging.handlers.RotatingFileHandler",
                'formatter': 'standard',
                # Pass the absolute path string to the handler
                'filename': str(log_path),
                'maxBytes': 1000000,
                'backupCount': 5,
            },
        },
        'loggers': {
            '': {
                'handlers': ['default', 'rotating_to_file'],
                # Set root lower than TRACE (5) so it doesn't filter out our data
                'level': 'TRACE',
                'propagate': True
            }
        }
    }

    apppath = os.path.abspath(os.path.dirname(sys.argv[0]))
    logging.config.dictConfig(dict_conf)

    log.critical(f"{apppath} Logging to '{log_path}' with {optstr} "
                 f"file '{file_level}' "
                 f"console '{console_level}'")
