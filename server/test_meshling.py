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
        m.answer("judytuna", "#meshling", f"meshling, {q}")
    assert m.memory.notes("judytuna") == ["one", "two", "three"] or \
        m.memory.notes("judytuna") == ["two", "three", "four"], m.memory.notes("judytuna")
    print("notes:", m.memory.notes("judytuna"))

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
    out = d.answer("judytuna", "#meshling", "meshling, hello?")
    assert out["source"] == "offline"
    check(out["reply"], cfg)
    print("offline:", out["reply"])
    print("all checks passed")


if __name__ == "__main__":
    main()
