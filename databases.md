# Databases

There are two separate SQLite 3 databases. The conversation database contains durable conversation history, while the knowledge database is a rebuildable search index for files at the knowledge directory.

|Database            |Default filename         |Purpose                                                                                                |Rebuildable                                                 |
|--------------------|-------------------------|-------------------------------------------------------------------------------------------------------|------------------------------------------------------------|
|Conversation history|`conversation_history.db`|Stores sessions, transcript data, delegated requests, local tool calls and long-term memory summaries. |No. It is the authoritative conversation record.            |
|Knowledge index     |`knowledge_index.db`     |Stores file metadata, text chunks and the FTS5 index used to search the configured knowledge directory.|Yes. The files at the knowledge directory are authoritative.|

## Conventions

Database records use UUIDv4 identifiers. Timestamps are stored both as Unix time in nanoseconds and as UTC text with nine fractional digits.

| Suffix    | SQLite type | Meaning                                             | Example                          |
| --------- | ----------- | --------------------------------------------------- | -------------------------------- |
| `_at_ns`  | `INTEGER`   | Unix time in nanoseconds.                           | `1790120431846330166`            |
| `_at_utc` | `TEXT`      | ISO 8601 UTC timestamp with nine fractional digits. | `2026-09-22T23:40:31.846330166Z` |

`created_at_*` fields describe when a database record was created. `updated_at_*` fields describe its most recent update. `modified_at_ns` fields describe file modification times as supplied by the filesystem.

## Conversation history database

This database contains conversation history and the backend activity associated with it.

### Relationship summary

| Parent        | Child                  | Relationship                                                 |
| ------------- | ---------------------- | ------------------------------------------------------------ |
| `sessions`    | `transcript_fragments` | One session may have many lossless transcript fragments. Deleting a session cascades to its fragments, turns, delegations, and delegated tool calls. |
| `sessions`    | `turns`                | One session may have many reconstructed dialogue turns.      |
| `sessions`    | `delegations`          | One session may have many delegated backend requests.        |
| `delegations` | `tool_runs`            | One delegation may have many local tool calls.               |
| `turns`       | `turns_fts`            | Each searchable turn has a corresponding FTS5 row linked by `turn_id`. This association is maintained by the application rather than by a foreign key. |

`memory_summaries` contains cross-session summary snapshots and deliberately has no foreign key to one particular session.

### Tables

| Table                  | Kind               | Purpose                                                      |
| ---------------------- | ------------------ | ------------------------------------------------------------ |
| `database_metadata`    | Ordinary table     | Stores database-format metadata.                             |
| `sessions`             | Ordinary table     | Stores one row for each invocation that starts a session.    |
| `transcript_fragments` | Ordinary table     | Stores every input and output transcript delta received from the service. |
| `turns`                | Ordinary table     | Stores searchable user and assistant turns reconstructed from transcript fragments. |
| `turns_fts`            | FTS5 virtual table | Provides text search over completed turn text.               |
| `delegations`          | Ordinary table     | Stores requests delegated from the service to the responses backend and their results. |
| `tool_runs`            | Ordinary table     | Stores local function calls made while completing a delegation. |
| `memory_summaries`     | Ordinary table     | Stores rolling long-term memory summaries made from conversation turns. |

### `database_metadata`

| Field            | Type and constraints | Meaning                                                      |
| ---------------- | -------------------- | ------------------------------------------------------------ |
| `metadata_id`    | `TEXT PRIMARY KEY`   | UUIDv4 identifier for the metadata record.                   |
| `created_at_ns`  | `INTEGER NOT NULL`   | Database creation time in Unix nanoseconds.                  |
| `created_at_utc` | `TEXT NOT NULL`      | Database creation time as an ISO 8601 UTC value.             |
| `schema_version` | `INTEGER NOT NULL`   | Database-format compatibility marker used by the application. |

### `sessions`

One row represents one attempted session, including attempts that fail during startup.

