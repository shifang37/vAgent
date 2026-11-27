"""P-12: every frozen tool surface is reproduced without the project-memory tools.

An old Run is restored from its recorded ``toolFeatures`` (or, when it recorded none, from
the pre-memory core surface). If any frozen variant leaked the two new tools, the restore
would either publish tools the Run never saw or refuse the resume with a signature
mismatch. These tests pin both halves: no frozen surface contains ``memory_*``, and the
current 20/18 surface does.
"""

import pytest
from shot_support import mock_tools

from vagent.tools import MEMORY_TOOL_NAMES, PROJECT_MEMORY_FEATURE, create_project_tools
from vagent.video.tools import (
    LIVE_VIDEO_RULES_VERSION,
    LIVE_VIDEO_TOOLS_VERSION,
    VIDEO_RULES_VERSION,
    VIDEO_TOOLS_VERSION,
    register_video_tools,
)

# Every toolsVersion a pre-P2 Run may have recorded, plus the 18/18 surface P2 supersedes.
FROZEN_VIDEO_VERSIONS = (4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 16, 18)
# Execution versions 1 and 2 were published before the capability; 3 is current.
FROZEN_EXECUTION_VERSIONS = (1, 2)


def surface(store):
    registry, _service, _adapter, _context = mock_tools(store)
    return registry


def names(registry):
    return {spec["function"]["name"] for spec in registry.specs()}


def test_the_published_surface_is_twenty_eighteen_with_both_memory_tools(store):
    assert (VIDEO_TOOLS_VERSION, VIDEO_RULES_VERSION) == (20, 18)
    assert (LIVE_VIDEO_TOOLS_VERSION, LIVE_VIDEO_RULES_VERSION) == (20, 18)
    tools = surface(store)
    assert set(MEMORY_TOOL_NAMES).issubset(names(tools))
    assert tools.features[PROJECT_MEMORY_FEATURE] == {"version": 1}


@pytest.mark.parametrize("version", FROZEN_VIDEO_VERSIONS)
def test_no_frozen_tool_surface_contains_the_memory_tools(store, version):
    tools = surface(store)
    frozen = tools.for_tools_version(version)
    assert frozen.features["video"]["toolsVersion"] == version
    assert not set(MEMORY_TOOL_NAMES) & names(frozen)
    assert PROJECT_MEMORY_FEATURE not in frozen.features


@pytest.mark.parametrize("version", FROZEN_EXECUTION_VERSIONS)
def test_no_frozen_execution_surface_contains_the_memory_tools(store, version):
    frozen = surface(store).for_execution_version(version)
    assert not set(MEMORY_TOOL_NAMES) & names(frozen)
    assert PROJECT_MEMORY_FEATURE not in frozen.features


def test_the_18_variant_reproduces_the_surface_recorded_just_before_p2(store):
    tools = surface(store)
    frozen = tools.for_tools_version(18)
    current = tools.features["video"]
    # Same signed configuration as a Run recorded by the previous release.
    assert frozen.features["video"] == {
        "mode": "mock",
        "toolsVersion": 18,
        "rulesVersion": 18,
        "capabilities": current["capabilities"],
        "tts": current["tts"],
    }
    assert not set(MEMORY_TOOL_NAMES) & names(frozen)
    assert "timeline_save" in names(frozen)


def test_a_recorded_pre_memory_feature_set_restores_without_the_memory_tools(store):
    tools = surface(store)
    recorded = tools.for_tools_version(18).features
    restored = tools.for_record_features(recorded)
    assert restored is not None and restored.features == recorded
    assert not set(MEMORY_TOOL_NAMES) & names(restored)


def test_a_run_that_recorded_no_features_restores_the_core_surface_of_its_time():
    tools = create_project_tools()
    assert set(MEMORY_TOOL_NAMES).issubset(names(tools))
    restored = tools.for_record_features({})
    assert restored is not None and restored.features == {}
    assert not set(MEMORY_TOOL_NAMES) & names(restored)
    # The rollback switch publishes exactly the surface such a Run signed.
    legacy = create_project_tools(memory=False)
    assert legacy.features == {}
    assert names(legacy) == names(restored)


def test_the_rollback_switch_removes_the_tools_and_the_feature():
    legacy = create_project_tools(memory=False)
    assert not set(MEMORY_TOOL_NAMES) & names(legacy)
    assert PROJECT_MEMORY_FEATURE not in legacy.features
    assert "project_read" in names(legacy)


def test_the_read_only_view_keeps_only_the_memory_reader(store):
    read_only = surface(store).read_only()
    assert "memory_read" in names(read_only)
    assert "memory_update" not in names(read_only)
    assert PROJECT_MEMORY_FEATURE in read_only.features


def test_registering_video_tools_keeps_every_frozen_surface_memory_free(store):
    registry, service, _adapter, _context = mock_tools(store)
    tools = register_video_tools(create_project_tools(), service)
    assert set(MEMORY_TOOL_NAMES).issubset(names(tools))
    for version in FROZEN_VIDEO_VERSIONS:
        frozen = tools.for_tools_version(version)
        assert frozen.features["video"]["toolsVersion"] == version
        assert not set(MEMORY_TOOL_NAMES) & names(frozen)
    for version in FROZEN_EXECUTION_VERSIONS:
        assert not set(MEMORY_TOOL_NAMES) & names(tools.for_execution_version(version))
