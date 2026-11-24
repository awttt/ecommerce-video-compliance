import base64

from app.core.direct_image import (
    IMAGE_KEY_NAME,
    EDITS_URL,
    GENERATIONS_URL,
    image_key_from_items,
    load_jobs,
    read_saved_file,
    remove_job,
    store_result_images,
    upstream_request,
    write_job,
)
from app.core.relay import KEY_NAME


def test_image_key_is_not_the_review_key():
    secret, item = image_key_from_items(
        [
            {"name": KEY_NAME, "group_id": 15, "key": "sk-review"},
            {"name": IMAGE_KEY_NAME, "group_id": 16, "group_name": "生图专用-bot创建", "key": "sk-image"},
        ]
    )
    assert secret == "sk-image"
    assert item["group_name"] == "生图专用-bot创建"


def test_size_and_quality_go_to_generations():
    url, payload, files = upstream_request(
        {"prompt": "红方块", "model": "gpt-image-1", "n": 1, "size": "1024x1024", "quality": "low"}
    )
    assert url == GENERATIONS_URL
    assert files is None
    assert payload == {"model": "gpt-image-1", "prompt": "红方块", "n": 1, "size": "1024x1024", "quality": "low"}


def test_reference_image_goes_to_edits():
    raw = base64.b64encode(b"\x89PNG\r\n").decode()
    url, payload, files = upstream_request(
        {
            "prompt": "改成蓝色",
            "model": "gpt-image-1",
            "n": 1,
            "size": "1024x1536",
            "quality": "high",
            "references": [f"data:image/png;base64,{raw}"],
        }
    )
    assert url == EDITS_URL
    assert payload["size"] == "1024x1536"
    assert payload["quality"] == "high"
    assert files[0][0] == "image"
    assert files[0][1][2] == "image/png"


def test_generated_images_stay_on_disk_after_reload():
    png = b"\x89PNG\r\n\x1a\n" + b"pixels"
    encoded = base64.b64encode(png).decode()
    job_id = "a" * 32
    images = store_result_images(job_id, [f"data:image/png;base64,{encoded}"])
    assert images[0]["url"].endswith("/files/image-1.png")
    write_job(
        {
            "id": job_id,
            "prompt": "红色礼盒",
            "model": "gpt-image-2.5-flare",
            "created_at": "2026-11-24T10:00:00+00:00",
            "orientation": "square",
            "images": images,
        }
    )
    again = load_jobs()
    assert [item["id"] for item in again] == [job_id]
    assert "base64" not in again[0]["images"][0]["url"]
    saved = read_saved_file(job_id, "image-1.png")
    assert saved is not None
    assert saved[0] == png
    assert saved[1] == "image/png"
    remove_job(job_id)
    assert load_jobs() == []
    assert read_saved_file(job_id, "image-1.png") is None


def test_generated_image_is_saved_for_download(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.direct_image.image_jobs_root", lambda: tmp_path)
    raw = b"\x89PNG\r\n\x1a\n" + b"png-bytes"
    url = "data:image/png;base64," + base64.b64encode(raw).decode()
    job_id = "a" * 32
    images = store_result_images(job_id, [url])
    assert images[0]["url"] == f"/v1/image-workbench/jobs/{job_id}/files/image-1.png"
    saved = read_saved_file(job_id, "image-1.png")
    assert saved is not None
    assert saved[0] == raw
    assert saved[1] == "image/png"


def test_missing_image_key_does_not_fall_back_to_review_key():
    try:
        image_key_from_items([{"name": KEY_NAME, "key": "sk-review"}])
    except LookupError as exc:
        assert "生图专用" in str(exc)
        assert "不会改用审核密钥" in str(exc)
    else:
        raise AssertionError("should reject")