| Field             | Type and constraints | Meaning                                                      |
| ----------------- | -------------------- | ------------------------------------------------------------ |
| `session_id`      | `TEXT PRIMARY KEY`   | UUIDv4 identifier. This is the parent key used throughout the conversation database. |
| `created_at_ns`   | `INTEGER NOT NULL`   | Time at which the local session record was created in Unix nanoseconds. |
| `created_at_utc`  | `TEXT NOT NULL`      | Time at which the local session record was created in UTC timestamp. |
| `updated_at_ns`   | `INTEGER NOT NULL`   | Time of the most recent status, identifier, usage or error update in Unix nanoseconds. |
| `updated_at_utc`  | `TEXT NOT NULL`      | Time of the most recent status, identifier, usage or error update in UTC timestamp. |
| `provider_session_id` | `TEXT`, nullable | Session identifier assigned by the service after a successful start. It can remain null if startup fails. |
| `status`          | `TEXT NOT NULL`      | Local lifecycle state. Values are `starting`, `active`, `complete`, `failed` and `interrupted`. |
| `voice`           | `TEXT NOT NULL`      | Voice selected for the session.                              |
| `backend_model`   | `TEXT NOT NULL`      | Responses model selected for delegated work.                 |
| `timezone`        | `TEXT NOT NULL`      | Timezone supplied to the backend for interpreting and reporting local times. This is normally an IANA timezone name. |
| `usage_json`      | `TEXT`, nullable     | JSON serialisation of the session usage object. It remains null if no final usage object was received. |
| `error_text`      | `TEXT`, nullable     | Terminal error associated with a failed session, or the recovery explanation for an interrupted session. |

### `transcript_fragments`

This is the lossless transcript event record. A fragment is a delta received from the API, not necessarily a word, sentence or complete speaking turn.

| Field             | Type and constraints         | Meaning                                                      |
| ----------------- | ---------------------------- | ------------------------------------------------------------ |
| `fragment_id`     | `TEXT PRIMARY KEY`           | UUIDv4 identifier for the fragment.                          |
| `created_at_ns`   | `INTEGER NOT NULL`           | Time at which the fragment was stored in Unix nanoseconds.   |
| `created_at_utc`  | `TEXT NOT NULL`              | Time at which the fragment was stored in UTC timestamp.      |
| `session_id`      | `TEXT NOT NULL`, foreign key | Owning `sessions.session_id`. The row is deleted if its session is deleted. |
| `sequence_number` | `INTEGER NOT NULL`           | Zero-based arrival sequence within the session. Together with `session_id`, this value is unique. |
| `role`            | `TEXT NOT NULL`, checked     | Speaker represented by the fragment. Only `user` and `assistant` are accepted. |
| `event_id`        | `TEXT`, nullable             | Event identifier supplied by the service, when available. It is provenance rather than the table primary key. |
| `start_ms`        | `INTEGER NOT NULL`           | Start offset on the session timeline in milliseconds.        |
| `end_ms`          | `INTEGER NOT NULL`           | End offset on the session timeline in milliseconds.          |
| `text`            | `TEXT NOT NULL`              | Transcript delta exactly as received. Adjacent fragments are not normalised at this table. |

The `transcript_session_sequence` index covers `(session_id, sequence_number)` for efficient ordered reconstruction.

### `turns`

Turns are convenient dialogue units reconstructed from adjacent fragments having the same role. They are the source used for recent-conversation context, history searches and memory summaries.

| Field                     | Type and constraints         | Meaning                                                      |
| ------------------------- | ---------------------------- | ------------------------------------------------------------ |
| `turn_id`                 | `TEXT PRIMARY KEY`           | UUIDv4 identifier for the reconstructed turn.                |
| `created_at_ns`           | `INTEGER NOT NULL`           | Time at which the turn record was assembled in Unix nanoseconds. |
| `created_at_utc`          | `TEXT NOT NULL`              | The same assembly time in UTC timestamp.                     |
| `session_id`              | `TEXT NOT NULL`, foreign key | Owning `sessions.session_id`. The row is deleted if its session is deleted. |
| `sequence_number`         | `INTEGER NOT NULL`           | Zero-based turn order within the session. Together with `session_id`, this value is unique. |
| `role`                    | `TEXT NOT NULL`, checked     | Speaker for the whole turn. Only `user` and `assistant` are accepted. |
| `start_ms`                | `INTEGER NOT NULL`           | Earliest timeline offset covered by the turn in milliseconds. |
| `end_ms`                  | `INTEGER NOT NULL`           | Latest timeline offset covered by the turn in milliseconds.  |
| `start_fragment_sequence` | `INTEGER NOT NULL`           | First `transcript_fragments.sequence_number` incorporated into the turn. A value of `-1` can be used for a turn without fragment provenance. |
| `end_fragment_sequence`   | `INTEGER NOT NULL`           | Last fragment sequence incorporated into the turn. A value of `-1` can be used for a turn without fragment provenance. |
| `completion_status`       | `TEXT NOT NULL`              | How the turn was assembled. Values are `complete` during normal operation and `recovered` when rebuilt after an interrupted process. |
| `text`                    | `TEXT NOT NULL`              | Complete reconstructed turn text, with outer whitespace removed. |

