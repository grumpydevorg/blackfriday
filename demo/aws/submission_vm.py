# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3==1.44.9", "pydantic==2.13.4"]
# ///
"""Build and run only the reviewed PHP fixture from a frozen BLACK FRIDAY Git export."""

import argparse
import gzip
import json
import os
import re
import sys
import tarfile
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from secrets import randbelow
from time import monotonic, sleep
from typing import Annotated
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from evaluator import AWS_CONFIG, VM_LIFESPAN, Fleet
from operate import (
    ACCOUNT,
    AWS_DIR,
    REGION,
    STACK,
    account_session,
    available_base_version,
    stack_outputs,
)
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_FIXTURE = 64 * 1024
ROUTE = '/quote?quantity='


class Export(BaseModel):
    model_config = ConfigDict(strict=True)
    schema_version: int = Field(alias='schema')
    commit: Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{40}$')]
    sha256: Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{64}$')]


def bounded_read(file, limit: int) -> bytes:
    data = file.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f'Fixture input exceeds {limit} bytes')
    return data


@dataclass(frozen=True)
class Approved:
    commit: str
    archive_sha256: str
    php: bytes

    def image_zip(self) -> bytes:
        data = BytesIO()
        with ZipFile(data, 'w', ZIP_DEFLATED) as package:
            for name, content in (
                ('Dockerfile', (AWS_DIR / 'Submission.Dockerfile').read_bytes()),
                ('submission.php', self.php),
            ):
                info = ZipInfo(name)
                info.create_system = 3
                info.external_attr = 0o100644 << 16  # The unprivileged guest must be able to read the PHP.
                package.writestr(info, content, compress_type=ZIP_DEFLATED)
        return data.getvalue()


def approve(manifest: Path, bundle: Path) -> Approved:
    with manifest.open('rb') as file:
        release = Export.model_validate_json(bounded_read(file, 4096))
    if release.schema_version != 1:
        raise ValueError('Expected a schema 1 submissions.py export')
    with bundle.open('rb') as file:
        compressed = bounded_read(file, MAX_FIXTURE)
    if sha256(compressed).hexdigest() != release.sha256:
        raise ValueError('Frozen archive checksum does not match the export')
    with gzip.GzipFile(fileobj=BytesIO(compressed)) as file:
        tar_bytes = bounded_read(file, MAX_FIXTURE)
    with tarfile.open(fileobj=BytesIO(tar_bytes), mode='r:') as source:
        entries = [entry for entry in source if re.fullmatch(r'[a-zA-Z0-9_.-]+/submission\.php', entry.name)]
        if len(entries) != 1 or entries[0].type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
            raise ValueError('Expected one regular submission.php at the root of the Git archive')
        with source.extractfile(entries[0]) as file:
            php = bounded_read(file, MAX_FIXTURE)
    if php != (AWS_DIR / 'submission.php').read_bytes():
        raise ValueError('Frozen PHP is not the reviewed fixture; no code was uploaded or executed')
    return Approved(release.commit, release.sha256, php)


