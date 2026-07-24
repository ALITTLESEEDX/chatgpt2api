from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.config import _normalize_image_storage_settings, _validate_image_storage_settings
from services.image_failure import image_failure
from services.image_storage_service import ImageStorageError, ImageStorageService
from services.r2_client import CloudflareR2Client, R2Error


S3_SETTINGS = {
    "enabled": True,
    "mode": "s3",
    "s3_account_id": "account",
    "s3_access_key_id": "access",
    "s3_secret_access_key": "secret",
    "s3_bucket": "images",
    "s3_prefix": "chatgpt2api/images",
    "public_base_url": "",
}


class FakeR2Client:
    objects: dict[str, bytes] = {}
    fail_upload = False

    def __init__(self, settings: dict[str, object], *, object_label: str = "对象") -> None:
        self.prefix = str(settings.get("prefix") or "").strip("/")

    def upload_bytes(self, key: str, payload: bytes, **kwargs) -> dict[str, object]:
        if self.fail_upload:
            raise R2Error("upload failed")
        object_key = "/".join(part for part in (self.prefix, key.lstrip("/")) if part)
        self.objects[object_key] = payload
        return {"key": object_key, "etag": "test"}

    def download_bytes(self, key: str, *, include_prefix: bool = False) -> bytes:
        object_key = key if include_prefix else "/".join((self.prefix, key))
        return self.objects[object_key]

    def delete_object(self, key: str, *, include_prefix: bool = False) -> bool:
        object_key = key if include_prefix else "/".join((self.prefix, key))
        return self.objects.pop(object_key, None) is not None

    def close(self) -> None:
        pass


class ImageStorageS3Tests(unittest.TestCase):
    def setUp(self) -> None:
        FakeR2Client.objects = {}
        FakeR2Client.fail_upload = False
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.service = ImageStorageService(Path(self.temp_dir.name) / "image_index.json")
        self.service.settings = lambda: dict(S3_SETTINGS)  # type: ignore[method-assign]

    def test_s3_save_waits_for_upload_and_indexes_object(self) -> None:
        with (
            patch("services.image_storage_service.CloudflareR2Client", FakeR2Client),
            patch("services.image_storage_service.config.cleanup_old_images", return_value=0),
        ):
            stored = self.service.save(b"image bytes", "https://example.test")

        self.assertEqual(stored.storage, "s3")
        self.assertIn(stored.rel, self.service._load_index())
        item = self.service._load_index()[stored.rel]
        self.assertTrue(item["s3"])
        self.assertEqual(FakeR2Client.objects[item["s3_key"]], b"image bytes")

    def test_s3_upload_failure_does_not_mark_image_as_stored(self) -> None:
        FakeR2Client.fail_upload = True
        with (
            patch("services.image_storage_service.CloudflareR2Client", FakeR2Client),
            patch("services.image_storage_service.config.cleanup_old_images", return_value=0),
            self.assertRaises(ImageStorageError),
        ):
            self.service.save(b"image bytes")

        self.assertEqual(self.service._load_index(), {})

    def test_s3_only_image_can_be_read_and_deleted(self) -> None:
        with (
            patch("services.image_storage_service.CloudflareR2Client", FakeR2Client),
            patch("services.image_storage_service.config.cleanup_old_images", return_value=0),
        ):
            stored = self.service.save(b"image bytes")
            with patch("services.image_storage_service._local_image_path", return_value=Path(self.temp_dir.name) / "missing.png"):
                self.assertEqual(self.service.get_bytes(stored.rel), b"image bytes")
                self.assertTrue(self.service.delete(stored.rel))

        self.assertEqual(FakeR2Client.objects, {})
        self.assertEqual(self.service._load_index(), {})


class ImageStorageConfigTests(unittest.TestCase):
    def test_s3_settings_are_normalized_and_validated(self) -> None:
        settings = _normalize_image_storage_settings(S3_SETTINGS)
        self.assertEqual(settings["mode"], "s3")
        self.assertEqual(settings["s3_prefix"], "chatgpt2api/images")
        _validate_image_storage_settings(settings)

    def test_s3_settings_require_credentials(self) -> None:
        settings = _normalize_image_storage_settings({"enabled": True, "mode": "s3"})
        with self.assertRaisesRegex(ValueError, "Account ID"):
            _validate_image_storage_settings(settings)

    def test_storage_failure_does_not_switch_or_verify_account(self) -> None:
        failure = image_failure("image_storage_failed")
        self.assertFalse(failure.switch_account)
        self.assertFalse(failure.verify_account)
        self.assertTrue(failure.retryable)


class R2ClientTests(unittest.TestCase):
    def test_object_key_adds_prefix_once(self) -> None:
        client = CloudflareR2Client(
            {
                "account_id": "account",
                "access_key_id": "access",
                "secret_access_key": "secret",
                "bucket": "bucket",
                "prefix": "chatgpt2api/images/",
            }
        )
        self.addCleanup(client.close)
        self.assertEqual(client.object_key("2026/07/image.png"), "chatgpt2api/images/2026/07/image.png")


if __name__ == "__main__":
    unittest.main()