The `turns_session_sequence` index covers `(session_id, sequence_number)`. Each turn is also inserted into `turns_fts` for full-text search.

### `turns_fts`

This FTS5 virtual table supports conversation history lookup. BM25 relevance scores are calculated when a query is run and are not stored as fields.

| Field | FTS treatment | Meaning |
| --- | --- | --- |
| `turn_id` | `UNINDEXED` | Identifier used to join a result back to `turns.turn_id`. |
| `text` | Indexed with `unicode61` | Searchable turn text. |

`turns_fts` is maintained alongside `turns`. It is a search structure, not a source of conversation data.

### `delegations`

A delegation is a request for work that the service handed to the responses backend. The complete backend answer is stored before it is divided into any smaller commentary appends.

| Field                    | Type and constraints         | Meaning                                                      |
| ------------------------ | ---------------------------- | ------------------------------------------------------------ |
| `delegation_id`          | `TEXT PRIMARY KEY`           | UUIDv4 identifier. This is the parent key for `tool_runs`.   |
| `created_at_ns`          | `INTEGER NOT NULL`           | Time at which backend work was recorded as starting in Unix nanoseconds. |
| `created_at_utc`         | `TEXT NOT NULL`              | The same creation time in UTC timestamp.                     |
| `updated_at_ns`          | `INTEGER NOT NULL`           | Time of the most recent delegation state update in Unix nanoseconds. |
| `updated_at_utc`         | `TEXT NOT NULL`              | The same update time in UTC timestamp.                       |
| `session_id`             | `TEXT NOT NULL`, foreign key | Owning `sessions.session_id`. The row is deleted if its session is deleted. |
| `external_delegation_id` | `TEXT NOT NULL`              | Delegation identifier assigned by the service. Together with `session_id`, this value is unique. It is reused for result commentary sent back to the service. |
| `offset_ms`              | `INTEGER NOT NULL`           | Timeline position in milliseconds, at which the delegation was created. |
| `status`                 | `TEXT NOT NULL`              | Backend lifecycle state. Values are `running`, `complete`, `failed` and `superseded`. |
| `request_text`           | `TEXT NOT NULL`              | User request inferred from the transcript at the delegation offset. |
| `response_text`          | `TEXT`, nullable             | Complete backend response. This remains the complete retained result even when the delivery has to be split into smaller chunks. |
| `error_text`             | `TEXT`, nullable             | Error message recorded when backend work fails.              |
| `latency_ms`             | `INTEGER`, nullable          | Elapsed backend processing time in milliseconds.             |

The `delegations_session` index covers `(session_id, created_at_ns)` for ordered per-session access.

### `tool_runs`

This table records application-owned function tools executed by the backend, such as local knowledge search, excerpt reading and conversation-history search. Hosted web search is performed by the Responses service and is not represented as a `tool_runs` row.

