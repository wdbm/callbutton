# Copyright (C) 2026 William Breaden Madden
# SPDX-License-Identifier: GPL-3.0-or-later

"""
################################################################################
#                                                                              #
# callbutton_memory                                                            #
#                                                                              #
################################################################################
#                                                                              #
# LICENCE INFORMATION                                                          #
#                                                                              #
# This program is a GPT-Live voice assistant with durable conversation history #
# and searchable local knowledgebase.                                          #
#                                                                              #
# copyright (C) 2026 William Breaden Madden                                    #
#                                                                              #
# This software is released under the terms of the GNU General Public License  #
# version 3 (GPLv3).                                                           #
#                                                                              #
# This program is free software: you can redistribute it and/or modify it      #
# under the terms of the GNU General Public License as published by the Free   #
# Software Foundation, either version 3 of the License, or (at your option)    #
# any later version.                                                           #
#                                                                              #
# This program is distributed in the hope that it will be useful, but WITHOUT  #
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or        #
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for     #
# more details.                                                                #
#                                                                              #
# For a copy of the GNU General Public License, see                            #
# <http://www.gnu.org/licenses>.                                               #
#                                                                              #
################################################################################

Usage:
    callbutton_memory [--input-device=<device>] [--output-device=<device>]
                      [--voice=<voice>] [--backend-model=<model>] [--transcripts]
                      [--history-db=<path>] [--knowledge-db=<path>]
                      [--knowledge-dir=<path>] [--rebuild-knowledge] [--no-web]
    callbutton_memory (-h | --help)
    callbutton_memory --version

Options:
    -h --help                   Show this help.
    --version                   Show the version.
    --input-device=<device>     Input device index or case-insensitive name fragment
    --output-device=<device>    Output device index or case-insensitive name fragment
    --voice=<voice>             Voice used for audio output [default: meridian]
    --backend-model=<model>     Responses model used for delegated work [default: gpt-5-mini]
    --transcripts               Print transcript fragments and long backend responses.
    --history-db=<path>         Conversation database [default: conversation_history.db]
    --knowledge-db=<path>       Knowledge index database [default: knowledge_index.db]
    --knowledge-dir=<path>      Knowledge source directory [default: knowledgebase]
    --rebuild-knowledge         Recreate the disposable knowledge index.
    --no-web                    Disable hosted web search.
"""

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import sys

from docopt import docopt

from . import __VERSION__


@dataclass(frozen=True)
class Config:
    input_device: str | None
    output_device: str | None
    voice: str
    backend_model: str
    transcripts: bool
    history_db: Path
    knowledge_db: Path
    knowledge_dir: Path
    rebuild_knowledge: bool
    web_search: bool
    timezone: str


def _resolved_path(value: str, working_directory: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = working_directory / path
    return path.resolve(strict=False)


def config_from_options(options, working_directory=None) -> Config:
    """
    Validate parsed options and resolve all data paths once.
    """
    working_directory = (working_directory or Path.cwd()).resolve()
    history_db = _resolved_path(options["--history-db"], working_directory)
    knowledge_db = _resolved_path(options["--knowledge-db"], working_directory)
    knowledge_dir = _resolved_path(options["--knowledge-dir"], working_directory)
    if history_db == knowledge_db:
        raise ValueError("the history and knowledge databases must be different files")
    if knowledge_dir.exists() and not knowledge_dir.is_dir():
        raise ValueError(f"the knowledge path is not a directory: {knowledge_dir}")
    return Config(
        input_device=options["--input-device"],
        output_device=options["--output-device"],
        voice=options["--voice"],
        backend_model=options["--backend-model"],
        transcripts=bool(options["--transcripts"]),
        history_db=history_db,
        knowledge_db=knowledge_db,
        knowledge_dir=knowledge_dir,
        rebuild_knowledge=bool(options["--rebuild-knowledge"]),
        web_search=not bool(options["--no-web"]),
        timezone=os.getenv("TZ", "America/Sao_Paulo"),
    )


def main() -> int:
    options = docopt(__doc__, version=__VERSION__)
    try:
        config = config_from_options(options)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY is not set.", file=sys.stderr)
        return 2
    from .app import async_main
    try:
        return asyncio.run(async_main(config))
    except KeyboardInterrupt:
        return 130
