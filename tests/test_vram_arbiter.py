# -*- coding: utf-8 -*-
"""Мок-тесты: VRAM-арбитр (без реальных GPU-вызовов где возможно)."""
import sys
sys.path.insert(0, r"C:/Users/mgosh/localis-public")
sys.path.insert(1, r"C:/Users/mgosh/localis-public/.venv/Lib/site-packages")

from core.vram_arbiter import VRAMArbiter, classify


def test_classify():
    assert classify("Qwen3-Coder-Next-UD-IQ3_XXS") == "xlarge"
    assert classify("nemotron-3.5-lightning:30b-a3b") == "large"
    assert classify("qwen3.6:35b-a3b") == "large"
    assert classify("glm-4.7-flash") == "medium"
    assert classify("qwen2.5:3b-instruct") == "tiny"
    # явное указание в extra сильнее эвристики
    assert classify("glm-4.7-flash", {"memory_class": "xlarge"}) == "xlarge"


def test_exclusive_slot_serializes_heavy():
    arb = VRAMArbiter()
    events = []
    info_a = arb.acquire("large", "nemotron-30b", on_event=events.append)
    assert info_a is not None and info_a["class"] == "large"

    acquired_b = []

    def try_acquire_b():
        info = arb.acquire("large", "qwen3.6-35b")
        acquired_b.append(info)

    import threading
    t = threading.Thread(target=try_acquire_b)
    t.start()
    import time
    time.sleep(0.3)
    # b не должен захватить слот, пока a держит
    assert not acquired_b, "exclusive slot must block second heavy model"
    arb.release(info_a)
    t.join(timeout=10)
    assert acquired_b, "b должен получить слот после release"
    arb.release(acquired_b[0])


def test_light_models_pass_without_slot():
    arb = VRAMArbiter()
    info = arb.acquire("tiny", "qwen2.5:3b-instruct")
    assert info is None  # лёгкие проходят свободно


if __name__ == "__main__":
    test_classify()
    test_exclusive_slot_serializes_heavy()
    test_light_models_pass_without_slot()
    print("ALL ARBITER MOCK TESTS PASSED")
