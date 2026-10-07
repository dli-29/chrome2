"""Claude's side panel: request parameters, the request -> tools -> request loop driven by a scripted fake client on a
real BrowserWindow and real pages, trusted input, password redaction, screenshots, refusals, Stop, the step limit,
API-key storage and the panel without the anthropic package."""
from __future__ import annotations

import base64
import json
import os
import re
import stat
import sys
import threading
import uuid
from types import SimpleNamespace

import pytest

from helpers import load, run_js, spin, wait_until

anthropic = pytest.importorskip("anthropic")
from anthropic.types.beta import BetaMessage  # noqa: E402

PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><title>Agent test</title></head><body>
<h1>Agent test</h1>
<p>Some text to read. <a id=next href="/agent-next">Next page</a></p>
<label>Name <input id=name></label>
<label>Password <input id=pw type=password value="hunter2"></label>
<input id=otp autocomplete="one-time-code" value="987654" aria-label="Code">
<button id=go type=button>Go</button>
<select id=size aria-label="Size"><option>Small</option><option>Large</option></select>
<div style="height:2500px"></div><p id=bottom>Bottom of the page</p>
<script>
window.events = [];
document.getElementById('go').addEventListener('click', e => events.push('click:' + e.isTrusted));
document.getElementById('name').addEventListener('input', e => events.push('input:' + e.isTrusted));
document.getElementById('name').addEventListener('keydown', e => events.push('keydown:' + e.isTrusted));
document.getElementById('size').addEventListener('change', e => events.push('change:' + e.target.value));
</script></body></html>"""
NEXT = b"""<!doctype html><title>Next page</title><p id=here>You made it</p>"""


@pytest.fixture
def pages(server):
    server.add("/agent", PAGE, "text/html; charset=utf-8")
    server.add("/agent-next", NEXT, "text/html; charset=utf-8")
    return server


# ── a scripted stand-in for the Anthropic client ──────────────────────────────────────────
def message(*blocks, stop="tool_use", model="claude-opus-5-5", usage=None, stop_details=None) -> BetaMessage:
    return BetaMessage.model_validate({
        "id": "msg_" + uuid.uuid4().hex[:12], "type": "message", "role": "assistant", "model": model,
        "content": list(blocks), "stop_reason": stop, "stop_sequence": None, "stop_details": stop_details,
        "usage": usage or {"input_tokens": 1000, "output_tokens": 100, "cache_creation_input_tokens": 0,
                           "cache_read_input_tokens": 0}})


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def use(name: str, **arguments) -> dict:
    return {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:16], "name": name, "input": arguments}


class FakeStream:
    """What client.beta.messages.stream(...) returns: a context manager that yields stream events and has
    get_final_message() and close(). With gate=True it blocks until closed (a reply that takes forever)."""

    def __init__(self, reply: BetaMessage | None, gate: bool = False):
        self.reply, self.gate = reply, gate
        self.closed = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.closed.set()

    def __iter__(self):
        if self.gate:
            self.closed.wait(30)
            raise RuntimeError("stream closed")
        for index, block in enumerate(self.reply.content):
            if block.type in ("text", "thinking"):
                yield SimpleNamespace(type="content_block_start", index=index, content_block=SimpleNamespace(type=block.type))
            if block.type == "text":
                for start in range(0, len(block.text), 5):
                    yield SimpleNamespace(type="content_block_delta", index=index,
                                          delta=SimpleNamespace(type="text_delta", text=block.text[start:start + 5]))
            elif block.type == "thinking" and block.thinking:
                yield SimpleNamespace(type="content_block_delta", index=index,
                                      delta=SimpleNamespace(type="thinking_delta", thinking=block.thinking))

    def get_final_message(self) -> BetaMessage:
        return self.reply

    def close(self) -> None:
        self.closed.set()


class FakeClient:
    """Answers each request with the next scripted step: a BetaMessage, a FakeStream, or a function of the request
    parameters returning either. Records every request's parameters."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict] = []
        self.streams: list[FakeStream] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **params) -> FakeStream:
        self.calls.append(params)
        if not self.script:
            raise AssertionError("the fake client ran out of scripted replies")
        step = self.script.pop(0)
        if callable(step):
            step = step(params)
        stream = step if isinstance(step, FakeStream) else FakeStream(step)
        self.streams.append(stream)
        return stream


def results_of(params: dict) -> list[dict]:
    """The tool_result blocks the request ends with (all in its last message)."""
    last = params["messages"][-1]
    assert last["role"] == "user"
    return [block for block in last["content"] if block.get("type") == "tool_result"]


