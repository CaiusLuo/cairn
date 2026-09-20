from cairn.events import Event


def test_event():
    event = Event(
        type="tool_call",
        data={
            "tool": "bash",
            "arguments": {
                "command": "pwd"
            },
        },
    )

    assert event.type == "tool_call"
    assert event.data["tool"] == "bash"