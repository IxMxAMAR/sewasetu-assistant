"""Two small models and a transcript.

One model plays the assistant, and it is the only thing that ever touches the
MCP tools. The other plays the citizen: it is handed the persona, it cannot see
tool results, and it says what a person on the phone would say. Neither is the
point of the exercise -- the point is what the portal's database looks like when
they have finished -- but a conversation that is scripted line by line would not
test whether the assistant can cope with somebody who answers a question with a
different question.

The assistant's instructions come from the MCP server itself (`initialize`), so
what the evals test is what the server publishes, not a copy of it kept here.
"""

from __future__ import annotations

import json
import os
import re

import requests

from mcpclient import failed, render_result, tool_specs

# Any OpenAI-shaped endpoint will do. OpenRouter by default, because that is what
# we ran against; EVAL_LLM_BASE points it at a gateway instead.
OPENROUTER = os.environ.get("EVAL_LLM_BASE", "https://openrouter.ai/api/v1").rstrip("/") \
    + "/chat/completions"

# A small model with a limited number of turns is what the department tests
# with, so it is what we test with. The turn budget below is the same shape.
DEFAULT_MODEL = "google/gemini-3.1-flash-lite"

HARNESS_NOTE = """
You are connected to a live government portal through tools. Everything you tell
the person must come from a tool result. Keep your replies short -- two or three
sentences -- because she is reading them on a small phone screen.
"""

CITIZEN_SYSTEM = """You are role-playing a person who is on the phone with an assistant that can
check and file old-age pension applications. You are not the assistant.

Who you are:
{voice}

What you want out of this call:
{goal}

How to reply:
- Speak only as this person would speak out loud, in one or two short sentences.
- Do not repeat, quote or paraphrase any of the text above, and never mention
  assistants, tools, systems, JSON or that you are an AI.
- Answer only what you are asked, with what the paragraph above tells you. If you
  do not know something it does not mention, say you do not know.
- When your goal is met, or you have been told something final, answer with the
  single word DONE and nothing else. Do that as soon as the call is over -- do not
  keep saying goodbye.

The call so far:
{history}

Your next line:"""

# The models we test with sometimes wrap their answer in a "### Thoughts" block,
# so look for DONE anywhere rather than only at the start.
DONE = re.compile(r"\bDONE\b", re.IGNORECASE)

ATTACHMENT_TRIGGER = re.compile(
    r"sending|sent|attach|photo|picture|aadhaar|aadhar|document|here it is|upload",
    re.IGNORECASE)


class Model:
    """A chat model, called the boring way."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: float = 120.0):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 temperature: float = 0.0) -> dict:
        body = {"model": self.model, "messages": messages, "temperature": temperature}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        resp = requests.post(OPENROUTER, timeout=self.timeout,
                             headers={"Authorization": "Bearer " + self.api_key,
                                      "Content-Type": "application/json"},
                             json=body)
        if resp.status_code != 200:
            raise RuntimeError("model call failed: %s %s" % (resp.status_code, resp.text[:300]))
        out = resp.json()
        self.calls += 1
        usage = out.get("usage") or {}
        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)
        return out["choices"][0]["message"]

    def stats(self) -> dict:
        return {"model": self.model, "calls": self.calls,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens}


def _clean(message: dict) -> dict:
    """Only the three keys the API will accept back."""
    out = {"role": "assistant"}
    if message.get("content"):
        out["content"] = message["content"]
    if message.get("tool_calls"):
        out["tool_calls"] = message["tool_calls"]
    return out


def _history(transcript: list[dict]) -> str:
    lines = []
    for turn in transcript:
        if turn["role"] == "citizen":
            lines.append("Person: " + turn["text"])
        elif turn["role"] == "assistant" and turn.get("text"):
            lines.append("Assistant: " + turn["text"])
    return "\n".join(lines) if lines else "(the assistant has not spoken yet)"


def read_attachment(path):
    import base64
    import os
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode("ascii")


def attachment_note(filename: str, encoded: str) -> str:
    return ("\n\n[The person has sent you a file with this message.\n"
            "filename: %s\nbase64: %s]" % (filename, encoded))


async def conversation(session, init, persona: dict, assistant: Model, citizen: Model,
                       fixture_dir, transcript: list[dict], max_turns: int,
                       log=print) -> None:
    """Run one persona's call to the end, appending to `transcript`."""
    tools = tool_specs(await session.list_tools())
    system = (init.instructions or "").strip() + "\n" + HARNESS_NOTE

    messages = [{"role": "system", "content": system}]
    attachment_encoded = None
    if persona.get("attachment"):
        import os
        attachment_encoded = read_attachment(os.path.join(fixture_dir, persona["attachment"]))
    attachment_sent = False

    said = persona["opening"]
    transcript.append({"role": "citizen", "text": said})

    for turn in range(max_turns):
        # The citizen's line, plus a document if that is what this line was about.
        content = said
        if attachment_encoded and not attachment_sent and ATTACHMENT_TRIGGER.search(said):
            content = said + attachment_note(persona["attachment"], attachment_encoded)
            attachment_sent = True
        messages.append({"role": "user", "content": content})

        reply = await _assistant_speaks(session, assistant, tools, messages, transcript, log)
        transcript.append({"role": "assistant", "text": reply})
        log("    assistant: %s" % (reply or "(said nothing)")[:220])

        if turn == max_turns - 1:
            break

        nxt = await _citizen_speaks(citizen, persona, transcript, log)
        if nxt is None:
            log("    (the citizen is finished)")
            break
        said = nxt
        transcript.append({"role": "citizen", "text": said})
        log("    citizen:   %s" % said[:220])


