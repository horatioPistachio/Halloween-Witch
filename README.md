# Halloween-Witch

MVP voice loop for an animatronic witch: microphone -> OpenAI Realtime (`gpt-realtime-2.1-mini`, text out) -> tag parser -> console, with cost per interaction.

See [.prompts/design-document-witch-MVP.prompt.md](.prompts/design-document-witch-MVP.prompt.md) for the design.

## Setup (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env   # then set OPENAI_API_KEY
```

## Run

```powershell
python -m witch --list-devices
python -m witch                 # default microphone
python -m witch --device "USB" # select by name substring
python -m witch --help
```

Stop with Ctrl+C. Each reply prints its cost and the run total. Every interaction is also appended to `usage.jsonl` (set `WITCH_USAGE_LOG=` to disable).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | required | API key (read from `.env` or environment) |
| `WITCH_MODEL` | `gpt-realtime-2.1-mini` | Realtime model |
| `WITCH_INPUT_DEVICE` | system default | Device index or name substring |
| `WITCH_VAD` | `semantic_vad` | `semantic_vad` or `server_vad` |
| `WITCH_REASONING_EFFORT` | `low` | `minimal` to `xhigh`; lower is faster and cheaper |
| `WITCH_MAX_OUTPUT_TOKENS` | `600` | Per-reply cap on output tokens |
| `WITCH_USAGE_LOG` | `usage.jsonl` | Per-interaction cost log |
| `WITCH_LOG_LEVEL` | `INFO` | Logging level |

## Tests

```powershell
pytest
```