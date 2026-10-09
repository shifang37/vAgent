"""Cancellable console input without a blocking executor thread at process exit."""

import asyncio
import sys
import unicodedata


def _erase(character):
    width = (
        0 if unicodedata.combining(character) else 2 if unicodedata.east_asian_width(character) in "WF" else 1
    )
    print("\b \b" * width, end="", flush=True)


async def _windows_line(console):
    characters = []
    extended = False
    surrogate = ""
    while True:
        if not console.kbhit():
            await asyncio.sleep(0.03)
            continue
        character = console.getwch()
        if extended:
            extended = False
            continue
        if character in {"\x00", "\xe0"}:
            extended = True
        elif character == "\x03":
            print(flush=True)
            raise asyncio.CancelledError
        elif character in {"\x04", "\x1a"}:
            print(flush=True)
            raise EOFError
        elif character in {"\r", "\n"}:
            print(flush=True)
            return "".join(characters)
        elif character == "\b":
            if characters:
                _erase(characters.pop())
        elif character == "\x15":
            while characters:
                _erase(characters.pop())
        elif "\ud800" <= character <= "\udbff":
            surrogate = character
        elif "\udc00" <= character <= "\udfff":
            if surrogate:
                character = (surrogate + character).encode("utf-16", "surrogatepass").decode("utf-16")
                surrogate = ""
                characters.append(character)
                print(character, end="", flush=True)
        elif character.isprintable():
            characters.append(character)
            print(character, end="", flush=True)
        # Pasted input can stay available for a long time; yield for Worker and stop.
        await asyncio.sleep(0)


async def read_prompt(prompt):
    print(prompt, end="", flush=True)
    if sys.platform == "win32":
        import msvcrt

        return await _windows_line(msvcrt)
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def readable():
        if not ready.done():
            ready.set_result(None)

    descriptor = sys.stdin.fileno()
    loop.add_reader(descriptor, readable)
    try:
        await ready
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        return line.rstrip("\r\n")
    finally:
        loop.remove_reader(descriptor)
