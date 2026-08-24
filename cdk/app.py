"""CDK app deploying the FastAPI Lambda behind an mTLS HTTP API.

Two deploy parameters, both required, both read from the environment:

  DOMAIN      Custom domain name for the API, e.g. "api.example.com". A public
              Route53 hosted zone for its parent ("example.com") must already exist
              in the target account: the stack creates the DNS-validated ACM
              certificate in it, plus the A alias record pointing at the API.
              The stack name is derived from it ("ApiExampleCom"), so several
              domains can be deployed side by side in one account.

  TRUSTSTORE  S3 URI of the mTLS truststore, the PEM bundle of CA certificates whose
              clients the API accepts, as "s3://bucket/key" with an optional
              "?versionId=..." to pin one object version. The bucket is not managed
              here and must already be readable by API Gateway.

The hosted-zone lookup runs at synth time, so CDK_DEFAULT_ACCOUNT and
CDK_DEFAULT_REGION must be set too; the cdk CLI exports them from your AWS profile.

    DOMAIN=api.example.com TRUSTSTORE=s3://my-bucket/truststore.pem cdk deploy

Clients must then present a certificate signed by a CA in the truststore. The
generated execute-api endpoint is disabled, so the custom domain is the only way in.
"""

import os
import pathlib
import re
import urllib.parse

import aws_cdk
import aws_cdk.aws_apigatewayv2 as apigw
import aws_cdk.aws_apigatewayv2_integrations as apigw_integrations
import aws_cdk.aws_certificatemanager as acm
import aws_cdk.aws_lambda as lambda_
import aws_cdk.aws_logs as logs
import aws_cdk.aws_route53 as route53
import aws_cdk.aws_route53_targets as route53_targets
import aws_cdk.aws_s3 as s3
import constructs
import uv_function

ROOT = pathlib.Path(__file__).parent.parent
SOURCE = ROOT / "fast_app"

RUNTIME = lambda_.Runtime.PYTHON_3_14
ARCHITECTURE = lambda_.Architecture.ARM_64


def parameter(name: str) -> str:
    """Reads a required deploy parameter from the environment."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required; export it before running cdk.")
    return value


def stack_id(domain: str) -> str:
    """Derives the stack name from DOMAIN, keeping one stack per domain in an account.

    Dots and hyphens are dropped and each label capitalised, so api.example.com becomes
    ApiExampleCom. Capitalising also normalises case, which DNS ignores but
    CloudFormation does not.

    A leading digit is rejected: hostnames allow one, stack names do not, and there is
    no name to fall back on now that the mapping is prefix-free.

    Note the two separators become the same boundary, so my-api.example.com and
    my.api.example.com derive the same name. Nothing here can catch that, since it
    only shows up across two deploys; keep such a pair out of one account.
    """
    label = r"[a-z0-9]+(-[a-z0-9]+)*"
    if not re.fullmatch(rf"{label}(\.{label})+", domain, re.IGNORECASE):
        raise SystemExit(f"DOMAIN must be a dotted hostname like api.example.com, got {domain!r}")
    if not re.match(r"[a-z]", domain, re.IGNORECASE):
        raise SystemExit(f"DOMAIN must start with a letter, as stack names do, got {domain!r}")
    return "".join(part.capitalize() for part in re.split(r"[.-]", domain))


def mtls_config(scope: constructs.Construct, uri: str) -> apigw.MTLSConfig:
    """Turns an ``s3://bucket/key[?versionId=...]`` truststore URI into an MTLSConfig.

    The truststore is the PEM bundle of CAs whose client certificates the API accepts;
    API Gateway reads it from S3 at deploy time, so the bucket lives outside this stack.

    The ``versionId`` query is our own extension: the s3:// scheme has no version syntax
    (the AWS CLI takes ``--version-id`` separately), but it keeps this to two parameters.
    Omit it and API Gateway tracks whatever the key currently holds.
    """
    parts = urllib.parse.urlparse(uri)
    key = parts.path.lstrip("/")
    query = urllib.parse.parse_qs(parts.query)
    if parts.scheme != "s3" or not parts.netloc or not key or query.keys() - {"versionId"}:
        raise SystemExit(
            f"TRUSTSTORE must look like s3://bucket/key[?versionId=...], got {uri!r}"
        )
    return apigw.MTLSConfig(
        bucket=s3.Bucket.from_bucket_name(scope, "Truststore", parts.netloc),
        key=key,
        # Pinning a version makes re-uploading the bundle an explicit, reviewable change.
        version=query.get("versionId", [None])[0],
    )


class FastAppStack(aws_cdk.Stack):
    """Lambda running the FastAPI app, fronted by an mTLS API Gateway v2 custom domain."""

    def __init__(
        self,
        scope: constructs.Construct,
        construct_id: str,
        *,
        domain: str,
        truststore: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        function = uv_function.UvFunction(
            self,
            "Function",
            project=ROOT,
            source=SOURCE,
            runtime=RUNTIME,
            architecture=ARCHITECTURE,
            handler="fast_app.main.handler",
            memory_size=1024,
            timeout=aws_cdk.Duration.seconds(10),
            log_group=logs.LogGroup(
                self,
                "LogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=aws_cdk.RemovalPolicy.DESTROY,
            ),
        )

        # Assumes DOMAIN's parent is itself the hosted zone (api.example.com -> example.com).
        zone = route53.HostedZone.from_lookup(self, "Zone", domain_name=domain.split(".", 1)[-1])

        domain_name = apigw.DomainName(
            self,
            "DomainName",
            domain_name=domain,
            certificate=acm.Certificate(
                self,
                "Certificate",
                domain_name=domain,
                validation=acm.CertificateValidation.from_dns(zone),
            ),
            mtls=mtls_config(self, truststore),
        )

        # $default route: API Gateway proxies every path and method to FastAPI.
        apigw.HttpApi(
            self,
            "HttpApi",
            default_integration=apigw_integrations.HttpLambdaIntegration("Integration", function),
            default_domain_mapping=apigw.DomainMappingOptions(domain_name=domain_name),
            # Without this the generated execute-api URL still serves the API without mTLS.
            disable_execute_api_endpoint=True,
        )

        route53.ARecord(
            self,
            "AliasRecord",
            zone=zone,
            record_name=domain,
            target=route53.RecordTarget.from_alias(
                route53_targets.ApiGatewayv2DomainProperties(
                    domain_name.regional_domain_name, domain_name.regional_hosted_zone_id
                )
            ),
        )

        aws_cdk.CfnOutput(self, "ApiUrl", value=f"https://{domain}/")


app = aws_cdk.App()
domain = parameter("DOMAIN")
FastAppStack(
    app,
    stack_id(domain),
    description=f"FastAPI Lambda behind an mTLS API Gateway for {domain}",
    domain=domain,
    truststore=parameter("TRUSTSTORE"),
    # HostedZone.from_lookup needs a concrete account and region to query at synth time.
    env=aws_cdk.Environment(
        account=os.environ["CDK_DEFAULT_ACCOUNT"], region=os.environ["CDK_DEFAULT_REGION"]
    ),
)
app.synth()
