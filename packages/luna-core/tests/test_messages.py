import base64

import pytest
from luna_core.messages import (
    Image,
    Message,
    Text,
    Thinking,
    ToolCall,
    ToolResult,
    image_from_file,
    message_from_dict,
    message_to_dict,
)


def test_user_message_with_text_and_image():
    img = Image("image/png", "AAAA")
    msg = Message.user("look", images=[img])
    assert msg.role == "user"
    assert msg.content == [Text("look"), img]
    assert msg.text == "look"


def test_assistant_text_and_tool_calls_properties():
    call = ToolCall("c1", "read_file", {"file_path": "a.py"})
    msg = Message.assistant([Thinking("hm", signature="sig"), Text("ok "), Text("go"), call])
    assert msg.text == "ok go"
    assert msg.tool_calls == [call]


def test_tool_results_message():
    res = ToolResult("c1", (Text("contents"),), name="read_file")
    msg = Message.tool_results([res])
    assert msg.role == "tool"
    assert msg.results == [res]
    assert res.text == "contents"


@pytest.mark.parametrize(
    "msg",
    [
        Message.user("hi", images=[Image("image/jpeg", "Zm9v")]),
        Message.assistant(
            [
                Thinking("why", signature="s", provider="anthropic"),
                Text("answer"),
                ToolCall("c1", "grep", {"pattern": "x"}, extra={"thought_signature": "t"}),
            ]
        ),
        Message.tool_results([ToolResult("c1", (Text("out"),), is_error=True, name="grep")]),
    ],
)
def test_round_trip(msg):
    data = message_to_dict(msg)
    assert data["v"] == 1
    again = message_from_dict(data)
    assert again == msg
    assert again.content[-1].__class__ is msg.content[-1].__class__


def test_extra_survives_round_trip():
    call = ToolCall("c1", "grep", {}, extra={"thought_signature": "t"})
    again = message_from_dict(message_to_dict(Message.assistant([call])))
    assert again.tool_calls[0].extra == {"thought_signature": "t"}


def test_unknown_block_type_is_rejected():
    with pytest.raises(ValueError, match="unknown block type"):
        message_from_dict({"v": 1, "role": "user", "content": [{"type": "video"}]})


def test_image_from_file(tmp_path):
    path = tmp_path / "shot.png"
    path.write_bytes(b"\x89PNG")
    img = image_from_file(path)
    assert img.media_type == "image/png"
    assert base64.b64decode(img.data) == b"\x89PNG"


def test_image_from_file_rejects_unknown_type(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("x")
    with pytest.raises(ValueError, match="not a supported image"):
        image_from_file(path)
