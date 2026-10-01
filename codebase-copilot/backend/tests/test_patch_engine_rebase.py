import pytest
from app.agent.patch_engine import PatchEngine

def test_rebase_staged_diff_noop_clean():
    """When the editor buffer is unchanged from base, rebase returns clean."""
    base_content = "def hello():\n    return 'world'\n"
    diff_text = (
        "--- a/test.py\n"
        "+++ b/test.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def hello():\n"
        "-    return 'world'\n"
        "+    return 'earth'\n"
    )
    current_content = base_content

    result = PatchEngine.rebase_staged_diff(
        file_path="test.py",
        base_content=base_content,
        diff_text=diff_text,
        current_content=current_content
    )

    assert result["status"] == "clean"
    assert result["has_collision"] is False
    assert result["diff"] == diff_text

def test_rebase_staged_diff_already_applied():
    """When the editor buffer already matches the agent proposed state."""
    base_content = "def hello():\n    return 'world'\n"
    diff_text = (
        "--- a/test.py\n"
        "+++ b/test.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def hello():\n"
        "-    return 'world'\n"
        "+    return 'earth'\n"
    )
    current_content = "def hello():\n    return 'earth'\n"

    result = PatchEngine.rebase_staged_diff(
        file_path="test.py",
        base_content=base_content,
        diff_text=diff_text,
        current_content=current_content
    )

    assert result["status"] == "already_applied"
    assert result["has_collision"] is False

def test_rebase_staged_diff_disjoint_shift():
    """
    User added lines at the top of the file in the editor buffer.
    Agent's diff targets the bottom of the file.
    Rebase must cleanly incorporate user edits and shift diff hunk anchors.
    """
    base_content = (
        "def helper():\n"
        "    return 1\n"
        "\n"
        "def target():\n"
        "    return 'old'\n"
    )
    diff_text = (
        "--- a/test.py\n"
        "+++ b/test.py\n"
        "@@ -4,2 +4,2 @@\n"
        " def target():\n"
        "-    return 'old'\n"
        "+    return 'new'\n"
    )
    # User added 2 import lines at the top
    current_content = (
        "import os\n"
        "import sys\n"
        "def helper():\n"
        "    return 1\n"
        "\n"
        "def target():\n"
        "    return 'old'\n"
    )

    result = PatchEngine.rebase_staged_diff(
        file_path="test.py",
        base_content=base_content,
        diff_text=diff_text,
        current_content=current_content
    )

    assert result["status"] == "rebased"
    assert result["has_collision"] is False
    assert len(result["conflicts"]) == 0
    # Diff must be shifted and contain the target change
    assert "-    return 'old'" in result["diff"]
    assert "+    return 'new'" in result["diff"]
    # Check that applying rebased diff onto current_content produces clean output
    patched_lines = PatchEngine._apply_unified_diff(current_content.splitlines(), result["diff"].splitlines())
    patched_text = "\n".join(patched_lines) + "\n"
    assert "import os" in patched_text
    assert "import sys" in patched_text
    assert "return 'new'" in patched_text

def test_rebase_staged_diff_collision_detected():
    """
    User modified the exact same function line that the Agent diff touches.
    Rebase must detect collision and provide conflict markers.
    """
    base_content = (
        "def compute():\n"
        "    value = 10\n"
        "    return value\n"
    )
    diff_text = (
        "--- a/test.py\n"
        "+++ b/test.py\n"
        "@@ -2,2 +2,2 @@\n"
        "-    value = 10\n"
        "+    value = 20\n"
        "     return value\n"
    )
    # User simultaneously changed value = 10 to value = 99 in editor
    current_content = (
        "def compute():\n"
        "    value = 99\n"
        "    return value\n"
    )

    result = PatchEngine.rebase_staged_diff(
        file_path="test.py",
        base_content=base_content,
        diff_text=diff_text,
        current_content=current_content
    )

    assert result["status"] == "conflict"
    assert result["has_collision"] is True
    assert len(result["conflicts"]) > 0
    assert "<<<<<<< YOUR CHANGES" in result["diff"]
    assert ">>>>>>> CLAY AGENT" in result["diff"]

def test_rebase_staged_diff_empty_base_new_file():
    """
    On an empty / new file, user typed some imports, agent also generated code.
    Both line 0 insertions must be complementary rather than a collision.
    """
    base_content = ""
    diff_text = (
        "--- a/new_file.py\n"
        "+++ b/new_file.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def agent_func():\n"
        "+    return True\n"
    )
    current_content = "import os\n"

    result = PatchEngine.rebase_staged_diff(
        file_path="new_file.py",
        base_content=base_content,
        diff_text=diff_text,
        current_content=current_content
    )

    assert result["status"] == "rebased"
    assert result["has_collision"] is False
    assert len(result["conflicts"]) == 0
    assert "+def agent_func():" in result["diff"]

def test_rebase_staged_diff_force_resolution_agent_wins():
    """
    When collision exists but force_resolution == 'agent_wins',
    the agent hunk overwrites the user collision cleanly.
    """
    base_content = "x = 1\n"
    diff_text = (
        "--- a/test.py\n"
        "+++ b/test.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-x = 1\n"
        "+x = 100\n"
    )
    current_content = "x = 50\n"

    result = PatchEngine.rebase_staged_diff(
        file_path="test.py",
        base_content=base_content,
        diff_text=diff_text,
        current_content=current_content,
        force_resolution="agent_wins"
    )

    assert result["status"] == "rebased"
    assert result["has_collision"] is False
    assert len(result["conflicts"]) == 0
    assert "-x = 50" in result["diff"]
    assert "+x = 100" in result["diff"]
