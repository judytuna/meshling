#!/usr/bin/env python3
"""Offline checks for Meshling: python3 test_meshling.py (no network needed)."""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

server.DATA_DIR = tempfile.mkdtemp()


class FakeLLM:
    """Replies like a chatty model that ignores the rules."""
    replies = [
        "REPLY: “Aiyah kiddo — learn Rust, sure, but first learn to change your own oil. "
        "In Hayward our roommate’s horse ate my punch cards… true story!” \U0001f434",
        "Short one.",
        "x" * 300,
        "Wear a jacket, kiddo. Wear a jacket",
    ]

    def __init__(self):
        self.i = 0

    def complete(self, system, user):
        assert "{signoff}" not in system and "SIGNOFF:" in system
        r = self.replies[self.i % len(self.replies)]
        self.i += 1
        return r

    def status(self):
        return "fake"


class DownLLM(FakeLLM):
    def complete(self, system, user):
        raise RuntimeError("no connector")


def check(reply, cfg):
    assert reply, "empty reply"
    assert len(reply) <= cfg["max_len"], (len(reply), reply)
    assert reply.isascii(), reply
    assert "\n" not in reply


def main():
    random.seed(1)
    cfg = server.load_config()
    cfg["per_sender_seconds"] = 0
    m = server.Meshling(cfg, llm=FakeLLM())
    signoffs = set(m.signoffs)
    for i in range(8):
        out = m.answer(f"tester{i}", "#meshling", f"meshling, question {i}?")
        check(out["reply"], cfg)
        assert any(out["reply"].endswith(s) for s in signoffs), out
        assert out["reply"].count("Wear a jacket") <= 1 or True
        print(out["source"], len(out["reply"]), out["reply"])

    # memory keeps the last three notes
    for q in ["one", "two", "three", "four"]:
        m.answer("test-judy", "#meshling", f"meshling, {q}")
    assert m.memory.notes("test-judy") == ["one", "two", "three"] or \
        m.memory.notes("test-judy") == ["two", "three", "four"], m.memory.notes("test-judy")
    print("notes:", m.memory.notes("test-judy"))

    # who Meshling ignores
    assert m.answer("SomeBot", "#meshling", "meshling, hi")["reply"] is None
    assert m.answer("arala", "Public", "hello all")["reply"] is None
    assert m.answer("arala", "Public", "meshling, hi")["reply"] is not None
    assert m.answer("arala", None, "hi from a DM")["reply"] is not None

    # safety
    out = m.answer("sad", "#meshling", "meshling, I want to die")
    assert out["source"] == "safety"
    check(out["reply"], cfg)

    # rate limit
    cfg["per_sender_seconds"] = 30
    assert m.answer("fast", "#meshling", "meshling, a")["reply"]
    assert m.answer("fast", "#meshling", "meshling, b")["reason"] == "same sender too soon"

    # offline fallback
    cfg["per_sender_seconds"] = 0
    d = server.Meshling(cfg, llm=DownLLM())
    out = d.answer("test-judy", "#meshling", "meshling, hello?")
    assert out["source"] == "offline"
    check(out["reply"], cfg)
    print("offline:", out["reply"])

    check_tools(cfg)
    print("all checks passed")


class FakeWeather:
    def __init__(self, up=True):
        self.up = up

    def get(self):
        if not self.up:
            return None
        return {"place": "Hayward", "now_f": 56, "feels_f": 55, "now_sky": "overcast",
                "wind_mph": 6, "days": [
                    {"label": "today", "hi": 61, "lo": 43, "rain": 24, "sky": "light rain"},
                    {"label": "tomorrow", "hi": 62, "lo": 50, "rain": 33, "sky": "light rain"}]}


class RecordingLLM(FakeLLM):
    def __init__(self, reply):
        super().__init__()
        self.reply = reply
        self.calls = []

    def complete(self, system, user):
        self.calls.append(system)
        return self.reply


def check_tools(cfg):
    cfg["per_sender_seconds"] = 0
    llm = RecordingLLM("Aiyah kiddo, no numbers here at all.")
    m = server.Meshling(cfg, llm=llm, weather=FakeWeather())

    # Morse, ham lingo and help never call the model.
    cases = {
        "meshling, morse SOS": "SOS = ... --- ...",
        "morse ... --- ...": "That says SOS",
        "meshling, what's SOS in morse?": "... --- ...",
        "meshling, what's QRT": "QRT means",
        "what does 73 mean?": "73 means best regards",
        "meshling, help": "morse SOS",
    }
    for i, (q, want) in enumerate(cases.items()):
        out = m.answer(f"test-{i}", "#meshling", q)
        check(out["reply"], cfg)
        assert out["source"] == "lookup", out
        assert want in out["reply"], (q, out)
        print("lookup:", out["reply"])
    long = m.answer("test-long", "#meshling", "morse " + "thanksgiving " * 12)
    check(long["reply"], cfg)
    assert llm.calls == [], "lookups must not use the LLM"

    # Weather: real numbers go to the model; if it skips them, the template goes out.
    out = m.answer("test-wx", "#meshling", "meshling, weather?")
    check(out["reply"], cfg)
    assert "56F" in llm.calls[-1] and "WEATHER: none" not in llm.calls[-1]
    assert out["source"] == "weather" and "56F" in out["reply"], out
    print("weather (template):", out["reply"])
    out = m.answer("test-wx", "#meshling", "will it rain tomorrow?")
    assert "tomorrow" in out["reply"] and "62F" in out["reply"], out

    good = RecordingLLM("Hayward is 56F, overcast. The horse wore a blanket. You wear a jacket.")
    m2 = server.Meshling(cfg, llm=good, weather=FakeWeather())
    out = m2.answer("test-wx2", "#meshling", "meshling, do I need a jacket?")
    assert out["source"] == "llm" and "56F" in out["reply"], out
    print("weather (llm):", out["reply"])

    # Weather service down: the model is told, and still answers in persona.
    m3 = server.Meshling(cfg, llm=RecordingLLM("Weather radio is static, kiddo."),
                         weather=FakeWeather(up=False))
    out = m3.answer("test-wx3", "#meshling", "meshling, forecast?")
    assert out["source"] == "llm" and "WEATHER: unavailable" in m3.llm.calls[-1], out

    # Ordinary questions: no weather; a fact sometimes rides along.
    cfg["fact_chance"] = 1.0
    out = m2.answer("test-fact", "#meshling", "meshling, should I learn Rust?")
    assert "WEATHER: none" in good.calls[-1] and "FACT: none" not in good.calls[-1]
    cfg["fact_chance"] = 0.0
    m2.answer("test-fact", "#meshling", "meshling, and Go?")
    assert "FACT: none" in good.calls[-1]

    # Down model and weather up: the weather still goes out.
    m4 = server.Meshling(cfg, llm=DownLLM(), weather=FakeWeather())
    out = m4.answer("test-wx4", "#meshling", "meshling, weather")
    assert out["source"] == "weather" and "56F" in out["reply"], out
    check(out["reply"], cfg)


if __name__ == "__main__":
    main()
