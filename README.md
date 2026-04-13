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
{"command":"LoadChannels","serial":19}
{"command":"FirstChannel","serial":20}
{"command":"NextChannel","serial":21}
```

## Command Reference

- `APIVersion?`: Returns API version information this program wants to use. This is the only query/response not in JSON format:
  `OK:3`
- `APIVersion` : Specifies the API version to use:
  `{"command":"APIVersion","message":"3","serial":"1","status":"OK"}`
- `Version?`: Returns version information:
  `{"command":"APIVersion","serial":1,"value":"3"}`
- `Description?`: Returns description information:
  `{"command":"Description?","message":"mag-1-2-3","serial":"3","status":"OK"}`
- `HasTuner?`: Returns tuner availability:
  `{"command":"HasTuner","message":"Yes","serial":"2","status":"OK"}`
- `HasPictureAttributes?`: Returns picture attributes availability:
  `{"command":"HasPictureAttributes","message":"No","serial":"4","status":"OK"}`
- `FlowControl?`: Returns flow control information:
  `{"command":"FlowControl?","message":"XON/XOFF","serial":"5","status":"OK"}`
- `BlockSize`: Sets the block (data chunk) size:
  `{"command":"BlockSize","message":"Blocksize 3080192","serial":"6","status":"OK"}`
- `LockTimeout?`: Returns lock timeout information:
  `{"command":"LockTimeout","message":"30000","serial":"8","status":"OK"}`
- `TuneChannel`: Requests the a "channel" be tuned using the provided data:
  `{"command":"TuneChannel","message":"InProgress `/usr/local/bin/roku-control --device roku9 --channum 318"`","serial":"9","status":"OK"}`
- `SignalStrengthPercent?`: Returns the signal strength:
  `{"command":"SignalStrengthPercent?","serial":11}`
- `HasLock?`: Returns if the Channel has been tuned:
  `{"command":"HasLock?","message":"No","serial":"12","status":"OK"}`
- `IsOpen?`: Returns if the program specified in the config file is running:
  `{"command":"IsOpen?","message":"Not Open yet","serial":"13","status":"WARN"}`
- `CloseRecorder`: Closes the recorder:
  `{"command":"CloseRecorder","message":"Terminating","serial":"9","status":"OK"}`
- `StartStreaming`: Starts streaming with external command:
  `{"command":"StartStreaming","message":"Streaming Started","serial":"11","status":"OK"}`
- `StopStreaming`: Stops streaming:
  `{"command":"StopStreaming","message":"Streaming Stopped","serial":"12","status":"OK"}`
- `XON`: Starts data flowing:
  `{"command":"XON","message":"Started Streaming","serial":"12","status":"OK"}`
- `XOFF`: Stops the flow of data (packets from external command are discarded):
  `{"command":"XOFF","message":"Stopped Streaming","serial":"13","status":"OK"}`
- `LoadChannels`: Returns the number of channels defined in [TUNER]/channels:
  `{"command":"LoadChannels","message":"52","serial":"19","status":"OK"}`
- `FirstChannel`: Returns first channel (channum, name, callsign, xmltvid, icon) defined in [TUNER]/channels:
  `{"command":"FirstChannel","message":"ChanNum,ChanName,Callsign,xmltvid,icon","serial":"20","status":"OK"}`
- `NextChannel`: Returns next channel (channum, name, callsign, xmltvid, icon) defined in [TUNER]/channels:
  `{"command":"NextChannel","message":"ChanNum,ChanName,Callsign,xmltvid,icon","serial":"21","status":"OK"}`

## Requirements

- Python 3.8+
