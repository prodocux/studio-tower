import pytest
from app.main import app
from app.services.storage import store
from fastapi.testclient import TestClient

client = TestClient(app)

# Helper headers
ALICE_AUTH = {"Authorization": "Bearer dev:alice_01:alice@example.com:Alice"}
BOB_AUTH = {"Authorization": "Bearer dev:bob_02:bob@example.com:Bob"}
CAROL_AUTH = {"Authorization": "Bearer dev:carol_03:carol@example.com:Carol"}


@pytest.fixture(autouse=True)
def clean_store():
    store.clear()
    yield
    store.clear()


def test_unauthenticated_request_returns_401():
    response = client.get("/v1/me")
    assert response.status_code == 401

    response = client.get("/v1/spaces")
    assert response.status_code == 401


def test_get_me():
    response = client.get("/v1/me", headers=ALICE_AUTH)
    assert response.status_code == 200
    data = response.json()
    assert data["uid"] == "alice_01"
    assert data["email"] == "alice@example.com"
    assert data["display_name"] == "Alice"


def test_agent_dm_auto_provisioning():
    response = client.get("/v1/spaces", headers=ALICE_AUTH)
    assert response.status_code == 200
    spaces = response.json()
    assert len(spaces) == 1
    dm = spaces[0]
    assert dm["kind"] == "agent_dm"
    assert dm["created_by"] == "alice_01"

    # Agent DM messages should have the welcome message
    msgs_res = client.get(f"/v1/spaces/{dm['space_id']}/messages", headers=ALICE_AUTH)
    assert msgs_res.status_code == 200
    msgs = msgs_res.json()
    assert len(msgs) == 1
    assert msgs[0]["role"] == "agent"


def test_two_account_isolation_alice_and_bob():
    # 1. Alice creates a shared space
    create_res = client.post(
        "/v1/spaces",
        json={"name": "Project Bersama"},
        headers=ALICE_AUTH,
    )
    assert create_res.status_code == 200
    alice_space = create_res.json()
    alice_space_id = alice_space["space_id"]

    # 2. Bob lists spaces -> should ONLY see Bob's own Agent DM, NOT Alice's space
    bob_spaces_res = client.get("/v1/spaces", headers=BOB_AUTH)
    assert bob_spaces_res.status_code == 200
    bob_space_ids = [s["space_id"] for s in bob_spaces_res.json()]
    assert alice_space_id not in bob_space_ids

    # 3. Bob attempts to GET Alice's space directly -> 403 Forbidden
    bob_get_res = client.get(f"/v1/spaces/{alice_space_id}", headers=BOB_AUTH)
    assert bob_get_res.status_code == 403

    # 4. Bob attempts to POST a message into Alice's space -> 403 Forbidden
    bob_post_res = client.post(
        f"/v1/spaces/{alice_space_id}/messages",
        json={"content": "I am intruding"},
        headers=BOB_AUTH,
    )
    assert bob_post_res.status_code == 403


def test_invite_accept_and_revoke_lifecycle():
    # Alice creates a space
    create_res = client.post(
        "/v1/spaces",
        json={"name": "Stunt & Disaster Unit"},
        headers=ALICE_AUTH,
    )
    space_id = create_res.json()["space_id"]

    # Alice creates an invite token
    invite_res = client.post(
        f"/v1/spaces/{space_id}/invites",
        json={},
        headers=ALICE_AUTH,
    )
    assert invite_res.status_code == 200
    token = invite_res.json()["token"]

    # Bob accepts the invite
    accept_res = client.post(
        f"/v1/invites/{token}/accept",
        headers=BOB_AUTH,
    )
    assert accept_res.status_code == 200
    assert accept_res.json()["space_id"] == space_id

    # Bob can now read and post messages in the space
    msg_res = client.post(
        f"/v1/spaces/{space_id}/messages",
        json={"content": "Bob joined the unit!"},
        headers=BOB_AUTH,
    )
    assert msg_res.status_code == 200
    assert msg_res.json()["sender_uid"] == "bob_02"

    # Alice creates another invite and then revokes it
    invite2_res = client.post(
        f"/v1/spaces/{space_id}/invites",
        json={},
        headers=ALICE_AUTH,
    )
    token2 = invite2_res.json()["token"]

    revoke_res = client.post(
        f"/v1/invites/{token2}/revoke",
        headers=ALICE_AUTH,
    )
    assert revoke_res.status_code == 200

    # Carol tries to accept the revoked invite -> 410 Gone
    carol_res = client.post(
        f"/v1/invites/{token2}/accept",
        headers=CAROL_AUTH,
    )
    assert carol_res.status_code == 410


def test_project_tagging_and_filtered_messages():
    # 1. Alice creates a space
    create_res = client.post(
        "/v1/spaces",
        json={"name": "Episodic Series A"},
        headers=ALICE_AUTH,
    )
    space_id = create_res.json()["space_id"]

    # 2. Add custom tags: #episode-1 and #vfx
    tag1_res = client.post(
        f"/v1/spaces/{space_id}/tags",
        json={"name": "Episode 1", "slug": "episode-1", "color": "#10B981"},
        headers=ALICE_AUTH,
    )
    assert tag1_res.status_code == 200
    tags = tag1_res.json()["tags"]
    assert any(t["slug"] == "episode-1" for t in tags)

    tag2_res = client.post(
        f"/v1/spaces/{space_id}/tags",
        json={"name": "VFX Review", "slug": "vfx", "color": "#8B5CF6"},
        headers=ALICE_AUTH,
    )
    assert tag2_res.status_code == 200

    # 3. Post messages with different tags
    client.post(
        f"/v1/spaces/{space_id}/messages",
        json={"content": "General production kickoff", "project_tag": "general"},
        headers=ALICE_AUTH,
    )
    client.post(
        f"/v1/spaces/{space_id}/messages",
        json={"content": "Episode 1 script breakdown", "project_tag": "episode-1"},
        headers=ALICE_AUTH,
    )
    client.post(
        f"/v1/spaces/{space_id}/messages",
        json={"content": "VFX drone shot render review", "project_tag": "vfx"},
        headers=ALICE_AUTH,
    )

    # 4. Query filtered by project_tag
    ep1_msgs = client.get(f"/v1/spaces/{space_id}/messages?tag=episode-1", headers=ALICE_AUTH).json()
    assert len(ep1_msgs) == 1
    assert ep1_msgs[0]["content"] == "Episode 1 script breakdown"

    vfx_msgs = client.get(f"/v1/spaces/{space_id}/messages?tag=vfx", headers=ALICE_AUTH).json()
    assert len(vfx_msgs) == 1
    assert vfx_msgs[0]["content"] == "VFX drone shot render review"

    all_msgs = client.get(f"/v1/spaces/{space_id}/messages?tag=all", headers=ALICE_AUTH).json()
    assert len(all_msgs) == 3
