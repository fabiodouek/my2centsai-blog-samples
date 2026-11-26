"""Package and deploy the durable function, publish a version, and point the alias at it.

Three functions with the same code:
  my2cents-ldm-pipeline   the full role, a 2-hour execution timeout, used by most demos
  my2cents-ldm-queue      the same with a 30-minute execution timeout, the most an SQS
                          event source mapping accepts (05_sqs.py)
  my2cents-ldm-leaky      a role without lambda:TerminateMicrovm, used by 09_leak.py

Durable execution can only be turned on when a function is created (DurableConfig).
The function bundles a recent boto3: the extension needs the lambda-microvms client
(boto3 1.43.35 or later), which the Lambda runtime's own boto3 may not have.
Executions must start through a version or alias, not $LATEST, so a replay always
runs the code that launched the MicroVM.
"""

import io
import shutil
import subprocess
import sys
import time
import zipfile

from common import ALIAS, FUNCTION, HERE, LEAKY_FUNCTION, QUEUE_FUNCTION, banner, image_arn, lam, say, tf

BUILD = HERE / "build" / "function"
RUNTIME = "python3.13"
FUNCTION_TIMEOUT = 120  # >= 71 s: session jobs go over HTTP and need retry_window + 11 s
DURABLE_CONFIG = {"ExecutionTimeout": 7200, "RetentionPeriodInDays": 7}


def package() -> bytes:
    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--target", str(BUILD),
                    "--platform", "manylinux2014_aarch64", "--python-version", "3.13",
                    "--only-binary=:all:", "--pre", "-r", str(HERE / "function" / "requirements.txt")],
                   check=True)
    shutil.copy(HERE / "function" / "handler.py", BUILD / "handler.py")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(BUILD.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, f.relative_to(BUILD))
    return buf.getvalue()


def wait_ready(name: str) -> None:
    while True:
        c = lam.get_function_configuration(FunctionName=name)
        if c["State"] == "Active" and c.get("LastUpdateStatus") in (None, "Successful"):
            return
        if c["State"] == "Failed" or c.get("LastUpdateStatus") == "Failed":
            raise RuntimeError(f"{name}: {c.get('StateReason') or c.get('LastUpdateStatusReason')}")
        time.sleep(2)


def deploy(name: str, role_arn: str, code: bytes, durable_config: dict = DURABLE_CONFIG) -> None:
    env = {"Variables": {"MICROVM_IMAGE_ARN": image_arn(), "MICROVM_EXECUTION_ROLE_ARN": tf()["microvm_role_arn"],
                         "BUCKET": tf()["bucket"]}}
    common = dict(Role=role_arn, Handler="handler.handler", Runtime=RUNTIME, Timeout=FUNCTION_TIMEOUT,
                  MemorySize=256, Environment=env)
    try:
        lam.get_function(FunctionName=name)
        lam.update_function_code(FunctionName=name, ZipFile=code)
        wait_ready(name)
        lam.update_function_configuration(FunctionName=name, **common)
        say(f"{name}: updated")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(10):  # a new IAM role takes a few seconds to be assumable
            try:
                lam.create_function(FunctionName=name, Code={"ZipFile": code}, Architectures=["arm64"],
                                    DurableConfig=durable_config, **common)
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "role" not in str(e).lower() or attempt == 9:
                    raise
                time.sleep(5)
        say(f"{name}: created (durable, {RUNTIME}, arm64, timeout {FUNCTION_TIMEOUT}s)")
    wait_ready(name)
    version = lam.publish_version(FunctionName=name)["Version"]
    try:
        lam.update_alias(FunctionName=name, Name=ALIAS, FunctionVersion=version)
    except lam.exceptions.ResourceNotFoundException:
        lam.create_alias(FunctionName=name, Name=ALIAS, FunctionVersion=version)
    say(f"{name}: version {version} published, alias '{ALIAS}' -> {version}")


def main() -> None:
    banner("Deploy the durable function")
    t0 = time.time()
    code = package()
    say(f"package: {len(code) / 1e6:.1f} MB ({time.time() - t0:.0f}s)")
    deploy(FUNCTION, tf()["function_role_arn"], code)
    deploy(QUEUE_FUNCTION, tf()["function_role_arn"], code,
           durable_config={"ExecutionTimeout": 1800, "RetentionPeriodInDays": 7})
    deploy(LEAKY_FUNCTION, tf()["leaky_role_arn"], code)


if __name__ == "__main__":
    main()
