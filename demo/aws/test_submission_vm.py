import json
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tarfile import TarInfo
from tarfile import open as open_tar
from zipfile import ZipFile

import pytest
from submission_vm import approve

APPROVED = Path(__file__).with_name('submission.php').read_bytes()


def export(tmp_path, php, extra=()):
    bundle = tmp_path / 'frozen.tar.gz'
    with open_tar(bundle, 'w:gz') as tar:
        for name, value in (('repo-commit/submission.php', php), *extra):
            entry = TarInfo(name)
            entry.size = len(value)
            tar.addfile(entry, BytesIO(value))
    manifest = tmp_path / 'export.json'
    manifest.write_text(json.dumps({
        'schema': 1, 'commit': 'a' * 40, 'sha256': sha256(bundle.read_bytes()).hexdigest(),
        'round': 'round-1', 'team': 'demo', 'path': str(bundle),
    }))
    return manifest, bundle


def test_only_approved_php_and_organizer_recipe_enter_the_image(tmp_path):
    manifest, bundle = export(tmp_path, APPROVED, [('repo-commit/Dockerfile', b'NOT THE ORGANIZER RECIPE')])
    approved = approve(manifest, bundle)
    with ZipFile(BytesIO(approved.image_zip())) as image:
        assert image.namelist() == ['Dockerfile', 'submission.php']
        assert image.read('submission.php') == APPROVED
        assert image.read('Dockerfile') == Path(__file__).with_name('Submission.Dockerfile').read_bytes()


def test_matching_archive_checksum_does_not_approve_different_code(tmp_path):
    manifest, bundle = export(tmp_path, b'<?php echo "different fixture";')
    with pytest.raises(ValueError, match='not the reviewed fixture'):
        approve(manifest, bundle)


def test_modified_archive_fails_before_packing(tmp_path):
    manifest, bundle = export(tmp_path, APPROVED)
    bundle.write_bytes(bundle.read_bytes() + b'modified')
    with pytest.raises(ValueError, match='archive checksum'):
        approve(manifest, bundle)