def result_text(result: dict) -> str:
    content = result["content"]
    return content if isinstance(content, str) else "\n".join(b.get("text", "") for b in content if b.get("type") == "text")


def label_of(page_text: str, name: str) -> int:
    match = re.search(r'\[(\d+)\] [a-z ]+ "' + re.escape(name) + '"', page_text)
    assert match, f"no element {name!r} in:\n{page_text}"
    return int(match.group(1))


def open_panel(window, client: FakeClient):
    window.toggle_agent_panel(True)
    panel = window.agent_panel
    panel.session.client_factory = lambda: client
    return panel


def ask(panel, prompt: str, timeout: float = 60.0) -> None:
    panel.input.setPlainText(prompt)
    panel.submit()
    wait_until(lambda: not panel.session.running, timeout, "Claude to finish")


def transcript(panel) -> list[tuple[str, str]]:
    from PyQt6.QtWidgets import QLabel
    out = []
    for i in range(panel.column.count()):
        widget = panel.column.itemAt(i).widget()
        if isinstance(widget, QLabel):
            out.append((widget.objectName()[5:], widget.property("raw") or widget.text()))
    return out


def load_agent_page(window, pages) -> object:
    tab = window.current_tab()
    assert load(tab.page, pages.url("/agent"))
    return tab


# ── request parameters ────────────────────────────────────────────────────────────────────
def test_request_parameters_per_model(fg):
    history = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    for model in fg.AGENT_MODELS:
        params = fg.agent_request(model, "high", history)
        assert params["model"] == model
        assert params["max_tokens"] == fg.AGENT_MAX_TOKENS
        assert params["messages"] is history
        assert params["cache_control"] == {"type": "ephemeral"}  # the conversation tail
        assert params["system"][-1]["cache_control"] == {"type": "ephemeral"}  # the frozen prefix
        assert "tool_choice" not in params  # auto: forced tool use is a 400 on these models
        names = [tool["name"] for tool in params["tools"]]
        assert {"read_page", "screenshot", "click", "type_text", "press_key", "scroll", "navigate", "go_back",
                "go_forward", "reload", "new_tab", "switch_tab", "list_tabs", "wait", "finish"} <= set(names)
        for tool in params["tools"]:
            assert tool["strict"] is True and tool["eager_input_streaming"] is True
            schema = tool["input_schema"]
            assert schema["type"] == "object" and schema["additionalProperties"] is False
            assert set(schema["required"]) <= set(schema["properties"])

    opus = fg.agent_request("claude-opus-5-5", "high", history)
    assert opus["thinking"] == {"type": "adaptive", "display": "updates"}
    assert opus["output_config"] == {"effort": "high"}
    assert opus["fallbacks"] == "default"
    assert set(opus["betas"]) == {"server-side-fallback-2026-07-01", "thinking-display-updates-2026-08-18"}

    sonnet = fg.agent_request("claude-sonnet-5-5", "medium", history)
    assert sonnet["thinking"]["type"] == "adaptive" and sonnet["output_config"] == {"effort": "medium"}
    assert sonnet["fallbacks"] == "default"

    fable = fg.agent_request("claude-fable-5-1", "xhigh", history)
    assert "thinking" not in fable and fable["output_config"] == {"effort": "xhigh"}
    assert fable["fallbacks"] == "default" and fable["betas"] == ["server-side-fallback-2026-07-01"]

    haiku = fg.agent_request("claude-haiku-4-5", "high", history)
    assert haiku["thinking"]["type"] == "enabled" and 1024 <= haiku["thinking"]["budget_tokens"] < haiku["max_tokens"]
    for absent in ("output_config", "fallbacks", "betas"):
        assert absent not in haiku
    assert "thinking" not in fg.agent_request("claude-haiku-4-5", "low", history)

    assert fg.agent_request("claude-unknown", "bogus", history)["model"] == fg.AGENT_DEFAULT_MODEL
    assert fg.agent_request("claude-opus-5-5", "bogus", history)["output_config"] == {"effort": "high"}


