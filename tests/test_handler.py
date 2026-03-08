import unittest
from unittest.mock import patch

import handler


class HandlerTests(unittest.TestCase):
    def test_validate_input_supports_aliases(self):
        payload = {
            "workflow": {"1": {"inputs": {"image": "input.png"}}},
            "inputs": [{"node": "1", "field": "seed", "value": 123}],
            "images": [{"name": "input.png", "image": "data:text/plain;base64,SGVsbG8="}],
            "output_mode": "inline",
        }

        normalized = handler.validate_input(payload)

        self.assertEqual(normalized["overrides"][0]["field"], "seed")
        self.assertEqual(normalized["assets"][0]["name"], "input.png")
        self.assertEqual(normalized["output_mode"], "inline")

    def test_validate_input_rejects_private_urls(self):
        payload = {
            "workflow": {"1": {"inputs": {}}},
            "assets": [{"name": "evil.png", "url": "http://127.0.0.1/evil.png"}],
        }

        normalized = handler.validate_input(payload)
        with self.assertRaises(ValueError):
            handler.prepare_assets("job-1", normalized["assets"])

    def test_apply_overrides(self):
        workflow = {
            "6": {"inputs": {"text": "old prompt"}},
            "53": {"inputs": {"seed": 1, "steps": 20}},
        }
        overrides = [
            {"node": "6", "field": "text", "value": "new prompt"},
            {"node": "53", "field": "seed", "value": 42},
        ]

        handler.apply_overrides(workflow, overrides)

        self.assertEqual(workflow["6"]["inputs"]["text"], "new prompt")
        self.assertEqual(workflow["53"]["inputs"]["seed"], 42)

    def test_remap_workflow_paths(self):
        workflow = {
            "10": {"inputs": {"image": "input.png", "mask": "mask.png"}},
            "11": {"inputs": {"nested": ["input.png", {"value": "mask.png"}]}},
        }
        mapping = {"input.png": "job-1/input.png", "mask.png": "job-1/mask.png"}

        remapped = handler.remap_workflow_paths(workflow, mapping)

        self.assertEqual(remapped["10"]["inputs"]["image"], "job-1/input.png")
        self.assertEqual(remapped["10"]["inputs"]["mask"], "job-1/mask.png")
        self.assertEqual(remapped["11"]["inputs"]["nested"][0], "job-1/input.png")

    @patch("handler.store_output_payload")
    @patch("handler.fetch_output_bytes")
    def test_collect_outputs_media_and_text(self, mock_fetch_output_bytes, mock_store_output_payload):
        prompt_history = {
            "outputs": {
                "1": {
                    "images": [{"filename": "image.png", "subfolder": "", "type": "output"}],
                    "text": ["hello world"],
                    "metadata": [{"foo": "bar"}],
                }
            }
        }
        mock_fetch_output_bytes.return_value = b"png"
        mock_store_output_payload.return_value = {
            "mode": "inline",
            "mime_type": "image/png",
            "size_bytes": 3,
            "data": "cG5n",
        }

        outputs = handler.collect_outputs("job-1", prompt_history, "inline")

        self.assertEqual(len(outputs), 3)
        image_output = outputs[0]
        self.assertEqual(image_output["filename"], "image.png")
        self.assertEqual(image_output["mode"], "inline")
        self.assertEqual(outputs[1]["type"], "text")
        self.assertEqual(outputs[2]["type"], "json")


if __name__ == "__main__":
    unittest.main()
