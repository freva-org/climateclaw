import climateclaw.services.storage.mongodb_storage as mongo_storage
import pytest
from climateclaw.services.storage.mongodb_storage import (
    MONGODB_COLLECTION_NAME,
    ThreadStorage,
)
from climateclaw.services.streaming.stream_variants import (
    SVAssistant,
    SVPrompt,
    SVStreamEnd,
    SVUser,
)


@pytest.mark.asyncio
async def test_save_and_read_thread(monkeypatch, patch_mongodb, GOOD_HEADERS):
    async def fake_topic(content):
        return "topic"

    monkeypatch.setattr(mongo_storage, "summarize_topic", fake_topic, raising=True)

    storage = await ThreadStorage.create()

    tid = "T123"
    user_id = "alice"

    # write prompt (no auto end)
    await storage.save_thread(
        thread_id=tid,
        user_id=user_id,
        content=[SVPrompt(payload='[{"role":"system","content":"s"}]')],
        append_to_existing=True,
    )
    # write user + assistant + explicit end
    await storage.save_thread(
        thread_id=tid,
        user_id=user_id,
        content=[SVUser(text="hi")],
        append_to_existing=True,
    )
    await storage.save_thread(
        thread_id=tid,
        user_id=user_id,
        content=[SVAssistant(text="hello"), SVStreamEnd(message="Done")],
        append_to_existing=True,
    )

    coll = patch_mongodb[MONGODB_COLLECTION_NAME]
    assert tid in coll.storage

    # Read back as wire variants (dicts)
    conv = await storage.read_thread(tid)
    kinds = [v.get("variant") for v in conv]
    # Prompt, User, Assistant, StreamEnd (no unexpected extra StreamEnd)
    assert kinds == ["Prompt", "User", "Assistant", "StreamEnd"]
    assert coll.storage[tid]["content"] == conv

    # Check the user_id is stored correctly
    assert (
        coll.storage[tid]["user_id"]
        == user_id
        == await storage.get_user_id_for_thread(tid)
    )
