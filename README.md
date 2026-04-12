# myth-genericrecorder

A "generic" mythtv external recorder. Very much like the generic mythexternrecorder that ships with mythtv but written in python and much more efficient that the current version that mythtv ships.

## Caveats
The configuration file is slightly different. Variables in use look like ${var_name} instead of the %var_name% that mythexternrecorder uses.

## Installation

```bash
sudo pipx ensurepath --global
sudo pipx install --global git+https://github.com/jpoet/myth-genericrecorder.git
```
or
```
sudo pipx ensurepath --global
git clone https://github.com/jpoet/myth-genericrecorder.git
cd myth-genericrecorder
sudo pipx install --global .
```

## Uninstall
```
sudo pipx uninstall --global  myth-genericrecorder
```

## Usage

### Basic Usage
Meant to be controlled as a "Black Box" recorder from mythbackend. For example:
```
/usr/local/bin/myth-genericrecorder --help
/usr/local/bin/myth-genericrecorder --conf /home/mythtv/etc/magewell-2-1-4.conf
```

Once it is running it is controlled via json messages. The very first message (APIVersion?) is an exception, since older versions of the protocal where plain text
```bash
APIVersion?
{"command":"APIVersion","serial":1,"value":"3"}
{"command":"Version?","serial":2}
{"command":"Description?","serial":3}
{"command":"HasTuner?","serial":4}
{"command":"HasPictureAttributes?","serial":5}
{"command":"FlowControl?","serial":6}
{"command":"BlockSize","serial":7,"value":"3080192"}
{"command":"LockTimeout?","serial":8}
{"atsc_major":0,"atsc_minor":0,"callsign":"CALLSIGN","chanid":100,"channum":"100","command":"TuneChannel","description":"","duration":1923,"freqid":"","inputid":16,"mplexid":0,"name":"Station Name","programid":"","recordid":4165,"serial":9,"seriesid":"","sourceid":4,"subtitle":"Subtitle","title":"Title","value":"96"}
{"command":"TuneStatus?","serial":10}
{"command":"SignalStrengthPercent?","serial":11}
{"command":"HasLock?","serial":12}
{"command":"IsOpen?","serial":13}
{"command":"StartStreaming","serial":14}
{"command":"XON","serial":15}
{"command":"XOFF","serial":16}
{"command":"StopStreaming","serial":17}
{"command":"CloseRecorder","serial":18}
```

## Command Reference

- `APIVersion?`: Returns API version information this program wants to use.
- `APIVersion` : Specifies the API version to use
- `Version?`: Returns version information
- `Description?`: Returns description information
- `HasTuner?`: Returns tuner availability
- `HasPictureAttributes?`: Returns picture attributes availability
- `FlowControl?`: Returns flow control information
- `BlockSize`: Sets the block (data chunk) size
- `LockTimeout?`: Returns lock timeout information
- `TuneChannel`: Requests the a "channel" be tuned using the provided data.
- `SignalStrengthPercent?`: Returns the signal strength
- `HasLock?": Returns if the Channel has been tuned.
- `IsOpen?": Returns if the program specified in the config file is running.
- `CloseRecorder`: Closes the recorder
- `StartStreaming`: Starts streaming with external command
- `StopStreaming`: Stops streaming
- `XON`: Starts data flowing
- `XOFF`: Stops the flow of data (packets from external command are discarded)

## Requirements

- Python 3.8+
