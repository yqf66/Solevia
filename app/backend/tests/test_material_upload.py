from fastapi.testclient import TestClient

from planner_api.materials.worker import MaterialProcessingWorker


def test_direct_upload_creates_queued_material_and_rejects_unsafe_path(
    client: TestClient, session_factory
) -> None:
    goal_id = client.post("/api/goals", json={"raw_input": "直接上传资料"}).json()["goal_id"]
    uploaded = client.post(
        f"/api/goals/{goal_id}/materials/upload?filename=docs%2Fdelivery.md",
        content=b"# Delivery\nMust finish before June.",
        headers={"Content-Type": "text/markdown"},
    )
    assert uploaded.status_code == 201, uploaded.text
    material = uploaded.json()
    assert material["title"] == "delivery.md"
    assert material["latest_revision"]["status"] == "QUEUED"
    assert MaterialProcessingWorker(session_factory).run_once() is True
    listed = client.get(f"/api/goals/{goal_id}/materials").json()
    assert listed[0]["latest_revision"]["status"] == "READY"
    assert listed[0]["latest_revision"]["metadata"]["chunk_count"] >= 1

    unsafe = client.post(
        f"/api/goals/{goal_id}/materials/upload?filename=..%2Fsecret.md",
        content=b"secret",
        headers={"Content-Type": "text/markdown"},
    )
    assert unsafe.status_code == 422