| Field            | Type and constraints         | Meaning                                                      |
| ---------------- | ---------------------------- | ------------------------------------------------------------ |
| `tool_run_id`    | `TEXT PRIMARY KEY`           | UUIDv4 identifier for the tool invocation.                   |
| `created_at_ns`  | `INTEGER NOT NULL`           | Time at which the invocation was recorded as starting in Unix nanoseconds. |
| `created_at_utc` | `TEXT NOT NULL`              | The same creation time in UTC timestamp.                     |
| `updated_at_ns`  | `INTEGER NOT NULL`           | Time of the most recent tool state update in Unix nanoseconds. |
| `updated_at_utc` | `TEXT NOT NULL`              | The same update time in UTC timestamp.                       |
| `delegation_id`  | `TEXT NOT NULL`, foreign key | Owning `delegations.delegation_id`. The row is deleted if its delegation is deleted. |
| `tool_name`      | `TEXT NOT NULL`              | Function-tool name requested by the backend.                 |
| `arguments_json` | `TEXT NOT NULL`              | Tool arguments serialised as JSON. Invalid incoming arguments are retained under an `unparsed` member before failure is recorded. |
| `result_json`    | `TEXT`, nullable             | Successful tool result serialised as JSON. It remains null for a running or failed invocation without a result. |
| `status`         | `TEXT NOT NULL`              | Tool lifecycle state. Values are `running`, `complete` and `failed`. |
| `error_text`     | `TEXT`, nullable             | Parsing, dispatch or execution error when the tool fails.    |

### `memory_summaries`

Memory summaries are cumulative snapshots used to carry salient information across sessions without submitting the entire transcript every time. They are derived data, but they are kept with durable conversation history rather than with the disposable knowledge index.

| Field               | Type and constraints | Meaning                                                      |
| ------------------- | -------------------- | ------------------------------------------------------------ |
| `summary_id`        | `TEXT PRIMARY KEY`   | UUIDv4 identifier for the summary snapshot.                  |
| `created_at_ns`     | `INTEGER NOT NULL`   | Time at which the summary was stored in Unix nanoseconds.    |
| `created_at_utc`    | `TEXT NOT NULL`      | The same creation time in UTC timestamp.                     |
| `updated_at_ns`     | `INTEGER NOT NULL`   | Update timestamp in Unix nanoseconds. Summaries are inserted as immutable snapshots, so this initially equals `created_at_ns`. |
| `updated_at_utc`    | `TEXT NOT NULL`      | The same update time in UTC timestamp.                       |
| `through_turn_ns`   | `INTEGER NOT NULL`   | `created_at_ns` boundary of the latest turn covered by this summary. |
| `text`              | `TEXT NOT NULL`      | Natural language summary of long-term memory.                |
| `source_turn_count` | `INTEGER NOT NULL`   | Number of recent `turns` rows selected for summary generation before the assembled transcript was limited to its final 30,000 characters (up to 100). An incorporated previous summary represents previous turns. |

The latest summary is the row with the greatest `through_turn_ns` value.

### FTS5 tables

| Table               | Fields created by SQLite | Purpose                                                   |
| ------------------- | ------------------------ | --------------------------------------------------------- |
| `turns_fts_config`  | `k`, `v`                 | FTS5 configuration values.                                |
| `turns_fts_content` | `id`, `c0`, `c1`         | Stored FTS content corresponding to `turn_id` and `text`. |
| `turns_fts_data`    | `id`, `block`            | Encoded full-text index blocks.                           |
| `turns_fts_docsize` | `id`, `sz`               | Encoded per-document token size information.              |
| `turns_fts_idx`     | `segid`, `term`, `pgno`  | Segment and term index used to find FTS data pages.       |

## Knowledge index database

This database is an index of text files at the knowledge directory. It can be deleted and reconstructed with `--rebuild-knowledge`.

The database is tied to one resolved knowledge directory path through `database_metadata.knowledge_root`. Reusing it with a different knowledge directory is rejected so that paths and indexed content cannot be confused.

### Relationship summary

| Parent | Child | Relationship |
| --- | --- | --- |
| `documents` | `chunks` | One indexed document may have many overlapping, line-addressable chunks. Deleting a document cascades to its chunks. |
| `chunks` | `chunks_fts` | Each chunk has a corresponding FTS5 row linked by `chunk_id`. This association is maintained by the application rather than by a foreign key. |

`index_runs` records scans of the whole knowledge directory and is not attached to individual document rows.

### Table summary

| Table               | Kind               | Purpose                                                      |
| ------------------- | ------------------ | ------------------------------------------------------------ |
| `database_metadata` | Ordinary table     | Stores database-format metadata and the knowledge directory represented by the index. |
| `index_runs`        | Ordinary table     | Records each indexing scan and its outcome.                  |
| `documents`         | Ordinary table     | Stores identity and source file metadata for each indexed file. |
| `chunks`            | Ordinary table     | Stores bounded, overlapping text sections with line provenance. |
| `chunks_fts`        | FTS5 virtual table | Provides BM25 search over paths and chunk contents.          |