def announce(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


@contextmanager
def uploaded(s3, bucket: str, key: str, image: bytes):
    result = s3.put_object(Bucket=bucket, Key=key, Body=image, IfNoneMatch='*', ContentType='application/zip')
    version = result.get('VersionId')
    if not version or version == 'null':
        # The caller verified versioning; never guess at an object version to delete.
        raise RuntimeError(f'S3 did not return a version for the provisional object {key}')
    try:
        yield f's3://{bucket}/{key}'
    finally:
        s3.delete_object(Bucket=bucket, Key=key, VersionId=version)
        try:
            s3.head_object(Bucket=bucket, Key=key, VersionId=version)
        except ClientError as error:
            if error.response['ResponseMetadata']['HTTPStatusCode'] != 404:
                raise
        else:
            raise RuntimeError(f'Provisional object version remains: {key}')
        announce('Temporary S3 archive version removed')


def await_image(client, arn: str, timeout: int, *, deleting: bool = False) -> str:
    deadline = monotonic() + timeout
    delete_requested = False
    while monotonic() < deadline:
        if deleting and not delete_requested:
            try:
                client.delete_microvm_image(imageIdentifier=arn)
                delete_requested = True
            except client.exceptions.ConflictException:
                pass  # AWS may still be finishing an interrupted image build.
            except client.exceptions.ResourceNotFoundException:
                return ''
        try:
            image = client.get_microvm_image(imageIdentifier=arn)
        except client.exceptions.ResourceNotFoundException:
            if deleting:
                return ''
        else:
            state = image['state']
            if deleting and state == 'DELETED':
                return ''
            if not deleting and state == 'CREATED':
                return image['latestActiveImageVersion']
            if state == ('DELETE_FAILED' if deleting else 'CREATE_FAILED'):
                raise RuntimeError(f'MicroVM image {arn} reached {state}')
        sleep(2)
    raise TimeoutError(f'MicroVM image {arn} did not finish {"deleting" if deleting else "building"}')


@contextmanager
def temporary_image(client, *, name: str, uri: str, build_role: str, base_version: str):
    arn = f'arn:aws:lambda:{REGION}:{ACCOUNT}:microvm-image:{name}'
    connector = f'arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS'
    try:
        client.create_microvm_image(
            name=name, clientToken=name, description='Disposable reviewed BLACK FRIDAY Git submission fixture',
            baseImageArn=f'arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1', baseImageVersion=base_version,
            buildRoleArn=build_role, codeArtifact={'uri': uri}, cpuConfigurations=[{'architecture': 'ARM_64'}],
            resources=[{'minimumMemoryInMiB': 1024}], egressNetworkConnectors=[connector],
            hooks={'port': 8080, 'microvmImageHooks': {'ready': 'ENABLED', 'readyTimeoutInSeconds': 30}},
            logging={'disabled': {}}, tags={'project': 'black-friday-demo', 'purpose': 'trusted-submission-proof'},
        )
        announce(f'Building temporary image {name}')
        yield arn, await_image(client, arn, 420)
    finally:
        await_image(client, arn, 240, deleting=True)
        announce(f'Temporary image removed: {name}')


@contextmanager
def approved_fleet(artifact: Approved):
    os.environ['AWS_IGNORE_CONFIGURED_ENDPOINT_URLS'] = 'true'
    with redirect_stdout(sys.stderr):
        session = account_session()
    outputs = stack_outputs(session, STACK)
    bucket = outputs['ArtifactBucket']
    alias = session.client('cloudformation').describe_stack_resource(
        StackName=STACK, LogicalResourceId='ImageBuildRole',
    )['StackResourceDetail']['PhysicalResourceId']
    role = session.client('iam').get_role(RoleName=alias)['Role']['Arn']
    base_version = available_base_version(session)
    name = f'black-friday-submission-{uuid4().hex[:16]}'
    key = f'packages/image/{name}.zip'
    s3 = session.client('s3', config=AWS_CONFIG)
    if s3.get_bucket_versioning(Bucket=bucket).get('Status') != 'Enabled':
        raise RuntimeError('The rehearsal artifact bucket must have versioning enabled')
    client = session.client('lambda-microvms', config=AWS_CONFIG)
    boto3.DEFAULT_SESSION = session  # Fleet uses the account-checked operator session.
    with (
        uploaded(s3, bucket, key, artifact.image_zip()) as uri,
        temporary_image(client, name=name, uri=uri, build_role=role, base_version=base_version) as image,
    ):
        arn, version = image
        os.environ.update(IMAGE_ARN=arn, IMAGE_VERSION=version)
        yield Fleet(REGION)


def connect_fixture(fleet: Fleet, vm: dict):
    shop = fleet.connect(vm, monotonic() + VM_LIFESPAN - 20)
    state = fleet.get(vm['microvmId'])
    if (
        state.get('executionRoleArn') or state.get('imageArn') != fleet.image
        or state.get('imageVersion') != fleet.version
    ):
        raise RuntimeError('Unexpected guest execution role or image identity')
    return shop


def run(artifact: Approved) -> dict:
    with approved_fleet(artifact) as fleet:
        vm = fleet.run(artifact.commit)
        vm_id = vm['microvmId']
        try:
            shop = connect_fixture(fleet, vm)
            quantity = randbelow(98) + 2
            quote = shop.check(f'{ROUTE}{quantity}').json()
            if quote != {'sku': 'cloud-mug', 'quantity': quantity, 'total_cents': quantity * 2500}:
                raise RuntimeError('The submitted PHP did not compute the expected quote')
            announce(f'Trusted PHP executed in {vm_id}; verified quantity {quantity}')
        finally:
            fleet.finish(vm_id, monotonic() + 30)
            announce(f'MicroVM terminated: {vm_id}')
    return {
        'schema': 1, 'commit': artifact.commit, 'archive_sha256': artifact.archive_sha256,
        'microvm_id': vm_id, 'microvm_state': 'terminated', 'quote': quote,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path, help='JSON receipt from submissions.py export')
    parser.add_argument('archive', type=Path, help='Frozen .tar.gz produced by the same export')
    parser.add_argument('--package-only', type=Path, help='Write the approved AWS image ZIP, without contacting AWS')
    args = parser.parse_args()
    try:
        approved = approve(args.manifest, args.archive)
        if args.package_only is not None:
            with args.package_only.open('xb') as file:
                file.write(approved.image_zip())
            result = {'schema': 1, 'commit': approved.commit, 'path': str(args.package_only)}
        else:
            result = run(approved)
    except (ValueError, OSError, EOFError, tarfile.TarError, RuntimeError, BotoCoreError, ClientError) as error:
        announce(f'Submission fixture: {error}')
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
