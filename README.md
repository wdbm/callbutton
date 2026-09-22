# callbutton

`callbutton` is a compact voice assistant which uses OpenAI GPT-Live. Continuous mono audio is streamed between the system microphone, the OpenAI Live API and the system speaker. Full-duplex conversation and interruption handling are provided by `gpt-live-1`. The implementation follows the OpenAI prescription for GPT-Live over [WebSockets]((https://developers.openai.com/api/docs/guides/voice-websockets?api=live)).

## setup

PortAudio and PipeWire or PulseAudio are required.

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install \
    'docopt>=0.6.2,<0.7' \
    'openai[realtime]>=3.13,<4' \
    'sounddevice>=0.5,<0.6'
```

The environment variable `OPENAI_API_KEY` must be set to the OpenAI API key. Optionally, an environment variable specifying an IANA timezone can be set:

```bash
export TZ="Europe/Dublin"
```