def test_tool_input_validation_and_keys(fg):
    assert fg.agent_tool_error("click", {"label": 3}) == ""
    assert "integer" in fg.agent_tool_error("click", {"label": "3"})
    assert "integer" in fg.agent_tool_error("click", {"label": True})
    assert "missing" in fg.agent_tool_error("click", {})
    assert "unexpected" in fg.agent_tool_error("click", {"label": 1, "x": 2})
    assert fg.agent_tool_error("wait", {"seconds": 11})
    assert fg.agent_tool_error("wait", {"seconds": 2.5}) == ""
    assert fg.agent_tool_error("scroll", {})
    assert fg.agent_tool_error("scroll", {"direction": "sideways"})
    assert fg.agent_tool_error("nope", {})
    assert fg.agent_tool_error("read_page", [1])

    from PyQt6.QtCore import Qt
    assert fg.agent_key("Enter")[0] == Qt.Key.Key_Return and fg.agent_key("Enter")[2] == "\r"
    key, modifiers, typed = fg.agent_key("Ctrl+A")
    assert key == Qt.Key.Key_A and modifiers & Qt.KeyboardModifier.ControlModifier and typed == ""
    assert fg.agent_key("ArrowDown")[0] == Qt.Key.Key_Down
    assert fg.agent_key("Shift+Tab")[1] & Qt.KeyboardModifier.ShiftModifier
    assert fg.agent_key("Escape")[0] == Qt.Key.Key_Escape
    assert fg.agent_key("b")[2] == "b"
    assert fg.agent_key("NotAKey") is None and fg.agent_key("") is None and fg.agent_key("Ctrl") is None


def test_echo_content_after_fallback(fg):
    reply = message(
        {"type": "thinking", "thinking": "", "signature": "sig1"},
        text("partial"),
        use("click", label=1),
        {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-4-8"},
         "trigger": {"type": "refusal", "category": "cyber"}},
        {"type": "thinking", "thinking": "", "signature": "sig2"},
        use("read_page"),
        stop="tool_use")
    kept = fg.agent_echo_content(reply.content)
    assert [b.type for b in kept] == ["text", "fallback", "thinking", "tool_use"]
    assert kept[-1].name == "read_page"
    plain = message(text("hi"), stop="end_turn")
    assert fg.agent_echo_content(plain.content) == list(plain.content)


# ── the loop on real pages ────────────────────────────────────────────────────────────────
def test_tool_loop_drives_real_pages(window, pages):
    tab = load_agent_page(window, pages)
    seen: dict = {}

    def act(params):
        page_text = result_text(results_of(params)[0])
        seen["page"] = page_text
        return message(text("On it."), use("click", label=label_of(page_text, "Go")),
                       use("type_text", label=label_of(page_text, "Name"), text="Ada Lovelace"),
                       use("select_option", label=label_of(page_text, "Size"), option="Large"))

    def scroll(params):
        results = results_of(params)
        seen["parallel"] = results
        return message(use("scroll", direction="down"))

    def navigate(params):
        seen["scrolled"] = result_text(results_of(params)[0])
        return message(use("new_tab", url=pages.url("/agent-next")))

    def finish(params):
        seen["new_tab"] = result_text(results_of(params)[0])
        return message(use("list_tabs"), use("switch_tab", index=0), use("go_back"))

    client = FakeClient(message(use("read_page")), act, scroll, navigate, finish,
                        lambda params: seen.setdefault("last", params) and None
                        or message(text("All done: clicked Go and typed the name."), stop="end_turn"))
    panel = open_panel(window, client)
    ask(panel, "Click Go and type my name")

    assert len(client.calls) == 6
    page_text = seen["page"]
    assert "[redacted]" in page_text and "hunter2" not in page_text and "987654" not in page_text
    assert 'textbox "Name" empty' in page_text and "Some text to read." in page_text and "# Agent test" in page_text

    # the page saw a real user: trusted click, keys and input
    events = run_js(window.tabs()[0].page, "JSON.stringify(window.events)")
    events = json.loads(events) if events else []
    assert "click:true" in events and "input:true" in events and "keydown:true" in events
    assert "change:Large" in events
    assert run_js(window.tabs()[0].page, "document.getElementById('name').value") == "Ada Lovelace"

    # all of one turn's results come back together, in order, in one user message
    parallel = seen["parallel"]
    assert len(parallel) == 3 and not any(r.get("is_error") for r in parallel)
    act_uses = [b for b in client.calls[2]["messages"][-2]["content"] if getattr(b, "type", "") == "tool_use"]
    assert [r["tool_use_id"] for r in parallel] == [b.id for b in act_uses]
    assert "Ada Lovelace" in result_text(parallel[1])
    assert "Scrolled the page down" in seen["scrolled"]
    assert "agent-next" in seen["new_tab"] and len(window.tabs()) == 2
    assert window.current_tab() is tab  # switched back
    last = results_of(client.calls[-1])
    assert "(current)" in result_text(last[0]) and "Switched to tab 0" in result_text(last[1])

    # the history only grows: each request starts with the previous one's messages, unchanged
    for before, after in zip(client.calls, client.calls[1:]):
        assert after["messages"][:len(before["messages"])] == before["messages"]
    roles = [m["role"] for m in client.calls[-1]["messages"]]
    assert roles == ["user", "assistant"] * 5 + ["user"]

    lines = transcript(panel)
    kinds = [kind for kind, _ in lines]
    assert lines[0] == ("User", "Click Go and type my name")
    assert ("Action", "› Clicked 'Go'") in lines
    assert any(kind == "Action" and "Typed \"Ada Lovelace\" into 'Name'" in value for kind, value in lines)
    assert any(kind == "Action" and value.startswith("› Read the page") for kind, value in lines)
    assert ("Text", "On it.") in lines and ("Text", "All done: clicked Go and typed the name.") in lines
    assert kinds.count("Text") == 2
    assert "tokens in" in panel.usage_label.text() and "$" in panel.usage_label.text()


