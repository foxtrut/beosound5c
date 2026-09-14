"""Tests for the to-do store: input cleaning, limits, ordering, and
crash-safe persistence."""
from __future__ import annotations

import json
import os
import stat

import pytest

import sources.todo.service  # noqa: F401 — puts sources/todo/ on sys.path, as at runtime
import todo_store
from todo_store import MAX_ITEMS, MAX_TEXT, NotFound, TodoError, TodoStore, clean_text


# ── Text cleaning ────────────────────────────────────────────────────────────

def test_clean_text_collapses_whitespace_and_control_characters():
    assert clean_text("  Mælk \n\t 2 l ") == "Mælk 2 l"
    assert clean_text("Æg\x00\x1b[31m") == "Æg [31m"


def test_clean_text_drops_bidi_overrides_but_keeps_emoji_joiners():
    assert clean_text("abc‮def") == "abcdef"
    family = "\U0001F469‍\U0001F467"
    assert clean_text(f"{family} kage") == f"{family} kage"


def test_clean_text_normalises_to_nfc():
    assert clean_text("Café") == "Café"


@pytest.mark.parametrize("raw", ["", "   ", None, 5, ["x"], "x" * (MAX_TEXT + 1)])
def test_clean_text_rejects(raw):
    with pytest.raises(TodoError):
        clean_text(raw)


# ── Changes ──────────────────────────────────────────────────────────────────

def test_add_update_delete_bump_the_version(tmp_path):
    store = TodoStore(tmp_path / "todo.json")
    item, v1 = store.add("Mælk")
    assert item == {"id": item["id"], "text": "Mælk", "done": False}
    assert len(item["id"]) == 32

    ticked, v2 = store.update(item["id"], done=True)
    assert ticked["done"] is True
    renamed, v3 = store.update(item["id"], text="Havremælk")
    assert renamed == {"id": item["id"], "text": "Havremælk", "done": True}
    v4 = store.delete(item["id"])

    assert [v1, v2, v3, v4] == [1, 2, 3, 4]
    assert store.snapshot() == {"version": 4, "items": []}


def test_text_is_stored_verbatim_not_as_markup(tmp_path):
    store = TodoStore(tmp_path / "todo.json")
    item, _ = store.add("<img src=x onerror=alert(1)>")
    assert item["text"] == "<img src=x onerror=alert(1)>"


def test_open_items_are_listed_before_ticked_ones(tmp_path):
    store = TodoStore(tmp_path / "todo.json")
    ids = [store.add(t)[0]["id"] for t in ("a", "b", "c", "d")]
    store.update(ids[0], done=True)
    store.update(ids[2], done=True)
    assert [i["text"] for i in store.snapshot()["items"]] == ["b", "d", "a", "c"]


def test_clear_done_removes_only_ticked_items(tmp_path):
    store = TodoStore(tmp_path / "todo.json")
    keep = store.add("keep")[0]["id"]
    gone = store.add("gone")[0]["id"]
    store.update(gone, done=True)

    removed, version = store.clear_done()
    assert removed == 1
    assert [i["id"] for i in store.snapshot()["items"]] == [keep]

    assert store.clear_done() == (0, version), "nothing to clear must not write"


def test_list_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(todo_store, "MAX_ITEMS", 3)
    store = TodoStore(tmp_path / "todo.json")
    for text in ("a", "b", "c"):
        store.add(text)
    with pytest.raises(TodoError):
        store.add("d")
    assert MAX_ITEMS >= 100


@pytest.mark.parametrize("bad_id", ["", "../../etc/passwd", "A" * 32, "0" * 31, None, 7])
def test_malformed_or_unknown_ids_are_not_found(tmp_path, bad_id):
    store = TodoStore(tmp_path / "todo.json")
    store.add("x")
    with pytest.raises(NotFound):
        store.update(bad_id, done=True)
    with pytest.raises(NotFound):
        store.delete(bad_id)
    with pytest.raises(NotFound):
        store.delete("f" * 32)


def test_update_validates_its_fields(tmp_path):
    store = TodoStore(tmp_path / "todo.json")
    item_id = store.add("x")[0]["id"]
    with pytest.raises(TodoError):
        store.update(item_id)
    with pytest.raises(TodoError):
        store.update(item_id, done="yes")
    with pytest.raises(TodoError):
        store.update(item_id, text="   ")
    assert store.snapshot()["version"] == 1


# ── Persistence ──────────────────────────────────────────────────────────────

def test_list_survives_a_restart_in_a_private_file(tmp_path):
    path = tmp_path / "todo.json"
    store = TodoStore(path)
    item_id = store.add("Rugbrød")[0]["id"]
    store.update(item_id, done=True)

    again = TodoStore(path)
    assert again.snapshot() == {"version": 2,
                                "items": [{"id": item_id, "text": "Rugbrød", "done": True}]}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_failed_write_changes_nothing_and_leaves_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "todo.json"
    store = TodoStore(path)
    store.add("before")

    def boom(*_):
        raise OSError("disk full")

    monkeypatch.setattr(todo_store.os, "replace", boom)
    with pytest.raises(OSError):
        store.add("after")
    monkeypatch.undo()

    assert [i["text"] for i in store.snapshot()["items"]] == ["before"]
    assert store.snapshot()["version"] == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["todo.json"]


def test_unreadable_file_is_set_aside_not_trusted(tmp_path):
    path = tmp_path / "todo.json"
    path.write_text("{not json")
    store = TodoStore(path)
    assert store.snapshot() == {"version": 0, "items": []}
    assert any(p.name.startswith("todo.json.corrupt-") for p in tmp_path.iterdir())


def test_invalid_entries_are_dropped_on_load(tmp_path):
    good = {"id": "a" * 32, "text": "ok", "done": False}
    path = tmp_path / "todo.json"
    path.write_text(json.dumps({"version": True, "items": [
        good,
        dict(good),                                    # duplicate id
        {"id": "../x", "text": "bad id", "done": False},
        {"id": "b" * 32, "text": "", "done": False},
        {"id": "c" * 32, "text": "no flag"},
        "not an item",
    ]}))
    assert TodoStore(path).snapshot() == {"version": 0, "items": [good]}
