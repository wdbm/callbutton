# Callbutton

Callbutton provides voice assistants which use OpenAI GPT-Live:

- `callbutton` is a transient assistant. It can delegate current-information questions to a backend with web search, but it does not retain conversations or inspect a local knowledgebase.
- `callbutton_memory` provides durable conversation history and a read-only, searchable local knowledgebase.

Continuous mono audio is streamed between the system microphone, the OpenAI Live API and the system speaker. Full-duplex conversation and interruption handling are provided by `gpt-live-1`. A responses model handles delegated work such as web searches, local knowledge retrieval and prior-conversation recall without blocking audio handling.

## Setup

Infrastructure like PortAudio, PipeWire and PulseAudio are required. A virtual environment can be prepared in the following way:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install \
    'docopt>=0.6.2,<0.7' \
    'openai[realtime]>=3.13,<4' \
    'sounddevice>=0.5,<0.6'
```

The `OPENAI_API_KEY` environment variable must be set. An IANA timezone may also be selected; `America/Sao_Paulo` is used when `TZ` is absent.

```bash
export OPENAI_API_KEY="..."
export TZ="Europe/Dublin"
```

## `callbutton`

`callbutton` can be launched in the following way:

```bash
./callbutton
```

The complete interface is shown by `./callbutton --help`. A different voice or responses backend model can be selected in a way like the following:

```bash
./callbutton --voice=tempo --backend-model=gpt-5-mini --transcripts
```

## `callbutton_memory`

The memory-enabled assistant can be launched from the directory where its data should be present:

```bash
./callbutton_memory
```

On first use, the following are created relative to the working directory:

- `conversation_history.db`, the durable conversation database;
- `knowledge_index.db`, the disposable FTS5/BM25 search index;
- `knowledgebase/`, the default read-only knowledgebase directory.

Alternatives can be selected independently in a way like the following:

```bash
./callbutton_memory \
    --history-db=conversation_history.db \
    --knowledge-db=knowledge_index.db \
    --knowledge-dir=knowledgebase
```

Other useful options include the following:

```text
--voice=<voice>             Session output voice (default voice: meridian)
--backend-model=<model>     Delegated responses model (default: gpt-5-mini)
--transcripts               Print transcript fragments and long backend responses
--no-web                    Disable hosted web search
--rebuild-knowledge         Recreate disposable knowledge index
--input-device=<device>     Select input device by index or name fragment
--output-device=<device>    Select output device by index or name fragment
```

The complete interface is shown by `./callbutton_memory --help`.

There is available a detailed [description of the databases](databases.md).

## Local knowledgebase

Text files placed at the knowledgebase directory (`knowledgebase` by default) are scanned recursively before each voice session. Files of at most 5 MiB are indexed while items like hidden paths, symbolic links and files possibly featuring secrets (as determined by filename, e.g. `credentials.json`, `.key`, `.pem`) are excluded. Database-associated files are also excluded.

If a text file is included in the knowledgebase directory, `callbutton_memory` should index it and incorporate it into the knowledgebase database and this knowledge should then be accessible verbally. For example, if the knowledgebase has knowledge stored about `AWK`, verbal requests regarding that knowledge could be something like the following:

>Please search the local knowledgebase for AWK and tell me what information is available and summarise the examples.

>According to the local knowledgebase, how can AWK be used to find the maximum or average of a column?

Searching for a distinctive subject from a document is more reliable than requesting a complete inventory. The knowledge tools search relevant text and read excerpts; they do not provide the model with an operation for listing every indexed document.

The knowledgebase index database is a cache and may be rebuilt explicitly in a way like the following:

```bash
./callbutton_memory --rebuild-knowledge
```

## Conversation history

Transcript fragments are retained losslessly and are also reconstructed into searchable user and assistant turns. Sessions, client delegations, local tool runs and rolling memory summaries are recorded. Every application-managed database record has a full UUIDv4 and a paired Unix-nanosecond/ISO-8601 UTC timestamp.

SQLite foreign keys, write-ahead logging and a busy timeout are enabled. The history writer runs separately from the session loop.

The conversation database is durable user data and can be backed up as required.

## Delegation and privacy

`callbutton_memory` uses client-owned Live delegation. When the Live model delegates a request, recent dialogue and any relevant memory summary are sent to the responses backend. That backend may call the following read-only tools:

- local knowledge search;
- bounded local excerpt reading;
- conversation-history search;
- hosted web search (unless `--no-web` was supplied).

Local files and raw history remain local except for excerpts and conversation context selected for transmission for the purposes of an OpenAI request. A completed backend result is returned to GPT-Live as commentary associated with the matching delegation. A corrected request supersedes unfinished earlier work so that an obsolete answer is not spoken.

Complete backend results are retained in the conversation database. Each Live `session.commentary.append` is limited to 500 tokens, so larger results are divided into conservative, coherent chunks and sent with the same delegation identifier. When `--transcripts` is enabled, the complete backend response is also printed between `[backend response]` markers, otherwise a brief note that the full text was saved is displayed.

## References

- <https://developers.openai.com/api/docs/guides/voice-websockets?api=live>
- <https://developers.openai.com/api/docs/guides/live-delegation?delegation-mode=client>
- <https://developers.openai.com/api/docs/guides/function-calling>