### `database_metadata`

Normally this table contains exactly one row.

| Field            | Type and constraints | Meaning                                                      |
| ---------------- | -------------------- | ------------------------------------------------------------ |
| `metadata_id`    | `TEXT PRIMARY KEY`   | UUIDv4 identifier for the metadata record.                   |
| `created_at_ns`  | `INTEGER NOT NULL`   | Database creation time in Unix nanoseconds.                  |
| `created_at_utc` | `TEXT NOT NULL`      | Database creation time in UTC timestamp.                     |
| `schema_version` | `INTEGER NOT NULL`   | Database-format compatibility marker used by the application. |
| `knowledge_root` | `TEXT NOT NULL`      | Absolute, resolved path of the knowledge directory represented by this database. |

### `index_runs`

One row is created every time the knowledge directory is scanned, including scans that find no changed files.

| Field            | Type and constraints | Meaning                                                      |
| ---------------- | -------------------- | ------------------------------------------------------------ |
| `index_run_id`   | `TEXT PRIMARY KEY`   | UUIDv4 identifier for the scan.                              |
| `created_at_ns`  | `INTEGER NOT NULL`   | Scan start time in Unix nanoseconds.                         |
| `created_at_utc` | `TEXT NOT NULL`      | The same start time in UTC timestamp.                        |
| `updated_at_ns`  | `INTEGER NOT NULL`   | Time at which the latest scan state was stored, normally the finish or failure time. |
| `updated_at_utc` | `TEXT NOT NULL`      | The same update time in UTC timestamp.                       |
| `status`         | `TEXT NOT NULL`      | Scan lifecycle state. Values are `running`, `complete` and `failed`. |
| `indexed_count`  | `INTEGER NOT NULL`   | Number of new or changed files indexed during the scan. Unchanged files are not counted. |
| `skipped_count`  | `INTEGER NOT NULL`   | Number of otherwise eligible file paths skipped because they could not safely be read or validated. |
| `removed_count`  | `INTEGER NOT NULL`   | Number of previously indexed documents removed because they disappeared, became ineligible or failed revalidation. |
| `warnings_json`  | `TEXT NOT NULL`      | JSON array of human-readable file warnings, or a scan-level error for a failed run. |

### `documents`

Each row represents one indexed source file. Hidden files, symbolic links, excluded credential or key files, invalid UTF-8 files, files containing NUL bytes and files larger than 5 MiB are not retained here.

| Field            | Type and constraints   | Meaning                                                      |
| ---------------- | ---------------------- | ------------------------------------------------------------ |
| `document_id`    | `TEXT PRIMARY KEY`     | Stable UUIDv4 identifier for the document. It is retained when an existing file is reindexed. |
| `created_at_ns`  | `INTEGER NOT NULL`     | Time at which the path was first indexed in Unix nanoseconds. |
| `created_at_utc` | `TEXT NOT NULL`        | The same first-indexed time in UTC timestamp.                |
| `updated_at_ns`  | `INTEGER NOT NULL`     | Time at which the stored file metadata and chunks were last replaced in Unix nanoseconds. |
| `updated_at_utc` | `TEXT NOT NULL`        | The same update time in UTC timestamp.                       |
| `relative_path`  | `TEXT NOT NULL UNIQUE` | POSIX-style path relative to `knowledge_root`. This is the provenance path returned with search results. |
| `size_bytes`     | `INTEGER NOT NULL`     | Source file size measured before indexing.                   |
| `modified_at_ns` | `INTEGER NOT NULL`     | Source file modification time supplied by the filesystem in Unix nanoseconds. Size and modification time are used to avoid rereading unchanged files. |
| `content_hash`   | `TEXT NOT NULL`        | Lowercase hexadecimal SHA-256 digest of the complete source file bytes. This is a per-file digest, not a digest of the knowledge root. |
| `line_count`     | `INTEGER NOT NULL`     | Number of logical text lines found after UTF-8 or UTF-8-with-BOM decoding. |

