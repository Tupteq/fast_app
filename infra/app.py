"""CDK app deploying the FastAPI Lambda behind an HTTP API."""

import pathlib
import shutil
import subprocess

import aws_cdk
import aws_cdk.aws_apigatewayv2 as apigw
import aws_cdk.aws_apigatewayv2_integrations as apigw_integrations
import aws_cdk.aws_lambda as lambda_
import aws_cdk.aws_logs as logs
import constructs

ROOT = pathlib.Path(__file__).parent.parent
BUILD_DIR = ROOT / "build"

RUNTIME = lambda_.Runtime.PYTHON_3_14
ARCHITECTURE = lambda_.Architecture.ARM_64
# Wheel platform tag matching ARCHITECTURE; update both together.
PLATFORM = "aarch64-manylinux2014"


def build() -> pathlib.Path:
    """Installs locked runtime deps plus the app into BUILD_DIR, and returns it.

    Runs on every synth so the packaged asset cannot drift from the source.
    """
    # ponytail: synth has a side effect (writes BUILD_DIR) and shells out to uv.
    # If that ever hurts (offline CI, synth-only runs), move this into CDK's
    # ILocalBundling so CDK owns the caching.
    shutil.rmtree(BUILD_DIR, ignore_errors=True)
    requirements = subprocess.run(
        [
            "uv", "export",
            "--no-dev",
            "--locked",  # Fail if uv.lock is stale; --frozen would silently package it.
            "--no-emit-project",
            "--no-hashes",
            "--color", "never",  # npx sets FORCE_COLOR; ANSI codes break `-r -` below.
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subprocess.run(
        [
            "uv", "pip", "install",
            "--target", str(BUILD_DIR),
            "--python-platform", PLATFORM,
            "--python-version", RUNTIME.name.removeprefix("python"),
            "--no-installer-metadata",
            "-r", "-",
        ],
        cwd=ROOT,
        check=True,
        input=requirements,
        text=True,
    )
    # Entry-point scripts carry this machine's absolute venv path in their shebang,
    # which makes the asset hash differ between machines. Lambda never runs them.
    shutil.rmtree(BUILD_DIR / "bin", ignore_errors=True)
    # Skip __pycache__: .pyc files embed source mtimes and would churn the asset hash.
    shutil.copytree(
        ROOT / "fast_app", BUILD_DIR / "fast_app", ignore=shutil.ignore_patterns("__pycache__")
    )
    return BUILD_DIR


class FastAppStack(aws_cdk.Stack):
    """Lambda running the FastAPI app, fronted by API Gateway v2."""

    def __init__(self, scope: constructs.Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)

        function = lambda_.Function(
            self,
            "Function",
            runtime=RUNTIME,
            architecture=ARCHITECTURE,
            handler="fast_app.main.handler",
            code=lambda_.Code.from_asset(str(build())),
            memory_size=1024,
            timeout=aws_cdk.Duration.seconds(10),
            log_group=logs.LogGroup(
                self,
                "LogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=aws_cdk.RemovalPolicy.DESTROY,
            ),
        )

        # $default route: API Gateway proxies every path and method to FastAPI.
        api = apigw.HttpApi(
            self,
            "HttpApi",
            default_integration=apigw_integrations.HttpLambdaIntegration("Integration", function),
        )

        aws_cdk.CfnOutput(self, "ApiUrl", value=api.url)


app = aws_cdk.App()
FastAppStack(app, "FastAppStack")
app.synth()
