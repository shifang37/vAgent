import inspect

import pytest
from langchain_core.messages import AIMessage

from vagent.storage import FileStore


@pytest.fixture
def store(tmp_path):
    with FileStore.open(tmp_path / "state") as instance:
        yield instance


def tool_call(name, args=None, call_id="call-1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": call_id}])


class ScriptedModel:
    name = "scripted-test"

    def __init__(self, callback):
        self.callback = callback
        self.calls = 0
        self.inputs = []

    async def generate(self, messages, tools):
        step = self.calls
        self.calls += 1
        self.inputs.append(messages)
        result = self.callback(messages, step)
        return await result if inspect.isawaitable(result) else result
