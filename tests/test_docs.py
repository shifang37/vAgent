"""Documentation gate: opt-in switches, offline commands and corrected claims.

The switches below shipped in H1–H6 but were only described in acceptance notes. These
tests keep the user-facing docs and the sample environment file in sync, and prove the
Application layer actually passes the switches through to the runner (not only Config).
"""

from pathlib import Path

from conftest import ScriptedModel
from langchain_core.messages import AIMessage

from vagent.application import ApplicationService
from vagent.config import Config, load_config
from vagent.memory import MemoryIndex
from vagent.summary import ExtractiveSummarizer
from vagent.tracing import Tracer

ROOT = Path(__file__).resolve().parents[1]
SWITCHES = (
    "VAGENT_CONTEXT_VERSION",
    "VAGENT_MEMORY",
    "VAGENT_MEMORY_LIMIT",
    "VAGENT_PROMPT_VERSION",
    "VAGENT_TRACING",
    "VAGENT_TRACE_SAMPLE",
    "VAGENT_TRACE_CONTENT",
)
COMMANDS = (
    "vagent usage --cost",
    "scripts/evaluate_behavior.py",
    "scripts/compare_cost.py",
)


M3_COMMANDS = (
    "vagent gc",
    "vagent snapshot",
    "vagent recover --from last-good",
    "scripts/measure_state.py",
)


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_the_user_docs_document_every_opt_in_switch():
    combined = read("README.md") + read("docs/USER_GUIDE.md")
    assert [name for name in SWITCHES if f"`{name}`" not in combined] == []
    example = read(".env.example")
    assert [name for name in SWITCHES if f"# {name}=" not in example] == []


def test_the_user_docs_document_the_offline_commands_and_the_sixty_second_limit():
    combined = read("README.md") + read("docs/USER_GUIDE.md")
    assert [command for command in COMMANDS if command not in combined] == []
    assert "60 秒" in combined


def test_the_user_docs_document_the_m3_retention_commands():
    combined = read("README.md") + read("docs/USER_GUIDE.md")
    assert [command for command in M3_COMMANDS if command not in combined] == []
    assert "state.last-good.json" in combined and "16 MiB" in combined
    assert "60 秒" in combined


def test_the_single_model_call_limit_is_recorded_as_a_fixed_decision():
    from vagent.runner import MODEL_CALL_LIMIT_SECONDS

    assert MODEL_CALL_LIMIT_SECONDS == 60.0
    combined = read("README.md") + read("docs/USER_GUIDE.md")
    assert "MODEL_CALL_LIMIT_SECONDS" in combined
    assert "不是配置项" in combined


def test_the_corrected_claims_stay_corrected():
    readme = read("README.md")
    assert "没有自动摘要" not in readme
    assert "文本模型尚无价格表" not in readme

    h3 = read("docs/AGENT_HARNESS_H3_ACCEPTANCE.md")
    assert "一并覆盖" not in h3
    assert "不代表真实模型行为" in h3

    h6 = read("docs/AGENT_HARNESS_H6_ACCEPTANCE.md")
    assert "22.70%" in h6 and "ScriptedModel" in h6
    assert "真只读快照" in h6 or "不执行崩溃恢复" in h6


def respond(_messages, _step):
    return AIMessage(content="完成")


async def test_application_wires_the_environment_switches_to_the_runner(tmp_path):
    off = Config(home=tmp_path / "off", api_key=None)
    async with ApplicationService.open(off, model=ScriptedModel(respond)) as service:
        runner = service.runner()
        assert (runner.context.format_version, runner.prompt_version) == (2, "v1")
        assert runner.memory is None and runner.tracing is None

    config = load_config(
        {
            "VAGENT_HOME": str(tmp_path / "on"),
            "VAGENT_MEMORY": "on",
            "VAGENT_MEMORY_LIMIT": "8",
            "VAGENT_CONTEXT_VERSION": "3",
            "VAGENT_PROMPT_VERSION": "v2",
            "VAGENT_TRACING": "on",
            "VAGENT_TRACE_SAMPLE": "25",
            "VAGENT_TRACE_CONTENT": "on",
        }
    )
    async with ApplicationService.open(config, model=ScriptedModel(respond)) as service:
        runner = service.runner()
        assert runner.context.format_version == 3
        assert isinstance(runner.summarizer, ExtractiveSummarizer)
        assert runner.prompt_version == "v2"
        assert isinstance(runner.memory, MemoryIndex) and runner.memory_limit == 8
        assert isinstance(runner.tracing, Tracer)
        assert (runner.tracing.sample_percent, runner.tracing.content) == (25, True)
