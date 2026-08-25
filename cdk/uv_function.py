"""A Lambda function packaged from a uv project, without Docker.

``UvFunction`` plays the role of ``aws-lambda-python-alpha``'s ``PythonFunction``, but
installs the locked dependencies with uv's cross-platform resolver instead of compiling
them inside a container. That keeps synth Docker-free — which matters here because the
CodeBuild project would otherwise need privileged mode, and an x86 runner would build
the arm64 asset under qemu.

The trade is that every runtime dependency must publish a wheel for the target platform;
uv fails the install rather than falling back to a source build. If that ever stops
being true, this is the construct to replace with the alpha one.

Packaging runs through CDK's ``ILocalBundling``, so the asset lands in ``cdk.out``
rather than the working tree, and CDK owns staging and cleanup.
"""

import pathlib
import shutil
import subprocess

import aws_cdk
import aws_cdk.aws_lambda as lambda_
import constructs
import jsii

# uv --python-platform tag per Lambda architecture, keyed by Architecture.name.
# manylinux2014 is the oldest glibc uv offers; Lambda's Amazon Linux 2023 is far newer,
# so anything resolved against this tag runs there.
PLATFORMS = {
    lambda_.Architecture.ARM_64.name: "aarch64-manylinux2014",
    lambda_.Architecture.X86_64.name: "x86_64-manylinux2014",
}


@jsii.implements(aws_cdk.ILocalBundling)
class _UvBundling:
    """Installs the locked runtime deps plus the app source into CDK's staging directory."""

    def __init__(
        self,
        *,
        project: pathlib.Path,
        source: pathlib.Path,
        runtime: lambda_.Runtime,
        architecture: lambda_.Architecture,
    ) -> None:
        self.project = project
        self.source = source
        self.runtime = runtime
        self.architecture = architecture

    def try_bundle(self, output_dir: str, _options: aws_cdk.BundlingOptions) -> bool:
        """Bundles into ``output_dir``, always locally.

        jsii hands the Docker options across positionally, hence the second parameter;
        none of them apply to a local build.

        Returning True tells CDK bundling is done, so the Docker image declared alongside
        this bundler is never pulled. A failure here raises rather than returning False,
        which would silently hand the build to a container that has no command to run.
        """
        target = pathlib.Path(output_dir)
        requirements = subprocess.run(
            [
                "uv", "export",
                "--no-dev",
                "--locked",  # Fail if uv.lock is stale; --frozen would silently package it.
                "--no-emit-project",  # The project itself is copied in below, not installed.
                "--no-hashes",
                "--color", "never",  # npx sets FORCE_COLOR; ANSI codes break `-r -` below.
            ],
            cwd=self.project,
            check=True,
            # stdout only: uv's explanation of a stale lock has to reach the terminal.
            stdout=subprocess.PIPE,
            text=True,
        ).stdout
        subprocess.run(
            [
                "uv", "pip", "install",
                "--target", str(target),
                "--python-platform", PLATFORMS[self.architecture.name],
                "--python-version", self.runtime.name.removeprefix("python"),
                "--no-installer-metadata",
                "-r", "-",
            ],
            cwd=self.project,
            check=True,
            input=requirements,
            text=True,
        )
        _strip_entry_points(target)
        # Skip __pycache__: .pyc files embed source mtimes and would churn the asset hash.
        shutil.copytree(
            self.source,
            target / self.source.name,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        return True


def _strip_entry_points(target: pathlib.Path) -> None:
    """Removes console scripts, which Lambda never runs, and every trace of them.

    Their shebang holds the absolute path of whichever interpreter uv resolved, so a
    laptop and a CodeBuild runner produce different bytes from the same lockfile. Deleting
    ``bin/`` is not enough on its own: each script's sha256 and length survive in the
    installing package's RECORD, which is what makes the asset hash machine-dependent.
    """
    shutil.rmtree(target / "bin", ignore_errors=True)
    for record in target.glob("*.dist-info/RECORD"):
        kept = [line for line in record.read_text().splitlines(True) if not line.startswith("bin/")]
        record.write_text("".join(kept))


class UvFunction(lambda_.Function):
    """Lambda function whose code asset is built from a uv project at synth time.

    :param project: Directory holding ``pyproject.toml`` and ``uv.lock``, whose non-dev
        dependencies are installed into the asset.
    :param source: Directory copied into the asset alongside them, i.e. the importable
        package the handler lives in.

    Every other ``lambda.Function`` prop is accepted and passed through, ``handler``
    included: it is an ordinary dotted path relative to the asset root, so a ``source``
    of ``fast_app`` makes ``fast_app.main.handler`` the handler for ``main.py``.
    """

    def __init__(
        self,
        scope: constructs.Construct,
        construct_id: str,
        *,
        project: pathlib.Path,
        source: pathlib.Path,
        runtime: lambda_.Runtime,
        architecture: lambda_.Architecture,
        **kwargs,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            runtime=runtime,
            architecture=architecture,
            code=lambda_.Code.from_asset(
                str(project),
                # The bundler ignores the source path, deriving everything from the
                # lockfile, so hashing the output is the only way to notice a dep change.
                asset_hash_type=aws_cdk.AssetHashType.OUTPUT,
                bundling=aws_cdk.BundlingOptions(
                    # Declared because BundlingOptions requires it; never pulled, since
                    # local bundling either succeeds or raises. Lazy, so naming it is free.
                    image=runtime.bundling_image,
                    local=_UvBundling(
                        project=project,
                        source=source,
                        runtime=runtime,
                        architecture=architecture,
                    ),
                ),
            ),
            **kwargs,
        )
