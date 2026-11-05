"""Strip only enclosing Markdown fences from model JSON responses."""

import pytest

from nanobot.channels.telegram.social.models import strip_json_fence


@pytest.mark.parametrize("text", [
    '```json\n{"value": 1}\n```',
    '```\n{"value": 1}\n```',
    '{"value": 1}',
    ' \n ```json\n \n{"value": 1}\n \n``` \n ',
    ' \n {"value": 1} \n ',
])
def test_strip_json_fence(text: str) -> None:
    assert strip_json_fence(text) == '{"value": 1}'
