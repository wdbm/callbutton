"""
Responses backend and its bounded read-only tools

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

import asyncio
import json
from typing import Any

from .async_utils import run_blocking

BACKEND_INSTRUCTIONS = (
    "Answer the caller's delegated request accurately and concisely for spoken delivery. "
    "Use local knowledge when it is relevant and search it before claiming that local "
    "information is unavailable. Use conversation history when the caller refers to prior "
    "sessions. Use web search whenever current, changing, or externally sourced information "
    "is needed and web search is available. Distinguish retrieved facts from inference. "
    "Mention a concise source path or web source when useful. Do not add conversational filler."
)
WEB_SEARCH_TOOL = {"type": "web_search"}


LOCAL_KNOWLEDGE_TOOL = {
    "type": "function",
    "name": "search_local_knowledge",
    "description": "Search indexed text files from the configured local knowledge directory.",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["query", "limit"],
        "additionalProperties": False,
    },
}
READ_EXCERPT_TOOL = {
    "type": "function",
    "name": "read_local_excerpt",
    "description": "Read a bounded line range from an eligible local knowledge text file.",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        "required": ["path", "start_line", "end_line"],
        "additionalProperties": False,
    },
}
HISTORY_SEARCH_TOOL = {
    "type": "function",
    "name": "search_conversation_history",
    "description": "Search the durable transcript from current and earlier conversations.",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["query", "limit"],
        "additionalProperties": False,
    },
}


class BackendAgent:
    def __init__(
        self,
        client,
        model,
        timezone,
        history_store,
        knowledge_store,
        executor,
        web_search=True,
    ):
        self.client = client
        self.model = model
        self.timezone = timezone
        self.history_store = history_store
        self.knowledge_store = knowledge_store
        self.executor = executor
        self.web_search = web_search

    @property
    def tools(self):
        tools = [LOCAL_KNOWLEDGE_TOOL, READ_EXCERPT_TOOL, HISTORY_SEARCH_TOOL]
        if self.web_search:
            tools.append(WEB_SEARCH_TOOL)
        return tools

    async def _context(self, request_text: str) -> str:
        recent, summary = await asyncio.gather(
            run_blocking(self.executor, self.history_store.recent_turns, 24),
            run_blocking(self.executor, self.history_store.latest_summary),
        )
        sections = []
        if summary:
            sections.append(f"Long-term memory summary:\n{summary['text'][:8_000]}")
        if recent:
            transcript = "\n".join(
                f"{item['role']}: {item['text']}" for item in recent if item["text"].strip()
            )
            sections.append(f"Recent conversation transcript:\n{transcript[-16_000:]}")
        sections.append(f"Delegated request:\n{request_text[-8_000:]}")
        return "\n\n".join(sections)

    async def answer(self, delegation_id: str, request_text: str) -> str:
        context = await self._context(request_text)
        input_items: list[Any] = [{"role": "user", "content": context}]
        for _ in range(8):
            response = await self.client.responses.create(
                model=self.model,
                instructions=(
                    f"{BACKEND_INSTRUCTIONS} The caller's timezone is {self.timezone}."
                ),
                input=input_items,
                tools=self.tools,
                tool_choice="auto",
                parallel_tool_calls=False,
                reasoning={"effort": "low"},
                store=False,
            )
            calls = [item for item in response.output if item.type == "function_call"]
            if not calls:
                answer = response.output_text.strip()
                if not answer:
                    raise RuntimeError("the backend returned no spoken result")
                return answer
            input_items.extend(response.output)
            for call in calls:
                output = await self._run_tool(delegation_id, call.name, call.arguments)
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": json.dumps(output, ensure_ascii=False),
                    }
                )
        raise RuntimeError("the backend exceeded the local tool-call limit")

    async def _run_tool(self, delegation_id: str, name: str, arguments_json: str):
        try:
            arguments = json.loads(arguments_json)
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            arguments = {"unparsed": arguments_json}
            tool_run_id = await run_blocking(
                self.executor,
                self.history_store.start_tool_run, delegation_id, name, arguments
            )
            await run_blocking(
                self.executor,
                self.history_store.finish_tool_run, tool_run_id, "failed", None, str(exc)
            )
            return {"error": str(exc)}

        tool_run_id = await run_blocking(
            self.executor,
            self.history_store.start_tool_run, delegation_id, name, arguments
        )
        try:
            if name == "search_local_knowledge":
                result = await run_blocking(
                    self.executor,
                    self.knowledge_store.search,
                    str(arguments.get("query", "")),
                    arguments.get("limit", 5),
                )
            elif name == "read_local_excerpt":
                result = await run_blocking(
                    self.executor,
                    self.knowledge_store.read_excerpt,
                    str(arguments.get("path", "")),
                    arguments.get("start_line", 1),
                    arguments.get("end_line", 50),
                )
            elif name == "search_conversation_history":
                result = await run_blocking(
                    self.executor,
                    self.history_store.search_turns,
                    str(arguments.get("query", "")),
                    arguments.get("limit", 5),
                )
            else:
                raise ValueError(f"unknown local tool {name!r}")
            await run_blocking(
                self.executor,
                self.history_store.finish_tool_run, tool_run_id, "complete", result, None
            )
            return result
        except Exception as exc:
            await run_blocking(
                self.executor,
                self.history_store.finish_tool_run, tool_run_id, "failed", None, str(exc)
            )
            return {"error": str(exc)}

    async def update_summary_if_needed(self):
        summary = await run_blocking(self.executor, self.history_store.latest_summary)
        through = 0 if summary is None else int(summary["through_turn_ns"])
        count = await run_blocking(
            self.executor, self.history_store.count_turns_after, through
        )
        if count < 40:
            return None
        turns = await run_blocking(self.executor, self.history_store.recent_turns, 100)
        transcript = "\n".join(f"{item['role']}: {item['text']}" for item in turns)
        previous = "" if summary is None else summary["text"]
        response = await self.client.responses.create(
            model=self.model,
            instructions=(
                "Create a compact factual memory summary for future conversations. Preserve "
                "stable preferences, commitments, names and unresolved work. Do not invent facts."
            ),
            input=f"Previous summary:\n{previous}\n\nRecent transcript:\n{transcript[-30_000:]}",
            max_output_tokens=800,
            store=False,
        )
        text = response.output_text.strip()
        if not text:
            return None
        latest_timestamp = await run_blocking(
            self.executor, self.history_store.latest_turn_timestamp
        )
        return await run_blocking(
            self.executor,
            self.history_store.add_summary, latest_timestamp, text, len(turns)
        )