def test_password_fields_stay_hidden(window, pages, fg):
    tab = load_agent_page(window, pages)
    browser = fg.AgentBrowser(window)

    def run(name, **arguments):
        box = {}
        browser.run(name, arguments, lambda content, error=False, log_line="": box.update(c=content, e=error, l=log_line))
        wait_until(lambda: box, 30, f"the {name} tool")
        return box

    page_text = run("read_page")["c"]
    assert 'textbox "Password" value=[redacted]' in page_text and 'textbox "Code" value=[redacted]' in page_text
    assert "hunter2" not in page_text and "987654" not in page_text
    typed = run("type_text", label=label_of(page_text, "Password"), text="new-secret-1")
    assert not typed["e"] and "new-secret-1" not in typed["c"] and "new-secret-1" not in typed["l"]
    assert "hidden" in typed["l"]
    assert run_js(tab.page, "document.getElementById('pw').value") == "new-secret-1"
    again = run("read_page")["c"]
    assert "new-secret-1" not in again and "value=[redacted]" in again
    # the numbering lives in Claude's own world: the page can't see it
    assert run_js(tab.page, "typeof window.__claudeAgent") == "undefined"


HARD = b"""<!doctype html><html><head><meta charset="utf-8"><title>Hard page</title></head><body style="margin:0">
<p>Above the frame</p>
<iframe id=frame src="/agent-frame" style="width:400px;height:120px;border:4px solid #888;margin-left:30px"></iframe>
<div id=host></div>
<div id=editor contenteditable=true aria-label="Editor" style="width:300px;height:60px;border:1px solid #000"></div>
<input id=email aria-label="Email address">
<textarea id=long aria-label="Long text"></textarea>
<div style="position:relative;width:200px;height:40px">
  <button id=under style="position:absolute;left:0;top:0;width:200px;height:40px">Covered</button>
  <div style="position:absolute;left:0;top:0;width:200px;height:40px;background:#eee"></div>
</div>
<script>
window.events = [];
const root = document.getElementById('host').attachShadow({mode: 'open'});
root.innerHTML = '<button id=shadowed>Shadow button</button>';
root.getElementById('shadowed').addEventListener('click', e => events.push('shadow:' + e.isTrusted));
document.getElementById('under').addEventListener('click', e => events.push('covered:' + e.isTrusted));
</script></body></html>"""
FRAME = b"""<!doctype html><html><body style="margin:10px"><p>Inside the frame</p>
<button id=inner onclick="parent.events.push('frame:' + event.isTrusted)">Frame button</button></body></html>"""


