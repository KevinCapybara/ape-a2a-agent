import asyncio
import hashlib
import json
import logging
import re
from os import getenv
from pathlib import Path
from mimetypes import guess_type
from urllib.parse import urlsplit

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    FilePart,
    FileWithBytes,
    InternalError,
    Message,
    Task,
    TaskState,
    TaskStatus,
    UnsupportedOperationError,
)
from a2a.utils import new_agent_text_message
from a2a.utils.errors import ServerError
from openai import APIError, AsyncOpenAI
from openai.types.responses import ResponseInputContentParam
from playwright.async_api import Error as BrowserError
from starlette.middleware.cors import CORSMiddleware

from browser_game import GAME_URL, win_tic_tac_toe
from number_memory import NumberMemory

logger = logging.getLogger("uvicorn.error")
number_memory = NumberMemory(Path(__file__).with_name("ape_memory.sqlite3"))

IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
CODE_REQUEST = re.compile(r"\b(?:write|generate|run|execute)\s+(?:(?:a|the|some|python)\s+)*(?:program|script|code)\b", re.IGNORECASE)
HASH_ALGORITHMS = ("md5", "sha1", "sha224", "sha256", "sha384", "sha512")
HASH_TOOL = {
    "type": "function",
    "name": "hash_chain",
    "description": "Compute the final hexadecimal digest after applying one or more hashes in order.",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Exact original text, preserving whitespace."},
            "algorithms": {
                "type": "array",
                "items": {"type": "string", "enum": list(HASH_ALGORITHMS)},
                "description": "Hash names in execution order, including repetitions.",
            },
        },
        "required": ["text", "algorithms"],
        "additionalProperties": False,
    },
}


def hash_chain(text: str, algorithms: list[str]) -> str:
    if (
        not isinstance(text, str)
        or not isinstance(algorithms, list)
        or not algorithms
        or any(algorithm not in HASH_ALGORITHMS for algorithm in algorithms)
    ):
        raise ValueError("Invalid hash chain arguments.")

    digest = text.encode("utf-8")
    for algorithm in algorithms:
        digest = hashlib.new(algorithm, digest).hexdigest().encode("ascii")
    return digest.decode("ascii")


def image_inputs(message: Message | None) -> list[ResponseInputContentParam]:
    images: list[ResponseInputContentParam] = []
    for part in message.parts if message else []:
        if not isinstance(part.root, FilePart):
            continue
        file = part.root.file
        if isinstance(file, FileWithBytes):
            mime_type = file.mime_type or guess_type(file.name or "")[0]
            if mime_type not in IMAGE_TYPES:
                continue
            url = f"data:{mime_type};base64,{file.bytes}"
        else:
            url = file.uri
            if url.startswith("data:"):
                if not url.startswith(tuple(f"data:{mime};base64," for mime in IMAGE_TYPES)):
                    continue
            elif urlsplit(url).scheme not in {"http", "https"} or (
                file.mime_type and file.mime_type not in IMAGE_TYPES
            ):
                continue
        images.append({"type": "input_image", "image_url": url, "detail": "low"})
    return images


