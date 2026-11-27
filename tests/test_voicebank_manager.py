from pathlib import Path
import zipfile

import pytest

from voicebank_manager import VoiceBankManager, _safe_zip_member_target


def test_safe_zip_member_stays_inside_destination(tmp_path: Path) -> None:
    destination = tmp_path / "voicebank"
    target = _safe_zip_member_target(destination, "voice/root.wav")
    assert target == destination.resolve() / "voice" / "root.wav"


@pytest.mark.parametrize(
    "member",
    [
        "../outside.wav",
        "voice/../../outside.wav",
        "../voicebank-escape/payload.wav",
        r"voice\..\outside.wav",
        r"C:\Windows\system.ini",
        r"\\server\share\outside.wav",
        "/absolute/outside.wav",
        "voice/\x00payload.wav",
    ],
)
def test_safe_zip_member_rejects_traversal_and_absolute_paths(
    tmp_path: Path, member: str
) -> None:
    with pytest.raises(ValueError):
        _safe_zip_member_target(tmp_path / "voicebank", member)


def test_import_rejects_malicious_zip_before_extracting_any_file(tmp_path: Path) -> None:
    archive = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("safe/first.wav", b"safe")
        zf.writestr("../outside.wav", b"escaped")

    destination = tmp_path / "voicebanks"
    manager = VoiceBankManager()
    with pytest.raises(ValueError, match="不正なパス"):
        manager.import_voicebank(str(archive), str(destination), display_name="malicious")

    assert not (tmp_path / "outside.wav").exists()
    assert not (destination / "malicious").exists()
