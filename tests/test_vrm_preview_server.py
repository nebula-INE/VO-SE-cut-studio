from __future__ import annotations

import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWebEngineCore")

from vrm_preview import _ViewerHttpServer, VRMViewerWidget


def test_viewer_http_server_binds_loopback_and_serves_selected_vrm_only(
    tmp_path: Path,
) -> None:
    index = tmp_path / "index.html"
    index.write_text("<html>local viewer</html>", encoding="utf-8")
    model = tmp_path / "sample.vrm"
    model.write_bytes(b"glTF-sample-model")

    route = "/__axral_model__/random-test-token.vrm"
    server = _ViewerHttpServer(tmp_path, route)
    server.model_path = model
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        host, port = server.server_address
        assert host == "127.0.0.1"
        origin = f"http://{host}:{port}"

        with urllib.request.urlopen(origin + "/index.html", timeout=3) as response:
            assert response.status == 200
            assert response.read() == b"<html>local viewer</html>"

        with urllib.request.urlopen(origin + route, timeout=3) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "model/gltf-binary"
            assert response.read() == b"glTF-sample-model"

        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(origin + "/__axral_model__/not-the-token.vrm", timeout=3)
        assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)



def test_vrm_root_bone_keeps_position_and_normalizes_rotation() -> None:
    target = SimpleNamespace(_bones={})

    VRMViewerWidget.set_bone_transform_3d(
        target, "Hips", 0.1, 0.2, 0.3, 0.0, 0.0, 0.0, 2.0
    )

    assert target._bones["hips"] == {
        "rotation": (0.0, 0.0, 0.0, 1.0),
        "position": (0.1, 0.2, 0.3),
    }


def test_vrm_child_bone_keeps_model_rest_position() -> None:
    target = SimpleNamespace(_bones={})

    VRMViewerWidget.set_bone_transform_3d(
        target, "LeftUpperArm", 5.0, 6.0, 7.0, 0.0, 0.0, 0.0, 1.0
    )

    assert target._bones["leftUpperArm"] == {"rotation": (0.0, 0.0, 0.0, 1.0)}
    assert "position" not in target._bones["leftUpperArm"]

def test_timeline_motion_pose_overrides_live_pose_and_can_be_cleared() -> None:
    target = SimpleNamespace(_timeline_state=None)
    pose = {
        "bones": {
            "head": {
                "position": (0.1, 0.2, 0.3),
                "rotation": (0.0, 0.0, 0.0, 1.0),
            },
            "hips": {
                "position": (0.4, 0.0, 0.0),
                "rotation": (0.0, 0.0, 0.0, 1.0),
            },
            "not-a-vrm-bone": {"rotation": (0.0, 0.0, 0.0, 1.0)},
        },
        "expressions": {"happy": 0.7, "unknown": 0.4},
    }

    VRMViewerWidget.set_timeline_motion_state(target, pose)

    assert set(target._timeline_state["bones"]) == {"head", "hips"}
    assert target._timeline_state["bones"]["hips"]["position"] == (0.4, 0.0, 0.0)
    assert target._timeline_state["expressions"] == {"happy": 0.7}

    VRMViewerWidget.clear_timeline_motion_state(target)
    assert target._timeline_state is None


def test_timeline_motion_pose_rejects_invalid_quaternion() -> None:
    target = SimpleNamespace(_timeline_state=None)
    VRMViewerWidget.set_timeline_motion_state(target, {
        "bones": {
            "head": {"position": (0.0, 0.0, 0.0), "rotation": (0.0, 0.0, 0.0, 0.0)}
        },
        "expressions": {},
    })
    assert target._timeline_state["bones"] == {}

def test_timeline_motion_pose_preserves_only_head_and_hips_positions() -> None:
    target = SimpleNamespace(_timeline_state=None)
    VRMViewerWidget.set_timeline_motion_state(target, {
        "bones": {
            "leftUpperArm": {
                "position": (9.0, 9.0, 9.0),
                "rotation": (0.0, 0.0, 0.0, 1.0),
            },
            "hips": {
                "position": (0.1, 0.2, 0.3),
                "rotation": (0.0, 0.0, 0.0, 1.0),
            },
        },
        "expressions": {},
    })
    assert "position" not in target._timeline_state["bones"]["leftUpperArm"]
    assert target._timeline_state["bones"]["hips"]["position"] == (0.1, 0.2, 0.3)