# A small model asked to play a person will sometimes answer with the brief
# instead of the person's words -- "Say only what the person would say out loud."
# That is not a line the assistant can reply to, and left alone it turns the call
# into ten rounds of the two of them saying goodbye. Ask once more, plainly, and
# if it happens again end the call.
ECHO = re.compile(r"say only what the person|one or two short sentences|"
                  r"answer with the single word|how to reply|"
                  r"what you want out of this call|who you are", re.IGNORECASE)

NUDGE = ("You repeated the brief instead of speaking. Reply with the person's next "
         "sentence and nothing else.")


async def _citizen_speaks(citizen: Model, persona: dict, transcript: list[dict],
                          log) -> str | None:
    """The citizen's next line, or None when the call is over."""
    messages = [{
        "role": "system",
        "content": CITIZEN_SYSTEM.format(voice=persona["voice"].strip(),
                                         goal=persona["goal"].strip(),
                                         history=_history(transcript)),
    }]
    reply = ""
    for attempt in range(2):
        reply = (citizen.complete(messages, temperature=0.7)["content"] or "").strip()
        if reply and not ECHO.search(reply):
            break
        log("    (the citizen answered with the brief%s)"
            % ("" if attempt == 0 else ", twice"))
        messages.append({"role": "user", "content": NUDGE})
    else:
        return None
    return None if DONE.search(reply) else reply


async def _assistant_speaks(session, model: Model, tools: list[dict], messages: list[dict],
                            transcript: list[dict], log, max_tool_rounds: int = 8) -> str:
    """One assistant reply, with as many tool calls as it takes to produce it."""
    for _ in range(max_tool_rounds):
        message = model.complete(messages, tools=tools)
        calls = message.get("tool_calls") or []
        messages.append(_clean(message))
        if not calls:
            return (message.get("content") or "").strip()

        for call in calls:
            name = call["function"]["name"]
            raw = call["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {}
                result_text, was_refused = "your arguments were not valid JSON", True
            else:
                try:
                    result = await session.call_tool(name, args)
                    result_text = render_result(result)
                    was_refused = failed(result)
                except Exception as exc:                       # noqa: BLE001 - reported, not raised
                    result_text, was_refused = "the call failed: %s" % exc, True
            transcript.append({"role": "tool", "tool": name, "args": args,
                               "failed": was_refused, "result": result_text[:6000]})
            log("    tool %-24s %s%s" % (name, "REFUSED " if was_refused else "", raw[:120]))
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": result_text[:6000]})
    return ""