def test_frames_shadow_dom_and_editors(window, server, fg):
    server.add("/agent-hard", HARD, "text/html; charset=utf-8")
    server.add("/agent-frame", FRAME, "text/html; charset=utf-8")
    tab = window.current_tab()
    assert load(tab.page, server.url("/agent-hard"))
    wait_until(lambda: run_js(tab.page, "document.getElementById('frame').contentDocument.readyState") == "complete")
    browser = fg.AgentBrowser(window)

    def run(name, **arguments):
        box = {}
        browser.run(name, arguments, lambda content, error=False, log_line="": box.update(c=content, e=error, l=log_line))
        wait_until(lambda: box, 30, f"the {name} tool")
        return box

    page_text = run("read_page")["c"]
    assert "Inside the frame" in page_text and "Shadow button" in page_text
    for name in ("Frame button", "Shadow button"):
        result = run("click", label=label_of(page_text, name))
        assert not result["e"] and "by script" not in result["c"], result
    covered = run("click", label=label_of(page_text, "Covered"))
    assert not covered["e"] and "by script" in covered["c"]  # nothing at its position takes the mouse: element.click()
    events = json.loads(run_js(tab.page, "JSON.stringify(events)"))
    assert events == ["frame:true", "shadow:true", "covered:false"]

    tricky = "Grüße, café @ 5 € — ok? \"quoted\" & <tag>"
    result = run("type_text", label=label_of(page_text, "Email address"), text=tricky)
    assert not result["e"]
    assert run_js(tab.page, "document.getElementById('email').value") == tricky
    run("type_text", label=label_of(page_text, "Email address"), text="")
    assert run_js(tab.page, "document.getElementById('email').value") == ""
    long_text = "line one\nline two " + "x" * 400
    run("type_text", label=label_of(page_text, "Long text"), text=long_text)
    assert run_js(tab.page, "document.getElementById('long').value") == long_text
    run("type_text", label=label_of(page_text, "Editor"), text="Rich text here")
    assert run_js(tab.page, "document.getElementById('editor').innerText.trim()") == "Rich text here"
    stale = run("click", label=999)
    assert stale["e"] and "read_page" in stale["c"]


CROSS = b"""<!doctype html><html><head><meta charset="utf-8"><title>Cross-site frame</title></head><body style="margin:0">
<iframe id=frame src="%s" style="position:absolute;left:40px;top:30px;width:400px;height:200px;border:0"></iframe>
<script>window.events = []; addEventListener('message', e => events.push(e.data));</script></body></html>"""
CROSS_FRAME = b"""<!doctype html><html><body style="margin:0">
<button id=b style="position:absolute;left:20px;top:20px;width:120px;height:40px"
  onclick="parent.postMessage('click:' + event.isTrusted, '*')">Inside</button>
<input id=i style="position:absolute;left:20px;top:100px;width:200px;height:30px"
  oninput="parent.postMessage('value:' + this.value, '*')">
</body></html>"""


def test_click_at_and_typing_into_another_sites_frame(window, server, fg):
    server.add("/agent-cross-frame", CROSS_FRAME, "text/html; charset=utf-8")
    server.add("/agent-cross", CROSS % f"http://localhost:{server.port}/agent-cross-frame".encode(), "text/html; charset=utf-8")
    tab = window.current_tab()
    assert load(tab.page, server.url("/agent-cross"))
    spin(1.0)  # (the frame loads on its own)
    browser = fg.AgentBrowser(window)

    def run(name, **arguments):
        box = {}
        browser.run(name, arguments, lambda content, error=False, log_line="": box.update(c=content, e=error, l=log_line))
        wait_until(lambda: box, 30, f"the {name} tool")
        return box

    assert run("click_at", x=10, y=10)["e"]  # no screenshot yet
    page_text = run("read_page")["c"]
    assert "[embedded frame: http://localhost" in page_text
    shot = run("screenshot")
    factor = browser.shot["factor"]
    events = lambda: json.loads(run_js(tab.page, "JSON.stringify(events)"))  # noqa: E731
    clicked = run("click_at", x=(40 + 20 + 60) * factor, y=(30 + 20 + 20) * factor)
    assert not clicked["e"], clicked
    wait_until(lambda: "click:true" in events(), 10, "the click inside the frame")
    run("click_at", x=(40 + 20 + 100) * factor, y=(30 + 100 + 15) * factor)
    typed = run("type_text", text="hello frame")
    assert not typed["e"] and "hello frame" not in typed["l"]  # (it can't see that field, so it doesn't repeat it)
    wait_until(lambda: "value:hello frame" in events(), 10, "typing inside the frame")
    assert shot["c"][1]["type"] == "image"
    assert run("click_at", x=99999, y=5)["e"]


