import glob
import logging
import logging.config
import sys
import os

# ==============================================================================
# TRACE LEVEL INJECTION & LIVE PROXY
# ==============================================================================
TRACE_LEVEL_NUM = 5
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")

# Inject the .trace() method into the core Logger class architecture
def trace(self, message, *args, **kws):
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)
logging.Logger.trace = trace

# The wrapper class that solves the initialization race condition
class LiveLogger:
    def __getattr__(self, name):
        return getattr(logging.getLogger(), name)

# Replaces your old: log = logging.getLogger()
log = LiveLogger()
# ==============================================================================

def setup_logging(filename, debug=False, quiet=False, default_level='INFO'):

    # Evaluate levels from command line arguments or old flags
    if quiet:
        file_level = 'DEBUG' if debug else 'INFO'
        console_level = 60  # Complete console mute
        optstr = '--quiet' + (' --debug' if debug else '')
    elif debug:
        file_level = 'DEBUG'
        console_level = 'DEBUG'
        optstr = '--debug'
    else:
        # Accepts 'TRACE', 'DEBUG', 'INFO', etc. straight from your CLI args
        file_level = default_level.upper()
        console_level = default_level.upper()
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
                'filename': filename,
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

    log.critical(f"{apppath} Logging to '{filename}' with {optstr} "
                 f"file '{file_level}' "
                 f"console '{console_level}'")
