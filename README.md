# APE A2A Agent

A Python agent for the [Agent Programming Exercise](https://ape.llm.phd/). It exposes an A2A 0.3 agent card and a JSON-RPC endpoint using the official Python A2A SDK. The agent passed all six APE checks in a local Windows run.

| Capability | Implementation |
| --- | --- |
| General QA | OpenAI Responses API |
| Tool usage | Model-selected hash chain computed with Python's `hashlib` |
| Image understanding | A2A image attachments passed to the model, with the answer in completed task history |
| Web browsing | Playwright drives Microsoft Edge through the live tic-tac-toe game |
| Code generation and execution | OpenAI-hosted Python Code Interpreter |
| Memory | SQLite stores number pairs across A2A sessions and server restarts |

## Run locally

Use Windows with Python 3.13, Microsoft Edge, and an OpenAI API key with access to GPT-6 Luna. From the repository folder in PowerShell, install the dependencies.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Set the configuration and start the server in the same PowerShell tab.

```powershell
$env:AGENT_MODE = 'qa'
$env:OPENAI_MODEL = 'gpt-6-luna'
$env:APE_ORIGIN = 'https://ape.llm.phd'
$key = Read-Host 'OpenAI API key' -AsSecureString
$env:OPENAI_API_KEY = [System.Net.NetworkCredential]::new('', $key).Password
.\.venv\Scripts\python.exe -m uvicorn agent:app --host 127.0.0.1 --port 8000
```

Open [APE](https://ape.llm.phd/), enter `http://127.0.0.1:8000/` as the service URL, and leave the authorization token blank. Run an individual check or select **Run All**. The agent card is at `http://127.0.0.1:8000/.well-known/agent-card.json`. The code execution check incurs hosted Code Interpreter charges in addition to model tokens.

The server creates `ape_memory.sqlite3` beside the source when the memory workflow first runs. Keep this file local. Echo mode is available by setting `AGENT_MODE` to `echo` before starting the server.