def test_screenshot_is_a_png_with_labels(window, pages, fg):
    from PyQt6.QtGui import QColor, QImage
    load_agent_page(window, pages)
    browser = fg.AgentBrowser(window)
    box = {}
    browser.run("screenshot", {}, lambda content, error=False, log_line="": box.update(c=content, e=error, l=log_line))
    wait_until(lambda: box, 30, "the screenshot")
    assert not box["e"] and box["l"] == "Took a screenshot"
    note, picture = box["c"]
    assert note["type"] == "text" and "Numbered boxes" in note["text"]
    assert picture["type"] == "image" and picture["source"]["type"] == "base64"
    assert picture["source"]["media_type"] == "image/png"
    png = base64.b64decode(picture["source"]["data"], validate=True)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    image = QImage.fromData(png, "PNG")
    assert not image.isNull() and 0 < image.width() <= fg.AGENT_SHOT_MAX and image.height() <= fg.AGENT_SHOT_MAX
    tag = QColor("#b4441f")
    tags = sum(1 for x in range(0, image.width(), 2) for y in range(0, min(image.height(), 400), 2)
               if QColor(image.pixel(x, y)) == tag)
    assert tags > 20, "no number tags painted on the screenshot"


def test_screenshot_reaches_the_model_as_an_image_block(window, pages):
    load_agent_page(window, pages)
    client = FakeClient(message(use("screenshot")), message(text("I see the page."), stop="end_turn"))
    panel = open_panel(window, client)
    ask(panel, "What do you see?")
    result = results_of(client.calls[1])[0]
    kinds = [block["type"] for block in result["content"]]
    assert kinds == ["text", "image"]
    assert base64.b64decode(result["content"][1]["source"]["data"]).startswith(b"\x89PNG")


def test_invalid_input_and_finish(window, pages):
    load_agent_page(window, pages)
    client = FakeClient(message(use("click", label="Go"), use("wait", seconds=60)),
                        message(use("finish", summary="Nothing left to do.")))
    panel = open_panel(window, client)
    ask(panel, "Do something")
    bad = results_of(client.calls[1])
    assert len(bad) == 2 and all(r.get("is_error") for r in bad)
    assert "INVALID_JSON" in json.loads(bad[0]["content"]) and "integer" in json.loads(bad[0]["content"])["error"]
    assert len(client.calls) == 2  # finish ends the loop without another request
    assert ("Summary", "Nothing left to do.") in transcript(panel)
    assert panel.session.history[-1]["content"][0]["content"] == "OK"


# ── refusals, Stop, the step limit ────────────────────────────────────────────────────────
def test_refusal_is_handled_before_reading_content(window, pages):
    load_agent_page(window, pages)
    refused = message(stop="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": None})
    client = FakeClient(refused, message(text("Sure."), stop="end_turn"))
    panel = open_panel(window, client)
    ask(panel, "Something it declines")
    errors = [value for kind, value in transcript(panel) if kind == "Error"]
    assert errors and "declined" in errors[0] and "cyber" in errors[0]
    assert [m["role"] for m in panel.session.history] == ["user"]  # nothing from the refused turn is kept
    assert panel.session.tokens["input"] == 1000  # it still counts
    ask(panel, "Something else")
    assert [m["role"] for m in client.calls[1]["messages"]] == ["user", "user"]
    assert not panel.stop_button.isVisible() and panel.send_button.isVisible()


def test_stop_cancels_the_stream_at_once(window, pages):
    load_agent_page(window, pages)
    hanging = FakeStream(None, gate=True)
    client = FakeClient(hanging, message(text("Hello again."), stop="end_turn"))
    panel = open_panel(window, client)
    panel.input.setPlainText("Take forever")
    panel.submit()
    wait_until(lambda: client.calls, 10, "the request")
    assert panel.session.running and panel.stop_button.isVisible()
    panel.stop_button.click()
    assert not panel.session.running and panel.send_button.isVisible()  # at once, not when the stream gives up
    assert hanging.closed.is_set()
    assert ("Notice", "Stopped.") in transcript(panel)
    spin(0.3)
    assert len(client.calls) == 1
    ask(panel, "Now answer")
    assert ("Text", "Hello again.") in transcript(panel)
    assert [m["role"] for m in client.calls[1]["messages"]] == ["user", "user"]