### `chunks`

Source text is divided into line-addressable sections targeted at approximately 4,000 characters, with approximately 400 characters of overlap to bound retrieval context. Blank lines and Markdown headings are preferred as boundaries. Reindexing a changed document replaces all of its chunk rows.

| Field            | Type and constraints         | Meaning                                                      |
| ---------------- | ---------------------------- | ------------------------------------------------------------ |
| `chunk_id`       | `TEXT PRIMARY KEY`           | UUIDv4 identifier for the generated chunk. New identifiers are assigned when document chunks are rebuilt. |
| `created_at_ns`  | `INTEGER NOT NULL`           | Time at which the chunk was generated in Unix nanoseconds.   |
| `created_at_utc` | `TEXT NOT NULL`              | The same creation time in UTC timestamp.                     |
| `updated_at_ns`  | `INTEGER NOT NULL`           | Chunk update time in Unix nanoseconds. Chunks are replaced rather than updated, so this initially equals `created_at_ns`. |
| `updated_at_utc` | `TEXT NOT NULL`              | The same update time in UTC timestamp.                       |
| `document_id`    | `TEXT NOT NULL`, foreign key | Owning `documents.document_id`. The row is deleted if its document is deleted. |
| `chunk_number`   | `INTEGER NOT NULL`           | Zero-based order within the document. Together with `document_id`, this value is unique. |
| `start_line`     | `INTEGER NOT NULL`           | One-based first source line represented by the chunk.        |
| `end_line`       | `INTEGER NOT NULL`           | One-based inclusive last source line represented by the chunk. |
| `content`        | `TEXT NOT NULL`              | Searchable source text for the chunk, with outer whitespace removed. Overlap means some source text can occur in adjacent chunks. |

The `chunks_document` index covers `(document_id, chunk_number)` for efficient ordered access.

### `chunks_fts`

This FTS5 virtual table supports BM25-ranked local-knowledge searches. Scores are calculated at query time and are not stored fields.

|Field          |FTS treatment           |Meaning                                                                                          |
|---------------|------------------------|-------------------------------------------------------------------------------------------------|
|`chunk_id`     |`UNINDEXED`             |Identifier used to join a result back to `chunks.chunk_id`.                                      |
|`relative_path`|Indexed with `unicode61`|Searchable source-relative path, allowing filenames and directory names to contribute to matches.|
|`content`      |Indexed with `unicode61`|Searchable chunk text.                                                                           |

Search results are joined back to `chunks` and `documents` to obtain line ranges and authoritative path metadata. `chunks_fts` should be treated as disposable search infrastructure rather than source content.

### FTS5 tables

SQLite creates the following tables to implement `chunks_fts`. They are internal and not intended for direct editing.

| Table                | Fields created by SQLite | Purpose                                                      |
| -------------------- | ------------------------ | ------------------------------------------------------------ |
| `chunks_fts_config`  | `k`, `v`                 | FTS5 configuration values.                                   |
| `chunks_fts_content` | `id`, `c0`, `c1`, `c2`   | Stored FTS content corresponding to `chunk_id`, `relative_path` and `content`. |
| `chunks_fts_data`    | `id`, `block`            | Encoded full-text index blocks.                              |
| `chunks_fts_docsize` | `id`, `sz`               | Encoded per-document token size information.                 |
| `chunks_fts_idx`     | `segid`, `term`, `pgno`  | Segment and term index used to find FTS data pages.          |

## Deletion and recovery implications

Deleting the conversation history database removes the authoritative record of prior sessions, transcripts, delegations, retained backend responses and memory summaries. It should therefore be backed up as durable user data.

Deleting the knowledge index database removes only derived metadata and search structures. The index can be reconstructed from the knowledge directory with `--rebuild-knowledge`. Deleting source files from the knowledge directory, by contrast, causes their `documents`, `chunks` and FTS rows to be removed during the next scan.

If a process ends while a conversation session is marked `starting` or `active`, the next opening of the history database reconstructs the session `turns` and `turns_fts` rows from the lossless `transcript_fragments`, then marks the session as `interrupted`, and the SQLite write-ahead log protects transactions that were committed before the abrupt exit.