class TextExecutor(AgentExecutor):
    def __init__(self, api_key: str | None, model: str) -> None:
        self.api_key = api_key
        self.model = model

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        text = context.get_user_input()
        images = image_inputs(context.message) if self.api_key else []
        if images:
            reply = await self.answer_question(text or "Identify the main subject in the image.", images)
            await event_queue.enqueue_event(
                Task(
                    id=context.task_id,
                    context_id=context.context_id,
                    status=TaskStatus(state=TaskState.completed),
                    history=[
                        new_agent_text_message(
                            reply, context_id=context.context_id, task_id=context.task_id
                        )
                    ],
                )
            )
            return
        elif not text.strip():
            reply = "Send me a text message."
        elif self.api_key and GAME_URL in text and "tic-tac-toe" in text.lower():
            try:
                reply = await win_tic_tac_toe()
            except (BrowserError, RuntimeError, TimeoutError) as error:
                logger.error("Tic-tac-toe browser task failed (%s).", type(error).__name__)
                raise ServerError(error=InternalError(message="The browser could not complete the game.")) from None
        elif self.api_key:
            reply = await asyncio.to_thread(number_memory.answer, text)
            if reply is None:
                reply = await self.answer_question(text)
        else:
            reply = f"Echo: {text}"
        await event_queue.enqueue_event(
            new_agent_text_message(reply, context_id=context.context_id)
        )

    async def answer_question(
        self, question: str, images: list[ResponseInputContentParam] | None = None
    ) -> str:
        run_code = not images and bool(CODE_REQUEST.search(question))
        instructions = (
            "Answer the user's question directly and accurately. "
            "Say when you are unsure. Follow any requested answer format. "
            "When asked to compute a hash or hash chain, call hash_chain with the exact "
            "original text and all requested algorithms in order."
        )
        tools = [HASH_TOOL]
        if run_code:
            instructions = (
                "Write and execute Python using the Python tool. "
                "Return the computed result from the tool in the user's requested format."
            )
            tools = [{"type": "code_interpreter", "container": {"type": "auto", "memory_limit": "1g"}}]

        model_input = question
        if images:
            model_input = [{
                "role": "user",
                "content": [{"type": "input_text", "text": question}, *images],
            }]
        try:
            async with AsyncOpenAI(
                api_key=self.api_key, timeout=120 if run_code else 45, max_retries=0
            ) as client:
                response = await client.responses.create(
                    model=self.model,
                    instructions=instructions,
                    input=model_input,
                    tools=tools,
                    tool_choice="required" if run_code else "auto",
                    parallel_tool_calls=False,
                    store=False,
                )
        except APIError as error:
            logger.error("OpenAI request failed (%s).", type(error).__name__)
            raise ServerError(error=InternalError(message="The model request failed.")) from None

        if run_code and not any(
            item.type == "code_interpreter_call" and item.status == "completed"
            for item in response.output
        ):
            raise ServerError(error=InternalError(message="The model did not complete a Python run."))

        for item in response.output:
            if item.type == "function_call" and item.name == HASH_TOOL["name"]:
                try:
                    return hash_chain(**json.loads(item.arguments))
                except (TypeError, ValueError):
                    raise ServerError(error=InternalError(message="The hash tool failed.")) from None

        if not response.output_text.strip():
            raise ServerError(error=InternalError(message="The model returned no text."))
        return response.output_text

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ServerError(error=UnsupportedOperationError())


mode = getenv("AGENT_MODE", "echo")
if mode not in {"echo", "qa"}:
    raise RuntimeError("AGENT_MODE must be echo or qa.")

qa_enabled = mode == "qa"
api_key = getenv("OPENAI_API_KEY", "").strip() if qa_enabled else None
if qa_enabled and not api_key:
    raise RuntimeError("Set OPENAI_API_KEY before starting the agent in QA mode.")

card = AgentCard(
    name="APE QA Agent" if qa_enabled else "APE Echo Agent",
    description="Answers general questions." if qa_enabled else "Echoes text sent in an A2A message.",
    url=getenv("AGENT_URL", "http://127.0.0.1:8000/"),
    version="0.1.0",
    default_input_modes=["text/plain", *IMAGE_TYPES] if qa_enabled else ["text/plain"],
    default_output_modes=["text/plain"],
    capabilities=AgentCapabilities(streaming=False),
    skills=[
        AgentSkill(
            id="general_qa",
            name="General QA",
            description="Answers standalone text questions with a hosted language model.",
            tags=["text", "qa"],
            examples=["What causes the seasons on Earth?"],
        )
        if qa_enabled
        else AgentSkill(
            id="echo",
            name="Echo text",
            description="Returns the text from your message with an Echo prefix.",
            tags=["text", "echo"],
            examples=["Hello, APE!"],
        )
    ] + ([
        AgentSkill(
            id="hash_chain",
            name="Hash chain",
            description="Computes chained hashes of text and returns the final hexadecimal digest.",
            tags=["hashing", "tool-use"],
        ),
        AgentSkill(
            id="image_understanding",
            name="Image understanding",
            description="Identifies subjects and answers questions about attached images.",
            tags=["image", "vision"],
            input_modes=list(IMAGE_TYPES),
        ),
        AgentSkill(
            id="tic_tac_toe",
            name="Tic-tac-toe browsing",
            description="Plays the supported web game and reads the number displayed after a win.",
            tags=["browser", "tic-tac-toe"],
        ),
        AgentSkill(
            id="code_execution",
            name="Code generation and execution",
            description="Writes and runs Python in a hosted Code Interpreter container.",
            tags=["python", "code-execution"],
        ),
        AgentSkill(
            id="number_memory",
            name="Number pair memory",
            description="Remembers number pairs and recalls them across A2A sessions.",
            tags=["memory", "number-pairs"],
        ),
    ] if qa_enabled else []),
)

handler = DefaultRequestHandler(
    agent_executor=TextExecutor(api_key, getenv("OPENAI_MODEL", "gpt-5-mini")),
    task_store=InMemoryTaskStore(),
)
app = A2AStarletteApplication(agent_card=card, http_handler=handler).build()

ape_origin = getenv("APE_ORIGIN", "").strip()
if ape_origin:
    app = CORSMiddleware(
        app,
        allow_origins=[ape_origin],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