def test_stop_during_tools_answers_every_call(window, pages):
    load_agent_page(window, pages)
    long_wait = message(use("wait", seconds=8), use("read_page"))
    client = FakeClient(long_wait, message(text("Okay."), stop="end_turn"))
    panel = open_panel(window, client)
    from PyQt6.QtWidgets import QFrame
    panel.input.setPlainText("Wait a while")
    panel.submit()
    pill = wait_until(lambda: window.current_tab().findChild(QFrame, "AgentPill"), 10, "the indicator")
    assert pill.isVisible()
    first = window.current_tab()
    from PyQt6.QtCore import QUrl
    other = window.new_tab(QUrl(pages.url("/agent-next")))  # Claude acts on the current tab: the indicator follows
    wait_until(lambda: other.findChild(QFrame, "AgentPill") is not None, 5, "the indicator on the new current tab")
    wait_until(lambda: first.findChild(QFrame, "AgentPill") is None, 5, "the indicator to leave the first tab")
    panel.session.stop()
    assert not panel.session.running
    wait_until(lambda: window.current_tab().findChild(QFrame, "AgentPill") is None, 5, "the indicator to go")
    stopped = panel.session.history[-1]
    assert stopped["role"] == "user" and len(stopped["content"]) == 2
    assert all(r["is_error"] and "stopped" in r["content"] for r in stopped["content"])
    spin(0.3)
    ask(panel, "Carry on")
    assert len(client.calls) == 2
    sent = client.calls[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user", "user"]


def test_cut_off_tool_call_is_not_run(window, pages):
    tab = load_agent_page(window, pages)
    cut = message(use("navigate", url=pages.url("/agent-next")), stop="max_tokens")
    client = FakeClient(cut, message(text("Continuing."), stop="end_turn"))
    panel = open_panel(window, client)
    ask(panel, "Go somewhere")
    assert tab.url().path() == "/agent"  # not run
    answered = panel.session.history[-1]
    assert answered["role"] == "user" and answered["content"][0]["is_error"]
    assert answered["content"][0]["tool_use_id"] == panel.session.history[-2]["content"][0].id
    assert any("cut off" in value for kind, value in transcript(panel) if kind == "Error")
    ask(panel, "Continue")
    assert [m["role"] for m in client.calls[1]["messages"]] == ["user", "assistant", "user", "user"]


def test_step_limit(window, pages):
    load_agent_page(window, pages)
    window.settings.set("agent_max_steps", 3)
    client = FakeClient(*[lambda params: message(use("wait", seconds=0.05)) for _ in range(5)])
    panel = open_panel(window, client)
    ask(panel, "Loop forever")
    assert len(client.calls) == 3
    assert panel.session.history[-1]["role"] == "user"  # the last round's results are kept, so it can continue
    notices = [value for kind, value in transcript(panel) if kind == "Notice"]
    assert notices and "Paused after 3 steps" in notices[-1]


def test_models_and_usage(window, pages, fg):
    load_agent_page(window, pages)
    window.settings.set("agent_model", "claude-haiku-4-5")
    usage = {"input_tokens": 2000, "output_tokens": 400, "cache_creation_input_tokens": 1000,
             "cache_read_input_tokens": 10000}
    fallback_usage = dict(usage, iterations=[
        {"type": "message", "model": "claude-opus-5-5", "input_tokens": 500, "output_tokens": 0,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 1000, "output_tokens": 200,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}])
    client = FakeClient(message(text("Hi."), stop="end_turn", model="claude-haiku-4-5", usage=usage),
                        message(text("Hi again."), stop="end_turn", usage=fallback_usage))
    panel = open_panel(window, client)
    assert panel.model_box.currentData() == "claude-haiku-4-5"
    ask(panel, "Hello")
    first = client.calls[0]
    assert first["model"] == "claude-haiku-4-5" and "fallbacks" not in first and "output_config" not in first
    expected = (2000 * 1.0 + 1000 * 1.25 + 10000 * 0.10 + 400 * 5.0) / 1e6
    assert panel.session.cost == pytest.approx(expected)
    panel.model_box.setCurrentIndex(panel.model_box.findData("claude-opus-5-5"))
    assert window.settings.get("agent_model") == "claude-opus-5-5"
    ask(panel, "Hello again")
    second = client.calls[1]
    assert second["model"] == "claude-opus-5-5" and second["fallbacks"] == "default"
    assert second["output_config"] == {"effort": "high"}
    expected += (500 * 4.0) / 1e6 + (1000 * 5.0 + 200 * 25.0) / 1e6  # each attempt at its own model's prices
    assert panel.session.cost == pytest.approx(expected)
    assert panel.session.tokens["cache_read"] == 10000
    assert "cached" in panel.usage_label.text()
    panel.new_chat()
    assert panel.session.history == [] and panel.session.cost == 0 and not panel.usage_label.isVisible()


def test_toolbar_button_and_shortcut_toggle_the_panel(window):
    assert window.agent_panel is None
    window.agent_button.click()
    panel = window.agent_panel
    assert panel is not None and panel.isVisible() and window.agent_button.isChecked()
    assert window.side_split.indexOf(panel) == 1 and window.side_split.indexOf(window.content) == 0
    window.act_agent.trigger()
    assert not panel.isVisible() and not window.agent_button.isChecked()
    window.toggle_agent_panel()
    assert panel.isVisible()


# ── the API key ───────────────────────────────────────────────────────────────────────────
@pytest.fixture
def memory_keyring(fg, monkeypatch):
    keyring = pytest.importorskip("keyring")
    from keyring.backend import KeyringBackend
    from keyring.backends import fail

    class MemoryKeyring(KeyringBackend):
        priority = 1

        def __init__(self):
            super().__init__()
            self.store: dict = {}

        def get_password(self, service, username):
            return self.store.get((service, username))

        def set_password(self, service, username, password):
            self.store[(service, username)] = password

        def delete_password(self, service, username):
            self.store.pop((service, username), None)

    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    monkeypatch.setattr(fg, "_secret_store", None)  # the app's SecretStore finds this keychain
    yield SimpleNamespace(keyring=keyring, backend=backend, fail=fail)
    keyring.set_keyring(previous)


def test_api_key_in_the_keychain(fg, tmp_path, memory_keyring, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = fg.AgentKeyStore(tmp_path)
    assert store.secrets is fg.secret_store() and store.keychain()  # the same keychain items as saved passwords
    assert store.load() == ("", "")
    assert store.save("  sk-ant-test-1  ") == "keychain"
    assert memory_keyring.backend.store == {(fg.KEYCHAIN_SERVICE, "anthropic_api_key"): "sk-ant-test-1"}
    assert fg.KEYCHAIN_SERVICE == "Chrome 2" and fg.secret_store().get("anthropic_api_key") == "sk-ant-test-1"
    assert not store.file.exists()
    assert store.load() == ("sk-ant-test-1", "keychain")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    assert store.load() == ("sk-ant-test-1", "keychain")  # a saved key wins
    store.remove()
    assert memory_keyring.backend.store == {}
    assert store.load() == ("sk-from-env", "environment")


def test_api_key_file_without_a_keychain(fg, tmp_path, memory_keyring, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    memory_keyring.keyring.set_keyring(memory_keyring.fail.Keyring())  # keyring installed, but no backend
    store = fg.AgentKeyStore(tmp_path)
    assert not store.keychain()
    assert store.save("sk-ant-file") == "file"
    assert store.file.read_text() == "sk-ant-file"
    if os.name == "posix":
        assert stat.S_IMODE(store.file.stat().st_mode) == 0o600
    assert store.load() == ("sk-ant-file", "file")
    store.remove()
    assert not store.file.exists() and store.load() == ("", "")
    monkeypatch.setitem(sys.modules, "keyring", None)  # keyring not installed at all
    monkeypatch.setattr(fg, "_secret_store", None)
    assert not store.keychain() and not fg.AgentKeyStore.keyring_installed()
    assert "pip install keyring" in fg.secret_store().problem()
    assert store.save("sk-ant-2") == "file" and store.load() == ("sk-ant-2", "file")


def test_panel_saves_the_key_and_builds_a_client(window, memory_keyring, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    window.toggle_agent_panel(True)
    panel = window.agent_panel
    panel.settings_button.click()
    assert panel.settings_area.isVisible()
    assert "No key yet" in panel.key_status.text()
    panel.key_field.setText("sk-ant-panel")
    panel._save_key()
    assert memory_keyring.backend.store[("Chrome 2", "anthropic_api_key")] == "sk-ant-panel"
    assert panel.key_field.text() == "" and "keychain" in panel.key_status.text()
    client = panel.session.client()
    assert isinstance(client, anthropic.Anthropic) and client.api_key == "sk-ant-panel"
    panel._remove_key()
    assert memory_keyring.backend.store == {} and "No key yet" in panel.key_status.text()


def test_panel_without_the_anthropic_package(window, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import anthropic -> ImportError
    window.toggle_agent_panel(True)
    panel = window.agent_panel
    hint = panel.hint
    assert hint is not None and "pip install anthropic" in hint.text()
    assert not panel.input.isEnabled() and not panel.send_button.isEnabled()
    assert panel.session.send("hello") is False
    assert any("pip install anthropic" in value for kind, value in transcript(panel) if kind == "Error")
    assert window.current_tab() is not None  # the browser carries on
